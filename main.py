"""
Deal Scraper – Desktop App Entry Point
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# Load external config (optional — fallback to defaults)
_config_path = os.path.join(BASE_DIR, 'config.json')
try:
    with open(_config_path, encoding='utf-8') as _f:
        CONFIG = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    CONFIG = {}

HOST = CONFIG.get('host', '127.0.0.1')
PORT = CONFIG.get('port', 5001)

import database as db
db.init_db()

from app import app as flask_app, _load_persisted_interval
_load_persisted_interval()

URL = f'http://{HOST}:{PORT}'

BROWSER_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]


def _run_flask():
    flask_app.run(host=HOST, port=PORT, debug=False,
                  use_reloader=False, threaded=True)


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
        
        latest_version, download_url = check_for_updates()
        if latest_version and download_url:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            
            result = messagebox.askyesno(
                "Update verfuegbar!", 
                f"Eine neue Version (v{latest_version}) des Deal Scrapers ist verfuegbar.\n\nMoechtest du sie jetzt herunterladen und installieren?"
            )
            
            if result:
                success = download_and_update(download_url)
                if not success:
                    messagebox.showerror("Fehler", "Update fehlgeschlagen. Stelle sicher, dass du die App als kompilierte .exe startest.")
            root.destroy()
    except Exception as e:
        pass


def main():
    check_updates_and_prompt()
    
    flask_thread = threading.Thread(target=_run_flask, daemon=True)
    flask_thread.start()

    for _ in range(20):
        time.sleep(0.3)
        try:
            import urllib.request
            urllib.request.urlopen(URL, timeout=1)
            break
        except Exception:
            pass

    browser = _find_browser()
    if browser:
        subprocess.Popen([
            browser,
            f'--app={URL}',
            '--start-fullscreen',
            '--start-maximized',
            '--disable-extensions',
        ])
    else:
        webbrowser.open(URL)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
