@echo off
set "DSH_CODEX_SANDBOX=workspace-write"
set "PHASE1_DB=C:\Users\32831\cc-scheduler\phase1\.local\queue.db"
set "PYTHONIOENCODING=utf-8"
cd /d C:\Users\32831\cc-scheduler
.venv\Scripts\python.exe phase1\scheduler_loop.py --executor codex --node implement --repo C:\codex-sandbox-test --once > C:\Users\32831\cc-scheduler\runtime\logs\codex_sbx_test.log 2>&1
