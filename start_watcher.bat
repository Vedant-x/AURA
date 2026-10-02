@echo off
cd /d "%~dp0"
echo [%date% %time%] Starting AURA watcher from %cd%>> "%~dp0watcher_log.txt"
python -u "%~dp0aura_watcher.py" >> "%~dp0watcher_log.txt" 2>&1
echo [%date% %time%] AURA watcher exited with code %errorlevel%>> "%~dp0watcher_log.txt"
