"""Updater — prueft GitHub Releases, laedt das .zip und ersetzt
die Installation in-place via Helfer-Batchskript.

Iter. 28: Echter In-Place-Update statt nur Browser oeffnen.
Iter. 40: Retry-Logik, Rollback, zentrales Logging, 30s Timeouts.

Ablauf:
1. check_for_updates() prueft die GitHub Releases API.
2. download_and_update() laedt das .zip ins Temp, entpackt es,
   sichert (Rollback) die aktuelle Installation, und spawnt
   update_helper.bat. Die laufende .exe beendet sich anschliessend;
   der Helper kopiert die neuen Dateien ueber die alte Installation
   (deals.db / ebay_session.json bleiben in %APPDATA%\\DealScraper).
3. Der Helper startet die neue .exe und loescht sich selbst.

Fallback: Wenn der Download oder Extract scheitert, oeffnet sich
die GitHub-Release-Seite im Browser wie vorher.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
import zipfile

# ── Logging setup ────────────────────────────────────────────────────────────
# In frozen mode: log next to the .exe.  In dev: project dir.
def _log_path() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(os.path.dirname(sys.executable), 'updater.log')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updater.log')

_handler = logging.FileHandler(_log_path(), encoding='utf-8')
_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger = logging.getLogger('updater')
logger.setLevel(logging.INFO)
logger.addHandler(_handler)
# Also echo to stdout so the console / app log picks it up
logger.addHandler(logging.StreamHandler())

GITHUB_REPO    = "fg618455-droid/Web-Scraper"
REQUEST_TIMEOUT = 30    # seconds per HTTP request
RETRY_COUNT     = 3     # attempts before giving up
RETRY_WAIT      = 5     # seconds between retries


def get_current_version() -> str:
    try:
        if getattr(sys, "frozen", False):
            base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "version.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("version", "1.0.0")
    except Exception:
        return "1.0.0"


def _parse_version(v: str) -> list[int]:
    return [int(x) for x in v.split(".") if x.isdigit()]


def _urlopen_retry(url: str, **req_kwargs) -> bytes:
    """GET url with retry logic. Raises on final failure."""
    last_exc = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "DealScraper-Updater"}, **req_kwargs
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return r.read()
        except Exception as exc:
            last_exc = exc
            logger.warning('Attempt %d/%d failed for %s: %s', attempt, RETRY_COUNT, url, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_WAIT)
    raise last_exc


def check_for_updates() -> tuple:
    """Returns (latest_version, release_info_dict) if a newer release exists,
    else (None, None)."""
    if not GITHUB_REPO or "DeinGitHubName" in GITHUB_REPO:
        return None, None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        raw = _urlopen_retry(url)
        data = json.loads(raw.decode())
        latest_version = data.get("tag_name", "").lstrip("v")
        if not latest_version:
            return None, None
        current = get_current_version()
        if _parse_version(latest_version) <= _parse_version(current):
            return None, None
        logger.info('Update available: v%s (current: v%s)', latest_version, current)
        return latest_version, data
    except Exception as e:
        logger.warning('Update check failed: %s', e)
        return None, None


def _find_zip_asset(release_data: dict) -> tuple:
    for asset in release_data.get("assets", []) or []:
        name = asset.get("name", "")
        if name.lower().endswith(".zip") and "windows" in name.lower():
            return asset.get("browser_download_url"), name
    return None, None


def _find_sha256_asset(release_data: dict, zip_name: str) -> str | None:
    """Look for a .sha256 companion file matching the zip name."""
    sha_name = (zip_name + ".sha256") if zip_name else None
    for asset in release_data.get("assets", []) or []:
        name = asset.get("name", "")
        if sha_name and name == sha_name:
            return asset.get("browser_download_url")
        if name.lower().endswith(".sha256") and "windows" in name.lower():
            return asset.get("browser_download_url")
    return None




def _backup_deals_db() -> None:
    """Copy deals.db to %APPDATA%\\DealScraper\\deals_backup_<timestamp>.db."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return
    db_src = os.path.join(appdata, "DealScraper", "deals.db")
    if not os.path.exists(db_src):
        logger.info('deals.db not found at %s — skipping backup', db_src)
        return
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(appdata, "DealScraper", f"deals_backup_{ts}.db")
        shutil.copy2(db_src, dst)
        logger.info('deals.db backed up to %s', dst)
    except Exception as exc:
        logger.warning('deals.db backup failed (non-fatal): %s', exc)


def _download_with_progress(url: str, dest: str) -> None:
    """Download url to dest with retry on failure."""
    last_exc = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DealScraper-Updater"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r, \
                 open(dest, "wb") as out:
                shutil.copyfileobj(r, out, length=1024 * 256)
            logger.info('Downloaded %s → %s', url, dest)
            return
        except Exception as exc:
            last_exc = exc
            logger.warning('Download attempt %d/%d failed: %s', attempt, RETRY_COUNT, exc)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_WAIT)
    raise last_exc


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_checksum_asset(release_data: dict) -> str | None:
    """Sucht sha256sums.txt oder checksums.txt Asset im Release."""
    for asset in release_data.get("assets", []) or []:
        name = asset.get("name", "").lower()
        if name in ("sha256sums.txt", "checksums.txt", "sha256.txt"):
            return asset.get("browser_download_url")
    return None


def _verify_sha256(zip_path: str, zip_name: str, checksum_url: str) -> bool:
    """Laedt Checksum-Datei, prueft SHA256 des ZIPs.

    Unterstuetzt zwei Formate:
    - Bundle (sha256sums.txt): '<hash>  <filename>' pro Zeile — matcht zip_name
    - Sidecar (.sha256): einzelner 64-Zeichen Hex-Token (kein Dateiname)
    Gibt True zurueck bei OK oder wenn keine Prüfsumme gefunden (non-fatal).
    """
    try:
        req = urllib.request.Request(checksum_url,
                                     headers={"User-Agent": "DealScraper-Updater"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            content = r.read().decode("utf-8", errors="replace")
        target = zip_name.lower()
        sidecar_hash = None
        for line in content.splitlines():
            parts = line.split()
            if len(parts) == 1 and len(parts[0]) == 64:
                # Sidecar-Format: nur der Hash, kein Dateiname
                sidecar_hash = parts[0]
                continue
            if len(parts) < 2:
                continue
            expected_hash, fname = parts[0], parts[-1].lstrip("*").lower()
            if fname == target or target.endswith(fname):
                actual = _sha256_file(zip_path)
                if actual.lower() != expected_hash.lower():
                    logger.error('SHA256-Fehler! Erwartet: %s  Erhalten: %s', expected_hash, actual)
                    return False
                logger.info('SHA256 OK: %s', actual[:16])
                return True
        # Fallback: Sidecar (single hash, no filename in file)
        if sidecar_hash:
            actual = _sha256_file(zip_path)
            if actual.lower() != sidecar_hash.lower():
                logger.error('SHA256-Fehler (sidecar)! Erwartet: %s  Erhalten: %s', sidecar_hash, actual)
                return False
            logger.info('SHA256 OK (sidecar): %s', actual[:16])
            return True
        logger.warning('Kein SHA256-Eintrag fuer %s gefunden — ueberspringe Pruefung.', zip_name)
        return True
    except Exception as e:
        logger.warning('SHA256-Pruefung fehlgeschlagen: %s — ueberspringe.', e)
        return True


def _extracted_root(extract_dir: str) -> str:
    entries = [e for e in os.listdir(extract_dir) if not e.startswith('.')]
    if len(entries) == 1:
        inner = os.path.join(extract_dir, entries[0])
        if os.path.isdir(inner):
            return inner
    return extract_dir


def _backup_install(install_dir: str, tmp_root: str) -> str:
    """Copy the current installation to a rollback directory inside tmp_root.
    Returns the rollback path so we can restore it on failure."""
    rollback_dir = os.path.join(tmp_root, 'rollback')
    os.makedirs(rollback_dir, exist_ok=True)
    try:
        shutil.copytree(install_dir, rollback_dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('*.log', '__pycache__'))
        logger.info('Rollback backup created at %s', rollback_dir)
    except Exception as exc:
        logger.warning('Rollback backup failed (non-fatal): %s', exc)
    return rollback_dir


def _restore_rollback(rollback_dir: str, install_dir: str) -> None:
    """Copy rollback backup back over the installation directory."""
    try:
        shutil.copytree(rollback_dir, install_dir, dirs_exist_ok=True)
        logger.info('Rollback restored from %s', rollback_dir)
    except Exception as exc:
        logger.error('Rollback FAILED: %s — manual reinstall may be needed', exc)


def _write_helper(extract_root: str, install_dir: str, exe_name: str,
                  tmp_root: str = "") -> str:
    helper_path = os.path.join(tempfile.gettempdir(), "dealscraper_update.bat")
    pid = os.getpid()
    cleanup = f'rd /s /q "{tmp_root}"\r\n' if tmp_root else ""
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
        + cleanup +
        'start "" "%DST%\\%EXE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(helper_path, "w", encoding="ascii", newline="") as f:
        f.write(script)
    return helper_path


def download_and_update(release_data, latest_version=None):
    """Perform the in-place update.

    release_data: the full release JSON from check_for_updates.
    Backwards-compat: if a string URL is passed, open it in the browser.
    """
    logger.info('download_and_update called (latest=%s)', latest_version)

    if isinstance(release_data, str):
        try:
            webbrowser.open(release_data)
        except Exception as exc:
            logger.error('Browser fallback failed: %s', exc)
            return False
        return True

    if not isinstance(release_data, dict):
        return False

    html_url = release_data.get("html_url", "")

    if not getattr(sys, "frozen", False):
        logger.info('Dev-mode: skipping in-place update. Release: %s', html_url)
        if html_url:
            print(f"[Updater] Update verfuegbar: {html_url}")
        return True

    zip_url, zip_name = _find_zip_asset(release_data)
    if not zip_url:
        logger.warning('No Windows .zip asset found in release')
        if html_url:
            webbrowser.open(html_url)
        return False

    tmp_root = None
    install_dir = os.path.dirname(sys.executable)
    exe_name    = os.path.basename(sys.executable)

    try:
        tmp_root  = tempfile.mkdtemp(prefix="dealscraper_update_")
        zip_path  = os.path.join(tmp_root, zip_name or "update.zip")

        logger.info('Downloading %s ...', zip_url)
        _download_with_progress(zip_url, zip_path)

        # SHA256-Verifikation: sidecar (.sha256) hat Vorrang, Fallback auf sha256sums.txt
        _zname = zip_name or "update.zip"
        sha_url = _find_sha256_asset(release_data, _zname)
        checksum_url = _find_checksum_asset(release_data)
        if sha_url:
            if not _verify_sha256(zip_path, _zname, sha_url):
                raise ValueError("SHA256 checksum mismatch — aborting update")
        elif checksum_url:
            if not _verify_sha256(zip_path, _zname, checksum_url):
                raise ValueError("SHA256 checksum mismatch — aborting update")
        else:
            logger.warning('No SHA256 asset in release — skipping checksum verification')

        extract_dir = os.path.join(tmp_root, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        logger.info('Extracting zip ...')
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        new_root = _extracted_root(extract_dir)

        # Backup current install before overwriting
        rollback_dir = _backup_install(install_dir, tmp_root)

        # Backup deals.db before the helper overwrites files
        _backup_deals_db()

        helper = _write_helper(new_root, install_dir, exe_name, tmp_root)
        logger.info('Helper written: %s — spawning and exiting', helper)

        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd.exe", "/c", helper],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | CREATE_NO_WINDOW,
        )
        os._exit(0)

    except Exception as e:
        logger.error('In-place update failed: %s', e)
        if tmp_root:
            rollback_candidate = os.path.join(tmp_root, 'rollback')
            if os.path.isdir(rollback_candidate):
                _restore_rollback(rollback_candidate, install_dir)
            shutil.rmtree(tmp_root, ignore_errors=True)
            logger.info('Cleaned up temp dir after failed update: %s', tmp_root)
        if html_url:
            try:
                webbrowser.open(html_url)
            except Exception:
                pass
        return False
