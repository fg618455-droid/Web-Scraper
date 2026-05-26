"""
Iter. 30 Diagnose:
  1) Startet eine eigene Chrome-Instanz mit --user-data-dir + --remote-debugging-port=9223
  2) Wartet bis Port erreichbar
  3) Connectet via Playwright via CDP
  4) Holt ebay.de + ein eBay-Item, prueft auf Akamai-Splash
  5) Schliesst Tab + Chrome

KEINE Aenderung an Felix' laufender App. Eigener Port (9223), eigenes Profile.
"""
import os
import sys
import time
import shutil
import subprocess
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9223
PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DealScraper", "_ProbeProfile")
DEFAULT_CHROME = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")

# Test-URLs: 1x harmlos, 1x ein echtes laufendes eBay-Item aus app.log
EBAY_HOME = "https://www.ebay.de/"
EBAY_ITEM = "https://www.ebay.de/itm/147304094372"  # aus app.log Iter. 29


def copy_ebay_cookies(default_user_data: str, target_user_data: str) -> dict:
    """Kopiert nur das Default-Profile (genauer: nur die Auth/Cookie-relevanten
    Files) von Felix' Chrome ins Probe-Profile, damit Akamai unsere Session
    als echt erkennt.

    Returnt einen Report-Dict (was wirklich kopiert wurde + warum nicht).
    """
    report = {"copied": [], "skipped": [], "errors": []}

    src_local_state = os.path.join(default_user_data, "Local State")
    src_default = os.path.join(default_user_data, "Default")
    dst_default = os.path.join(target_user_data, "Default")

    if not os.path.isfile(src_local_state):
        report["errors"].append(f"Local State fehlt: {src_local_state}")
        return report
    if not os.path.isdir(src_default):
        report["errors"].append(f"Default-Profile fehlt: {src_default}")
        return report

    os.makedirs(target_user_data, exist_ok=True)
    os.makedirs(dst_default, exist_ok=True)
    os.makedirs(os.path.join(dst_default, "Network"), exist_ok=True)

    files_to_copy = [
        ("Local State", src_local_state, os.path.join(target_user_data, "Local State")),
        ("Cookies", os.path.join(src_default, "Network", "Cookies"),
         os.path.join(dst_default, "Network", "Cookies")),
        ("Cookies-journal", os.path.join(src_default, "Network", "Cookies-journal"),
         os.path.join(dst_default, "Network", "Cookies-journal")),
        ("Preferences", os.path.join(src_default, "Preferences"),
         os.path.join(dst_default, "Preferences")),
    ]
    for label, src, dst in files_to_copy:
        if not os.path.exists(src):
            report["skipped"].append(f"{label}: src fehlt")
            continue
        try:
            shutil.copy2(src, dst)
            report["copied"].append(f"{label} ({os.path.getsize(src)} bytes)")
        except PermissionError as e:
            report["errors"].append(f"{label}: locked ({e})")
        except Exception as e:
            report["errors"].append(f"{label}: {e}")
    return report


def wait_for_cdp(port: int, timeout_s: float = 8.0) -> dict | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    import json
                    return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(0.3)
    return None


def main():
    print(f"=== Diagnose CDP-Pfad (Iter. 30) ===")
    print(f"Profile-Dir : {PROFILE_DIR}")
    print(f"Default-Src : {DEFAULT_CHROME}")
    print(f"CDP-Port    : {PORT}")
    print()

    # 1) Profile bauen
    print("[1/5] Kopiere Auth-Files aus Default-Profile ...")
    report = copy_ebay_cookies(DEFAULT_CHROME, PROFILE_DIR)
    for k in ("copied", "skipped", "errors"):
        for line in report[k]:
            print(f"      {k}: {line}")
    print()

    # 2) Chrome starten
    print("[2/5] Starte Chrome mit eigenem user-data-dir + Port ...")
    args = [
        CHROME,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--window-size=1100,700",
        "--window-position=300,300",
        "about:blank",
    ]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"      Chrome PID: {proc.pid}")

    # 3) Port-Probe
    print("[3/5] Warte auf CDP-Port ...")
    info = wait_for_cdp(PORT)
    if not info:
        print(f"      FEHLER: Port {PORT} nicht erreichbar nach 8s — Chrome-Single-Instance hat den Aufruf wahrscheinlich an Felix' Default-Master delegiert ODER eine Browser-Erweiterung blockiert.")
        return 1
    print(f"      OK: {info.get('Browser')} via {info.get('webSocketDebuggerUrl', '')[:60]}...")
    print()

    # 4) CDP-Connect + ebay-Test
    print("[4/5] Connect via Playwright + hole ebay-Item ...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("      FEHLER: Playwright nicht installiert (pip install playwright)")
        return 2

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        except Exception as e:
            print(f"      FEHLER: connect_over_cdp: {e}")
            return 3
        print(f"      Connected. Contexts: {len(browser.contexts)}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        # Erst Homepage warm-up
        page = ctx.new_page()
        try:
            page.goto(EBAY_HOME, wait_until="domcontentloaded", timeout=15_000)
            home_len = len(page.content() or "")
            print(f"      ebay.de Homepage: {home_len} bytes")
        except Exception as e:
            print(f"      Homepage-Goto FEHLER: {e}")

        # Dann Item
        try:
            page.goto(EBAY_ITEM, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_function(
                    "() => document.body && document.body.innerHTML.length > 40000",
                    timeout=8_000,
                )
            except Exception:
                pass
            html = page.content() or ""
            n = len(html)
            akamai = any(m in html for m in [
                "splashui/captcha",
                "Pardon Our Interruption",
                "blocked our access",
                "Checking your browser",
                "captcha",
            ])
            print(f"      Item-HTML: {n} bytes, Akamai-marker: {akamai}")
            if n > 100_000 and not akamai:
                print(f"      ✓✓✓ DURCH! eBay liefert Vollseite — Akamai-Bypass geht.")
                title_match = "<title" in html
                price_match = "x-price-primary" in html or "data-testid=\"x-price-primary\"" in html
                print(f"      title-Tag: {title_match}, x-price-primary CSS: {price_match}")
            elif akamai or n < 40_000:
                print(f"      ✗ GEBLOCKT — Akamai-Splash oder zu kurz")
                # Dump die ersten 500 Zeichen fuer Diagnose
                print("      --- HTML head ---")
                print((html[:500] or "<leer>").replace("\n", " "))
                print("      --- end ---")
            else:
                print(f"      ? Grenzwertig — weder klare Block-Marker noch volle Page")
        except Exception as e:
            print(f"      Item-Goto FEHLER: {e}")

        try:
            page.close()
        except Exception:
            pass
        try:
            browser.close()  # disconnected only, Chrome bleibt offen
        except Exception:
            pass

    # 5) Cleanup
    print()
    print("[5/5] Schliesse Chrome-Probe ...")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    print("      Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
