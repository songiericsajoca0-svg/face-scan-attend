@echo off
setlocal
cd /d "%~dp0"
echo.
echo Installing/checking the MongoDB Python driver...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installation failed. Check that Python is installed and available in PATH.
  pause
  exit /b 1
)
echo.
echo Starting Automatic Face Recognition Attendance System...
echo Open http://127.0.0.1:5000 in Google Chrome or Microsoft Edge.
python app.py
pause
