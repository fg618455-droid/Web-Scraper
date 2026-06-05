@echo off
echo ============================================
echo  Deal Scraper - Playwright Fix
echo ============================================
echo.

echo [1/5] Loesche kaputte PLAYWRIGHT_BROWSERS_PATH Umgebungsvariable...
REG DELETE "HKCU\Environment" /v "PLAYWRIGHT_BROWSERS_PATH" /f 2>nul
if errorlevel 1 (
    echo       Variable war nicht gesetzt - OK.
) else (
    echo       Erfolgreich geloescht.
)
set PLAYWRIGHT_BROWSERS_PATH=

echo.
echo [2/5] Pruefe Python-Installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Python nicht gefunden. Bitte Python 3.11+ installieren.
    pause
    exit /b 1
)
python --version

echo.
echo [3/5] Pruefe/Installiere playwright Paket...
python -m pip install --upgrade playwright -q
if errorlevel 1 (
    echo FEHLER: playwright konnte nicht installiert werden.
    pause
    exit /b 1
)

echo.
echo [4/5] Installiere Playwright Chromium Browser (System-weit)...
python -m playwright install chromium
if errorlevel 1 (
    echo FEHLER: Playwright Browser-Installation fehlgeschlagen.
    echo Versuche mit explizitem Pfad...
    set PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\ms-playwright
    python -m playwright install chromium
    if errorlevel 1 (
        echo FEHLER: Playwright-Installation zweifach fehlgeschlagen.
        echo Stelle sicher dass Internet-Verbindung besteht.
        pause
        exit /b 1
    )
)

echo.
echo [5/5] Verifiziere Installation...
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop(); print('Playwright OK')"
if errorlevel 1 (
    echo WARNUNG: Playwright-Test nicht bestanden. App trotzdem starten?
    pause
)

echo.
echo ============================================
echo  Playwright erfolgreich installiert!
echo  Starte Deal Tracker...
echo ============================================
echo.
cd /d "%~dp0"
python main.py
