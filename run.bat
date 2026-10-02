@echo off
rem 로컬 실행. DATABASE_URL 은 .env 에서 읽는다.
cd /d %~dp0
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
