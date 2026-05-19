"""
Run once to create a Desktop shortcut that launches Deal Scraper.
python create_shortcut.py
"""
import os
import sys
import subprocess
import tempfile

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
MAIN_PY    = os.path.join(BASE_DIR, 'main.py')
ICON_PATH  = os.path.join(BASE_DIR, 'icon.ico')
DESKTOP    = os.path.join(os.path.expanduser('~'), 'Desktop')
LNK_PATH   = os.path.join(DESKTOP, 'Deal Scraper.lnk')

# Prefer pythonw.exe so no console window appears
PYTHON_EXE = sys.executable
PYTHONW    = PYTHON_EXE.replace('python.exe', 'pythonw.exe')
if os.path.exists(PYTHONW):
    PYTHON_EXE = PYTHONW

icon_line = f'oLink.IconLocation = "{ICON_PATH}"' if os.path.exists(ICON_PATH) else ''

VBS = f"""Set oWS = WScript.CreateObject("WScript.Shell")
Set oLink = oWS.CreateShortcut("{LNK_PATH}")
oLink.TargetPath = "{PYTHON_EXE}"
oLink.Arguments = "{MAIN_PY}"
oLink.WorkingDirectory = "{BASE_DIR}"
oLink.Description = "Deal Scraper - Angebote finden"
{icon_line}
oLink.Save
"""

tmp = tempfile.NamedTemporaryFile(suffix='.vbs', delete=False,
                                   mode='w', encoding='utf-8')
tmp.write(VBS)
tmp.close()

try:
    subprocess.run(['cscript', '//nologo', tmp.name], check=True)
    print(f"Desktop-Verknüpfung erstellt: {LNK_PATH}")
    if not os.path.exists(ICON_PATH):
        print("Hinweis: icon.ico nicht gefunden. Führe zuerst 'python make_icon.py' aus.")
except Exception as e:
    print(f"Fehler: {e}")
finally:
    os.unlink(tmp.name)
