@echo off
echo ============================================
echo  Deal Scraper - Playwright Fix
echo ============================================
echo.

echo [1/3] Loesche kaputte PLAYWRIGHT_BROWSERS_PATH Umgebungsvariable...
REG DELETE "HKCU\Environment" /v "PLAYWRIGHT_BROWSERS_PATH" /f 2>nul
if errorlevel 1 (
    echo       Variable war nicht gesetzt - OK.
) else (
    echo       Erfolgreich geloescht.
)

echo.
echo [2/3] Installiere Playwright Chromium Browser (System-weit)...
set PLAYWRIGHT_BROWSERS_PATH=
python -m playwright install chromium
if errorlevel 1 (
    echo FEHLER: Playwright-Installation fehlgeschlagen.
    echo Stelle sicher dass Python und playwright installiert sind.
    pause
    exit /b 1
)

echo.
echo [3/3] Starte Deal Tracker...
python main.py

