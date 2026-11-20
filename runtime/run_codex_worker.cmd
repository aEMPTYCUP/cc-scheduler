@echo off
rem Codex real executor worker for the task platform.
rem
rem DSH_CODEX_SANDBOX: the Codex Windows sandbox is currently non-functional on
rem this host (elevated backend never completes the runner pipe handshake).
rem danger-full-access keeps the executor usable; tasks still run inside an
rem isolated per-task worktree and the prompt forbids destructive git verbs.
rem Re-evaluate once the Codex desktop app is updated and `codex sandbox echo ok`
rem succeeds (platform_doctor.py --deep reports it).
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
set DSH_CODEX_SANDBOX=danger-full-access
set DSH_REPO_PATH=C:\Users\32831\OneDrive\Desktop\期货分析
".venv\Scripts\python.exe" phase1\scheduler_loop.py --executor codex --node implement --repo "%DSH_REPO_PATH%" --max-tasks 1 --watch-interval 900 >> runtime\logs\codex_worker.log 2>&1
exit /b %errorlevel%
