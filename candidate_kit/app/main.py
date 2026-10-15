
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables win over .env

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, MongoDB, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back UTC-aware datetimes, never naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

MIN_MS = 100_000_000_000          # below this it looks like seconds
MAX_MS = 4_102_444_800_000        # 2100-01-01
GRACE_SECONDS = 600               # R2
MIN_OVERTIME_MINUTES = 30         # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# Index names are explicit so queries can hint them.
IDX_EMP_DATE = "emp_code_1_date_-1"
IDX_DATE_EMP = "date_-1_emp_code_1"
IDX_STATUS_DATE = "status_1_date_-1_emp_code_1"
IDX_EMP_PUNCH = "emp_code_1_punch_in_-1"
IDX_JOINED = "joined_on_1"


def ensure_indexes() -> None:
    """Idempotent: creating an identical index again is a no-op."""
    e, a = db.employees, db.attendance_logs
    e.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_1")
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_1_emp_code_1")
    e.create_index([("joined_on", ASCENDING)], name=IDX_JOINED)
    # unique => the database, not Python, guarantees one record per (emp_code, date)
    a.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name=IDX_EMP_DATE)
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_DATE_EMP)
    a.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name=IDX_STATUS_DATE
    )
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name=IDX_EMP_PUNCH)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time and rule helpers
# --------------------------------------------------------------------------- #
def as_utc(dt: datetime) -> datetime:
    """Naive datetimes (should not happen with tz_aware client) are treated as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def whole_seconds(dt: datetime) -> datetime:
    """R1: truncate to whole seconds."""
    return as_utc(dt).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(seconds=ms // 1000)  # floor => truncated to whole seconds


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)  # integer maths, no float error


def now_seconds() -> datetime:
    return whole_seconds(datetime.now(UTC))


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def hm(value: str) -> tuple[int, int]:
    return int(value[:2]), int(value[3:5])


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def ist_at(day: str, hhmm: str, plus_days: int = 0) -> datetime:
    y, m, d = (int(x) for x in day.split("-"))
    h, mi = hm(hhmm)
    return datetime(y, m, d, h, mi, tzinfo=IST) + timedelta(days=plus_days)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts started the day before
    when the punch-in is earlier than shift_end."""
    local = whole_seconds(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < time(*hm(shift_end)):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    secs = int((whole_seconds(punch_in) - ist_at(day, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift end, only if >= 30. Overnight shifts end the next day."""
    end = ist_at(day, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((whole_seconds(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not float round())."""
    secs = int((whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds())
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive(emp: dict, day: str, status: str, pin: Optional[datetime], pout: Optional[datetime]) -> dict:
    """All stored derived fields for a record, per the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENT_STATUSES or pin is None:
        return out
    out["late_minutes"] = compute_late_minutes(pin, day, emp["shift_start"])
    if pout is not None:
        wh = compute_work_hours(pin, pout)
        out["work_hours"] = float(wh)
        out["overtime_minutes"] = compute_overtime(pout, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = wh < HALF_DAY_LIMIT
    return out


def round_half_up(value: Any, places: int) -> float:
    """R8. Goes through the shortest repr so 2.675 rounds to 2.68 as a human expects."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


def parse_date(value: str, name: str = "date") -> date:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a real calendar date")


def month_bounds(month: str) -> tuple[date, date]:
    if not MONTH_RE.match(month):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        first = date(int(month[:4]), int(month[5:]), 1)
    except ValueError:
        raise HTTPException(422, "invalid month")
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def count_working_days(first: date, last: date) -> int:
    """R7: Mon-Fri between two dates inclusive (at most 31 iterations of calendar arithmetic)."""
    n, d = 0, first
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # rejects floats, strings, bools
StatusT = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusT = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field("18:30", pattern=HHMM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatusT = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusT] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 4. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for key, val in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):
            val = {"from": to_ms(val.get("from")), "to": to_ms(val.get("to"))}
        changes[key] = val
    return {"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Never exposes _id; fills defaults for legacy documents; instants become epoch ms."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Employees and attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # unique index decides the winner of a race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: dict = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    secs = int((ts - whole_seconds(rec["punch_in"])).total_seconds())
    if secs <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if secs > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
    derived = derive(emp, rec["date"], rec["status"], rec["punch_in"], ts)
    updated = db.attendance_logs.find_one_and_update(
        # the filter is the compare-and-set: only one concurrent punch-out can match
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def list_plan(emp_code, date_from, date_to, status, page, page_size) -> dict:
    if date_from is not None:
        df = parse_date(date_from, "date_from")
    if date_to is not None:
        dt_ = parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and df > dt_:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    hint = IDX_EMP_DATE if emp_code else IDX_STATUS_DATE if status else IDX_DATE_EMP
    return {
        "filter": q,
        "sort": [("date", DESCENDING), ("emp_code", ASCENDING)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
        "hint": hint,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    p = list_plan(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(p["filter"]) if p["filter"] else db.attendance_logs.estimated_document_count()
    cursor = db.attendance_logs.find(p["filter"], sort=p["sort"], skip=p["skip"], limit=p["limit"], hint=p["hint"])
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{day}")
def regularize(emp_code: str, day: str, body: RegularizeIn):
    parse_date(day, "date")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    sent = body.model_fields_set
    for f in ("status", "punch_in", "punch_out"):
        if f in sent and getattr(body, f) is None:
            raise HTTPException(422, f"{f} must not be null")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old["punch_in"]
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            secs = int((whole_seconds(new_out) - whole_seconds(new_in)).total_seconds())
            if secs <= 0 or secs > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive(emp, day, new_status, new_in, new_out)}

    def differs(field: str) -> bool:
        a, b = old[field], new[field]
        if isinstance(a, datetime) or isinstance(b, datetime):
            return to_ms(a) != to_ms(b)
        return a != b

    requested = [f for f in ("status", "punch_in", "punch_out") if f in sent]
    if not any(differs(f) for f in requested) and not (
        new_status in ("ABSENT", "LEAVE") and (old["punch_in"] or old["punch_out"])
    ):
        raise HTTPException(422, "request changes nothing")

    changes = {f: {"from": old[f], "to": new[f]} for f in new if differs(f)}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n_hist = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            # optimistic lock: nobody changed the record since we read it
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Pipeline builders (used by the real endpoints AND by /admin/explain)
# --------------------------------------------------------------------------- #
IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
# stored half_day is authoritative; missing means false
PRESENT_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
IS_WEEKDAY = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, 5]}
LATE_FLAG = {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def month_range(month: str) -> tuple[str, str]:
    first, last = month_bounds(month)
    return first.isoformat(), last.isoformat()


def monthly_plan(emp_code: str, month: str) -> dict:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_WEIGHT, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": LATE_FLAG},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]
    return {"kind": "aggregate", "coll": "attendance_logs", "pipeline": pipeline, "hint": IDX_EMP_DATE}


def summary_plan(month: str, department: Optional[str]) -> dict:
    first, last = month_range(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, PRESENT_WEIGHT, 0]}},
            # work hours kept as integer hundredths so the mean can be rounded exactly in Python
            "work_cents": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
            "work_n": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
            "late_count": {"$sum": LATE_FLAG},
            "late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    pipeline = [
        # start from employees: people with zero logs still produce a row, later joiners are filtered out
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": per_employee, "as": "stats"}},
        {"$set": {"s": {"$ifNull": [{"$first": "$stats"}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "work_cents": {"$sum": {"$ifNull": ["$s.work_cents", 0]}},
            "work_n": {"$sum": {"$ifNull": ["$s.work_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]
    return {"kind": "aggregate", "coll": "employees", "pipeline": pipeline, "hint": IDX_JOINED}


def leaderboard_plan(month: str, limit: int, department: Optional[str]) -> dict:
    first, last = month_range(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); ties share a rank
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return {"kind": "aggregate", "coll": "attendance_logs", "pipeline": pipeline, "hint": IDX_DATE_EMP}


def trend_plan(department: str, from_: str, to_: str) -> dict:
    start, end = parse_date(from_, "from"), parse_date(to_, "to")
    if end < start:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    codes = db.employees.distinct("emp_code", {"department": department})
    if not codes:
        raise HTTPException(404, "unknown department")
    # headcount inputs: employees grouped by joined_on (at most one row per distinct join date)
    base, joiners = 0, []
    for g in db.employees.aggregate([{"$match": {"department": department}},
                                     {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}]):
        if g["_id"] <= from_:
            base += g["n"]
        elif g["_id"] <= to_:
            joiners.append({"d": g["_id"], "n": g["n"]})

    head = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to_}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": {"$cond": [IS_PRESENT, PRESENT_WEIGHT, 0]}},
                    "late_count": {"$sum": LATE_FLAG}}},
    ]
    window = {"documents": [-6, 0]}
    tail = [
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"d": 1}},
        # gap filling: one row per calendar day, weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day",
                                              "bounds": [datetime(start.year, start.month, start.day, tzinfo=UTC),
                                                         datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)]}}},
        {"$set": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
        }},
        {"$set": {"headcount": {"$add": [base, {"$sum": {"$map": {
            "input": joiners, "as": "j",
            "in": {"$cond": [{"$lte": ["$$j.d", "$date"]}, "$$j.n", 0]}}}}]}}},
        # rate in integer 10000ths, rounded half-up; null unless working day with headcount > 0
        {"$set": {"rate_units": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}},
            None]}}},
        {"$set": {"rate_n": {"$cond": [{"$eq": ["$rate_units", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "win_sum": {"$sum": "$rate_units", "window": window},
            "win_n": {"$sum": "$rate_n", "window": window}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate_units": 1, "win_sum": 1, "win_n": 1}},
    ]
    # Fallback head used only when the department has no logs at all in range (guarantees a seed row).
    seed = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$replaceWith": {"_id": from_, "present_count": 0, "late_count": 0}},
    ]
    return {"kind": "aggregate", "coll": "attendance_logs", "pipeline": head + tail, "hint": IDX_EMP_DATE,
            "seed_pipeline": seed + tail}


# --------------------------------------------------------------------------- #
# 7. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    plan = monthly_plan(emp_code, month)
    row = next(db[plan["coll"]].aggregate(plan["pipeline"], hint=plan["hint"]), None) or {}
    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last)  # R7: count from joined_on
    present = row.get("present_days", 0)
    pct = round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    plan = summary_plan(month, department)
    items = []
    for r in db[plan["coll"]].aggregate(plan["pipeline"], hint=plan["hint"]):
        avg = None
        if r["work_n"]:
            avg = round_half_up(Decimal(int(r["work_cents"])) / Decimal(int(r["work_n"])) / 100, 2)
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": avg,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    plan = leaderboard_plan(month, limit, department)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])}
        for r in db[plan["coll"]].aggregate(plan["pipeline"], hint=plan["hint"])
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
    to: str = Query(..., pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
):
    plan = trend_plan(department, from_, to)
    rows = list(db.attendance_logs.aggregate(plan["pipeline"], hint=plan["hint"]))
    if not rows:  # no logs at all in range: re-run from a one-row seed so every day is still produced
        rows = list(db.employees.aggregate(plan["seed_pipeline"]))
    items = []
    for r in rows:
        units = r.get("rate_units")
        win_n = int(round(r["win_n"]))
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": None if units is None else float(Decimal(int(round(units))) / Decimal(10000)),
            "moving_avg_7d": None if win_n == 0 else round_half_up(
                Decimal(int(round(r["win_sum"]))) / Decimal(win_n) / Decimal(10000), 4),
        })
    items.sort(key=lambda x: x["date"])
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint
# --------------------------------------------------------------------------- #
def need(value: Any, name: str):
    if value is None or value == "":
        raise HTTPException(422, f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusT] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        p = list_plan(emp_code, date_from, date_to, status, page, page_size)
        inner: dict = {"find": "attendance_logs", "filter": p["filter"],
                       "sort": dict(p["sort"]), "skip": p["skip"], "limit": p["limit"], "hint": p["hint"]}
        coll = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            plan = monthly_plan(need(emp_code, "emp_code"), need(month, "month"))
        elif endpoint == "department_summary":
            plan = summary_plan(need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            plan = leaderboard_plan(need(month, "month"), limit, department)
        else:
            plan = trend_plan(need(department, "department"), need(from_, "from"), need(to, "to"))
        coll = plan["coll"]
        inner = {"aggregate": coll, "pipeline": plan["pipeline"], "cursor": {}, "hint": plan["hint"]}
    try:
        raw = db.command({"explain": inner, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(500, f"explain failed: {exc}")
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}