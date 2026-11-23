@echo off
rem Codex real executor worker for the task platform.
rem
rem DSH_CODEX_SANDBOX=workspace-write: the Codex Windows sandbox works, but only
rem when the process runs in an INTERACTIVE, ELEVATED session (this task is
rem registered with LogonType=InteractiveToken + RunLevel=HighestAvailable).
rem Root cause found 2026-11-20: over SSH (non-interactive) the runner pipe
rem handshake fails; without elevation the setup cannot grant write ACEs
rem (os error 5). platform_doctor.py --deep re-verifies it.
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
set DSH_CODEX_SANDBOX=workspace-write
set DSH_REPO_PATH=C:\Users\32831\OneDrive\Desktop\期货分析
".venv\Scripts\python.exe" phase1\scheduler_loop.py --executor codex --node implement --repo "%DSH_REPO_PATH%" --max-tasks 1 --watch-interval 900 >> runtime\logs\codex_worker.log 2>&1
exit /b %errorlevel%
