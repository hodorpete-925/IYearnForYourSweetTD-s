@echo off
rem Launches the IYearn control panel with no console window.
rem Safe to double-click twice: a second launch just opens the browser tab.
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0control_panel.py"
