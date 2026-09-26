@echo off
REM Manual/interactive launcher: restarts everything Porygon runs locally.
REM
REM Kills any running watcher (quotes_watch_local.py — the quotes/porygonwow
REM bot, !feature requests and Porygon Z), any running control panel
REM (porygon_panel.py), and any console left over from an earlier run of this
REM file, then starts the panel and the watcher fresh. A running process keeps
REM its old code in memory, so run this after every code change.
REM
REM The console stays open with the watcher's live output; Ctrl+C to stop.
REM For an unattended, auto-restarting version (used by the scheduled task,
REM logs to a file instead of the console), use run_quotes_watch_local.bat.
REM (porygon_logger.bat's log viewer is deliberately left alone — it only
REM reads activity.log, so it never needs restarting.)
cd /d "%~dp0"

echo Stopping running Porygon processes...
powershell -NoProfile -Command "$all = Get-CimInstance Win32_Process; $parent = ($all | Where-Object { $_.ProcessId -eq $PID }).ParentProcessId; $all | Where-Object { $_.ProcessId -ne $parent -and ((($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'quotes_watch_local|porygon_panel') -or ($_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'start_porygon')) } | ForEach-Object { Write-Host ('  stopping pid ' + $_.ProcessId + ' (' + $_.Name + ')'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Starting the control panel...
start "" pythonw porygon_panel.py

echo Starting the watcher...
python quotes_watch_local.py
pause
