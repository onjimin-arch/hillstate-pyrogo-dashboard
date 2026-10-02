@echo off
rem 작업 스케줄러 등록용: schtasks /Create /SC DAILY /ST 09:00 /TN "KPI-Collect" /TR "%~dp0collect.bat"
cd /d %~dp0
python scripts\collect.py >> data\collect.log 2>&1
