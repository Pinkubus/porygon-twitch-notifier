@echo off
REM Opens the Porygon-Z control panel (porygon_panel.py) with no console window.
REM Safe to open/close any time; it only reads/writes local status files.
cd /d "%~dp0"
start "" pythonw porygon_panel.py
