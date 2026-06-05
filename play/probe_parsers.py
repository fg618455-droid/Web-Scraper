"""Iter. 37: Probiert _make_generic_parser auf jedem Probe-HTML mit
verschiedenen Selektor-Sets. Zeigt welche Selektoren wieviele Deals liefern.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

CASES = [
    # (site, file, base_url, selectors_to_try)
    ('Idealo', 'debug_html/_probe_Idealo.html', 'https://www.idealo.de', [
        ['[data-testid="resultItem"]', 'div[class*="resultItem"]'],
        ['article', '[role="listitem"]'],
        ['div.offerList-item', '[class*="productOffers"]'],
    ]),
    ('Galaxus', 'debug_html/_probe_Galaxus.html', 'https://www.galaxus.de', [
        ['article'],
        ['article[class]'],
        ['[data-testid="product-card"]', 'article.product'],
    ]),
    ('Coolblue', 'debug_html/_probe_Coolblue.html', 'https://www.coolblue.de', [
        ['[data-product-uuid]', 'article.product'],
        ['[class*="product-card"]', '[class*="product-tile"]'],
        ['article'],
    ]),
    ('Christ', 'debug_html/_probe_Christ.html', 'https://www.christ.de', [
        ['[class*="tile"]', 'article'],
        ['div.tile'],
        ['[data-product]'],
    ]),
    ('Watchshop', 'debug_html/_probe_Watchshop.html', 'https://www.watchshop.com', [
        ['[data-fp-tile]', '[data-product]'],
        ['[class*="product"]'],
        ['article', '.product'],
    ]),
    ('Chronext', 'debug_html/_probe_Chronext.html', 'https://www.chronext.com', [
        ['[data-testid*="product"]', '[class*="ProductCard"]'],
        ['article', '[class*="product"]'],
        ['[class*="ItemTile"]', '[class*="product-card"]'],
    ]),
]

for site, fn, base, selsets in CASES:
    full = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), fn)
    if not os.path.isfile(full):
        print(f'{site:14s} NO HTML')
        continue
    html = open(full, encoding='utf-8').read()
    print(f'\n=== {site} ({len(html)} bytes) ===')
    best = (0, None)
    for sels in selsets:
        parser = scraper._make_generic_parser(site, sels, base)
        try:
            deals = parser(html, default_model='', keyword='macbook' if site in ('Idealo','Galaxus','Coolblue') else 'garmin')
        except Exception as e:
            print(f'  sels={sels} ERR {e}')
            continue
        n = len(deals)
        print(f'  sels={sels} -> {n} deals')
        if deals:
            for d in deals[:3]:
                print(f'    - {d["title"][:60]!r} {d.get("price")}')
        if n > best[0]:
            best = (n, sels)
    print(f'  >>> BEST for {site}: {best}')
