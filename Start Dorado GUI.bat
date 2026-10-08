@echo off
cd /d "%~dp0"
start "" pythonw dorado_gui.pyw
if errorlevel 1 python dorado_gui.pyw
