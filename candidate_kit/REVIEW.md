# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds shift start from the punch-in's own (UTC/naive) clock, not IST. | Shift 09:30, punch 10:05 IST (04:35 UTC) -> start 09:30 UTC, negative, returns 0 instead of 35. | Shift start built in IST on the attendance date. |
| 2 | `compute_late_minutes` | Grace test is `floored minutes > 10`. | 09:40:01 floors to 10, so returns 0; R2 says 10. | Compare whole seconds `> 600`, then floor to minutes. |
| 3 | `compute_late_minutes`, `punch_in` | No overnight handling; `date` taken from the punch-in's calendar day. | 22:00-06:00 shift, punch-in 00:30 gets the wrong date and negative lateness. | `attendance_date()` implements R1; late uses that date. |
| 4 | all helpers | No truncation to whole seconds; naive/aware datetimes mixed. | 09:40:00.900 treated as late; `TypeError` comparing aware and naive. | `whole_seconds()`, `tz_aware=True` client, one UTC convention. |
| 5 | `compute_work_hours` | Float `round()` is banker's/float rounding. | 9h07m30s = 9.125 -> 9.12, should be 9.13. | Decimal seconds/3600 with `ROUND_HALF_UP`. |
| 6 | `compute_overtime` | No 30-minute minimum; end always on the same day (overnight wrong); aware vs naive crash. | Out 18:45 gives 15 instead of 0; 22-06 shift gives hugely negative/zero. | R3 threshold; shift end +1 day when overnight. |
| 7 | `create_employee` | Check-then-insert race, no unique index. | Parallel creates both return 201. | Unique index on `emp_code`; catch `DuplicateKeyError` -> 409. |
| 8 | `EmployeeIn` | No validation (code format, email, HH:MM, `shift_start == shift_end`, real `joined_on`). | `emp_code="x"`, `joined_on="2026-02-30"` accepted. | Pydantic patterns + validators -> 422. |
| 9 | `create_employee` | `created_at = datetime.now()` is naive local time and returned as a datetime. | Response `created_at` not epoch ms. | UTC, ms precision, `to_ms` at the boundary. |
| 10 | `list_employees` | `skip = page*page_size` skips page 1; `total` ignores the filter; no sort; no bounds. | `page=1` returns 2nd page; `page=0` -> negative skip 500. | `(page-1)*size`, filtered count, sort by `emp_code`, `Query(ge/le)`. |
| 11 | `punch_in` | Unknown employee: `emp` is None -> `TypeError` 500. | POST with unknown `emp_code`. | 404. |
| 12 | `punch_in` | `fromtimestamp(ms/1000)` uses server local time; `if body.punched_at` treats 0 as missing; no range check; floats/strings coerced. | `punched_at` in seconds accepted. | Strict `EpochMs` (422), `is not None`, UTC. |
| 13 | `punch_in` | Check-then-insert race. | Two parallel requests both 201. | Unique `(emp_code, date)` index, `DuplicateKeyError` -> 409. |
| 14 | `punch_in` | `status` unrestricted; response leaks `id` (`_id`) and raw datetimes. | `status="ABSENT"` accepted. | Literal of 3 presence statuses; `record_out()`. |
| 15 | `list_attendance` | Loads whole collection, sorts in Python, no tiebreak, leaks `_id`, raw datetimes, no defaults for legacy docs, no validation. | Slow at 100k; `date_from > date_to` not 422; `page=0` odd slice. | DB sort/skip/limit, indexes + hint, `record_out()`, validation. |
| 16 | `health` | Always "ok", never touches MongoDB. | DB down still 200. | Ping, 503 on failure. |
| 17 | startup | No indexes created. | `explain` shows COLLSCAN. | `ensure_indexes()` in lifespan. |

**Looked at, not a defect:** `load_dotenv()` (does not override real env vars, as required); the `IST` constant (+05:30 correct); defaults 09:30/18:30 (match the contract); `doc.pop("_id")` (fine, but replaced by `record_out`/`employee_out`).