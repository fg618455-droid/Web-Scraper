import sys
import os
import json
import urllib.request
import subprocess

# ----- KONFIGURATION -----
# 1. Dein GitHub-Name und der Name des Repositories
# Laut deinem Screenshot ist es: "fg618455-droid/Web-Scraper"
GITHUB_REPO = "fg618455-droid/Web-Scraper" 

# 2. Die aktuelle Version (wichtig fürs Update!)
CURRENT_VERSION = "1.0.0"
# -------------------------

def check_for_updates():
    if not GITHUB_REPO or "DeinGitHubName" in GITHUB_REPO:
        return None, None
        
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={'User-Agent': 'DealScraper-Updater'})
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode())
                latest_version = data.get("tag_name", "").lstrip("v")
                
                def parse_version(v):
                    return [int(x) for x in v.split(".") if x.isdigit()]
                    
                if parse_version(latest_version) > parse_version(CURRENT_VERSION):
                    assets = data.get("assets", [])
                    if assets:
                        for asset in assets:
                            if asset["name"].endswith(".exe"):
                                return latest_version, asset["browser_download_url"]
    except Exception as e:
        print(f"Update Check Error: {e}")
    return None, None

def download_and_update(download_url):
    try:
        exe_path = sys.executable
        # Nur updaten, wenn als kompilierte .exe gestartet
        if not exe_path.endswith(".exe") or "python" in exe_path.lower():
            print("Updates funktionieren nur in der kompilerten .exe Version.")
            return False

        new_exe_path = exe_path + ".new"
        
        # Neue Datei herunterladen
        req = urllib.request.Request(download_url, headers={'User-Agent': 'DealScraper-Updater'})
        with urllib.request.urlopen(req) as response, open(new_exe_path, 'wb') as out_file:
            out_file.write(response.read())

        bat_path = os.path.join(os.path.dirname(exe_path), "update.bat")
        exe_name = os.path.basename(exe_path)
        new_exe_name = os.path.basename(new_exe_path)
        
        # Flag um das Fenster zu verstecken
        CREATE_NO_WINDOW = 0x08000000
        
        bat_content = f"""@echo off
timeout /t 2 /nobreak > NUL
del "{exe_name}"
ren "{new_exe_name}" "{exe_name}"
start "" "{exe_name}"
del "%~f0"
"""
        with open(bat_path, "w") as f:
            f.write(bat_content)
            
        subprocess.Popen(
            [bat_path],
            shell=True,
            creationflags=CREATE_NO_WINDOW
        )
        sys.exit(0)
    except Exception as e:
        print(f"Update failed: {e}")
        return False
