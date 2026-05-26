"""Iter. 31/34: System-Tray.

Iter. 31 hat den Tray eingefuehrt — damals startete er Chrome `--app=...`
Subprozesse fuer die UI-Fenster. Iter. 34 ersetzt das durch pywebview:
der Tray ruft jetzt Callbacks die das main-thread-eigene pywebview-Window
zeigen/verstecken.

Verantwortlichkeiten:
  * Icon im Notification-Area sichtbar halten
  * Menue: Anzeigen / Scrape-Fenster / Jetzt scrapen / Beenden
  * Klick-Callbacks an main.py weiterreichen — Window-Lifecycle gehoert
    nicht hier rein, sondern in den main-thread der pywebview kontrolliert.

Lifecycle: AppTray.run() wird in Iter. 34 im EIGENEN thread aufgerufen
(vorher main-thread). pystray-Win32-Backend funktioniert robust in jedem
thread, weil es eine eigene Win32-Message-Loop oeffnet.
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.request


def _icon_path() -> str | None:
    """Findet icon.ico zur Laufzeit. Sowohl Source-Mode als auch PyInstaller-
    Frozen werden unterstuetzt (im Bundle liegt icon.ico via spec datas
    direkt im _MEIPASS-Root).
    """
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(getattr(sys, '_MEIPASS', ''), 'icon.ico'))
        candidates.append(os.path.join(os.path.dirname(sys.executable), 'icon.ico'))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico'))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _load_icon_image():
    """Laedt das Tray-Icon als PIL.Image. Fallback: kleines blaues Quadrat —
    sorgt zumindest fuer ein klickbares Tray-Symbol falls icon.ico wegen
    Build-Pech nicht im Bundle landet.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    path = _icon_path()
    if path:
        try:
            return Image.open(path)
        except Exception:
            pass
    return Image.new('RGBA', (32, 32), (33, 150, 243, 255))


class AppTray:
    """System-Tray-Controller.

    Erwartet Callbacks die im main-thread implementiert sind (pywebview-
    Window-Operationen). Falls ein Callback None ist, faellt der Tray
    auf HTTP/Webbrowser-Fallbacks zurueck.

      url:              Basis-URL der laufenden Flask-Instanz
      on_show_main:     Callback: bringt das Haupt-UI-Fenster nach vorn
      on_show_scrape:   Callback: zeigt das Scrape-Status-Fenster
      on_scrape:        Callback: triggert /api/scrape (default: HTTP-POST)
      on_quit:          Callback: schliesst alles inkl. Persistent-Chromium
    """

    def __init__(self, url: str,
                 on_show_main=None, on_show_scrape=None,
                 on_scrape=None, on_quit=None):
        self.url = url.rstrip('/')
        self.on_show_main = on_show_main
        self.on_show_scrape = on_show_scrape
        self.on_scrape = on_scrape
        self.on_quit = on_quit
        self._icon = None
        self._quitting = False

    # ── Aktionen ────────────────────────────────────────────────────────────

    def _open_main_window(self, *_):
        if self.on_show_main:
            try:
                self.on_show_main()
                return
            except Exception as e:
                print(f"[Tray] on_show_main failed: {e}")
        # Fallback wenn kein pywebview-Callback: Default-Browser
        try:
            import webbrowser
            webbrowser.open(self.url)
        except Exception:
            pass

    def _open_scrape_window(self, *_):
        if self.on_show_scrape:
            try:
                self.on_show_scrape()
                return
            except Exception as e:
                print(f"[Tray] on_show_scrape failed: {e}")
        try:
            import webbrowser
            webbrowser.open(f'{self.url}/scrape-window')
        except Exception:
            pass

    def _trigger_scrape(self, *_):
        if self.on_scrape:
            try:
                self.on_scrape()
                return
            except Exception as e:
                print(f"[Tray] on_scrape failed: {e}")
        # Default: HTTP-POST /api/scrape im Hintergrund
        def _post():
            try:
                req = urllib.request.Request(
                    f'{self.url}/api/scrape', method='POST',
                    headers={'Content-Type': 'application/json'},
                    data=b'{}',
                )
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    def _quit(self, *_):
        self._quitting = True
        try:
            if self.on_quit:
                self.on_quit()
        except Exception:
            pass
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    # ── Lifecycle ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Blockt bis User 'Beenden' klickt. Iter. 34: laeuft in worker-thread
        (nicht mehr main-thread) — pywebview braucht main-thread fuer sich.
        """
        try:
            import pystray
        except ImportError:
            print('[Tray] pystray nicht installiert, falle auf passive Endlos-Schleife zurueck')
            import time
            try:
                while not self._quitting:
                    time.sleep(1)
            except KeyboardInterrupt:
                self._quit()
            return

        image = _load_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem('Deal Scraper anzeigen', self._open_main_window, default=True),
            pystray.MenuItem('Scrape-Fenster', self._open_scrape_window),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Jetzt scrapen', self._trigger_scrape),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Beenden', self._quit),
        )
        self._icon = pystray.Icon(
            'DealScraper',
            image,
            'Deal Tracker',
            menu,
        )
        self._icon.run()
