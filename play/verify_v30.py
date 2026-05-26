"""Iter. 30 Live-Verifikation: ruft die produktiven scraper-Funktionen auf
und checkt, ob die in app.log dokumentierten 'blocked' Items nun durchgehen.

Nutzt KEINE Flask-Route, kein Port-Konflikt mit der laufenden v27.exe —
isoliert die scraper-Logik in einem eigenen Python-Prozess.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Simuliere main.py's profile setup
import shutil


def stage_profile():
    profile = os.path.join(os.environ.get("LOCALAPPDATA", ""), "DealScraper", "ScraperProfile")
    default_user_data = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    os.makedirs(os.path.join(profile, "Default", "Network"), exist_ok=True)

    for src_rel, dst_rel in [
        ("Local State", "Local State"),
        (os.path.join("Default", "Preferences"), os.path.join("Default", "Preferences")),
    ]:
        src = os.path.join(default_user_data, src_rel)
        dst = os.path.join(profile, dst_rel)
        if os.path.exists(src):
            try:
                shutil.copy2(src, dst)
                print(f"  staged {src_rel} -> {os.path.getsize(src)} bytes")
            except Exception as e:
                print(f"  {src_rel}: {e}")
    os.environ["DEALSCRAPER_PROFILE_PATH"] = profile
    return profile


def main():
    print("=== Iter. 30 Live-Verifikation ===\n")
    print("[1/3] Stage profile from Default-Chrome:")
    profile = stage_profile()
    print(f"  Profile: {profile}\n")

    import scraper

    print("[2/3] Pre-flight: _persistent_available() ->", scraper._persistent_available())
    print()

    test_urls = [
        "https://www.ebay.de/itm/147304094372",  # aus app.log blocked-Liste
        "https://www.ebay.de/itm/206288742210",  # aus app.log blocked-Liste
        "https://www.ebay.de/itm/157929800656",  # aus app.log blocked-Liste
    ]

    print("[3/3] refresh_ebay_item gegen blocked-history URLs:")
    successes = 0
    blocked = 0
    failures = 0
    for url in test_urls:
        t0 = time.time()
        result = scraper.refresh_ebay_item(url)
        dt = time.time() - t0
        if result is None:
            print(f"  FAIL    {url:60s} ({dt:.1f}s)")
            failures += 1
        elif result.get('blocked'):
            print(f"  BLOCKED {url:60s} ({dt:.1f}s)")
            blocked += 1
        else:
            keys = sorted(result.keys())
            preview = {k: result[k] for k in keys if k in ('price', 'bid_count', 'listing_type', 'ended')}
            print(f"  OK      {url:60s} ({dt:.1f}s) -> {preview}")
            successes += 1

    print(f"\n=== Summary: {successes} OK, {blocked} blocked, {failures} fail ===")
    if successes >= 2:
        print(">>> AKAMAI-BYPASS GREIFT — v30 ready <<<")
        return 0
    else:
        print(">>> Bypass funktioniert NICHT zuverlaessig — weiter diagnostizieren <<<")
        return 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        # Triggert atexit -> _shutdown_persistent
        pass
    sys.exit(rc)
