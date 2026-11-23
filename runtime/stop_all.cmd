@echo off
rem Stop all cc-scheduler daemons (the python workers; their restart-loop cmd
rem wrappers exit once the child python is gone).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'ui_server|minimax_reviewer|bridge_feeder' } | ForEach-Object { Write-Host ('stopping PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }"
echo Done. Daemons will restart at next logon via the Startup vbs; delete that vbs to disable autostart.
