@echo off
cd /d "%~dp0"
set PATH=%PATH%;C:\Program Files\Tesseract-OCR
echo Starting MailSort ... open http://localhost:5000 in your browser
python app.py
pause
