# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # MONGO_URI / MONGO_DB (docker run -p 27017:27017 mongo:7)
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup. Needs MongoDB 6.0+ (`$densify`, `$setWindowFields`).
Interactive docs: http://localhost:8000/docs

## Status
All endpoints in `openapi.yaml` are implemented, including `/admin/explain/{endpoint}`.
Nothing is intentionally missing. Review notes are in `REVIEW.md`, design answers in `DECISIONS.md`.

## Notes
* Analytics read the stored `work_hours` / `late_minutes` / `overtime_minutes` / `half_day`.
* Rounding is exact half-up (Decimal), done in Python on integer sums coming from the pipelines.