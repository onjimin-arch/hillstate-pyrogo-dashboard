@echo off
chcp 65001 >nul
rem 작업 스케줄러 등록용: schtasks /Create /SC DAILY /ST 09:00 /TN "KPI-Collect" /TR "%~dp0collect.bat"
rem 배포본은 앱 안에서 자동 수집(AUTO_COLLECT)이 돌므로, 이 배치는 로컬/외부 스케줄러용이다.
cd /d "%~dp0"
set PYTHONUTF8=1

set PY=.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [오류] 가상환경이 없습니다: %PY%
    exit /b 1
)

if not exist "data" mkdir "data"
"%PY%" scripts\collect.py >> "data\collect.log" 2>&1
