@echo off
rem Agent bridge reply worker - one controlled round of the bridge loop.
rem
rem This is the half of the loop that was missing: Doubao's replies land in
rem bridge-live\inbox, and nothing was consuming them. One round:
rem   1. drain every mailbox event through the deterministic rules + the local 7B
rem      advisory channel (QwenRouter). A delivery that is genuinely awaiting
rem      review is parked as an audit request (card -> auditing); a superseded
rem      delivery is acknowledged and the card is left untouched.
rem   2. run the real independent acceptance auditor (claude -p, read-only) on
rem      every pending audit request. Double-audit guarded; a failure is
rem      dead-lettered, never a fake PASS.
rem   3. drain again so the returned audit_completed event settles the card.
rem
rem It never merges, never pushes, never touches the console queue.db.
rem A PASS still lands on needs_human: integration stays a human gate.
rem Scheduled hourly by the AgentBridgeWorker scheduled task.
cd /d C:\Users\32831\cc-scheduler
set PYTHONIOENCODING=utf-8
set PYTHONPATH=C:\Users\32831\wt\agent-bridge-implementation\.claude
".venv\Scripts\python.exe" -m agent_bridge.live_controller --data-root C:\Users\32831\wt\bridge-live --qwen-url http://192.168.1.222:8000/v1 --qwen-model Qwen2.5-7B-Instruct-4bit --qwen-timeout 60 --audit-timeout 5400 --max-audits-per-round 2 >> runtime\logs\bridge_worker.log 2>&1
echo [%date% %time%] bridge_worker round exited rc=%errorlevel% >> runtime\logs\bridge_worker.log
exit /b %errorlevel%
