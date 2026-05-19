@echo off
echo ============================================
echo  Deal Scraper - Kompilierung zur .exe
echo ============================================
echo.
echo Installiere PyInstaller...
pip install pyinstaller

echo.
echo Kompiliere main.py in eine einzige .exe Datei...
pyinstaller --noconfirm --clean --windowed --icon=icon.ico --name "DealScraper" --add-data "templates;templates" --add-data "static;static" main.py

echo.
echo ============================================
echo  FERTIG!
echo  Deine verschickbare Datei liegt im Ordner:
echo  dist\DealScraper.exe
echo ============================================
pause
