@echo off
echo ============================================
echo  Deal Scraper - Kompilierung zur .exe
echo ============================================
echo.

echo [1/3] Installiere PyInstaller...
pip install pyinstaller
if errorlevel 1 (
    echo FEHLER: pip install fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [2/3] Installiere Playwright Chromium-Browser fuer das Bundle...
echo       (werden in den "play" Unterordner installiert und ins .exe-Paket aufgenommen)
set PLAYWRIGHT_BROWSERS_PATH=%CD%\play
python -m playwright install chromium
if errorlevel 1 (
    echo WARNUNG: Playwright-Browser konnten nicht installiert werden.
    echo          Das Bundle nutzt dann die systemweit installierten Browser.
)

echo.
echo [3/3] Kompiliere mit PyInstaller (DealScraper.spec)...
pyinstaller --noconfirm --clean DealScraper.spec
if errorlevel 1 (
    echo FEHLER: PyInstaller fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  FERTIG!
echo  Die App liegt im Ordner: dist\DealScraper\
echo  Starten mit:             dist\DealScraper\DealScraper.exe
echo ============================================
pause
