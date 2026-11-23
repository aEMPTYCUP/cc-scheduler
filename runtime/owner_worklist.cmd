@echo off
rem READ-ONLY: per-owner mailbox worklist for one Doubao account (single-line ASCII JSON on stdout).
rem Usage: owner_worklist.cmd <owner>
rem Called from the Mac side hourly task:
rem   ssh winpc "C:/Users/32831/cc-scheduler/runtime/owner_worklist.cmd doubao-shenlanking"
rem Reads only: responsibility cards, console queue.db, scheduler idle signal. Writes nothing.
cd /d C:\Users\32831\cc-scheduler
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" phase1\owner_worklist.py --owner %1
exit /b %errorlevel%
