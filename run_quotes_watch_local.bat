@echo off
REM Launcher used by the "PorygonQuotesWatcher" scheduled task (runs at system
REM startup, regardless of login). Restarts quotes_watch_local.py if it ever
REM exits/crashes; all output goes to quotes_watch_local.log.
cd /d "%~dp0"
:loop
"C:\Users\Williwaugh\AppData\Local\Programs\Python\Python313\python.exe" quotes_watch_local.py >> quotes_watch_local.log 2>&1
timeout /t 5 /nobreak >nul
goto loop
