@echo off
REM tare.tools 1-Click Release Train Landing Shortcut
REM Just double-click this file to approve all pending release trains without opening a terminal!

cd /d "%~dp0\..\.."
python relay\relay_mesh.py land-all
pause
