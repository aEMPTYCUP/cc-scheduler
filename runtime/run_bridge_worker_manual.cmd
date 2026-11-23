@echo off
rem Manual audit round - same code path as the scheduled hourly worker, but with
rem its own log file so the two do not interleave. It re-audits the requests
rem parked in audit-requests/ (T-188 reopened from dead-letter, T-251).
rem The audit-runner exclusive lock keeps the hourly pump from double-auditing.
cd /d C:\Users\32831\cc-scheduler
set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\Users\32831\wt\agent-bridge-implementation\.claude
".venv\Scripts\python.exe" -m agent_bridge.live_controller --data-root C:\Users\32831\wt\bridge-live --qwen-url http://192.168.1.222:8000/v1 --qwen-model Qwen2.5-7B-Instruct-4bit --qwen-timeout 60 --audit-timeout 5400 > runtime\logs\bridge_manual_round.log 2>&1
echo [%date% %time%] manual round exited rc=%errorlevel% >> runtime\logs\bridge_manual_round.log
exit /b %errorlevel%
