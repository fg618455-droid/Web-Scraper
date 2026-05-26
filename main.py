"""
Deal Scraper - Desktop App Entry Point
Starts Flask in a background thread, then opens Edge/Chrome in app mode.

Run: python main.py
"""

import json
import os
import sys
import time
import threading
import subprocess
import webbrowser

# -- Playwright browser path fix --
# When running as a PyInstaller .exe, Playwright automatically looks for
# browser binaries in _internal/play (sys._MEIPASS/play).  That folder only
# exists when the browsers were bundled at build-time (see build-scraper.bat).
# If the bundled folder is missing we fall back to the system-wide installation
# that setup.bat creates (usually %LOCALAPPDATA%\ms-playwright).
# Setting PLAYWRIGHT_BROWSERS_PATH here propagates to the Node.js subprocess
# that Playwright spawns, so it must happen before ANY playwright import.
def _has_chromium(base_dir: str) -> bool:
    """Prueft ob im Verzeichnis ein gueltiges Chromium-Binary liegt."""
    import glob
    for pattern in ["**/chrome.exe", "**/chromium.exe", "**/chrome-linux", "**/chrome"]:
        if glob.glob(os.path.join(base_dir, pattern), recursive=True):
            return True
    return False


def _fix_playwright_browser_path():
    """
    Stelle sicher dass PLAYWRIGHT_BROWSERS_PATH auf einen gueltigen Pfad zeigt
    UND dort wirklich ein Chromium-Binary liegt.

    Problem: Wenn die kompilierte .exe einmal gestartet wurde, kann
    PLAYWRIGHT_BROWSERS_PATH als Windows-Umgebungsvariable auf
    dist\\DealScraper\\_internal\\play gesetzt sein. Dieser Pfad kann
    existieren aber LEER sein (keine Browser drin). Ohne Fix schlaegt jeder
    Playwright-Aufruf fehl -- auch im Source-Mode.
    """
    current = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")

    # Wenn der aktuelle Pfad existiert UND ein Chromium-Binary enthaelt: nichts tun
    if current and os.path.isdir(current) and _has_chromium(current):
        return

    # PyInstaller .exe: gebundelte Browser in _internal/play bevorzugen
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        bundled = os.path.join(meipass, "play")
        if os.path.isdir(bundled):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled
            return

    # Source-Mode ODER .exe ohne gebundelte Browser:
    # Systemweit installierte Browser suchen (von setup.bat / playwright install)
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
        os.path.join(os.environ.get("USERPROFILE", ""),
                     "AppData", "Local", "ms-playwright"),
        # Playwright default path in AppData\Local
        os.path.join(os.environ.get("APPDATA", "").replace("Roaming", "Local"), "ms-playwright"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate) and _has_chromium(candidate):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = candidate
            print(f"[PW-Fix] Using system Playwright at: {candidate}")
            return

    # Nichts gefunden: veraltete Env-Variable loeschen damit Playwright
    # seinen eigenen Default-Pfad verwendet (statt einem falschen)
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        print(f"[PW-Fix] No valid Chromium found, removing bad PLAYWRIGHT_BROWSERS_PATH={current!r}")
        del os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    else:
        print("[PW-Fix] No PLAYWRIGHT_BROWSERS_PATH set, Playwright will use its own default.")


_fix_playwright_browser_path()
# -- End Playwright fix --

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# Load external config (optional - fallback to defaults)
_config_path = os.path.join(BASE_DIR, "config.json")
try:
    with open(_config_path, encoding="utf-8") as _f:
        CONFIG = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    CONFIG = {}

HOST = CONFIG.get("host", "127.0.0.1")
PORT = CONFIG.get("port", 5001)


def _setup_dealscraper_chrome_profile() -> str | None:
    """Iter. 30: bereitet ein dediziertes Chrome-User-Profile fuer das Scraping
    vor. Hintergrund:

      * In Iter. 29 hat main.py den App-Chrome mit --remote-debugging-port=9222
        gestartet, in der Annahme dass scraper.fetch_ebay_via_cdp diesen
        Browser via CDP nutzen kann. Live-Diagnose 2026-05-25 hat aber
        gezeigt dass der Port NIE erreichbar wurde — wenn Felix' normaler
        Chrome offen ist, behandelt Chrome '--app=...' als child-Aufruf
        und der CDP-Flag wird ignoriert (Single-Instance-Verhalten pro
        User-Data-Dir).
      * Loesung: scraper.fetch_ebay_via_persistent startet eine
        SEPARATE Chromium-Instanz mit eigenem user-data-dir. Diese
        Instanz kollidiert nicht mit Felix' Default-Chrome.
      * Damit Akamai diese frische Instanz nicht als "leerer Bot"
        erkennt, kopieren wir aus dem Default-Profile die identitaets-
        relevanten Files (Local State, Preferences). Cookies sind beim
        laufenden Default-Chrome locked und kommen NICHT mit — Tests
        haben aber gezeigt dass /itm/<id> auch ohne Login-Cookies durch
        Akamai durchgeht (Browser-Fingerprint reicht).

    Returns the absolute profile path (or None on hard failure).
    Idempotent: kopiert nur beim ersten Start.
    """
    import shutil
    profile = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "DealScraper", "ScraperProfile")
    if not profile or not os.environ.get("LOCALAPPDATA"):
        return None

    default_user_data = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                     "Google", "Chrome", "User Data")
    try:
        os.makedirs(os.path.join(profile, "Default", "Network"), exist_ok=True)
    except Exception as e:
        print(f"[Profile] mkdir failed: {e}")
        return None

    # Idempotent: skippe die Kopie wenn schon Files da sind UND aelter als 7 Tage
    # (so dass die Files irgendwann auf-frischen, falls Felix sein Chrome updated).
    flag = os.path.join(profile, ".staged")
    needs_stage = True
    if os.path.exists(flag):
        try:
            age = time.time() - os.path.getmtime(flag)
            needs_stage = age > 7 * 24 * 3600
        except Exception:
            pass

    if needs_stage and os.path.isdir(default_user_data):
        files = [
            (os.path.join(default_user_data, "Local State"),
             os.path.join(profile, "Local State")),
            (os.path.join(default_user_data, "Default", "Preferences"),
             os.path.join(profile, "Default", "Preferences")),
        ]
        for src, dst in files:
            if not os.path.exists(src):
                continue
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                # locked / permission: nicht fatal, weiter
                print(f"[Profile] copy {os.path.basename(src)}: {e}")
        try:
            with open(flag, "w") as fp:
                fp.write(str(time.time()))
        except Exception:
            pass

    os.environ["DEALSCRAPER_PROFILE_PATH"] = profile
    return profile


_setup_dealscraper_chrome_profile()

import database as db
db.init_db()

from app import app as flask_app, _load_persisted_interval
_load_persisted_interval()

URL = f"http://{HOST}:{PORT}"

BROWSER_CANDIDATES = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
]


def _run_flask():
    flask_app.run(host=HOST, port=PORT, debug=False,
                  use_reloader=False, threaded=True)


def _another_instance_running() -> bool:
    """True wenn schon eine DealScraper-Instanz auf PORT lauscht.

    Iter. 29: Verhindert dass ein zweiter Doppelklick still im Hintergrund
    haengt (kann den Port nicht binden, Flask wirft EADDRINUSE im daemon-
    Thread, der Prozess bleibt ohne UI offen). Stattdessen oeffnen wir nur
    den Browser auf die bereits laufende Instanz und beenden uns selbst.
    """
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((HOST if HOST not in ('', '0.0.0.0') else '127.0.0.1', PORT))
        sock.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _print_lan_urls():
    """Zeigt alle LAN-IPs auf denen Flask erreichbar ist — nuetzlich wenn
    HOST=0.0.0.0 gesetzt ist und Felix vom Handy zugreifen will."""
    if HOST not in ('0.0.0.0', ''):
        return
    try:
        import socket
        hostname = socket.gethostname()
        ips = set()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith('127.'):
                ips.add(ip)
        if ips:
            print('Erreichbar vom Handy/LAN unter:')
            for ip in sorted(ips):
                print(f'  http://{ip}:{PORT}')
    except Exception:
        pass


def _find_browser():
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def check_updates_and_prompt():
    try:
        import tkinter as tk
        from tkinter import messagebox
        from updater import check_for_updates, download_and_update

        latest_version, release_data = check_for_updates()
        if latest_version and release_data:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)

            result = messagebox.askyesno(
                "Update verfuegbar!",
                "Eine neue Version (v{}) des Deal Scrapers ist verfuegbar."
                "\n\nJetzt automatisch herunterladen und installieren?"
                " Die App startet danach mit der neuen Version neu."
                .format(latest_version)
            )

            if result:
                download_and_update(release_data, latest_version)
            root.destroy()
    except Exception:
        pass


def main():
    # Iter. 29: Single-Instance-Lock. Wenn eine andere Instanz schon laeuft,
    # nur den Browser auf die existierende Instanz oeffnen und Exit.
    if _another_instance_running():
        print(f"DealScraper laeuft bereits auf {URL} — oeffne nur das Fenster.")
        browser = _find_browser()
        if browser:
            # Iter. 31 fix: --start-fullscreen verdeckte die Taskleiste und
            # damit das Tray-Icon. --start-maximized allein liefert ein
            # randloses grosses Fenster, ohne die Taskleiste zu killen.
            subprocess.Popen([
                browser,
                "--app={}".format(URL),
                "--start-maximized",
                "--disable-extensions",
            ])
        else:
            webbrowser.open(URL)
        return

    check_updates_and_prompt()

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()
    _print_lan_urls()

    for _ in range(20):
        time.sleep(0.3)
        try:
            import urllib.request
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            pass

    browser = _find_browser()
    cdp_port = CONFIG.get("cdp_port", 9222)
    if browser:
        # Iter. 29: Chrome bekommt CDP-Port + Anti-Automation-Flag damit die
        # App den User-Browser als eBay-Renderer benutzen kann (umgeht Akamai
        # weil dieser Browser Felix' echte Login-Cookies + Browser-Fingerprint
        # mitbringt). Siehe scraper.fetch_ebay_via_cdp.
        subprocess.Popen([
            browser,
            "--app={}".format(URL),
            "--start-maximized",
            "--disable-extensions",
            f"--remote-debugging-port={cdp_port}",
            "--remote-allow-origins=*",
            "--disable-blink-features=AutomationControlled",
        ])
        # Pfad fuer scraper.fetch_ebay_via_cdp bekannt machen
        os.environ["DEALSCRAPER_CDP_PORT"] = str(cdp_port)
    else:
        webbrowser.open(URL)

    # Iter. 31: System-Tray haelt den Prozess am Leben — wenn Felix das Chrome-
    # Fenster zumacht laeuft die App im Hintergrund weiter (Background-Refresh,
    # Geocode etc. ticken durch). Tray-Menu erlaubt erneutes Oeffnen, manuellen
    # Scrape und sauberes Beenden inkl. Persistent-Chromium-Shutdown.
    def _on_tray_quit():
        try:
            import scraper
            scraper._shutdown_persistent()
        except Exception:
            pass
    try:
        from tray import AppTray
        AppTray(URL, browser, cdp_port=cdp_port, on_quit=_on_tray_quit).run()
    except Exception as e:
        # Fallback wenn pystray fehlt / krasht: alte passive Schleife
        print(f"[Tray] Start fehlgeschlagen ({e}) — bleibe passiv im Hintergrund")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
