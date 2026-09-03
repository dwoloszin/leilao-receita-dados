@echo off
cd /d "%~dp0"
echo ==== %date% %time% ==== >> daily_log.txt
"C:\Program Files\Python312\python.exe" scraper.py --workers 5 --delay 0.25 >> daily_log.txt 2>&1
