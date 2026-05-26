"""Updater - prueft GitHub Releases, laedt das .zip und ersetzt
die Installation in-place via Helfer-Batchskript.

Iter. 28: Echter In-Place-Update statt nur Browser oeffnen.

Ablauf:
1. check_for_updates() prueft die GitHub Releases API.
2. download_and_update() laedt das .zip ins Temp, entpackt es,
   und spawnt update_helper.bat. Die laufende .exe beendet sich
   anschliessend; der Helper kopiert die neuen Dateien ueber die
   alte Installation (deals.db / ebay_session.json bleiben in
   %APPDATA%\\DealScraper, sie sind nicht im Install-Verzeichnis).
3. Der Helper startet die neue .exe und loescht sich selbst.

Fallback: Wenn der Download oder Extract scheitert, oeffnet sich
die GitHub-Release-Seite im Browser wie vorher.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
import zipfile

GITHUB_REPO = "fg618455-droid/Web-Scraper"


def get_current_version():
    try:
        # version.json liegt neben main.py/database.py — im frozen Bundle
        # extrahiert PyInstaller das nach sys._MEIPASS/version.json.
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "version.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("version", "1.0.0")
    except Exception:
        return "1.0.0"


def _parse_version(v):
    return [int(x) for x in v.split(".") if x.isdigit()]


def check_for_updates():
    """Returnt (latest_version, release_info_dict) wenn ein neueres Release
    existiert, sonst (None, None). release_info_dict hat html_url + assets
    fuer den Download.
    """
    if not GITHUB_REPO or "DeinGitHubName" in GITHUB_REPO:
        return None, None

    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(
            url, headers={"User-Agent": "DealScraper-Updater"})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status != 200:
                return None, None
            data = json.loads(response.read().decode())

        latest_version = data.get("tag_name", "").lstrip("v")
        if not latest_version:
            return None, None

        current = get_current_version()
        if _parse_version(latest_version) <= _parse_version(current):
            return None, None

        return latest_version, data
    except Exception as e:
        print(f"Update Check Error: {e}")
        return None, None


def _find_zip_asset(release_data):
    """Sucht im Release-JSON den Windows-x64-.zip-Asset heraus."""
    for asset in release_data.get("assets", []) or []:
        name = asset.get("name", "")
        if name.lower().endswith(".zip") and "windows" in name.lower():
            return asset.get("browser_download_url"), name
    return None, None


def _download_with_progress(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "DealScraper-Updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
        shutil.copyfileobj(r, out, length=1024 * 256)


def _extracted_root(extract_dir: str) -> str:
    """PyInstaller-ZIPs haben den DealScraper-Ordner als einzigen Top-Level-
    Eintrag. Wir geben den inneren Ordner zurueck, damit das Copy ohne
    doppelte Verschachtelung passiert.
    """
    entries = [e for e in os.listdir(extract_dir)
               if not e.startswith('.')]
    if len(entries) == 1:
        inner = os.path.join(extract_dir, entries[0])
        if os.path.isdir(inner):
            return inner
    return extract_dir


def _write_helper(extract_root: str, install_dir: str, exe_name: str) -> str:
    """Schreibt update_helper.bat ins Temp und gibt den Pfad zurueck.

    Der Helper: wartet bis die laufende .exe weg ist, kopiert mit robocopy
    den neuen Stand drueber, startet die neue .exe, loescht sich selbst.
    """
    helper_path = os.path.join(tempfile.gettempdir(), "dealscraper_update.bat")
    pid = os.getpid()
    # robocopy:
    #   /E   = alle Unterordner inkl. leerer
    #   /IS  = "include same" ueberschreibt selbst bei gleichem Timestamp
    #   /R:2 /W:1 = 2 Retries, 1s Wartezeit (Locked-File-Robustness)
    #   /NFL /NDL /NJH /NJS = leise Logs
    script = (
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "PID={pid}"\r\n'
        f'set "SRC={extract_root}"\r\n'
        f'set "DST={install_dir}"\r\n'
        f'set "EXE={exe_name}"\r\n'
        ":waitloop\r\n"
        'tasklist /FI "PID eq %PID%" 2>nul | find /I "%PID%" >nul\r\n'
        "if not errorlevel 1 (\r\n"
        "  timeout /T 1 /NOBREAK >nul\r\n"
        "  goto waitloop\r\n"
        ")\r\n"
        "timeout /T 1 /NOBREAK >nul\r\n"
        'robocopy "%SRC%" "%DST%" /E /IS /R:3 /W:1 /NFL /NDL /NJH /NJS >nul\r\n'
        'start "" "%DST%\\%EXE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(helper_path, "w", encoding="ascii", newline="") as f:
        f.write(script)
    return helper_path


def download_and_update(release_data, latest_version=None):
    """Fuehrt das echte In-Place-Update durch.

    release_data: das vollstaendige Release-JSON von check_for_updates.
                  Backwards-Compat: wenn ein String uebergeben wird, oeffnen
                  wir wie frueher nur die URL im Browser.
    """
    # Backwards-Compat: alte Signatur (string-URL) -> Browser-Fallback
    if isinstance(release_data, str):
        try:
            webbrowser.open(release_data)
            return True
        except Exception:
            return False

    if not isinstance(release_data, dict):
        return False

    html_url = release_data.get("html_url", "")

    if not getattr(sys, "frozen", False):
        # Dev-Mode: kein In-Place-Update, nur Browser
        if html_url:
            webbrowser.open(html_url)
        return True

    zip_url, zip_name = _find_zip_asset(release_data)
    if not zip_url:
        if html_url:
            webbrowser.open(html_url)
        return False

    try:
        tmp_root = tempfile.mkdtemp(prefix="dealscraper_update_")
        zip_path = os.path.join(tmp_root, zip_name or "update.zip")
        print(f"[Updater] Lade {zip_url} ...")
        _download_with_progress(zip_url, zip_path)

        extract_dir = os.path.join(tmp_root, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        new_root = _extracted_root(extract_dir)
        install_dir = os.path.dirname(sys.executable)
        exe_name = os.path.basename(sys.executable)

        helper = _write_helper(new_root, install_dir, exe_name)
        print(f"[Updater] Helper geschrieben: {helper}")

        # v33-Fix: alte Variante "cmd /c start /MIN ..." zeigte ein
        # minimiertes-aber-sichtbares Konsolen-Fenster fuer die Dauer des
        # robocopy-Helpers. CREATE_NO_WINDOW unterdrueckt das komplett —
        # der Update-Flow ist jetzt visuell unsichtbar bis die neue .exe
        # ihr Fenster oeffnet.
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd.exe", "/c", helper],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | CREATE_NO_WINDOW,
        )
        # Aktuelle .exe-Process killen, Helper macht den Rest.
        os._exit(0)
    except Exception as e:
        print(f"[Updater] In-Place-Update fehlgeschlagen: {e}")
        if html_url:
            try:
                webbrowser.open(html_url)
            except Exception:
                pass
        return False
