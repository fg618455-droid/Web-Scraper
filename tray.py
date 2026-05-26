"""Iter. 31: System-Tray + Detach.

Hintergrund: bisher hielt main.py den Prozess via `while True: sleep(1)` am
Leben. Wenn Felix das Chrome-`--app`-Fenster schloss, blieb die App zwar
laufen, aber er hatte keinen sichtbaren Indikator mehr und auch keinen Weg
sie sauber wiederzubekommen ausser ueber Task-Manager. Tray loest beides:

  * Icon im Windows-Notification-Area zeigt dass die App laeuft
  * Menue: Anzeigen / Scrape jetzt / Scrape-Fenster / Beenden
  * Doppelklick aufs Icon = Anzeigen (neues Chrome --app=...)
  * Beenden = sauberer Shutdown inkl. Persistent-Chromium

Plyer-Toast am Scrape-Ende wird aus app._do_scrape gefeuert; der Tray ist
das Heimat des Icons, nicht des Toast-Triggers.
"""
from __future__ import annotations

import os
import subprocess
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
    """Laedt das Tray-Icon als PIL.Image. Fallback: kleines transparentes
    Quadrat — sorgt zumindest fuer ein klickbares Tray-Symbol falls icon.ico
    wegen Build-Pech nicht im Bundle landet.
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

    Erwartet:
      url:              Basis-URL der laufenden Flask-Instanz (http://127.0.0.1:5001)
      browser_path:     Pfad zur Chrome/Edge-Binary (None = webbrowser-Fallback)
      cdp_port:         Optionaler Debug-Port fuer Chrome --app
      on_quit:          Optionaler Callback der vor Process-Exit laeuft
                        (z.B. Persistent-Chromium schliessen).
    """

    def __init__(self, url: str, browser_path: str | None,
                 cdp_port: int | None = None, on_quit=None):
        self.url = url.rstrip('/')
        self.browser_path = browser_path
        self.cdp_port = cdp_port
        self.on_quit = on_quit
        self._icon = None
        self._quitting = False

    # ── Aktionen ────────────────────────────────────────────────────────────

    def _chrome_args(self, target_url: str, extra: list[str] | None = None) -> list[str]:
        args = [
            self.browser_path,
            f'--app={target_url}',
            '--disable-extensions',
        ]
        if self.cdp_port:
            args.append(f'--remote-debugging-port={self.cdp_port}')
            args.append('--remote-allow-origins=*')
            args.append('--disable-blink-features=AutomationControlled')
        if extra:
            args.extend(extra)
        return args

    def _open_main_window(self, *_):
        """Oeffnet das Haupt-UI in einem neuen Chrome --app Fenster.
        Wenn schon eines auf ist macht Chrome eh ein zweites - kein Problem.
        """
        if not self.browser_path:
            import webbrowser
            webbrowser.open(self.url)
            return
        try:
            subprocess.Popen(self._chrome_args(
                self.url,
                ['--start-maximized'],
            ))
        except Exception:
            import webbrowser
            webbrowser.open(self.url)

    def _open_scrape_window(self, *_):
        """Oeffnet das Mini-Scrape-Fenster (380x540) das den Scrape-Status
        live anzeigt. Praktisch wenn Felix die App im Tray hat aber zusehen
        will was gerade lauft.
        """
        target = f'{self.url}/scrape-window'
        if not self.browser_path:
            import webbrowser
            webbrowser.open(target)
            return
        try:
            subprocess.Popen(self._chrome_args(
                target,
                ['--window-size=380,540', '--window-position=80,80'],
            ))
        except Exception:
            import webbrowser
            webbrowser.open(target)

    def _trigger_scrape(self, *_):
        """Loest einen globalen Scrape aus indem POST /api/scrape gehit wird.
        Background thread damit das Tray-Menu nicht blockiert (Scrape kann
        Sekunden brauchen, sogar nur fuer den Start-Lock).
        """
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
        """Blockt bis der User 'Beenden' klickt. Soll im Main-Thread aufgerufen
        werden — pystray-Win32 funktioniert robuster mit eigener Message-Loop
        im Owner-Thread.
        """
        try:
            import pystray
        except ImportError:
            # Tray nicht verfuegbar - fallback auf altes Verhalten
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
            'Deal Scraper',
            menu,
        )
        # icon.run() blockt — perfekt fuer Main-Thread
        self._icon.run()
