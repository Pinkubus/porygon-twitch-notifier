@echo off
REM Live console logger for Porygon's Twitch/quote/reaction-role activity
REM (successes + failures only — no scan/plumbing noise). Polls activity.log
REM via git pull, since GitHub Actions job logs can't be downloaded until
REM the run finishes.
cd /d "%~dp0"
python watch_activity.py
pause
