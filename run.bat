@echo off
chcp 65001 >nul
rem 로컬 실행. DATABASE_URL·AUTH_DISABLED 는 .env 에서 읽는다.
rem 127.0.0.1 로 바인딩한다. AUTH_DISABLED=1 로컬 개발 중에 0.0.0.0 으로 열면
rem 같은 네트워크의 누구나 로그인 없이 관리자로 들어올 수 있다.
cd /d "%~dp0"
set PYTHONUTF8=1

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [오류] 가상환경이 없습니다: %PY%
    echo        README.md 의 "로컬 개발 환경" 절차를 먼저 실행하세요.
    pause
    exit /b 1
)

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
