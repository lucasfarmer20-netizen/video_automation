@echo off
REM Double-click to launch the Illuminated Bestiary studio for remote (Tailscale) access.
cd /d "%~dp0"
".venv\Scripts\python.exe" run_studio.py
pause
