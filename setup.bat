@echo off
echo ============================================
echo  Deal Scraper - Setup
echo ============================================
echo.

echo [1/3] Installiere Python-Pakete...
pip install -r requirements.txt
if errorlevel 1 (
    echo FEHLER: pip install fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [2/3] Installiere Playwright Chromium Browser...
python -m playwright install chromium
if errorlevel 1 (
    echo FEHLER: Playwright-Installation fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [3/3] Erstelle Icon und Desktop-Verknuepfung...
python make_icon.py
python create_shortcut.py

echo.
echo ============================================
echo  Setup abgeschlossen!
echo  Starte die App mit:  python main.py
echo  Oder nutze die Desktop-Verknuepfung.
echo ============================================
pause
