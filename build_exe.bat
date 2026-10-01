@echo off
REM Build a standalone Windows executable (no Python needed to run it).
REM One-time: pip install pyinstaller
python -m PyInstaller --onefile --windowed --name OhmTrack ^
    --icon assets\icon.ico --add-data "assets\icon.png;assets" ut620_app.py
echo.
echo Done. The standalone app is: dist\OhmTrack.exe
