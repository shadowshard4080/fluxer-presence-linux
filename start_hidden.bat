@echo off
rem Launches fluxer_presence.py with no console window.
rem Drop a shortcut to this file in shell:startup to run it at login.
cd /d "%~dp0"
start "" pythonw "%~dp0fluxer_presence.py"
