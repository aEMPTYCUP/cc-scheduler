@echo off
rem cc-scheduler console (board + automatic bridge projection). The board GO
rem drives the real headless claude executor on the production pool (B9 fix:
rem the default simulated adapter was refused by RealPoolSimulationGuard).
rem No READY tasks => GO idles harmlessly; it never touches futures tasks.
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\python.exe" phase1\scripts\ui_server.py --port 8799 --adapter claude --repo-path "C:\Users\32831\OneDrive\Desktop\期货分析" >> runtime\logs\ui_server.log 2>&1
echo [%date% %time%] ui_server exited rc=%errorlevel%, restart in 10s >> runtime\logs\ui_server.log
timeout /t 10 /nobreak >nul
goto loop
