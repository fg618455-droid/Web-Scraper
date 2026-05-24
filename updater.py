"""
Updater - prueft GitHub Releases auf neue Version und oeffnet die Release-Seite
im Browser. Felix laedt die .zip dann manuell und ersetzt seinen DealScraper-Ordner.

Iter. 26: download_and_update macht jetzt webbrowser.open(release_url) statt
des brueckigen .zip-Extract-Versuchs. Grund: PyInstaller baut ein Multi-File-
Bundle (DealScraper.exe + _internal/ mit hunderten Dateien) - eine laufende
.exe kann sich selbst auf Windows nicht zuverlaessig atomar ersetzen, und der
.zip enthaelt nicht NUR die .exe sondern den ganzen Ordner-Tree. Easy-Path:
GitHub-Release-Seite oeffnen, Felix laedt manuell.
"""

import json
import os
import urllib.request
import webbrowser

GITHUB_REPO = "fg618455-droid/Web-Scraper"


def get_current_version():
    try:
        path = os.path.join(os.path.dirname(__file__), "version.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("version", "1.0.0")
    except Exception:
        return "1.0.0"


def _parse_version(v):
    return [int(x) for x in v.split(".") if x.isdigit()]


def check_for_updates():
    """Returnt (latest_version, release_html_url) wenn ein neueres Release
    existiert, sonst (None, None). release_html_url zeigt auf die menschen-
    lesbare GitHub-Release-Seite mit Download-Buttons.
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

        release_url = data.get("html_url") or (
            f"https://github.com/{GITHUB_REPO}/releases/tag/v{latest_version}"
        )
        return latest_version, release_url
    except Exception as e:
        print(f"Update Check Error: {e}")
        return None, None


def download_and_update(release_url, latest_version=None):
    """Oeffnet die Release-Seite im Browser. latest_version wird mit-akzeptiert
    fuer die Backwards-Compat-Signatur von main.py:check_updates_and_prompt.
    """
    try:
        if not release_url:
            return False
        webbrowser.open(release_url)
        return True
    except Exception as e:
        print(f"Open release page failed: {e}")
        return False
