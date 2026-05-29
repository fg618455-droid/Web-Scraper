"""Iter. 37 Probe: testet fetch_search_via_persistent auf bot-walled Sites.

Erwartung: Akamai/DataDome-Sites die im normalen Batch 'blocked' liefern,
sollen ueber persistent context HTML zurueckgeben.

Usage: python play/probe_persistent_sites.py
"""
import os, sys, time, json, re

# Sicherstellen dass das Profile-Path env-var gesetzt ist (sonst _persistent_available=False)
if not os.environ.get('DEALSCRAPER_PROFILE_PATH'):
    localapp = os.environ.get('LOCALAPPDATA', '')
    if localapp:
        path = os.path.join(localapp, 'DealScraper', 'ScraperProfile')
        os.makedirs(path, exist_ok=True)
        os.environ['DEALSCRAPER_PROFILE_PATH'] = path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

SITES = [
    ('Saturn',     'https://www.saturn.de/de/search.html?query=macbook+air+m4',
                   '[data-test="mms-product-card"]'),
    ('Idealo',     'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q=macbook+air+m4',
                   '[data-testid="resultItem"], .offerList-item, article'),
    ('Kaufland',   'https://www.kaufland.de/item/search/?search_value=macbook+air+m4',
                   '[data-testid="product-card"], article.product'),
    ('Galaxus',    'https://www.galaxus.de/de/search?q=macbook+air+m4',
                   'article[data-product], [data-testid*="product"], [class*="product-tile"]'),
    ('Coolblue',   'https://www.coolblue.de/zoeken?query=macbook+air+m4',
                   '[data-product-uuid], article.product, [class*="product-card"]'),
    ('Chrono24',   'https://www.chrono24.de/search/index.htm?query=garmin+forerunner+265',
                   '.article-item, [class*="article"], [data-article-id]'),
    ('Backmarket', 'https://www.backmarket.de/de-de/search?q=macbook+air+m4',
                   '[data-test="product-card"], article[data-product]'),
    ('Christ',     'https://www.christ.de/search/?q=garmin+forerunner+265',
                   '[data-product], .product, [class*="product"]'),
    ('Watchshop',  'https://www.watchshop.com/search.html?q=garmin+forerunner+265',
                   '[data-fp-tile], [data-product], .product'),
    ('Chronext',   'https://www.chronext.com/de/search?query=garmin+forerunner+265',
                   '[data-testid*="product"], article, [class*="ProductCard"]'),
]


def card_count(html: str, selectors_str: str) -> int:
    """Wuerde der Generic-Parser hier Cards finden? Schnell-Check via Regex."""
    if not html:
        return 0
    n = 0
    # Mini-Estimator: erstes selector-Set
    sel = selectors_str.split(',')[0].strip()
    # crude: try data-test="..." pattern
    m = re.search(r'data-test\s*=\s*"([^"]+)"', sel)
    if m:
        # count occurrences in html
        n = html.count(f'data-test="{m.group(1)}"')
    if not n:
        # Try class*=
        m2 = re.search(r'class\s*\*=\s*"([^"]+)"', sel)
        if m2:
            n = html.count(m2.group(1))
    if not n:
        # JSON-LD Produkte
        n = html.count('"@type":"Product"') + html.count('"@type": "Product"')
    return n


print('Persistent available:', scraper._persistent_available())
print('Profile path:', scraper._persistent_profile_path())
print('=' * 78)

results = []
for name, url, wait_sel in SITES:
    t0 = time.time()
    try:
        html = scraper.fetch_search_via_persistent(
            url, wait_selectors=wait_sel, settle_ms=2500)
    except Exception as e:
        html = None
        print(f'{name:14s} ERR {e}')
        results.append({'site': name, 'status': 'error', 'err': str(e)})
        continue
    dt = time.time() - t0
    if html is None:
        print(f'{name:14s} BLOCKED  {dt:.1f}s')
        results.append({'site': name, 'status': 'blocked', 'time': round(dt, 1)})
        continue
    lo = html.lower()
    blocked = any(m in lo for m in scraper._BOT_CHALLENGE_MARKERS)
    cards = card_count(html, wait_sel)
    print(f'{name:14s} {"BLOCKED" if blocked else "OK"}  {len(html):>7d}b  ~{cards:3d} cards  {dt:.1f}s')
    # debug HTML schreiben fuer Selektor-Analyse
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..',
                            'debug_html', f'_probe_{name}.html')
    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html[:300_000])
    except Exception:
        pass
    results.append({'site': name, 'status': 'blocked' if blocked else 'ok',
                    'bytes': len(html), 'cards': cards, 'time': round(dt, 1)})

print('=' * 78)
print(json.dumps(results, ensure_ascii=False, indent=2))
