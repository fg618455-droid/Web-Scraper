"""Iter. 30 Diagnose #2: launch_persistent_context HEADLESS, mit denselben
Auth-Files-Kopien wie #1. Testet ob:
  a) headless durchkommt
  b) /bfl/viewbids/<id> (Bid-History) erreichbar ist
"""
import os
import sys
import shutil

PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DealScraper", "_ProbeProfile2")
DEFAULT_CHROME = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
EBAY_ITEM = "https://www.ebay.de/itm/147304094372"
EBAY_BIDS = "https://www.ebay.de/bfl/viewbids/147304094372?item=147304094372&rt=nc"


def stage_profile():
    os.makedirs(os.path.join(PROFILE, "Default", "Network"), exist_ok=True)
    src_local = os.path.join(DEFAULT_CHROME, "Local State")
    src_pref  = os.path.join(DEFAULT_CHROME, "Default", "Preferences")
    if os.path.exists(src_local):
        try:
            shutil.copy2(src_local, os.path.join(PROFILE, "Local State"))
            print(f"  staged Local State ({os.path.getsize(src_local)} bytes)")
        except Exception as e:
            print(f"  Local State copy: {e}")
    if os.path.exists(src_pref):
        try:
            shutil.copy2(src_pref, os.path.join(PROFILE, "Default", "Preferences"))
            print(f"  staged Preferences ({os.path.getsize(src_pref)} bytes)")
        except Exception as e:
            print(f"  Preferences copy: {e}")


def test_url(ctx, label, url):
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        try:
            page.wait_for_function(
                "() => document.body && document.body.innerHTML.length > 30000",
                timeout=6_000,
            )
        except Exception:
            pass
        html = page.content() or ""
        n = len(html)
        blocked = any(m in html.lower() for m in [
            "splashui/captcha", "pardon our interruption", "blocked our access",
            "checking your browser", "datadome", "px-captcha",
        ])
        ok = (not blocked) and n > 80_000
        status = "OK    " if ok else ("BLOCK " if blocked else "SHORT ")
        print(f"  {status} [{label}] {n:>8} bytes (blocked={blocked})  {url[:80]}")
        if not ok:
            head = (html[:300] or "<leer>").replace("\n", " ")
            print(f"         head: {head}")
        return ok, html
    except Exception as e:
        print(f"  ERR    [{label}] {e}")
        return False, ""
    finally:
        try: page.close()
        except: pass


def run(headless_mode):
    print(f"\n=== Test headless={headless_mode} ===")
    from playwright.sync_api import sync_playwright
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=headless_mode,
            args=args,
            channel="chrome",  # echte Chrome-Binary statt bundled chromium → besser gegen Akamai
        )
        try:
            ok_item, html_item = test_url(ctx, "/itm/<id>    ", EBAY_ITEM)
            ok_bids, html_bids = test_url(ctx, "/bfl/viewbids", EBAY_BIDS)

            if ok_item:
                # quick parse-check
                has_title = "<title" in html_item
                has_price = ("x-price-primary" in html_item) or ('"price"' in html_item.lower())
                print(f"         item parseable: title={has_title} price-marker={has_price}")
            if ok_bids:
                has_table = "<table" in html_bids.lower()
                has_eur = " EUR" in html_bids or "€" in html_bids
                # count bids zeilen ansatzweise
                from collections import Counter
                row_hits = html_bids.lower().count("<tr")
                print(f"         bids parseable: table={has_table} eur-marker={has_eur} <tr>={row_hits}")
            return ok_item, ok_bids
        finally:
            try: ctx.close()
            except: pass


def main():
    print("Staging profile from Default-Chrome:")
    stage_profile()

    print()
    item_h, bids_h = run(True)   # headless
    if not (item_h and bids_h):
        item_v, bids_v = run(False)  # headed fallback

    print("\n=== Summary ===")


if __name__ == "__main__":
    sys.exit(main() or 0)
