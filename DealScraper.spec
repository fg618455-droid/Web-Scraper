# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    # 'play' contains the Playwright Chromium browser binaries.
    # Run build-scraper.bat which installs them there automatically.
    # If the folder doesn't exist yet the build will succeed but scraping
    # will fall back to the system-installed browsers (see main.py).
    # Iter. 31: icon.ico + version.json mit ins Bundle damit Tray-Icon
    # zur Laufzeit ladbar ist und der Updater die aktuelle Version kennt.
    datas=(
        [('templates', 'templates'),
         ('static', 'static'),
         ('icon.ico', '.')] +
        ([('version.json', '.')] if __import__('os').path.isfile('version.json') else []) +
        ([('play', 'play')] if __import__('os').path.isdir('play') else [])
    ),
    # Iter. 31: pystray nutzt platform-spezifische Backends die PyInstaller
    # nicht automatisch findet. Explizit aufnehmen damit der frozen Build
    # das Tray-Icon zeigen kann.
    # Iter. 34: pywebview-edgechromium braucht pythonnet/clr_loader plus die
    # platform-Module. Sonst kommt zur Laufzeit "No module named webview...".
    hiddenimports=[
        'pystray._win32',
        'pystray._base',
        'PIL.Image',
        'PIL.ImageDraw',
        'plyer.platforms.win.notification',
        'webview',
        'webview.platforms.edgechromium',
        'webview.platforms.winforms',
        'clr_loader',
        'pythonnet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DealScraper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DealScraper',
)
