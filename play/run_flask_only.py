"""Iter. 37 dev helper: startet nur Flask ohne pywebview-UI.

Wird im Autonom-Modus genutzt damit Scrape-Tests im Headless-Bash funktionieren
ohne dass pywebview den main-thread blockt.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Profile-Path setup wie main._setup_dealscraper_chrome_profile (Iter. 30):
# Ohne das ist _persistent_available()=False und der Persistent-Pfad
# fuer Iter. 37 wird nie gewaehlt.
localapp = os.environ.get('LOCALAPPDATA', '')
if localapp and not os.environ.get('DEALSCRAPER_PROFILE_PATH'):
    profile_path = os.path.join(localapp, 'DealScraper', 'ScraperProfile')
    os.makedirs(profile_path, exist_ok=True)
    os.environ['DEALSCRAPER_PROFILE_PATH'] = profile_path

from app import app as flask_app, _load_persisted_interval
_load_persisted_interval()

HOST = '127.0.0.1'
PORT = 5001
print(f'[flask-only] starting on {HOST}:{PORT}, profile={os.environ.get("DEALSCRAPER_PROFILE_PATH")}')
flask_app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
