@echo off
rem Doubao backlog feeder: keeps one claimable responsibility card per account
rem (backpressure), refilling from the shared pool only when a slot frees.
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\python.exe" phase1\bridge_feeder.py --loop --interval 30 --buffer 1 >> runtime\logs\bridge_feeder.log 2>&1
echo [%date% %time%] bridge_feeder exited rc=%errorlevel%, restart in 10s >> runtime\logs\bridge_feeder.log
timeout /t 10 /nobreak >nul
goto loop
