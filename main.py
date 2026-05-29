"""
Deal Scraper - Desktop App Entry Point
Starts Flask in a background thread, opens a native PyWebView window
(Edge-WebView2 backend), and keeps the app alive via the system tray.

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


_NO_WIN = 0x08000000  # CREATE_NO_WINDOW — verhindert jeden Terminal-Flash


def _git_pull_background():
    """Im Dev-Modus: git pull still im Hintergrund. Kein Neustart, kein Flash.
    Neuer Code ist beim naechsten App-Start aktiv."""
    if getattr(sys, 'frozen', False):
        return

    def _pull():
        try:
            subprocess.run(
                ['git', 'pull', '--ff-only'],
                capture_output=True, text=True,
                timeout=20, cwd=BASE_DIR,
                creationflags=_NO_WIN,
            )
        except Exception:
            pass

    threading.Thread(target=_pull, daemon=True, name='git-pull').start()


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
    if not getattr(__import__('sys'), 'frozen', False):
        return  # Dev-Mode: kein In-Place-Update moeglich, Dialog weglassen
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


# ── Iter. 34: PyWebView-Lifecycle ────────────────────────────────────────────
#
# Architektur:
#   * Flask laeuft im daemon-thread (wie bisher)
#   * Tray laeuft in EIGENEM thread via Icon.run() — vorher main-thread, jetzt
#     worker-thread, weil pywebview den main-thread fuer sich braucht
#   * pywebview.start() blockt den main-thread bis alle windows zerstoert sind
#   * Window-X-Klick: closing-handler ruft window.hide() und cancelt das Close.
#     So lebt die App im Tray weiter ohne den webview-Backend zu killen.
#   * Tray "Beenden": setzt _really_quit, ruft window.destroy() — closing-handler
#     erlaubt dann das echte Close, webview.start() returnt, main() exit'd.

_really_quit = threading.Event()
_windows = {}  # 'main' | 'scrape' -> webview.Window

# Iter. 35 Stufe A: DWM Dark Title-Bar.
# Edge-WebView2 ignoriert <meta name="theme-color"> — die Windows-Title-Bar
# bleibt OS-Default-grau. Workaround: Desktop Window Manager API
# DwmSetWindowAttribute mit DWMWA_USE_IMMERSIVE_DARK_MODE (20 = Win10 20H1+/Win11,
# 19 = pre-20H1 fallback). Funktioniert unabhaengig vom System-Theme.
_WINDOW_TITLES = ('Deal Tracker', 'Deal Tracker — Scrape')


def _set_dark_title_bar(hwnd) -> bool:
    """Setzt Dark-Mode + App-Farbe auf die native Windows-Titelleiste (Win11+).

    Zwei DWM-Attribute:
      DWMWA_USE_IMMERSIVE_DARK_MODE (20/19) — dunkle Steuerelemente
      DWMWA_CAPTION_COLOR (35)              — Hintergrundfarbe (Win11 22000+)
      DWMWA_TEXT_COLOR    (36)              — Textfarbe (Win11 22000+)

    App-Farbe #0a0c1c entspricht --bg-1 (Topbar-Farbe) → nahtlose Integration.
    COLORREF-Format: 0x00BBGGRR.
    """
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        dwmapi = ctypes.windll.dwmapi

        # Schritt 1: Dark-Mode aktivieren (Controls werden hell/dunkel)
        dark = ctypes.c_int(1)
        for attr in (20, 19):
            if dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), ctypes.c_uint(attr),
                ctypes.byref(dark), ctypes.sizeof(dark)
            ) == 0:
                break

        # Schritt 2: Titelleisten-Hintergrund = App-Bg #0a0c1c (COLORREF 0x001C0C0A)
        # Schritt 3: Titelleisten-Text = helles Weiß #e5e9f5 (COLORREF 0x00F5E9E5)
        bg_color   = ctypes.c_int(0x001C0C0A)
        text_color = ctypes.c_int(0x00F5E9E5)
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(35),
            ctypes.byref(bg_color), ctypes.sizeof(bg_color)
        )
        dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(36),
            ctypes.byref(text_color), ctypes.sizeof(text_color)
        )
        return True
    except Exception as e:
        print(f"[DWM] title bar styling failed: {e}")
    return False


def _find_hwnd_by_title(title: str) -> int:
    try:
        import ctypes
        ctypes.windll.user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        return int(hwnd) if hwnd else 0
    except Exception:
        return 0


# Iter. 35 Stufe B: Taskbar-Icon-Fix.
# Im Source-Mode (python.exe) zeigt die Taskleiste das generische Python-Icon
# weil Windows den Prozess der python.exe zuordnet. Zwei Win32-Calls noetig:
#   1. SetCurrentProcessExplicitAppUserModelID — separate Taskbar-Gruppe
#   2. WM_SETICON pro HWND — eigentliches Icon (ICON_SMALL fuer Taskbar)
# Im frozen Build (DealScraper.exe) hat die exe schon das richtige Icon, der
# Code schadet aber nicht.
_APP_USER_MODEL_ID = "fg618455.DealTracker.App"


def _set_app_user_model_id():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            _APP_USER_MODEL_ID
        )
    except Exception as e:
        print(f"[Taskbar] AppUserModelID failed: {e}")


def _set_window_icon(hwnd: int, ico_path: str) -> bool:
    if not hwnd or not ico_path or not os.path.isfile(ico_path):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.restype = ctypes.c_void_p
        h_icon = user32.LoadImageW(
            None, ico_path, IMAGE_ICON, 0, 0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        if not h_icon:
            return False
        user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON, ICON_BIG, ctypes.c_void_p(h_icon))
        user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON, ICON_SMALL, ctypes.c_void_p(h_icon))
        return True
    except Exception as e:
        print(f"[Taskbar] WM_SETICON failed: {e}")
        return False


def _apply_dark_title_bars():
    """Wird von webview.start(func=...) im worker-thread aufgerufen sobald die
    GUI-Schleife laeuft. Sucht beide Fenster per Title und setzt:
      * DWM-Dunkel-Title-Bar (Stufe A — falls frameless mal scheitert)
      * Taskbar/Window-Icon via WM_SETICON (Stufe B — fixt 'python'-Icon in
        Source-Mode)
    FindWindowW findet auch hidden=True Windows.

    Iter. 36 Stufe C: zusaetzlich wird _switch_to_flask gestartet damit das
    Splash-Window auf die Flask-URL umschaltet sobald /api/health antwortet.
    """
    ico_path = _ICON_PATH
    deadline = time.time() + 4.0
    pending = set(_WINDOW_TITLES)
    while pending and time.time() < deadline:
        for title in list(pending):
            hwnd = _find_hwnd_by_title(title)
            if hwnd:
                _set_dark_title_bar(hwnd)
                if ico_path:
                    _set_window_icon(hwnd, ico_path)
                pending.discard(title)
        if pending:
            time.sleep(0.1)
    if pending:
        print(f"[Window] konnte HWND nicht finden fuer: {pending}")

    # Iter. 36 Stufe C: Splash → Flask wechseln
    _switch_to_flask()


def _switch_to_flask():
    """Wartet bis Flask /api/health antwortet, dann laedt das Hauptfenster
    die echte URL statt des Splash-HTML.
    """
    import urllib.request
    main_win = _windows.get('main')
    if not main_win:
        return
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'{URL}/api/health', timeout=0.6).read()
            try:
                main_win.load_url(URL)
            except Exception as e:
                print(f"[Splash] load_url failed: {e}")
            return
        except Exception:
            time.sleep(0.25)
    print("[Splash] Flask antwortete nicht in 20s — Splash bleibt sichtbar")


# wird in main() befuellt, damit _apply_dark_title_bars das Icon setzen kann
_ICON_PATH = ""


# Iter. 36 Stufe C: Splash-Screen. Pywebview laedt erst statisches HTML,
# wechselt dann via window.load_url(URL) sobald Flask /api/health antwortet.
# Verhindert das ~1-2s weisse/leere Fenster nach App-Start.
SPLASH_HTML = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"/>
<title>Deal Tracker</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; }
  body {
    background: radial-gradient(ellipse at top, #1e293b 0%, #0b1220 55%, #050810 100%);
    color: #e2e8f0;
    font-family: 'Segoe UI Variable', 'Segoe UI', system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    user-select: none; -webkit-user-select: none;
    overflow: hidden;
  }
  .splash-mark {
    width: 88px; height: 88px;
    border-radius: 22px;
    background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #d946ef 100%);
    display: grid; place-items: center;
    box-shadow: 0 16px 40px rgba(129, 140, 248, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.22);
    margin-bottom: 22px;
    animation: float 3s ease-in-out infinite;
  }
  .splash-mark svg { width: 44px; height: 44px; color: #fff; }
  .splash-title {
    font-size: 22px; font-weight: 800; letter-spacing: -0.4px;
    background: linear-gradient(135deg, #fff 0%, #c4b5fd 100%);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 6px;
  }
  .splash-sub {
    font-size: 12px; color: rgba(226, 232, 240, 0.55);
    letter-spacing: 0.6px; text-transform: uppercase;
    margin-bottom: 28px;
  }
  .splash-spinner {
    width: 36px; height: 36px;
    border-radius: 50%;
    border: 3px solid rgba(129, 140, 248, 0.15);
    border-top-color: #8b5cf6;
    animation: spin 0.9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes float {
    0%, 100% { transform: translateY(0); }
    50%      { transform: translateY(-6px); }
  }
</style></head>
<body>
  <div class="splash-mark">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/>
      <circle cx="7" cy="7" r="1.5" fill="currentColor"/>
    </svg>
  </div>
  <div class="splash-title">Deal Tracker</div>
  <div class="splash-sub">startet&hellip;</div>
  <div class="splash-spinner"></div>
</body></html>"""




def _make_closing_handler(window):
    """Returnt einen Handler der hide() statt destroy() macht — ausser
    _really_quit ist gesetzt (Tray-Beenden-Pfad).
    """
    def handler():
        if _really_quit.is_set():
            return True  # erlaube echtes close
        try:
            window.hide()
        except Exception:
            pass
        return False  # cancel close
    return handler


def _bring_window_to_front(title: str):
    """Iter. 35: pywebview's window.show() un-hidet nur — bringt das Window
    nicht in den Vordergrund wenn es schon sichtbar ist (z.B. hinter anderen
    Apps). Win32 SetForegroundWindow + SW_RESTORE holen es echt nach vorn.
    """
    try:
        import ctypes
        hwnd = _find_hwnd_by_title(title)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
    except Exception as e:
        print(f"[Win32] bring-to-front '{title}' failed: {e}")


def _show_main_window():
    win = _windows.get('main')
    if win:
        try:
            win.show()
            try:
                win.restore()  # un-minimize falls minimiert
            except Exception:
                pass
        except Exception as e:
            print(f"[webview] show main failed: {e}")
    _bring_window_to_front('Deal Tracker')


def _show_scrape_window():
    win = _windows.get('scrape')
    if win:
        try:
            win.show()
            try:
                win.restore()
            except Exception:
                pass
        except Exception as e:
            print(f"[webview] show scrape failed: {e}")
    _bring_window_to_front('Deal Tracker — Scrape')


def _trigger_scrape_via_http():
    """Tray-Callback fuer 'Jetzt scrapen' — POST /api/scrape im Hintergrund."""
    def _post():
        try:
            import urllib.request
            req = urllib.request.Request(
                f'{URL}/api/scrape', method='POST',
                headers={'Content-Type': 'application/json'}, data=b'{}',
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


def _shutdown_persistent_quiet():
    try:
        import scraper
        scraper._shutdown_persistent()
    except Exception:
        pass


def _tray_quit():
    """Tray-'Beenden'-Callback. Schliesst Persistent-Chromium und destroyt
    alle pywebview-Windows — danach returnt webview.start() im main-thread.
    """
    _really_quit.set()
    _shutdown_persistent_quiet()
    try:
        import webview as _wv
        for w in list(_wv.windows):
            try:
                w.destroy()
            except Exception:
                pass
    except Exception:
        pass


def _chrome_app_fallback():
    """Letzte Rueckfallebene falls pywebview nicht startet (zb WebView2 fehlt
    auf Win10-LTSC). Vorher gemachte Aufrufe: Chrome --app=URL."""
    browser = _find_browser()
    if browser:
        try:
            subprocess.Popen([
                browser,
                f"--app={URL}",
                "--start-maximized",
                "--disable-extensions",
            ])
            return
        except Exception:
            pass
    webbrowser.open(URL)


def main():
    # Iter. 35 Stufe B: Taskbar/Windows-Identitaet. Muss VOR dem ersten Window-
    # Create laufen damit Windows den Prozess als eigene App gruppiert (sonst
    # erbt die Taskleiste das python.exe-Icon).
    _set_app_user_model_id()

    # Iter. 29/34: Single-Instance-Lock. Wenn eine andere Instanz schon laeuft,
    # frag sie einfach ihr Fenster zu zeigen (HTTP-Endpunkt /api/window/show)
    # und beende uns selbst.
    if _another_instance_running():
        print(f"DealScraper laeuft bereits auf {URL} — bitte die bestehende Instanz zeigen.")
        try:
            import urllib.request
            urllib.request.urlopen(f"{URL}/api/window/show", timeout=2).read()
        except Exception:
            pass  # Fenster konnte nicht aktiviert werden — kein Browser-Fallback
        return

    # Auto-Update: git pull still im Hintergrund, neuer Code beim naechsten Start.
    _git_pull_background()

    check_updates_and_prompt()

    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()
    _print_lan_urls()

    # Iter. 36 Stufe C: kein blockierender Pre-Wait mehr — Splash-Window
    # erscheint sofort, _switch_to_flask wartet im worker-thread auf /api/health.

    # Iter. 34: kein Chrome-Subprozess mehr. Iter. 31 Tray-Callbacks werden
    # an pywebview-Window-Methoden gebunden statt subprocess.Popen([chrome]).
    try:
        from tray import AppTray
        tray = AppTray(
            URL,
            on_show_main=_show_main_window,
            on_show_scrape=_show_scrape_window,
            on_scrape=_trigger_scrape_via_http,
            on_quit=_tray_quit,
        )
        tray_thread = threading.Thread(target=tray.run, daemon=True, name='AppTray')
        tray_thread.start()
    except Exception as e:
        print(f"[Tray] Start fehlgeschlagen ({e}) — App laeuft ohne Tray weiter.")
        tray = None

    # Module-Globals so setzen dass app.py /api/window/show drauf zugreifen kann
    import app as _app_module
    _app_module.window_show_callback = _show_main_window
    # Iter. 36: Klick auf Status-Pille im Hauptfenster oeffnet Scrape-Fenster
    _app_module.scrape_window_show_callback = _show_scrape_window

    # PyWebView-Windows aufsetzen + starten (blockt bis alle destroyed)
    try:
        import webview
    except Exception as e:
        print(f"[webview] Import failed ({e}) — Chrome-Fallback.")
        _chrome_app_fallback()
        # Tray haelt prozess am Leben
        while not _really_quit.is_set():
            time.sleep(1)
        return

    icon_path = os.path.join(BASE_DIR, "icon.ico")
    if not os.path.isfile(icon_path):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cand = os.path.join(meipass, "icon.ico")
            if os.path.isfile(cand):
                icon_path = cand

    # _apply_dark_title_bars sieht den Pfad ueber dieses Modul-Global
    global _ICON_PATH
    _ICON_PATH = icon_path if os.path.isfile(icon_path) else ""

    main_win = webview.create_window(
        'Deal Tracker',
        html=SPLASH_HTML,
        width=1400,
        height=900,
        min_size=(900, 600),
        background_color='#0b1220',
        text_select=True,
    )
    scrape_win = webview.create_window(
        'Deal Tracker — Scrape',
        f'{URL}/scrape-window',
        width=560,
        height=760,
        min_size=(420, 540),
        background_color='#0b1220',
        hidden=True,
        on_top=True,
    )
    _windows['main'] = main_win
    _windows['scrape'] = scrape_win

    # closing-Events: hide statt destroy ausser beim echten Beenden
    main_win.events.closing += _make_closing_handler(main_win)
    scrape_win.events.closing += _make_closing_handler(scrape_win)

    # DPI-Awareness: verhindert pixeligen/verschwommenen Text in WebView2.
    # Muss vor webview.start() gesetzt werden.
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:
            pass

    try:
        webview.start(
            _apply_dark_title_bars,
            icon=icon_path if os.path.isfile(icon_path) else None,
            debug=False,
            private_mode=False,
        )
    except Exception as e:
        print(f"[webview] start failed ({e}) — Chrome-Fallback.")
        _chrome_app_fallback()
        while not _really_quit.is_set():
            time.sleep(1)
        return

    # webview.start() returnt nur wenn alle Windows zerstoert sind. Aufraeumen.
    _shutdown_persistent_quiet()


if __name__ == "__main__":
    main()
