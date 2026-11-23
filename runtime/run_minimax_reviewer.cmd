@echo off
rem MiniMax-M3 independent acceptance/integration worker. Auto-reviews REVIEW
rem tasks, runs objective checks in each worktree, rewrites REWORK back to doubao.
cd /d C:\Users\32831\cc-scheduler
set PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db
set PYTHONIOENCODING=utf-8
:loop
".venv\Scripts\python.exe" phase1\minimax_reviewer.py --interval 60 >> runtime\logs\minimax_reviewer.log 2>&1
echo [%date% %time%] minimax_reviewer exited rc=%errorlevel%, restart in 10s >> runtime\logs\minimax_reviewer.log
timeout /t 10 /nobreak >nul
goto loop
