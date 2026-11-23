@echo off
rem Headless claude worker -- the replacement for the Mac Doubao programming
rem node. The Doubao accounts are web sessions nobody wakes, so this worker
rem promotes unblocked backlog tasks, claims the next READY task, implements
rem it with headless `claude -p`, and hands the result to the independent
rem minimax reviewer. Deliberately sequential: four concurrent claude
rem processes exhausted this machine's memory.
rem The log is written by the worker itself (--log-file) rather than by a
rem `>>` redirection here: the wrapper's handle is held exclusively, so a
rem restarting wrapper could fail to reopen the file and lose the output.
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\python.exe" phase1\scripts\headless_batch.py --loop --permission-mode bypassPermissions --timeout 2400 --interval 30 --log-file runtime\logs\headless_worker.log
echo [%date% %time%] headless_worker exited rc=%errorlevel%, restart in 10s
timeout /t 10 /nobreak >nul
goto loop
