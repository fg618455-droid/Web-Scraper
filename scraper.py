"""
Scraping logic.
- Kleinanzeigen: Playwright (JS rendering), dynamic search targets, extracts images
- mac-store24 / asgoodasnew: requests + BeautifulSoup, Apple refurbished stores
All three run in parallel via ThreadPoolExecutor.
"""

import atexit
import json
import os
import random
import re
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_SITE_NAMES = [
    # Separate Playwright flow (own cookie/session handling)
    'Kleinanzeigen',
    # Simple requests-based (server-rendered HTML)
    'mac-store24', 'Apple',
    # All below go through scrape_anti_bot_batch() with shared Playwright+stealth:
    'eBay', 'markt.de', 'quoka',
    'Otto', 'Mindfactory', 'Alternate', 'Gravis', 'future-x', 'Conrad',
    'Refurbed',
    'Idealo', 'notebooksbilliger', 'Cyberport',
    'Amazon', 'Kaufland', 'Backmarket',
    # Iter. 36 (2026-05-26): 40 weitere Sites aus websitenliste.md.
    # Generic Parser (_make_generic_parser) mit gaengigen Selektor-Sets.
    # Anti-Bot-blockierte Sites werden ehrlich als status='blocked' reportet.
    # ── Tech / Elektronik (9) ─────────────────────────────────────
    'MediaMarkt', 'Saturn', 'Galaxus', 'Coolblue', 'Computeruniverse',
    'Expert', 'Euronics', 'ReBuy', 'Jacob',
    # ── Uhren & Accessoires (14) ─────────────────────────────────
    'Christ', 'Chrono24', 'Uhrzeit.org', 'Uhrinstinkt', 'Valmano',
    'Brandfield', 'Watchshop', 'Chronext', 'Wardow', 'Fashionette',
    'Kapten-Son', 'Fossil', 'Skagen', 'Liebeskind-Berlin',
    # ── Parfuem & Beauty (14) ────────────────────────────────────
    'Douglas', 'Flaconi', 'Notino', 'Parfumdreams', 'Sephora',
    'Easycosmetic', 'Pieper', 'Lookfantastic', 'Beautywelt',
    'Ludwigbeck', 'Basler-Beauty', 'Hagel-Shop', 'Shop-Apotheke',
    'DocMorris',
    # ── Marktplaetze & Trend-Shops (10, ohne Etsy/BestSecret) ────
    'Zalando', 'AboutYou', 'Asos', 'Etsy', 'BestSecret',
    'Veepee', 'Snipes', 'HHV', 'Breuninger', 'Baur', 'Lidl',
]

# Per-site state field semantics:
#   ok     : True  = at least one HTTP request succeeded
#            False = scraper crashed completely
#            None  = not run yet
#   status : 'ok'      = fetched & got results
#            'empty'   = fetched OK but parser found 0 (selectors stale or no match)
#            'blocked' = HTTP 403/503/429 on every target (anti-bot)
#            'error'   = network error / timeout / 404
#   detail : free-text reason for the chosen status (e.g. 'HTTP 403 on all 4 targets')
STATUS = {
    'last_scrape': None,
    'scraping':    False,
    # Iter. 36: Live-Status was aktuell laeuft (Site/Target/Keyword/Group).
    # UI zeigt das im Overlay an: "Suche „macbook air m4" auf MediaMarkt (Computer Apple)"
    'current': {
        'site': None, 'target': None, 'keyword': None, 'group': None,
    },
    'sites': {
        name: {'ok': None, 'last': None, 'count': 0, 'status': None, 'detail': None}
        for name in _SITE_NAMES
    },
}
# Lock für thread-sicheres Schreiben in STATUS (wird von mehreren Threads mutiert)
_STATUS_LOCK = threading.Lock()


def _set_current(site=None, target=None, keyword=None, group=None) -> None:
    """Iter. 36: Update the live-progress hint shown in the scrape overlay."""
    STATUS['current'] = {
        'site': site, 'target': target, 'keyword': keyword, 'group': group,
    }


def _load_last_scrape_from_db() -> None:
    """Iter. 35: nach App-Restart STATUS['last_scrape'] aus der DB rekonstruieren.
    Sonst zeigt die UI 'Noch nicht gescrapt' obwohl Deals da sind. Lese MAX
    last_seen aus der deals-Tabelle — das ist der Zeitpunkt des juengsten Scrapes.
    """
    try:
        import sqlite3
        try:
            from paths import resolve_user_file
            db_path = resolve_user_file('deals.db')
        except Exception:
            import os
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'deals.db')
        con = sqlite3.connect(db_path)
        row = con.execute('SELECT MAX(last_seen) FROM deals').fetchone()
        con.close()
        if row and row[0]:
            STATUS['last_scrape'] = row[0]
    except Exception:
        pass


_load_last_scrape_from_db()

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'de-DE,de;q=0.9',
}

_MODEL_KEYWORDS = [
    ('macbook air m4',  'MacBook Air M4'),
    ('macbook pro m4',  'MacBook Pro M4'),
    ('mac mini m4',     'Mac mini M4'),
    ('mac mini',        'Mac mini M4'),
    ('macbook air',     'MacBook Air M4'),
    ('macbook pro',     'MacBook Pro M4'),
]


def _detect_model(text: str) -> str | None:
    t = text.lower()
    for kw, label in _MODEL_KEYWORDS:
        if kw in t:
            return label
    return None


def _extract_ram(text: str) -> str | None:
    m = re.search(r'(\d{1,3})\s*GB\s*(?:RAM|Unified)', text, re.IGNORECASE)
    return m.group(0) if m else None


def _extract_ssd(text: str) -> str | None:
    m = re.search(r'(\d+)\s*(GB|TB)\s*(?:SSD|Storage)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'(\d+)\s*TB', text, re.IGNORECASE)
    return m.group(0) if m else None


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    # Match the FIRST German-format price: "1.250,99", "1.250", "650"
    # Using \d{3} after the dot ensures thousands-separators are recognised correctly
    # and prevents concatenating multiple prices like "1.250 VB 1.599" → 12501599
    m = re.search(r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)', text)
    if not m:
        return None
    raw = m.group(1).replace('.', '').replace(',', '.')
    try:
        val = float(raw)
        return val if val >= 1 else None
    except ValueError:
        return None


_BUYING_RE = re.compile(
    r'\b(ankauf|ankäufe|ankaufen|gesucht|suche[n]?|kaufe|biete\s+nicht)\b',
    re.IGNORECASE,
)

_GARBAGE_RE = re.compile(
    r'\b('
    r'defekt|defekte[snr]?|kaputt|nicht\s+funktionstüchtig|funktioniert\s+nicht|geht\s+nicht'
    r'|ersatzteil(?:e|spender)?|bastler|bastel|reparatur(?:bedürftig)?'
    r'|wasserschaden|displaybruch|displayschaden|displayriss'
    r'|tausch(?:e|en)?|getauscht'
    r'|sonstiges?|diverse[sn]?'
    r'|gestohlen|unterschlagen|ausweis|leergehäuse'
    r')\b',
    re.IGNORECASE,
)


def _is_buying_request(title: str) -> bool:
    return bool(_BUYING_RE.search(title or ''))


def _is_garbage_listing(title: str, desc: str | None = None) -> bool:
    """True if the listing is broken, an exchange, accessory-only, or otherwise unwanted."""
    text = f'{title or ""} {desc or ""}'
    return bool(_GARBAGE_RE.search(text))


_ACCESSORY_WORDS = re.compile(
    r'\b('
    r'dock(?:ing)?(?:station)?'
    r'|sch[uü]tzh[uü]lle|h[uü]lle|sleeve|tasche|etui'
    r'|sch[uü]tzfolie|sch[uü]tzglas|displaysch[uü]tz|panzerglas'
    r'|adapter|hub|usb[-\s]?c[-\s]?hub|kartenleser'
    r'|ladeger[aä]t|ladekabel|netzteil|charger|powerbank'
    r'|st[aä]nder|halterung|halter|wandhalterung|mount'
    r'|kabel(?:set)?|magsafe[-\s]?kabel'
    r'|skin|aufkleber|sticker|folie'
    r'|abdeckung|displayschutzglas'
    # Cases / backs / covers (catches "MagSafe-Rückseite", "Bumper",
    # "Hard Case", "Silikon Case", "Back Cover") — these are the
    # ones flooding the iPhone results.
    r'|r[uü]ckseite|backplate|back[\s-]?cover|bumper'
    r'|(?:hard|silikon|silicon|gel|tpu|leder|leather)[\s-]?case'
    r'|kameralinse|kameraschutz|linsenschutz|objektivschutz|kameraglas'
    r')\b',
    re.IGNORECASE,
)

_FOR_PRODUCT = re.compile(
    r'\b(für|fuer|passend\s+(?:für|fuer)|kompatibel\s+(?:mit|für|fuer))\b',
    re.IGNORECASE,
)

# Words that are almost always accessories on their own — matched regardless
# of position in the title (no "before keyword" heuristic needed).
_DEFINITELY_ACCESSORY = re.compile(
    r'\b(dockingstation|docking[\s-]?station|schutzfolie|schutzglas|displayschutz'
    r'|panzerglas|ladekabel|netzteil|magsafe[-\s]?kabel|powerbank|usb[-\s]?c[-\s]?hub'
    r'|tastatur[\s-]?h[uü]lle|laptop[\s-]?h[uü]lle|laptop[\s-]?tasche|laptop[\s-]?sleeve'
    r'|notebook[\s-]?tasche|notebook[\s-]?sleeve|handy[\s-]?h[uü]lle|smartphone[\s-]?h[uü]lle'
    # Case / back / cover patterns flooding the iPhone results (2026-05).
    # These are unambiguous accessories regardless of where they sit in the
    # title — "iPhone 17 Pro Hard Case" still needs to be filtered out
    # even though the keyword comes first.
    r'|magsafe[\s-]?r[uü]ckseite|magsafe[\s-]?cover|magsafe[\s-]?case'
    r'|iphone[\s-]?case|smartphone[\s-]?case'
    r'|(?:hard|silikon|silicon|gel|tpu|leder|leather)[\s-]?case'
    r'|backplate|back[\s-]?cover|r[uü]ckseite|bumper'
    r'|displayfolie|kameraschutz|kameralinsen[\s-]?schutz'
    r'|objektivschutz|kameraglas|linsenschutz|kameralinse'
    r')\b',
    re.IGNORECASE,
)


def _is_accessory(title: str, target_keyword: str | None = None) -> bool:
    """Detect accessory-only listings (dock, case, charger, etc.).
    target_keyword: the keyword being searched (e.g. 'mac mini m4') —
    if the title looks like an accessory FOR that keyword, it's filtered out."""
    if not title:
        return False
    if _DEFINITELY_ACCESSORY.search(title):
        return True
    has_acc = bool(_ACCESSORY_WORDS.search(title))
    has_for = bool(_FOR_PRODUCT.search(title))
    if has_acc and has_for:
        return True
    # Heuristic: title starts with accessory keyword + product name follows
    if has_acc and target_keyword:
        # e.g. "Hülle für iPhone 15" with target keyword "iphone 15"
        for word in target_keyword.lower().split():
            if len(word) >= 3 and word in title.lower():
                # If accessory word appears BEFORE the target keyword, it's likely "Acc FOR Product"
                acc_match = _ACCESSORY_WORDS.search(title)
                target_pos = title.lower().find(word)
                if acc_match and acc_match.start() < target_pos:
                    return True
    return False


_KW_STOP = {'der', 'die', 'das', 'und', 'mit', 'für', 'fuer', 'in', 'on', 'a', 'and'}
# Between two keyword tokens we allow at most 2 short filler tokens (e.g. screen size "13",
# year "2025") separated by punctuation/whitespace. This rejects "iPhone SE 3 A 15 Bionic"
# for keyword "iphone 15" (too many words between), while still accepting
# "MacBook Air 13 M4" for keyword "macbook air m4".
_KW_GAP = r'(?:[\s\-,/().\'"]+\w{1,5}){0,2}[\s\-,/().\'"]*'


def _matches_keyword(title: str, keyword: str | None) -> bool:
    """Title must contain the keyword as a near-contiguous phrase.

    Primary check: tokens must appear in keyword order; at most two short filler
    tokens (≤5 chars, e.g. "13", "2025") are tolerated between them.
    This prevents 'iPhone SE 3 ... A 15 Bionic' from matching 'iphone 15'.

    Fallback (any-order): if the ordered match fails, accept the title if every
    keyword token appears somewhere in it regardless of order.  This covers
    listings like "Apple M4 Mac mini 512GB" for keyword "mac mini m4", where
    the chip designation precedes the product name.

    - Numbers ('15') get a trailing word boundary so '150' won't match.
    - Stop-words ('der/die/und/...') are ignored.
    - Empty keyword/title → accept everything.
    """
    if not keyword or not title:
        return True
    title_lower = title.lower()
    words = [w for w in keyword.lower().split() if len(w) >= 2 and w not in _KW_STOP]
    if not words:
        return True
    parts = []
    for w in words:
        if w.isdigit():
            parts.append(rf'{re.escape(w)}\b')
        else:
            parts.append(re.escape(w))
    # Primary: ordered match (fast path, prevents false positives like iphone 15 vs iphone 150)
    ordered_pattern = r'\b' + _KW_GAP.join(parts)
    if re.search(ordered_pattern, title_lower):
        return True
    # Fallback: all tokens present anywhere in the title (handles "M4 Mac mini" order)
    return all(re.search(rf'\b{p}', title_lower) for p in parts)


def _is_unwanted(title: str, desc: str | None = None, target_keyword: str | None = None) -> bool:
    return (_is_buying_request(title)
            or _is_garbage_listing(title, desc)
            or _is_accessory(title, target_keyword)
            or not _matches_keyword(title, target_keyword))


_PICKUP_RE = re.compile(r'\b(nur\s+abholung|selbstabholung|abholung\s+only|kein\s+versand)\b', re.IGNORECASE)
_SHIPPING_RE = re.compile(r'\b(versand|verschicke|verschickt|porto|paket|dhl|hermes)\b', re.IGNORECASE)


def _detect_pickup_only(title: str, desc: str | None, has_shipping_badge: bool) -> bool:
    """Returns True if the listing is pickup-only (no shipping)."""
    if has_shipping_badge:
        return False
    text = f'{title or ""} {desc or ""}'
    if _PICKUP_RE.search(text):
        return True
    # If neither shipping nor pickup mentioned, default to "unknown" (not pickup-only)
    return False


def _keyword_to_slug(keyword: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', keyword.lower().strip()).strip('-')


def _best_image(el) -> str | None:
    """Extract the best image URL from an img element."""
    if el is None:
        return None
    for attr in ('src', 'data-src', 'data-lazy-src', 'data-original'):
        val = (el.get(attr) or '').strip()
        if val and val.startswith('http') and not val.startswith('data:'):
            return val
    # srcset: take the last (largest) entry
    srcset = el.get('srcset', '') or el.get('data-srcset', '')
    if srcset:
        parts = [p.strip().split(' ')[0] for p in srcset.split(',') if p.strip()]
        for p in reversed(parts):
            if p.startswith('http'):
                return p
    return None


# ── Kleinanzeigen ─────────────────────────────────────────────────────────────

def scrape_kleinanzeigen(targets: list[dict]) -> list[dict]:
    """targets: [{'name': str, 'keyword': str}, ...]"""
    deals: list[dict] = []
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx  = browser.new_context(user_agent=HEADERS['User-Agent'])
            page = ctx.new_page()

            for target in targets:
                slug        = _keyword_to_slug(target['keyword'])
                target_name = target['name']
                kw_encoded  = target['keyword'].replace(' ', '+')
                # Primary URL: slug-based (fast, known to work)
                # Fallback URL: query-param format (more robust for multi-word keywords)
                urls_to_try = [
                    f'https://www.kleinanzeigen.de/s-{slug}/k0',
                    f'https://www.kleinanzeigen.de/s-anzeigen/{slug}/k0',
                    f'https://www.kleinanzeigen.de/s-anzeigen/q-{kw_encoded}/k0',
                ]
                target_deals_before = len(deals)
                for url in urls_to_try:
                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=25_000)
                        try:
                            page.click('#gdpr-banner-accept', timeout=3_000)
                        except Exception:
                            pass
                        # Wait for listing items to appear (more robust than fixed timeout)
                        try:
                            page.wait_for_selector(
                                'article.aditem, li.ad-listitem article, '
                                '[data-testid="ad-item"], article[class*="aditem"]',
                                timeout=6_000
                            )
                        except Exception:
                            page.wait_for_timeout(2_500)

                        soup = BeautifulSoup(page.content(), 'lxml')

                        # Extended selector set — covers current and legacy KA DOM
                        items = soup.select(
                            'article.aditem, '
                            'li.ad-listitem article, '
                            '[data-testid="ad-item"], '
                            'article[class*="aditem"], '
                            'li[class*="ad-listitem"]'
                        )

                        found_this_url = 0
                        for item in items:
                            try:
                                title_el = (
                                    item.select_one('a.ellipsis')
                                    or item.select_one('.aditem-main--middle--headline')
                                    or item.select_one('[class*="headline"]')
                                    or item.select_one('h2')
                                    or item.select_one('h3')
                                )
                                price_el = item.select_one(
                                    '.aditem-main--middle--price-shipping--price, '
                                    '[class*="price"]'
                                )
                                link_el = (
                                    item.select_one('a[href*="/s-anzeige/"]')
                                    or item.select_one('a[href*="/anzeige/"]')
                                    or item.select_one('a')
                                )
                                desc_el = item.select_one(
                                    '.aditem-main--middle--description, '
                                    '[class*="description"], p'
                                )
                                img_el = (
                                    item.select_one('.aditem-image img')
                                    or item.select_one('[class*="image"] img')
                                    or item.select_one('img')
                                )
                                loc_el = item.select_one(
                                    '.aditem-main--top--left, .aditem-locationarea, '
                                    '[class*="location"]'
                                )
                                ship_el = item.select_one(
                                    '.aditem-main--middle--price-shipping--shipping, '
                                    '[class*="shipping"]'
                                )

                                if not title_el or not link_el:
                                    continue

                                title     = title_el.get_text(' ', strip=True)
                                desc      = desc_el.get_text(' ', strip=True)[:300] if desc_el else None
                                if _is_unwanted(title, desc, target.get('keyword')):
                                    continue
                                price_raw = price_el.get_text(strip=True) if price_el else ''
                                price     = _parse_price(price_raw) if any(c.isdigit() for c in price_raw) else None
                                if price is None or price < 100:
                                    continue
                                image_url = _best_image(img_el)
                                location  = loc_el.get_text(' ', strip=True)[:80] if loc_el else None
                                ship_text = ship_el.get_text(' ', strip=True).lower() if ship_el else ''
                                # Tri-state: True (versand möglich), False ("kein versand"), None (unknown).
                                if 'kein' in ship_text and 'versand' in ship_text:
                                    has_ship = False
                                elif 'versand' in ship_text:
                                    has_ship = True
                                else:
                                    has_ship = None
                                pickup    = _detect_pickup_only(title, desc, bool(has_ship))

                                href = link_el.get('href', '')
                                if href.startswith('/'):
                                    href = 'https://www.kleinanzeigen.de' + href
                                if not href.startswith('http'):
                                    continue

                                deals.append({
                                    'title':        title[:255],
                                    'price':        price,
                                    'url':          href,
                                    'website':      'Kleinanzeigen',
                                    'model':        target_name,
                                    'ram':          _extract_ram(title),
                                    'ssd':          _extract_ssd(title),
                                    'image_url':    image_url,
                                    'description':  desc,
                                    'location':     location,
                                    'pickup_only':  pickup,
                                    'shipping_available': (1 if has_ship is True
                                                           else 0 if has_ship is False
                                                           else None),
                                    'listing_type': 'fixed',
                                })
                                found_this_url += 1
                            except Exception as e:
                                logger.debug(f'KA item error: {e}')

                        if found_this_url > 0:
                            logger.info(f'Kleinanzeigen {target_name}: {found_this_url} deals via {url}')
                            break  # Success — no need to try fallback URLs
                        else:
                            # Save debug HTML so we can inspect what went wrong
                            _save_debug_html('Kleinanzeigen', target['keyword'], page.content())
                            logger.warning(f'Kleinanzeigen 0 results for "{slug}" via {url}, trying next URL...')

                    except PWTimeout:
                        logger.warning(f'Kleinanzeigen timeout: {url}')
                    except Exception as e:
                        logger.error(f'Kleinanzeigen page error ({slug}): {e}')

            browser.close()

        _set_site_status('Kleinanzeigen',
                         status='ok' if deals else 'empty',
                         detail=None if deals else 'Playwright OK, all URL formats returned 0 — check debug_html/',
                         count=len(deals), ok=True)
        logger.info(f'Kleinanzeigen: {len(deals)} deals')

    except Exception as e:
        logger.error(f'Kleinanzeigen scraper failed: {e}')
        _set_site_status('Kleinanzeigen', status='error', detail=str(e)[:120],
                         count=0, ok=False)

    return deals


# ── eBay ──────────────────────────────────────────────────────────────────────

_EBAY_AUCTION_RE  = re.compile(r'\b(\d+)\s+Gebote?\b', re.IGNORECASE)
# Remaining time strings on eBay listings come in a few shapes:
#   "Restzeit Noch 2 Std 14 Min"
#   "Endet in 5 T 3 Std"
#   "Noch 38 Min"
_EBAY_REMAIN_RE   = re.compile(
    r'(?:Noch|Endet\s+in)\s+'
    r'(?:(\d+)\s*T(?:age?)?)?\s*'
    r'(?:(\d+)\s*Std)?\s*'
    r'(?:(\d+)\s*Min)?',
    re.IGNORECASE,
)
# Seller row in attribute area looks like "consumer333 100% positiv (487)" or
# "iphone.4.sale 97,4% positiv (270)" — last numeric block is the rating count.
_EBAY_SELLER_RE = re.compile(
    r'^([\w.\-_]{2,40})\s+\d+(?:[.,]\d+)?\s*%\s*positiv\s*\(\d[\d.,]*\)\s*$'
)


def _parse_auction_remaining(attrs_text: str) -> str | None:
    """Convert eBay's remaining-time string to an absolute ISO timestamp.
    eBay search results show end-time ONLY as a relative duration:
      'Noch 2 Std 14 Min' / 'Endet in 5 T 3 Std' / 'Noch 38 Min'
    The absolute date elsewhere in the attribute rows (e.g. '17. Mai 01:01')
    is the LISTING date, not the end date — do not parse it as end-time.
    Returns None if no explicit countdown marker is present."""
    from datetime import timedelta
    m = _EBAY_REMAIN_RE.search(attrs_text)
    if not m:
        return None
    days, hours, mins = (int(g) if g else 0 for g in m.groups())
    if days == 0 and hours == 0 and mins == 0:
        return None
    end = datetime.now() + timedelta(days=days, hours=hours, minutes=mins)
    return end.isoformat(timespec='minutes')


def _extract_ebay_seller(item) -> str | None:
    """Find seller name on a current-DOM eBay search-card.
    Order of attempts:
      1. legacy CSS selectors (still works on some pages)
      2. scan .s-card__attribute-row text for the 'NAME XX% positiv (NN)' pattern
    Returns the username only (no rating suffix), or None."""
    seller_el = item.select_one(
        '.s-card__seller-info, .s-item__seller-info-text, '
        '[class*="seller-info"], [class*="seller"]'
    )
    if seller_el:
        seller_raw = seller_el.get_text(' ', strip=True)
        seller = re.sub(r'\s*\(\d[\d.,]*\).*$', '', seller_raw).strip()
        seller = re.sub(r'\s*\d+[.,]?\d*\s*%\s*positiv.*$', '', seller, flags=re.I).strip()
        if seller and len(seller) <= 80:
            return seller
    for row in item.select('.s-card__attribute-row, .s-item__details'):
        txt = row.get_text(' ', strip=True)
        m = _EBAY_SELLER_RE.match(txt)
        if m:
            return m.group(1)
    return None


def _parse_ebay_page(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    # eBay migrated DOM from `s-item` → `s-card` in late 2025; keep both for resilience.
    for item in soup.select('li.s-card, li.s-item, div.s-item'):
        try:
            title_el = item.select_one('.s-card__title, .s-item__title, [class*="s-card__title"]')
            price_el = item.select_one('.s-card__price, .s-item__price, [class*="s-card__price"]')
            link_el  = item.select_one('a.s-item__link, a[href*="/itm/"], a')
            img_el   = item.select_one('img.s-card__image, .s-item__image img, img')

            if not title_el or not link_el:
                continue

            title = title_el.get_text(' ', strip=True)
            # Strip eBay's "Neues Angebot" badge sometimes prepended to title
            title = re.sub(r'^(Neues Angebot|New Listing)', '', title).strip()
            # eBay injects a placeholder "Shop on eBay" card; skip it
            if title.lower().startswith('shop on ebay') or len(title) < 5:
                continue
            if _is_unwanted(title, None, keyword):
                continue

            href = link_el.get('href', '').split('?')[0]
            # Skip placeholder/redirect URLs eBay uses for the "Shop on eBay" card
            if not href.startswith('http') or '/123456' in href:
                continue
            # Skip non-DE marketplace cards
            if 'ebay.de/itm/' not in href:
                continue

            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue

            # Detect auction vs fixed-price:
            #   "27 Gebote · Restzeit Noch …" → auction
            #   "Sofort-Kaufen" / "oder Preisvorschlag" / no marker → fixed
            attrs_text   = ' '.join(el.get_text(' ', strip=True)
                                    for el in item.select('.s-card__attribute-row'))
            # Fallback: some result variants don't use .s-card__attribute-row;
            # scan the whole item text for the "N Gebote" pattern so we don't
            # mis-classify auctions as fixed-price.
            search_text  = attrs_text or item.get_text(' ', strip=True)
            auction_match = _EBAY_AUCTION_RE.search(search_text)
            listing_type = 'auction' if auction_match else 'fixed'
            bid_count    = int(auction_match.group(1)) if auction_match else None
            ends_at      = _parse_auction_remaining(search_text) if auction_match else None

            seller = _extract_ebay_seller(item)

            deals.append({
                'title':           title[:255],
                'price':           price,
                'url':             href,
                'website':         'eBay',
                'model':           _detect_model(title) or default_model,
                'ram':             _extract_ram(title),
                'ssd':             _extract_ssd(title),
                'image_url':       _best_image(img_el),
                'description':     None,
                'listing_type':    listing_type,
                'seller':          seller,
                'bid_count':       bid_count,
                'auction_ends_at': ends_at,
            })
        except Exception as e:
            logger.debug(f'eBay item error: {e}')
    return deals


def _ebay_item_id(url: str) -> str | None:
    """Extract the 12-digit item ID from any flavour of eBay item URL.
    Examples:
      https://www.ebay.de/itm/397951913994            → '397951913994'
      https://www.ebay.de/itm/Apple-Mac-mini/397951913994 → '397951913994'
      https://www.ebay.de/itm/?item=397951913994      → '397951913994'
    """
    if not url:
        return None
    m = re.search(r'/itm/(?:[^/?#]*-)?(\d{9,15})', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]item=(\d{9,15})', url)
    return m.group(1) if m else None


# German month abbreviations as eBay writes them on the bid-history page.
# `mär` / `mrz` both occur in the wild; both map to 3.
_DE_BID_MONTHS = {
    'jan': 1, 'feb': 2, 'mär': 3, 'mar': 3, 'mrz': 3,
    'apr': 4, 'mai': 5, 'jun': 6, 'juni': 6,
    'jul': 7, 'juli': 7, 'aug': 8,
    'sep': 9, 'sept': 9, 'okt': 10, 'nov': 11, 'dez': 12,
}

_EBAY_BID_TIME_RE = re.compile(
    r'(\d{1,2})\s*\.?\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4}),\s+(\d{1,2}):(\d{2}):(\d{2})'
)


def _parse_ebay_bid_time(s: str) -> str | None:
    """`16 Mai 2026, 1:17:36 MESZ` → `2026-05-16T01:17:36`.
    Returns None on any parsing failure (caller will skip the row).
    Timezone suffix (MESZ/MEZ) is dropped — eBay times for DE listings are
    Berlin local, which matches our user's local clock. Good enough for the chart."""
    if not s:
        return None
    m = _EBAY_BID_TIME_RE.search(s)
    if not m:
        return None
    day, mon_raw, year, hr, mn, sc = m.groups()
    mon = _DE_BID_MONTHS.get(mon_raw.lower()[:4]) or _DE_BID_MONTHS.get(mon_raw.lower()[:3])
    if not mon:
        return None
    try:
        return f'{int(year):04d}-{mon:02d}-{int(day):02d}T{int(hr):02d}:{int(mn):02d}:{int(sc):02d}'
    except ValueError:
        return None


def _parse_ebay_bid_row(bidder_raw: str, amount_raw: str, time_raw: str) -> dict | None:
    amount_m = re.search(r'(?:EUR\s+)?([\d.,]+)', amount_raw or '', re.IGNORECASE)
    if not amount_m:
        return None
    price = _parse_price(amount_m.group(1))
    if price is None or price < 0.01:
        return None
    ts = _parse_ebay_bid_time(time_raw or '')
    if not ts:
        return None
    bidder_m = re.match(r'^([^\s]+)', bidder_raw or '')
    bidder = bidder_m.group(1) if bidder_m else None
    return {'price': price, 'changed_at': ts, 'bidder': bidder}


def _sort_and_dedupe_bids(bids: list[dict]) -> list[dict]:
    seen: set = set()
    clean: list[dict] = []
    for bid in bids:
        key = (int(round(bid['price'] * 100)), bid['changed_at'], bid.get('bidder'))
        if key in seen:
            continue
        seen.add(key)
        clean.append(bid)
    clean.sort(key=lambda b: (b['changed_at'], b['price'], b.get('bidder') or ''))
    return clean


def parse_ebay_bid_history(raw: str) -> list[dict]:
    """Parse eBay bid-history HTML or copied table text.

    Handles multiple formats:
      1. Classic HTML <table> with <tr>/<td>
      2. Modern eBay div-based layout (data-testid rows, aria-label cells)
      3. Tab-separated or newline-separated copy-paste text
      4. Single-line "bidder EUR amount date" format
    """
    if not raw:
        return []

    bids: list[dict] = []

    # ── Path A: HTML parsing ──────────────────────────────────────────────
    soup = BeautifulSoup(raw, 'lxml')

    # A1: Classic <table> rows
    for tr in soup.select('table tr'):
        cells = tr.select('td')
        if len(cells) < 3:
            continue
        bid = _parse_ebay_bid_row(
            cells[0].get_text(' ', strip=True),
            cells[1].get_text(' ', strip=True),
            cells[2].get_text(' ', strip=True),
        )
        if bid:
            bids.append(bid)
    if bids:
        return _sort_and_dedupe_bids(bids)

    # A2: Modern div-based rows — eBay sometimes wraps bids in <div> grids.
    # Look for any row-like container whose children hold bidder/price/time.
    for row in soup.select('[data-testid*="bid"], [class*="bid-row"], '
                           '[class*="BidRow"], [class*="bidRow"], '
                           'div[role="row"], li[role="row"]'):
        cells = row.find_all(['div', 'span', 'td'], recursive=False)
        if len(cells) < 3:
            # try one level deeper
            cells = [c for c in row.select('div > span, div > div')
                     if c.get_text(strip=True)]
        if len(cells) >= 3:
            bid = _parse_ebay_bid_row(
                cells[0].get_text(' ', strip=True),
                cells[1].get_text(' ', strip=True),
                cells[2].get_text(' ', strip=True),
            )
            if bid:
                bids.append(bid)
    if bids:
        return _sort_and_dedupe_bids(bids)

    # A3: Fallback — scan ALL text nodes that look like bid data.
    # Find spans/divs containing EUR amounts and walk siblings.
    for el in soup.find_all(string=re.compile(r'EUR\s+[\d.,]+', re.I)):
        parent = el.find_parent(['tr', 'div', 'li', 'section'])
        if not parent:
            continue
        text_parts = [t.strip() for t in parent.get_text('\t', strip=True).split('\t')
                      if t.strip()]
        if len(text_parts) >= 3:
            # Find the EUR part index
            for idx, part in enumerate(text_parts):
                if re.search(r'\bEUR\s+[\d.,]+', part, re.I):
                    bidder = text_parts[idx - 1] if idx > 0 else ''
                    time_raw = text_parts[idx + 1] if idx + 1 < len(text_parts) else ''
                    bid = _parse_ebay_bid_row(bidder, part, time_raw)
                    if bid:
                        bids.append(bid)
                    break
    if bids:
        return _sort_and_dedupe_bids(bids)

    # ── Path B: Plain-text parsing (clipboard copy-paste) ─────────────────
    text = raw

    # B1: Tab/newline separated — split on whitespace delimiters
    lines = [ln.strip() for ln in re.split(r'[\r\n\t]+', text) if ln.strip()]
    for i, line in enumerate(lines):
        if not re.search(r'\bEUR\s+[\d.,]+', line, re.IGNORECASE):
            continue
        bidder = lines[i - 1] if i > 0 else ''
        time_raw = lines[i + 1] if i + 1 < len(lines) else ''
        bid = _parse_ebay_bid_row(bidder, line, time_raw)
        if bid:
            bids.append(bid)
    if bids:
        return _sort_and_dedupe_bids(bids)

    # B2: Combined single-line format — each line has all three fields:
    #   "1***w (225) EUR 351,00 17 Mai 2026, 9:48:46 MESZ"
    _COMBINED_RE = re.compile(
        r'(\S+(?:\s+\(\d+\))?)\s+'           # bidder (optionally with feedback score)
        r'(EUR\s+[\d.,]+)\s+'                 # amount
        r'(\d{1,2}\s+\w+\s+\d{4},\s+\d{1,2}:\d{2}:\d{2}\s*\w*)',  # timestamp
        re.IGNORECASE,
    )
    for line in text.splitlines():
        for m in _COMBINED_RE.finditer(line):
            bid = _parse_ebay_bid_row(m.group(1), m.group(2), m.group(3))
            if bid:
                bids.append(bid)
    if bids:
        return _sort_and_dedupe_bids(bids)

    # B3: Last resort — just find every EUR amount with a nearby timestamp
    _PRICE_RE = re.compile(r'EUR\s+([\d.,]+)', re.I)
    for m in _PRICE_RE.finditer(text):
        # Look for a timestamp within ~80 chars after the price
        after = text[m.end():m.end() + 80]
        bid = _parse_ebay_bid_row('', m.group(0), after)
        if bid:
            bids.append(bid)

    return _sort_and_dedupe_bids(bids)


# Module-level cache: when eBay's anti-bot wall has been seen recently we
# stop hammering /bfl/viewbids/. Resets after _EBAY_BLOCK_COOLDOWN_SEC so
# we'll automatically recover once the IP gets unbanned.
#
# Iter. 27 C10: Zwei getrennte Cooldowns — der unauthenticated 30-min-Block
# darf den authentifizierten Pfad NICHT killen. Wenn ein User in der
# Zwischenzeit per "eBay-Login" einloggt, soll die Login-Session sofort
# probiert werden duerfen (haerterer Akamai-Block setzt eigenen 10-min-TTL).
_EBAY_BLOCKED_UNTIL: float = 0.0          # plain-requests-Pfad
_EBAY_AUTH_BLOCKED_UNTIL: float = 0.0     # PW + login-session-Pfad
_EBAY_BLOCK_COOLDOWN_SEC      = 30 * 60   # 30 min — matches the auction-refresh tick
_EBAY_AUTH_BLOCK_COOLDOWN_SEC = 10 * 60   # 10 min — kuerzer weil Auth-Pfad teurer ist


def _ebay_response_is_blocked(html: str) -> bool:
    """Cheap classifier for eBay/Akamai block pages. Reuses the same marker
    list the PW batch uses so the criteria stay consistent."""
    if not html:
        return False
    lo = html.lower()
    return any(m in lo for m in _BOT_CHALLENGE_MARKERS) or 'splashui' in lo


def _parse_ebay_inline_bids(html: str) -> list[dict]:
    """Iter. 27 C11: Mancne eBay-Item-Pages embedden Bid-Snippets als JSON in
    inline-scripts (raptor.config / viewItemRequestModel / bidHistoryModel).
    Wir versuchen die rauszuziehen wenn /bfl/viewbids/ blockiert ist.

    Erwartetes Format pro Bid (mehrere Varianten beobachtet):
      {"bidder": "x***y", "amount": "23,50 EUR", "time": "2026-05-25T17:35:00"}
      {"bidderUserName": "x***y", "bidAmount": {"value": 23.5}, "bidTime": 173...}
    Wir akzeptieren beides und mappen auf unser Standard-Schema.
    """
    if not html:
        return []
    import json as _json

    bids: list[dict] = []

    # Suche nach JSON-Bloecken die wie Bid-Listen aussehen
    candidates = re.findall(
        r'(?:bidHistory|bidEvents|bids)["\']?\s*:\s*(\[[^\[\]]{0,30000}\])',
        html, re.IGNORECASE | re.DOTALL,
    )
    for raw in candidates:
        try:
            arr = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        if not isinstance(arr, list):
            continue
        for entry in arr:
            if not isinstance(entry, dict):
                continue
            # Amount-Extraktion
            amt = entry.get('bidAmount') or entry.get('amount') or entry.get('price')
            if isinstance(amt, dict):
                amt = amt.get('value') or amt.get('amount')
            price = None
            if amt is not None:
                price = _parse_price(str(amt))
            if price is None or price < 0.01:
                continue
            # Zeit-Extraktion (entweder ISO-String oder Unix-ms)
            tval = entry.get('bidTime') or entry.get('time') or entry.get('timestamp')
            ts_iso: str | None = None
            if isinstance(tval, (int, float)) and tval > 1_000_000_000:
                try:
                    ms = int(tval) if tval > 1e12 else int(tval * 1000)
                    ts_iso = datetime.fromtimestamp(ms / 1000.0).isoformat(timespec='seconds')
                except (ValueError, OSError):
                    pass
            elif isinstance(tval, str):
                ts_iso = _parse_ebay_bid_time(tval) or tval[:19] if 'T' in tval else None
            if not ts_iso:
                continue
            bidder = entry.get('bidderUserName') or entry.get('bidder')
            bids.append({'price': price, 'changed_at': ts_iso, 'bidder': bidder})
        if bids:
            break  # erste passende Bid-Liste reicht

    return _sort_and_dedupe_bids(bids) if bids else []


def scrape_ebay_bid_history(item_url: str) -> list[dict]:
    """Fetch eBay's public bid-history page for an auction and return all bids.

    Strategy:
      1. Try plain requests (fast) — works if the page is server-rendered.
      2. If 0 bids found AND not a known block page, retry with Playwright.
      3. Save debug HTML on failure so we can inspect what eBay returned.

    Has a module-level cooldown: once we see an Akamai/Splash block, we stop
    hitting the endpoint for `_EBAY_BLOCK_COOLDOWN_SEC` seconds. Otherwise
    the auction-refresh thread fires 22 hopeless requests every 30 min.

    Endpoint: https://www.ebay.de/bfl/viewbids/<ITEM_ID>?item=<ITEM_ID>&rt=nc
    Returns sorted-ascending list:
      [{'price': 2.85, 'changed_at': '2026-05-15T18:56:45', 'bidder': '9***d'}, ...]
    Empty list on any failure — caller falls back to its own snapshot history.
    """
    global _EBAY_BLOCKED_UNTIL, _EBAY_AUTH_BLOCKED_UNTIL

    item_id = _ebay_item_id(item_url)
    if not item_id:
        return []

    from urllib.parse import urlparse
    p = urlparse(item_url)
    host  = p.netloc or 'www.ebay.de'
    proto = p.scheme or 'https'
    url = f'{proto}://{host}/bfl/viewbids/{item_id}?item={item_id}&rt=nc'

    html = ''

    # ── Attempt 0a (Iter. 30): Persistent off-screen Chromium ─────────────
    # Highest priority — Diagnose 2026-05-25 zeigte zwar dass /bfl/viewbids/
    # auch via Persistent nur 43kB liefert (vermutlich login-required), aber
    # Versuch ist billig und kostet keinen Cooldown. Wenn's mal greift,
    # nehmen wir die volle History.
    persist_html = fetch_ebay_via_persistent(url)
    if persist_html:
        persist_bids = parse_ebay_bid_history(persist_html)
        if persist_bids:
            logger.info(f'bid-history for {item_id}: {len(persist_bids)} bids parsed (persistent)')
            return persist_bids
        logger.info(f'bid-history for {item_id}: persistent HTML had no parseable bids')

    # ── Attempt 0b (Iter. 29): CDP via App-Chrome ─────────────────────────
    # Fallback fuer den seltenen Fall dass der App-Chrome Master ist.
    cdp_html = fetch_ebay_via_cdp(url)
    if cdp_html:
        cdp_bids = parse_ebay_bid_history(cdp_html)
        if cdp_bids:
            logger.info(f'bid-history for {item_id}: {len(cdp_bids)} bids parsed (CDP)')
            return cdp_bids
        logger.info(f'bid-history for {item_id}: CDP HTML had no parseable bids')

    # ── Iter. 27 C10: Cooldown gate, ausdifferenziert auth/unauth ─────────
    # Skip the requests/PW paths if their cooldowns are active.
    have_login_session = False
    try:
        from ebay_session import has_session as _ebay_has_session
        have_login_session = _ebay_has_session()
    except Exception:
        pass

    now = time.time()
    if have_login_session:
        if now < _EBAY_AUTH_BLOCKED_UNTIL:
            logger.debug(
                'bid-history skipped for %s — eBay AUTH block cooldown active for %.0f more seconds',
                item_id, _EBAY_AUTH_BLOCKED_UNTIL - now,
            )
            return []
    else:
        if now < _EBAY_BLOCKED_UNTIL:
            logger.debug(
                'bid-history skipped for %s — eBay block cooldown active for %.0f more seconds',
                item_id, _EBAY_BLOCKED_UNTIL - now,
            )
            return []

    # ── Attempt 1: plain requests (fast) — skipped when logged in ─────────
    if not have_login_session:
        sess = requests.Session()
        sess.headers.update(_BIG_SHOP_HEADERS)
        try:
            sess.get(f'{proto}://{host}/', timeout=10)
            r = sess.get(url, timeout=15)
            if r.status_code == 200:
                html = r.text
        except Exception as e:
            logger.debug('bid-history requests fetch failed for %s: %s', item_id, e)

        # Detect block FAST so we don't even attempt Playwright when eBay is in
        # full-deny mode (saves ~25 s of wasted browser startup per call).
        if _ebay_response_is_blocked(html):
            _EBAY_BLOCKED_UNTIL = now + _EBAY_BLOCK_COOLDOWN_SEC
            _save_debug_html('bid_history', item_id, html)
            logger.warning(
                'bid-history: eBay block page detected — cooldown enabled for %d min',
                _EBAY_BLOCK_COOLDOWN_SEC // 60,
            )
            return []

        bids = parse_ebay_bid_history(html) if html else []
        if bids:
            logger.info(f'bid-history for {item_id}: {len(bids)} bids parsed (requests)')
            return bids
    else:
        logger.debug('bid-history: skipping requests path, using PW with login session')

    # ── Attempt 2: Playwright (JS-rendered pages) ─────────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug('bid-history: Playwright not available, skipping JS fallback')
        if html:
            _save_debug_html('bid_history', item_id, html)
            logger.info(f'bid-history for {item_id}: 0 bids parsed (requests only, debug saved)')
        return []

    try:
        with sync_playwright() as pw:
            # Iter. 27 C9: PW-Pfad bekommt jetzt die gleichen Stealth-Patches +
            # Header die scrape_anti_bot_batch verwendet. Im Iter. 26 war hier
            # nur naked Playwright + storage_state — Akamai erkannte das
            # leicht (navigator.webdriver=true, kein Sec-Ch-Ua usw.). Mit dem
            # vollen Stealth-Profil haben wir eine echte Chance auch bei
            # /bfl/viewbids/ durchzukommen, besonders zusammen mit der
            # Login-Session.
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            ctx_kwargs = dict(
                user_agent=_BIG_SHOP_HEADERS.get('User-Agent', ''),
                locale='de-DE',
                timezone_id='Europe/Berlin',
                viewport={'width': 1366, 'height': 768},
                screen={'width': 1920, 'height': 1080},
                color_scheme='light',
                extra_http_headers={
                    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"Windows"',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Upgrade-Insecure-Requests': '1',
                },
            )
            # If the user logged in via the "eBay-Login"-button (Iter. 25),
            # inject the saved cookies so /bfl/viewbids/ returns the bid
            # table instead of redirecting to /signin (best-effort — see note).
            try:
                from ebay_session import session_path_for_playwright
                sess_path = session_path_for_playwright()
                if sess_path:
                    ctx_kwargs['storage_state'] = sess_path
                    logger.debug('bid-history: using saved eBay login session')
            except Exception:
                pass
            ctx = browser.new_context(**ctx_kwargs)
            ctx.add_init_script(_PW_STEALTH_JS)
            page = ctx.new_page()
            # Iter. 27 C9: Warmup-GET auf die eBay-Homepage damit Akamai uns
            # erstmal als "echter Browser" sieht bevor wir auf /bfl/viewbids/
            # gehen. Spart manchmal den Splash-Page-Hit.
            try:
                page.goto(f'{proto}://{host}/', wait_until='domcontentloaded', timeout=10_000)
                page.wait_for_timeout(800)
            except Exception:
                pass
            page.goto(url, wait_until='domcontentloaded', timeout=20_000)
            # Wait for table content to render
            try:
                page.wait_for_selector(
                    'table tr td, [data-testid*="bid"], [class*="bid"]',
                    timeout=8_000, state='attached',
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)
            html = page.content()
            ctx.close()
            browser.close()
    except Exception as e:
        logger.warning('bid-history PW fetch failed for %s: %s', item_id, e)

    # Same block-detect on the PW response so the cooldown also engages
    # when eBay shows the splash-UI page (which Playwright happily renders).
    # Iter. 27 C10: Wenn Login-Session aktiv war, nutzen wir den AUTH-Cooldown
    # (kuerzer, 10 min) — andernfalls den langen unauth-Cooldown.
    if _ebay_response_is_blocked(html):
        if have_login_session:
            _EBAY_AUTH_BLOCKED_UNTIL = time.time() + _EBAY_AUTH_BLOCK_COOLDOWN_SEC
            cooldown_min = _EBAY_AUTH_BLOCK_COOLDOWN_SEC // 60
            pfad = 'AUTH'
        else:
            _EBAY_BLOCKED_UNTIL = time.time() + _EBAY_BLOCK_COOLDOWN_SEC
            cooldown_min = _EBAY_BLOCK_COOLDOWN_SEC // 60
            pfad = 'UNAUTH'
        _save_debug_html('bid_history', item_id, html)
        logger.warning(
            'bid-history (PW %s): eBay block page detected — cooldown enabled for %d min',
            pfad, cooldown_min,
        )
        return []

    bids = parse_ebay_bid_history(html) if html else []
    if bids:
        logger.info(f'bid-history for {item_id}: {len(bids)} bids parsed (playwright)')
        return bids

    # Iter. 27 C11: Fallback — Item-Page-eingebettetes JSON. Manche eBay-
    # Item-Detail-Pages haben bidHistory-Arrays in inline-Scripts; nicht so
    # vollstaendig wie /bfl/viewbids/ aber besser als leeres Array wenn
    # Akamai die viewbids-Seite blockt.
    try:
        import requests as _rq
        sess = _rq.Session()
        sess.headers.update(_BIG_SHOP_HEADERS)
        sess.get(f'{proto}://{host}/', timeout=10)
        r2 = sess.get(item_url, timeout=15)
        if r2.status_code == 200 and not _ebay_response_is_blocked(r2.text):
            inline_bids = _parse_ebay_inline_bids(r2.text)
            if inline_bids:
                logger.info(
                    'bid-history for %s: %d bids parsed from item-page inline JSON (C11 fallback)',
                    item_id, len(inline_bids),
                )
                return inline_bids
    except Exception as e:
        logger.debug('bid-history C11 fallback failed for %s: %s', item_id, e)

    if html:
        _save_debug_html('bid_history', item_id, html)
    logger.info(f'bid-history for {item_id}: 0 bids parsed (playwright + C11)')
    return bids


# Iter. 27 B6: Endzeit-Parsing fuer eBay-Item-Page. Suchergebnis liefert nur
# relative Countdown-Strings ("Noch 2h 14m"), die Item-Page hat oft
# (a) data-Attribute (data-tm-itemEndTimeStamp) mit Unix-ms, oder
# (b) Microdata <meta itemprop="availabilityEnds"> mit ISO,
# (c) JavaScript-Variable raptor.config.itemEndTime mit Unix-ms,
# (d) sichtbarer Text "Endet in 2 Std 14 Min" / "Endet am 25. Mai, 17:35".
# Wir versuchen alle Strategien der Reihe nach.
_ITEM_END_ABS_RE = re.compile(
    r'[Ee]ndet\s+(?:am\s+)?(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜ]+)\.?\s*'
    r'(?:(\d{4}),?\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?',
)
_ITEM_END_UNIX_RE = re.compile(
    r'(?:itemEndDate|itemEndTime|endTimeMs|endDate|endTime)\D*?(\d{13})'
)


def _parse_ebay_item_end_time(soup, html: str) -> str | None:
    """Best-effort end-time parsing for an eBay item page. Returns ISO-8601
    (Berlin local, no TZ tag — matches our other timestamps) or None."""
    from datetime import timedelta

    # (a) JavaScript-Variable in inline-script — eBay embeds endTime as ms
    m = _ITEM_END_UNIX_RE.search(html)
    if m:
        try:
            ts_ms = int(m.group(1))
            dt = datetime.fromtimestamp(ts_ms / 1000.0)
            # Sanity: in the future (skip stale listings whose page mentions a
            # past end-time) and within 90 days (skip junk matches like
            # creation timestamps).
            now = datetime.now()
            if dt > now and (dt - now).total_seconds() < 90 * 86400:
                return dt.isoformat(timespec='minutes')
        except (ValueError, OSError):
            pass

    # (b) Microdata <meta itemprop="availabilityEnds" content="...">
    meta = soup.select_one('meta[itemprop="availabilityEnds"], '
                           'meta[itemprop="priceValidUntil"]')
    if meta and meta.get('content'):
        try:
            txt = str(meta['content']).strip().replace('Z', '')
            dt = datetime.fromisoformat(txt)
            if dt > datetime.now():
                return dt.isoformat(timespec='minutes')
        except ValueError:
            pass

    # (c) data-Attribute am Countdown-Element
    el = soup.select_one('[data-tm-itemendtimestamp], [data-end-time], '
                         '[data-endtime], [data-end-time-stamp]')
    if el:
        for attr in ('data-tm-itemendtimestamp', 'data-end-time',
                     'data-endtime', 'data-end-time-stamp'):
            raw = el.get(attr)
            if not raw:
                continue
            try:
                ts_ms = int(raw)
                dt = datetime.fromtimestamp(ts_ms / 1000.0)
                if dt > datetime.now():
                    return dt.isoformat(timespec='minutes')
            except (ValueError, OSError):
                continue

    # (d) Sichtbarer Text — relative ("Endet in 2 Std 14 Min") wieder via
    # _EBAY_REMAIN_RE, absolute ("Endet am 25. Mai 17:35") via lokalem Regex
    text = soup.get_text(' ', strip=True)[:8000]   # cap to avoid huge regex passes

    # relativ
    rem = _parse_auction_remaining(text)
    if rem:
        return rem

    # absolut
    m = _ITEM_END_ABS_RE.search(text)
    if m:
        day, mon_raw, year, hr, mn, sc = m.groups()
        mon = _DE_BID_MONTHS.get(mon_raw.lower()[:4]) or _DE_BID_MONTHS.get(mon_raw.lower()[:3])
        if mon:
            try:
                yr = int(year) if year else datetime.now().year
                dt = datetime(yr, mon, int(day), int(hr), int(mn), int(sc or 0))
                # Year rollover for Dec->Jan boundary listings — only if more
                # than 30 days in the past.
                if (datetime.now() - dt).total_seconds() > 30 * 86400:
                    dt = dt.replace(year=yr + 1)
                if dt > datetime.now():
                    return dt.isoformat(timespec='minutes')
            except ValueError:
                pass

    return None


def parse_ebay_item_page_html(html: str) -> dict:
    """Parse a fully-loaded eBay /itm/ page HTML and return the volatile fields:
    {price, bid_count, listing_type, auction_ends_at, ended}.

    Iter. 29: Extracted from refresh_ebay_item() so the same logic can be reused
    by the Bookmarklet-Sync endpoint (/api/ebay-paste-html) where the HTML is
    submitted directly from the user's browser — bypassing Akamai because the
    browser already holds a valid session cookie.

    Returns dict with ONLY the keys we successfully parsed (no None values).
    Empty dict if nothing parseable.
    """
    if not html:
        return {}

    soup = BeautifulSoup(html, 'lxml')
    out: dict = {}

    # ── Price: multiple selectors, then meta tags
    price = None
    price_el = soup.select_one(
        '[itemprop="price"], .x-price-primary span.ux-textspans, '
        '.x-price-primary span, .x-bin-price__price, .x-price-approx, '
        '[class*="price-primary"]'
    )
    if price_el:
        price = _parse_price(price_el.get_text(' ', strip=True))
    if price is None:
        meta = soup.select_one(
            'meta[itemprop="price"], meta[property="product:price:amount"]'
        )
        if meta and meta.get('content'):
            try:
                price = float(str(meta['content']).replace(',', '.'))
            except ValueError:
                price = None
    if price is not None and price >= 1:
        out['price'] = price

    # ── Bid count
    bid_count = None
    bid_el = soup.select_one(
        '.x-bid-count, [data-testid*="bid-count"], '
        '[class*="bid-count"], [class*="BidCount"], '
        '[class*="bidding__action--bids"], [class*="vi-bidding"] [class*="bids"]'
    )
    bid_text = bid_el.get_text(' ', strip=True) if bid_el else html
    m = re.search(r'(\d+)\s+Gebote?\b', bid_text, re.IGNORECASE)
    if m:
        bid_count = int(m.group(1))
    if bid_count is not None:
        out['bid_count'] = bid_count
        out['listing_type'] = 'auction'

    # ── Endzeit aus Item-Page (absolutes Datum oder relativer Countdown)
    ends_iso = _parse_ebay_item_end_time(soup, html)
    if ends_iso:
        out['auction_ends_at'] = ends_iso

    # ── Auction-Ende-Marker
    body_lo = html.lower()
    ended_markers = (
        'this listing has ended',
        'diese auktion ist beendet',
        'this listing was ended',
        'auktion beendet',
        'item ended',
        'angebot beendet',
        'das angebot wurde beendet',
        'bidding has ended',
        'gebotsabgabe beendet',
        'sold for',
        '"itemavailability":"outofstock"',
        '"itemavailability":"discontinued"',
        '"itemavailability":"soldout"',
        'originalangebot ansehen',
        'wurde vom verkäufer',
        'wurde vom verkaeufer',
        'angebot wurde vom verkäufer',
        'da es einen fehler enthielt',
        '>beendet<',
    )
    if any(m_ in body_lo for m_ in ended_markers):
        out['ended'] = True

    return out


# ─────────────────────────────────────────────────────────────────────────
# Iter. 29: CDP-Fetch über den App-eigenen Chrome (Akamai-Bypass).
# main.py startet Chrome mit --remote-debugging-port=9222. Wenn der erreich-
# bar ist, koennen wir per Playwright connect_over_cdp() einen Hintergrund-
# Tab DARIN aufmachen. Dieser Chrome ist Felix' echter User-Browser mit
# echten eBay-Cookies + echtem Browser-Fingerprint — Akamai laesst ihn durch.
# ─────────────────────────────────────────────────────────────────────────

_CDP_LAST_TRIED_AT: float = 0.0
_CDP_LAST_OK: bool = False
_CDP_RECHECK_SEC: float = 10.0


def _cdp_endpoint() -> str:
    port = os.environ.get('DEALSCRAPER_CDP_PORT', '9222')
    return f'http://127.0.0.1:{port}'


def _cdp_available() -> bool:
    """Cheap probe: GET /json/version on the CDP port.

    Cached for _CDP_RECHECK_SEC to avoid spamming when Chrome isn't running
    with the debug flag (older user opened the app once without restart).
    """
    global _CDP_LAST_TRIED_AT, _CDP_LAST_OK
    now = time.time()
    if now - _CDP_LAST_TRIED_AT < _CDP_RECHECK_SEC:
        return _CDP_LAST_OK
    _CDP_LAST_TRIED_AT = now
    try:
        import requests as _req
        r = _req.get(f'{_cdp_endpoint()}/json/version', timeout=1.5)
        _CDP_LAST_OK = (r.status_code == 200)
    except Exception:
        _CDP_LAST_OK = False
    return _CDP_LAST_OK


# ─────────────────────────────────────────────────────────────────────────
# Iter. 30: Persistent-Context-Fetch (echter Akamai-Bypass).
#
# Iter. 29 CDP via App-Chrome scheiterte live: wenn Felix' Default-Chrome
# offen ist, ignoriert ein zweiter Chrome-Aufruf mit gleichem User-Data-Dir
# den --remote-debugging-port-Flag (Single-Instance-Verhalten). Resultat:
# Port 9222 wurde nie erreichbar → CDP-Pfad inaktiv → alle Refreshes via
# requests → blocked.
#
# Loesung: separate Chromium-Instanz mit eigenem user-data-dir, gestartet
# direkt aus Python via launch_persistent_context. Kollidiert nicht mit
# Felix' Default-Chrome. Off-screen Window-Position macht sie unsichtbar.
# Diagnose 2026-05-25 hat /itm/<id> = 787 KB Vollseite, kein Akamai-Marker
# bestaetigt (siehe play/diagnose_persistent_headless.py).
#
# Headless=True wird von Akamai erkannt → wir nutzen headed mit Off-Screen.
# ─────────────────────────────────────────────────────────────────────────

_PERSIST_LOCK = threading.Lock()
_PERSIST_PW = None     # playwright.sync_api.Playwright instance
_PERSIST_CTX = None    # BrowserContext (launch_persistent_context returnt direkt einen Context)
_PERSIST_LAST_USE = 0.0
_PERSIST_DISABLED = False  # set to True after a hard failure to skip subsequent attempts for a while
_PERSIST_DISABLED_UNTIL = 0.0

# Iter. 37: Sites die wir ueber den persistent off-screen Chromium-Context
# scrapen. Akamai/DataDome/PerimeterX erkennt headless=True zuverlaessig;
# headed off-screen mit eigenem user-data-dir kommt durch. Ein Browser-Pool
# spart 6+ Chromium-Startups pro Scrape — der bestehende _PERSIST_LOCK
# serialisiert alles automatisch.
_PERSIST_SITES = {
    # Akamai-walled (in Iter. 36 reproduzierbar Status='blocked')
    'eBay', 'Saturn', 'Idealo', 'Chrono24', 'Kaufland',
    'Fossil', 'Skagen',
    # DataDome-walled
    'Etsy', 'Zalando', 'AboutYou', 'Asos', 'BestSecret', 'Veepee',
    # Vermutlich bot-walled — wir geben ihnen via Persistent eine zweite Chance.
    'Coolblue', 'Galaxus', 'Computeruniverse', 'Cyberport', 'Gravis',
    'notebooksbilliger', 'Backmarket', 'Breuninger',
    # Uhren/Accessoires (oft CF/DataDome)
    'Christ', 'Chronext', 'Valmano', 'Watchshop',
    'Uhrzeit.org', 'Uhrinstinkt', 'Brandfield', 'Wardow', 'Fashionette',
    'Kapten-Son', 'Liebeskind-Berlin',
    # Beauty (oft DataDome/Cloudflare)
    'Douglas', 'Flaconi', 'Notino', 'Parfumdreams', 'Sephora',
}


def _persistent_profile_path() -> str | None:
    """Pfad zum dedicated Chrome-Profile. None falls nicht initialisiert."""
    p = os.environ.get("DEALSCRAPER_PROFILE_PATH", "")
    if p and os.path.isdir(os.path.dirname(p)):
        return p
    # Fallback: bauen wir selbst (Source-Run ohne main.py-Setup)
    localapp = os.environ.get("LOCALAPPDATA", "")
    if localapp:
        return os.path.join(localapp, "DealScraper", "ScraperProfile")
    return None


def _persistent_available() -> bool:
    """True wenn das Persistent-Profile bereit ist und Playwright importierbar."""
    if _PERSIST_DISABLED and time.time() < _PERSIST_DISABLED_UNTIL:
        return False
    if not _persistent_profile_path():
        return False
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _clear_chrome_window_placement(profile: str):
    """Loescht gespeicherte Fensterposition aus dem Chrome-Profil.
    Verhindert dass Chrome beim Start --window-position ignoriert."""
    import json as _json
    prefs_path = os.path.join(profile, 'Default', 'Preferences')
    if not os.path.exists(prefs_path):
        return
    try:
        with open(prefs_path, 'r', encoding='utf-8') as f:
            prefs = _json.loads(f.read())
        changed = False
        for key in ('window_placement', 'last_known_google_url', 'last_session_exited_cleanly'):
            if key in prefs.get('browser', {}):
                del prefs['browser'][key]
                changed = True
        if changed:
            with open(prefs_path, 'w', encoding='utf-8') as f:
                f.write(_json.dumps(prefs))
    except Exception as e:
        logger.debug("Could not clear window placement: %s", e)


def _ensure_persistent_context():
    """Lazy-startet sync_playwright + launch_persistent_context, cached fuer
    die Lifetime des Prozesses. Caller muss _PERSIST_LOCK bereits halten.

    Iter. 37: Wenn `with sync_playwright()` (Batch-Pfad) im selben Thread
    geschlossen wurde, ist der greenlet-runner tot — _PERSIST_PW kann nicht
    mehr genutzt werden ("cannot switch to a different thread"). Wir
    detektieren das hier und starten frisch.
    """
    global _PERSIST_PW, _PERSIST_CTX
    if _PERSIST_CTX is not None:
        # check ob noch alive — fail-Pattern beim Greenlet-Tod ist eine
        # ThreadException, nicht nur "context closed".
        try:
            _PERSIST_CTX.pages  # raises wenn closed
            return _PERSIST_CTX
        except Exception as e:
            logger.info('Persistent context died (%s), restarting', e)
            _PERSIST_CTX = None
            try:
                if _PERSIST_PW is not None:
                    _PERSIST_PW.stop()
            except Exception:
                pass
            _PERSIST_PW = None

    profile = _persistent_profile_path()
    if not profile:
        return None
    try:
        os.makedirs(os.path.join(profile, "Default", "Network"), exist_ok=True)
    except Exception:
        pass

    from playwright.sync_api import sync_playwright

    # Gespeicherte Fensterposition aus dem Chrome-Profil loeschen, damit
    # --window-position nicht vom Session-Restore ueberschrieben wird.
    _clear_chrome_window_placement(profile)

    _PERSIST_PW = sync_playwright().start()
    chrome_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--no-restore-session-state",
        "--disable-session-crashed-bubble",
        "--window-position=-9999,-9999",
        "--window-size=1,1",
    ]
    # channel='chrome' bevorzugt die echte System-Chrome-Binary. Akamai
    # toleriert sie besser als das gebundelte Chromium-Build. Fallback auf
    # default (bundled) wenn Chrome nicht installiert ist.
    try:
        _PERSIST_CTX = _PERSIST_PW.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=False,
            args=chrome_args,
            channel="chrome",
        )
    except Exception as e:
        logger.info('Persistent context: channel=chrome failed (%s) — fallback to bundled chromium', e)
        try:
            _PERSIST_CTX = _PERSIST_PW.chromium.launch_persistent_context(
                user_data_dir=profile,
                headless=False,
                args=chrome_args,
            )
        except Exception as e2:
            logger.warning('Persistent context launch failed: %s', e2)
            try:
                _PERSIST_PW.stop()
            except Exception:
                pass
            _PERSIST_PW = None
            return None

    logger.info('Persistent context started (profile=%s)', profile)

    # Chrome restores the saved window position from the profile, ignoring
    # --window-position. Force-minimize via CDP so the window stays hidden.
    try:
        _page = _PERSIST_CTX.pages[0] if _PERSIST_CTX.pages else _PERSIST_CTX.new_page()
        _cdp = _PERSIST_CTX.new_cdp_session(_page)
        _win = _cdp.send("Browser.getWindowForTarget")
        _cdp.send("Browser.setWindowBounds", {
            "windowId": _win["windowId"],
            "bounds": {"windowState": "minimized"},
        })
        _cdp.detach()
    except Exception as _e:
        logger.debug("CDP window minimize failed: %s", _e)

    return _PERSIST_CTX


def _shutdown_persistent():
    global _PERSIST_PW, _PERSIST_CTX
    with _PERSIST_LOCK:
        try:
            if _PERSIST_CTX is not None:
                _PERSIST_CTX.close()
        except Exception:
            pass
        _PERSIST_CTX = None
        try:
            if _PERSIST_PW is not None:
                _PERSIST_PW.stop()
        except Exception:
            pass
        _PERSIST_PW = None


atexit.register(_shutdown_persistent)


def fetch_ebay_via_persistent(url: str, timeout_ms: int = 20_000) -> str | None:
    """Holt <url> ueber den persistenten Off-Screen-Chrome. Returnt HTML
    oder None bei Block/Fehler.

    Konkurrenz: _PERSIST_LOCK serialisiert alle Aufrufe — der Background-
    Refresh-Loop und User-Manual-Clicks teilen sich einen Chrome.
    """
    global _PERSIST_LAST_USE, _PERSIST_DISABLED, _PERSIST_DISABLED_UNTIL

    if not _persistent_available():
        return None

    with _PERSIST_LOCK:
        ctx = _ensure_persistent_context()
        if ctx is None:
            # konnte nicht starten — kurz disable
            _PERSIST_DISABLED = True
            _PERSIST_DISABLED_UNTIL = time.time() + 120
            return None

        html = ''
        page = None
        try:
            page = ctx.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            # eBay-Item-Seiten sind 100kB+ vollgeladen; Akamai-Splash ist <40kB.
            try:
                page.wait_for_function(
                    "() => document.body && document.body.innerHTML.length > 40000",
                    timeout=6_000,
                )
            except Exception:
                pass
            html = page.content() or ''
        except Exception as e:
            logger.warning('persistent fetch goto failed for %s: %s', url, e)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            _PERSIST_LAST_USE = time.time()

        if not html:
            return None
        if _ebay_response_is_blocked(html):
            logger.info('persistent fetch: Akamai splash for %s (len=%d)', url, len(html))
            return None
        if len(html) < 5_000:
            logger.info('persistent fetch: HTML too short for %s (len=%d)', url, len(html))
            return None
        return html


# ── Iter. 37: Generische Persistent-Context-Fetch fuer Such-Seiten ───────────
# fetch_ebay_via_persistent ist eBay-spezifisch (40kB Schwelle, hartes
# Akamai-Splash-Detect). Generelle Sites haben unterschiedliche Page-Sizes,
# brauchen Cookie-Banner-Klicks und unterschiedliche Wait-Selektoren.
# Diese Helper macht das fuer alle _PERSIST_SITES.

_COOKIE_ACCEPT_SELECTORS = (
    'button[id*="accept"]',
    'button[id*="Accept"]',
    'button[class*="accept"]',
    'button[class*="consent"]',
    '[data-testid="accept-cookies"]',
    '[data-testid*="cookie"] button',
    '#onetrust-accept-btn-handler',
    '.cmpboxbtn.cmpboxbtnyes',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    '[id*="sp-cc-accept"]',
    '[id*="sp-cc-rejectall"]',
    '#consent-page button[type="submit"]',  # eBay
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Akzeptieren")',
    'button:has-text("Zustimmen")',
    'button:has-text("Annehmen")',
    'button:has-text("Accept")',
    'button:has-text("Alle Cookies akzeptieren")',
)


def fetch_search_via_persistent(
    url: str,
    *,
    wait_selectors: str | None = None,
    settle_ms: int = 2500,
    timeout_ms: int = 25_000,
    min_html_bytes: int = 5_000,
) -> str | None:
    """Generic search-page fetch via persistent off-screen Chromium.
    Returns rendered HTML or None on bot-challenge / too-short / timeout.

    Unlike fetch_ebay_via_persistent this:
      - tries to dismiss a cookie banner (common across DE shops)
      - does progressive scrolling to trigger lazy-loaded cards
      - waits for the provided card selector (if given)
      - auto-restart bei Greenlet-Tod (Iter. 37: passiert nach jedem
        `with sync_playwright()` Block im selben Thread)
    """
    return _fetch_search_via_persistent_inner(
        url, wait_selectors=wait_selectors, settle_ms=settle_ms,
        timeout_ms=timeout_ms, min_html_bytes=min_html_bytes,
        _retry_on_thread_death=True,
    )


def _fetch_search_via_persistent_inner(
    url: str,
    *,
    wait_selectors: str | None,
    settle_ms: int,
    timeout_ms: int,
    min_html_bytes: int,
    _retry_on_thread_death: bool,
) -> str | None:
    global _PERSIST_LAST_USE, _PERSIST_DISABLED, _PERSIST_DISABLED_UNTIL, _PERSIST_PW, _PERSIST_CTX

    if not _persistent_available():
        return None

    with _PERSIST_LOCK:
        try:
            ctx = _ensure_persistent_context()
        except Exception as e:
            msg = str(e).lower()
            if _retry_on_thread_death and ('thread' in msg or 'greenlet' in msg):
                logger.info('Persistent ensure_ctx raised thread-death (%s) — forced shutdown + restart', e)
                _PERSIST_CTX = None
                _PERSIST_PW = None
                try:
                    ctx = _ensure_persistent_context()
                except Exception as e2:
                    logger.warning('Persistent ensure_ctx retry failed: %s', e2)
                    return None
            else:
                raise
        if ctx is None:
            _PERSIST_DISABLED = True
            _PERSIST_DISABLED_UNTIL = time.time() + 120
            return None

        try:
            from playwright.sync_api import TimeoutError as PWTimeout
        except Exception:
            PWTimeout = Exception   # type: ignore

        html = ''
        page = None
        try:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            except Exception as e:
                logger.info('persistent search: goto failed %s: %s', url, e)
                return None

            # Cookie-Banner wegklicken (best-effort, scheitert leise)
            try:
                for sel in _COOKIE_ACCEPT_SELECTORS:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=300):
                            btn.click(timeout=1500)
                            page.wait_for_timeout(400)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # Networkidle fuer dynamische SPA-Renders
            try:
                page.wait_for_load_state('networkidle', timeout=6_000)
            except Exception:
                pass

            # Auf Card-Selektor warten falls angegeben — laenger als im Batch-
            # Pfad weil Sites wie Idealo/Chronext SPA-Renders haben die >10s
            # brauchen bis die ersten Search-Results gemounted sind.
            if wait_selectors:
                try:
                    page.wait_for_selector(wait_selectors, timeout=15_000, state='attached')
                except Exception:
                    pass

            # Lazy-Load triggern
            try:
                for pct in (0.3, 0.6, 0.9, 1.0):
                    page.evaluate(f'window.scrollTo(0, document.body.scrollHeight * {pct})')
                    page.wait_for_timeout(300 + random.randint(0, 200))
            except Exception:
                pass

            # Zweiter Wait nach Scrolling — manche Sites laden mehr Cards via
            # IntersectionObserver bei Scroll.
            if wait_selectors:
                try:
                    page.wait_for_selector(wait_selectors, timeout=3_000, state='attached')
                except Exception:
                    pass

            try:
                page.wait_for_timeout(settle_ms)
            except Exception:
                pass

            try:
                html = page.content() or ''
            except Exception as e:
                logger.info('persistent search: content() failed %s: %s', url, e)
                html = ''
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            _PERSIST_LAST_USE = time.time()

        if not html:
            return None
        lo = html.lower()
        if any(m in lo for m in _BOT_CHALLENGE_MARKERS):
            logger.info('persistent search: bot-challenge for %s (len=%d)', url, len(html))
            return None
        if len(html) < min_html_bytes:
            logger.info('persistent search: HTML too short %s (len=%d)', url, len(html))
            return None
        return html


def fetch_ebay_via_cdp(url: str, timeout_ms: int = 25_000) -> str | None:
    """Open <url> in the running App-Chrome (via CDP), return rendered HTML.

    Returns None if CDP is unavailable, the page didn't load, or eBay's
    Akamai-splash was detected. Caller falls back to other strategies.

    The new tab opens in the same window the user already sees — they'll
    notice a brief flash but the tab auto-closes within ~5-15s.
    """
    if not _cdp_available():
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug('CDP fetch: Playwright not installed')
        return None

    html = ''
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(_cdp_endpoint())
            except Exception as e:
                logger.warning('CDP connect failed: %s', e)
                return None

            # Use the existing default context so Felix' cookies are shared.
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
                # eBay items have a lot of JS — wait for body to have real content.
                # Akamai-splash is small (<40 kB), real page is 100 kB+.
                try:
                    page.wait_for_function(
                        "() => document.body && document.body.innerHTML.length > 40000",
                        timeout=8_000,
                    )
                except Exception:
                    # Page might be short but real (auction ended, etc.) — proceed.
                    pass
                html = page.content() or ''
            except Exception as e:
                logger.warning('CDP goto failed for %s: %s', url, e)
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            # browser.close() on a CDP connection only disconnects the client —
            # it does NOT terminate the underlying Chrome (the App's main window).
            try:
                browser.close()
            except Exception:
                pass

        if _ebay_response_is_blocked(html):
            logger.info('CDP fetch: Akamai splash detected for %s (len=%d)', url, len(html))
            return None
        if len(html) < 5_000:
            logger.info('CDP fetch: HTML too short for %s (len=%d) — likely blocked', url, len(html))
            return None
        return html
    except Exception as e:
        logger.warning('CDP fetch unexpected error for %s: %s', url, e)
        return None


def refresh_ebay_item(url: str) -> dict | None:
    """Fetch a single eBay item page and parse current price + bid count.
    Used for the modal's "Jetzt aktualisieren" button so users can poll
    a live auction faster than the scheduled scrape interval.

    Strategy (Iter. 30):
      1. Persistent off-screen Chromium (verified 2026-05-25 to bypass Akamai
         with a 787kB real page) — primary path.
      2. CDP via App-Chrome (Iter. 29 fallback for the rare case it actually
         works, e.g. when Felix' Default-Chrome is NOT open).
      3. Plain requests — last resort, almost always blocked.
      4. If everything is blocked, return {'blocked': True} so the UI is honest.

    Returns a dict containing ONLY the fields we could actually parse — keys
    are omitted (not set to None) when parsing fails, so the caller can do a
    safe `.update()` without wiping existing values. Returns None only if the
    HTTP request itself failed completely.
    """
    # ── Attempt 1: Persistent off-screen Chromium ────────────────────────────
    persist_html = fetch_ebay_via_persistent(url)
    if persist_html:
        parsed = parse_ebay_item_page_html(persist_html)
        if parsed:
            logger.info('refresh_ebay_item: parsed via persistent for %s (fields=%s)',
                        url, sorted(parsed.keys()))
            return parsed
        logger.info('refresh_ebay_item: persistent HTML had nothing parseable for %s', url)

    # ── Attempt 2: CDP via App-Chrome (Iter. 29 legacy backup) ───────────────
    cdp_html = fetch_ebay_via_cdp(url)
    if cdp_html:
        parsed = parse_ebay_item_page_html(cdp_html)
        if parsed:
            logger.info('refresh_ebay_item: parsed via CDP for %s (fields=%s)',
                        url, sorted(parsed.keys()))
            return parsed
        logger.info('refresh_ebay_item: CDP HTML had nothing parseable for %s', url)

    # ── Attempt 3: plain requests (legacy path) ──────────────────────────────
    import requests
    session = requests.Session()
    session.headers.update(_BIG_SHOP_HEADERS)
    try:
        session.get('https://www.ebay.de/', timeout=10)
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            logger.warning('refresh_ebay_item HTTP %s for %s', r.status_code, url)
            return None
    except Exception as e:
        logger.warning('refresh_ebay_item HTTP fail: %s', e)
        return None

    # Iter. 26: Akamai splash detection
    if _ebay_response_is_blocked(r.text) or len(r.text) < 40_000:
        logger.info('refresh_ebay_item: eBay returned splash/short page for %s (len=%d) — treating as blocked',
                    url, len(r.text))
        return {'blocked': True}

    return parse_ebay_item_page_html(r.text)


def scrape_ebay(targets: list[dict]) -> list[dict]:
    # eBay returns 403 without a prior cookie-grant from the homepage.
    return _generic_scrape(
        'eBay', targets,
        lambda kw: f'https://www.ebay.de/sch/i.html?_nkw={kw.replace(" ", "+")}&_sop=10',
        _parse_ebay_page,
        timeout=15,
        warmup_url='https://www.ebay.de/',
    )


# ── Apple Store (manufacturer reference price) ───────────────────────────────
# Doesn't produce regular deals — writes apple_price into search_targets, so the
# UI can show "−X% vs. Apple" on every card.

# Keyword fragment → Apple product family page path. Longest match wins
# (`iphone 15 pro` before `iphone 15` before `iphone`).
_APPLE_URL_MAP = {
    'macbook air m4':  '/de/shop/buy-mac/macbook-air/13-zoll-m4',
    'macbook pro m4':  '/de/shop/buy-mac/macbook-pro',
    'mac mini m4':     '/de/shop/buy-mac/mac-mini',
    'mac mini':        '/de/shop/buy-mac/mac-mini',
    'macbook air':     '/de/shop/buy-mac/macbook-air',
    'macbook pro':     '/de/shop/buy-mac/macbook-pro',
    'imac':            '/de/shop/buy-mac/imac',
    'mac studio':      '/de/shop/buy-mac/mac-studio',
    'mac pro':         '/de/shop/buy-mac/mac-pro',
    'ipad pro':        '/de/shop/buy-ipad/ipad-pro',
    'ipad air':        '/de/shop/buy-ipad/ipad-air',
    'ipad mini':       '/de/shop/buy-ipad/ipad-mini',
    'ipad':            '/de/shop/buy-ipad/ipad',
    'iphone 17 pro':   '/de/shop/buy-iphone/iphone-17-pro',
    'iphone 17':       '/de/shop/buy-iphone/iphone-17',
    'iphone 16 pro':   '/de/shop/buy-iphone/iphone-16-pro',
    'iphone 16':       '/de/shop/buy-iphone/iphone-16',
    'iphone 15 pro':   '/de/shop/buy-iphone/iphone-15-pro',
    'iphone 15':       '/de/shop/buy-iphone/iphone-15',
    'iphone se':       '/de/shop/buy-iphone/iphone-se',
    'iphone':          '/de/shop/buy-iphone',
    'apple watch ultra':  '/de/shop/buy-watch/apple-watch-ultra',
    'apple watch':     '/de/shop/buy-watch',
    'airpods pro':     '/de/shop/buy-airpods/airpods-pro',
    'airpods max':     '/de/shop/buy-airpods/airpods-max',
    'airpods':         '/de/shop/buy-airpods/airpods',
}

_APPLE_PRICE_RE = re.compile(r'(\d{1,3}(?:\.\d{3})*),(\d{2})\s*€', re.IGNORECASE)


def _resolve_apple_url(keyword: str) -> str | None:
    kw = (keyword or '').lower().strip()
    if not kw:
        return None
    # Sort markers by length descending so 'iphone 15 pro' wins over 'iphone'.
    for marker in sorted(_APPLE_URL_MAP.keys(), key=lambda x: -len(x)):
        if marker in kw:
            return f'https://www.apple.com{_APPLE_URL_MAP[marker]}'
    return None


def _extract_apple_price(html: str) -> float | None:
    """Pick the lowest plausible price from an Apple product page.
    Apple buy-pages list every config; the cheapest is what users see as 'Ab €X'."""
    # First try structured data: <meta property="product:price:amount" content="..."/>
    soup = BeautifulSoup(html, 'lxml')
    for meta in soup.select('meta[property="product:price:amount"], meta[itemprop="price"]'):
        try:
            val = float((meta.get('content') or '').replace(',', '.'))
            if val >= 100:
                return val
        except (ValueError, TypeError):
            pass

    # Then JSON-LD product offers
    import json
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or '{}')
        except (json.JSONDecodeError, TypeError):
            continue
        # Apple sometimes wraps multiple products in a graph
        nodes = data if isinstance(data, list) else [data]
        prices: list[float] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            offers = node.get('offers')
            if isinstance(offers, dict):
                offers = [offers]
            for off in (offers or []):
                if not isinstance(off, dict):
                    continue
                p = off.get('price') or off.get('lowPrice')
                try:
                    prices.append(float(str(p).replace(',', '.')))
                except (ValueError, TypeError):
                    pass
        plausible = [p for p in prices if p >= 100]
        if plausible:
            return min(plausible)

    # Final fallback: regex over the whole HTML — Apple writes "Ab 1.299,00 €"
    candidates: list[float] = []
    for m in _APPLE_PRICE_RE.finditer(html):
        whole, cents = m.group(1).replace('.', ''), m.group(2)
        try:
            val = float(f'{whole}.{cents}')
            if 100 <= val <= 20000:
                candidates.append(val)
        except ValueError:
            pass
    return min(candidates) if candidates else None


def _scrape_apple_via_playwright(url: str) -> str | None:
    """Fetch an Apple product page via Playwright when requests gets a 403.

    Apple's CDN (Akamai) sometimes blocks plain HTTP clients.  Playwright
    with a real browser UA bypasses this for most regions.
    Returns page HTML or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx  = browser.new_context(user_agent=HEADERS['User-Agent'])
            page = ctx.new_page()
            page.goto(url, wait_until='domcontentloaded', timeout=20_000)
            # Wait for at least one price element to be rendered
            try:
                page.wait_for_selector('[class*="price"], .rc-prices-fullprice', timeout=6_000)
            except PWTimeout:
                pass
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        logger.warning(f'Apple Playwright fallback failed for {url}: {e}')
        return None


def scrape_apple(targets: list[dict]) -> list[dict]:
    """Resolve a manufacturer reference price for each target.

    Doesn't return deals — writes `search_targets.apple_price` via the database
    helper so the UI can show '−X% vs. Apple' on every card.

    Strategy:
      1. Try fast requests.get() first.
      2. On 403 (Akamai block) fall back to Playwright with a real browser.
      3. On 404 the product path is stale — set price to None.
    """
    import database as db
    sess = requests.Session()
    sess.headers.update(_BIG_SHOP_HEADERS)
    succeeded = 0
    skipped   = 0
    playwright_used = 0
    for target in targets:
        url = _resolve_apple_url(target.get('keyword') or target.get('name', ''))
        if not url:
            skipped += 1
            db.set_apple_price(target['name'], None)
            continue
        html: str | None = None
        try:
            resp = sess.get(url, timeout=12)
            if resp.status_code == 404:
                db.set_apple_price(target['name'], None)
                continue
            if resp.status_code == 403:
                # Akamai blocked the plain request — try Playwright
                logger.info(f'Apple 403 for {target["name"]}, retrying via Playwright')
                html = _scrape_apple_via_playwright(url)
                playwright_used += 1
            else:
                resp.raise_for_status()
                html = resp.text
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.info(f'Apple 403 for {target["name"]}, retrying via Playwright')
                html = _scrape_apple_via_playwright(url)
                playwright_used += 1
            else:
                logger.warning(f'Apple ({target["name"]}) {url}: {e}')
        except Exception as e:
            logger.warning(f'Apple ({target["name"]}) {url}: {e}')

        if html:
            price = _extract_apple_price(html)
            db.set_apple_price(target['name'], price)
            if price:
                succeeded += 1
            else:
                logger.warning(f'Apple ({target["name"]}): HTML received but no price found')

    detail_parts = [f'{succeeded}/{len(targets)} Preise']
    if playwright_used:
        detail_parts.append(f'Playwright used {playwright_used}x (403 fallback)')
    if succeeded:
        _set_site_status('Apple', status='ok',
                         detail=', '.join(detail_parts), count=succeeded, ok=True)
    elif skipped == len(targets):
        _set_site_status('Apple', status='empty',
                         detail='kein passender Apple-Pfad', count=0, ok=True)
    else:
        _set_site_status('Apple', status='error',
                         detail='keine Preise extrahiert', count=0, ok=False)
    return []   # Apple is reference-only; no deals fed into the pipeline.


# ── mac-store24 (keyword-based search) ───────────────────────────────────────

def _parse_mac_store24_page(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for prod in soup.select(
        '.product-box, .product-item, article.product, '
        '[class*="product-card"], [class*="listing-box"]'
    ):
        try:
            title_el = prod.select_one('.product-name, h2, h3, [class*="title"], [class*="name"]')
            price_el = prod.select_one('.product-price, .price, [class*="price"]')
            link_el  = prod.select_one('a')
            img_el   = prod.select_one('img')

            if not title_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue

            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href  = link_el.get('href', '') if link_el else ''
            if href.startswith('/'):
                href = 'https://www.mac-store24.com' + href
            if not href.startswith('http'):
                continue

            deals.append({
                'title':       title[:255],
                'price':       price,
                'url':         href,
                'website':     'mac-store24',
                'model':       _detect_model(title) or default_model,
                'ram':         _extract_ram(title),
                'ssd':         _extract_ssd(title),
                'image_url':   _best_image(img_el),
                'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'mac-store24 item error: {e}')
    return deals


def _scrape_mac_store24_json(keyword: str, default_model: str):
    '''Shopify Predictive Search API -- returns list of deals or None on failure.

    /search.json liefert auf mac-store24 nur Theme-HTML (Content-Type lügt).
    /search/suggest.json ist die offizielle Shopify-API und liefert echtes JSON.
    Preise kommen als Euro-Strings ("1699.00"), NICHT in Cents.
    '''
    url = (
        'https://www.mac-store24.com/search/suggest.json'
        '?q={}&resources[type]=product&resources[limit]=10'
        .format(keyword.replace(' ', '+'))
    )
    headers = dict(HEADERS)
    headers['Accept'] = 'application/json'
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.debug('mac-store24 JSON API HTTP %s', resp.status_code)
            return None
        body_preview = resp.text[:80].lstrip()
        if not body_preview.startswith('{'):
            logger.warning('mac-store24 suggest.json kein JSON, body=%r', body_preview)
            return None
        data = resp.json()
    except Exception as exc:
        logger.debug('mac-store24 JSON API error: %s', exc)
        return None

    products = (
        data.get('resources', {})
            .get('results', {})
            .get('products', [])
    )
    if not products:
        logger.debug('mac-store24 suggest.json: 0 products for %r', keyword)
        return []

    deals = []
    for prod in products:
        try:
            title = prod.get('title', '')
            if not title or _is_unwanted(title, None, keyword):
                continue
            price_raw = prod.get('price') or prod.get('price_min')
            if price_raw is None:
                continue
            try:
                price = float(str(price_raw).replace(',', '.'))
            except (ValueError, TypeError):
                continue
            if price < 100:
                continue
            handle = prod.get('handle', '')
            if not handle:
                continue
            url_prod = 'https://www.mac-store24.com/products/' + handle
            image_url = None
            feat = prod.get('featured_image') or {}
            if isinstance(feat, dict):
                image_url = feat.get('url')
            if not image_url:
                img = prod.get('image')
                if isinstance(img, str):
                    image_url = img
            deals.append({
                'title':        title[:255],
                'price':        price,
                'url':          url_prod,
                'website':      'mac-store24',
                'model':        _detect_model(title) or default_model,
                'ram':          _extract_ram(title),
                'ssd':          _extract_ssd(title),
                'image_url':    image_url,
                'description':  (prod.get('body') or prod.get('body_html') or '')[:500] or None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as exc:
            logger.debug('mac-store24 JSON item error: %s', exc)
    return deals


def scrape_mac_store24(targets: list[dict]) -> list[dict]:
    '''
    mac-store24 ist ein Shopify-Shop - Suchergebnisse sind JS-gerendert.
    Primaer: Shopify JSON Search API (kein Browser noetig).
    Fallback: HTML-Parser (liefert meist 0 Treffer auf JS-Seiten).
    '''
    all_deals = []
    any_ok = False
    detail_parts = []

    for target in targets:
        keyword       = target.get('keyword', '')
        default_model = target.get('name', 'Mac')

        deals = _scrape_mac_store24_json(keyword, default_model)
        if deals is not None:
            any_ok = True
            all_deals.extend(deals)
            detail_parts.append('JSON API: {} Treffer'.format(len(deals)))
            continue

        # Fallback HTML (bleibt als Sicherheitsnetz)
        html_url = ('https://www.mac-store24.com/search?search='
                    + keyword.replace(' ', '+'))
        try:
            resp = requests.get(html_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            html_deals = _parse_mac_store24_page(resp.text, default_model, keyword)
            any_ok = True
            all_deals.extend(html_deals)
            detail_parts.append('HTML: {} Treffer'.format(len(html_deals)))
        except Exception as exc:
            detail_parts.append('Fehler: {}'.format(exc))

    detail = '; '.join(detail_parts) if detail_parts else 'Keine Targets'
    _set_site_status(
        'mac-store24',
        status='ok' if all_deals else ('ok' if any_ok else 'error'),
        detail=detail,
        count=len(all_deals),
        ok=any_ok,
    )
    return all_deals


# ── asgoodasnew (keyword-based search) ───────────────────────────────────────

def _parse_asgoodasnew_page(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for prod in soup.select(
        '.product-item, .product, article, '
        '[class*="product-box"], [class*="listing-item"], [class*="product-card"]'
    ):
        try:
            title_el = prod.select_one(
                'h2, h3, .product-title, .product-name, [class*="title"], [class*="name"]'
            )
            price_el = prod.select_one('.price, [class*="price"]')
            link_el  = prod.select_one('a')
            img_el   = prod.select_one('img')

            if not title_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue

            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href  = link_el.get('href', '') if link_el else ''
            if href.startswith('/'):
                href = 'https://www.asgoodasnew.de' + href
            if not href.startswith('http'):
                continue

            deals.append({
                'title':       title[:255],
                'price':       price,
                'url':         href,
                'website':     'asgoodasnew',
                'model':       _detect_model(title) or default_model,
                'ram':         _extract_ram(title),
                'ssd':         _extract_ssd(title),
                'image_url':   _best_image(img_el),
                'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'asgoodasnew item error: {e}')
    return deals


def scrape_asgoodasnew(targets: list[dict]) -> list[dict]:
    return _generic_scrape(
        'asgoodasnew', targets,
        lambda kw: f'https://www.asgoodasnew.de/catalogsearch/result/?q={kw.replace(" ", "+")}',
        _parse_asgoodasnew_page,
    )


# ── markt.de (generic German classifieds) ────────────────────────────────────

def _parse_markt_de(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for prod in soup.select(
        'article.classified, .clsy-c-resultlist-item, '
        '[class*="resultlist-item"], [class*="ad-item"]'
    ):
        try:
            title_el = prod.select_one(
                'h2, h3, .clsy-c-resultlist-item__title, [class*="title"]'
            )
            price_el = prod.select_one('[class*="price"]')
            link_el  = prod.select_one('a')
            img_el   = prod.select_one('img')
            loc_el   = prod.select_one('[class*="location"], [class*="city"]')

            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue

            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = link_el.get('href', '')
            if href.startswith('/'):
                href = 'https://www.markt.de' + href
            if not href.startswith('http'):
                continue

            deals.append({
                'title':       title[:255],
                'price':       price,
                'url':         href,
                'website':     'markt.de',
                'model':       _detect_model(title) or default_model,
                'ram':         _extract_ram(title),
                'ssd':         _extract_ssd(title),
                'image_url':   _best_image(img_el),
                'description': None,
                'location':    loc_el.get_text(' ', strip=True)[:80] if loc_el else None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'markt.de item error: {e}')
    return deals


def scrape_markt_de(targets: list[dict]) -> list[dict]:
    return _generic_scrape(
        'markt.de', targets,
        lambda kw: f'https://www.markt.de/suche.htm?query={kw.replace(" ", "+")}',
        _parse_markt_de,
        timeout=15,
    )


# ── quoka.de (generic German classifieds) ────────────────────────────────────

def _parse_quoka(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for prod in soup.select('article, .ad-list-item, [class*="result-item"], [class*="ad-item"]'):
        try:
            title_el = prod.select_one('h2, h3, [class*="title"], [class*="headline"]')
            price_el = prod.select_one('[class*="price"]')
            link_el  = prod.select_one('a')
            img_el   = prod.select_one('img')
            loc_el   = prod.select_one('[class*="location"], [class*="region"]')

            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue

            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = link_el.get('href', '')
            if href.startswith('/'):
                href = 'https://www.quoka.de' + href
            if not href.startswith('http'):
                continue

            deals.append({
                'title':       title[:255],
                'price':       price,
                'url':         href,
                'website':     'quoka',
                'model':       _detect_model(title) or default_model,
                'ram':         _extract_ram(title),
                'ssd':         _extract_ssd(title),
                'image_url':   _best_image(img_el),
                'description': None,
                'location':    loc_el.get_text(' ', strip=True)[:80] if loc_el else None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'quoka item error: {e}')
    return deals


def scrape_quoka(targets: list[dict]) -> list[dict]:
    # Quoka migrated from the slug-style /qpc/k,c0/q,… URL (now 404) to a simple
    # /anzeigen?q= query — that's what their on-site search form posts to.
    return _generic_scrape(
        'quoka', targets,
        lambda kw: f'https://www.quoka.de/anzeigen?q={kw.replace(" ", "+")}',
        _parse_quoka,
        timeout=15,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Additional generic scrapers (best-effort — many sites use anti-bot, may 403)
# ═══════════════════════════════════════════════════════════════════════════

_BIG_SHOP_HEADERS = {
    'User-Agent': HEADERS['User-Agent'],
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Upgrade-Insecure-Requests': '1',
}


def _set_site_status(name: str, *, status: str, detail: str | None, count: int, ok: bool):
    s = STATUS['sites'][name]
    s['ok']     = ok
    s['status'] = status
    s['detail'] = detail
    s['count']  = count
    s['last']   = datetime.now().isoformat()


def _generic_scrape(name: str, targets: list[dict], url_builder, parser,
                    timeout: int = 12, warmup_url: str | None = None) -> list[dict]:
    """Shared boilerplate for HTTP-based shop scrapers with graceful failure.
    `parser(html, default_model, keyword)` filters by keyword to reject related products.

    Status semantics:
      - any HTTP success + any deal      → status='ok'
      - all targets HTTP 4xx/5xx blocked → status='blocked'
      - HTTP success but parser empty    → status='empty'
      - network/timeout on all targets   → status='error'
    """
    deals = []
    http_failures: list[int] = []   # status codes per failed target
    other_failures: list[str] = []  # network/timeout msgs
    http_ok_targets = 0

    try:
        sess = requests.Session()
        sess.headers.update(_BIG_SHOP_HEADERS)
        if warmup_url:
            try:
                # Warm-up grabs first-party cookies (some sites e.g. eBay 403 without them).
                sess.get(warmup_url, timeout=timeout)
            except Exception as e:
                logger.debug(f'{name} warmup failed: {e}')
        for target in targets:
            url = url_builder(target['keyword'])
            try:
                resp = sess.get(url, timeout=timeout)
                resp.raise_for_status()
                http_ok_targets += 1
                deals.extend(parser(resp.text, target['name'], target.get('keyword')))
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                http_failures.append(code)
                logger.warning(f'{name} HTTP {code} for "{target["keyword"]}"')
            except Exception as e:
                other_failures.append(str(e))
                logger.warning(f'{name} ({url}): {e}')

        if deals:
            _set_site_status(name, status='ok', detail=None, count=len(deals), ok=True)
        elif http_ok_targets > 0:
            _set_site_status(name, status='empty',
                             detail=f'fetched {http_ok_targets}/{len(targets)} pages, parser matched 0',
                             count=0, ok=True)
        elif http_failures:
            codes = sorted(set(http_failures))
            label = 'blocked' if any(c in (401, 403, 429, 503) for c in codes) else 'error'
            _set_site_status(name, status=label,
                             detail=f'HTTP {",".join(str(c) for c in codes)} on all {len(targets)} targets',
                             count=0, ok=False)
        else:
            _set_site_status(name, status='error',
                             detail=other_failures[0][:120] if other_failures else 'no requests succeeded',
                             count=0, ok=False)
        logger.info(f'{name}: {len(deals)} deals (status={STATUS["sites"][name]["status"]})')
    except Exception as e:
        logger.error(f'{name} scraper failed: {e}')
        _set_site_status(name, status='error', detail=str(e)[:120], count=0, ok=False)
    return deals


def _abs_url(href: str, base: str) -> str | None:
    if not href:
        return None
    if href.startswith('http'):
        return href
    if href.startswith('//'):
        return 'https:' + href
    if href.startswith('/'):
        return base + href
    return None


# ── Debug: save HTML when parser finds 0 results ─────────────────────────────

_DEBUG_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug_html')


def _save_debug_html(site_name: str, keyword: str, html: str):
    """Save page HTML for debugging when a parser returns 0 results.
    Files are overwritten each run, so disk usage stays constant.

    Iter. 37: cap auf 800 KB hochgesetzt — Persistent-Context-Sites wie
    Cyberport/Idealo liefern 500-3500 KB HTML, mit 200 KB-Cap verloren wir
    die meisten Produkt-Cards die am Seiten-Ende lazy-loaded werden.
    """
    try:
        os.makedirs(_DEBUG_HTML_DIR, exist_ok=True)
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', f'{site_name}_{keyword}')
        path = os.path.join(_DEBUG_HTML_DIR, f'{safe_name}.html')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html[:800_000])
        logger.info(f'Debug HTML saved: {path}')
    except Exception as e:
        logger.debug(f'Could not save debug HTML: {e}')


# ── JSON-LD product extractor (universal fallback) ────────────────────────────

def _extract_jsonld_products(html: str, website: str, default_model: str,
                             keyword: str | None = None) -> list[dict]:
    """Extract products from JSON-LD structured data embedded in the page.
    Many e-commerce sites include <script type="application/ld+json"> blocks
    with product info for SEO — this works even when CSS selectors fail."""
    soup = BeautifulSoup(html, 'lxml')
    deals = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '{}')
        except (json.JSONDecodeError, TypeError):
            continue

        items = []
        if isinstance(data, list):
            items.extend(data)
        elif isinstance(data, dict):
            if '@graph' in data:
                items.extend(data['@graph'] if isinstance(data['@graph'], list) else [data['@graph']])
            else:
                items.append(data)

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get('@type', '')
            # Handle both 'Product' and 'ItemList' containing products
            if item_type == 'ItemList':
                for el in item.get('itemListElement', []):
                    if isinstance(el, dict):
                        prod = el.get('item', el)
                        if isinstance(prod, dict):
                            _try_add_jsonld_product(prod, website, default_model, keyword, deals)
            elif item_type in ('Product', 'IndividualProduct'):
                _try_add_jsonld_product(item, website, default_model, keyword, deals)

    if deals:
        logger.info(f'{website}: JSON-LD fallback found {len(deals)} products')
    return deals


def _try_add_jsonld_product(item: dict, website: str, default_model: str,
                            keyword: str | None, deals: list[dict]):
    """Try to extract a deal from a single JSON-LD Product object."""
    title = item.get('name', '').strip()
    if not title or len(title) < 5:
        return
    if _is_unwanted(title, None, keyword):
        return

    # Extract price from offers
    price = None
    offers = item.get('offers', {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict):
        price_str = str(offers.get('price', offers.get('lowPrice', '')))
        try:
            price = float(price_str.replace(',', '.')) if price_str else None
        except ValueError:
            price = _parse_price(price_str)
    if price is None or price < 100:
        return

    url = item.get('url', offers.get('url', '')) if isinstance(offers, dict) else item.get('url', '')
    if not url:
        return

    img = ''
    image_data = item.get('image', '')
    if isinstance(image_data, list):
        img = image_data[0] if image_data else ''
    elif isinstance(image_data, dict):
        img = image_data.get('url', image_data.get('contentUrl', ''))
    else:
        img = str(image_data)

    deals.append({
        'title':        title[:255],
        'price':        price,
        'url':          url,
        'website':      website,
        'model':        _detect_model(title) or default_model,
        'ram':          _extract_ram(title),
        'ssd':          _extract_ssd(title),
        'image_url':    img or None,
        'description':  item.get('description', '')[:500] if item.get('description') else None,
        'pickup_only':  False,
        'listing_type': 'fixed',
    })


# ── Schema.org microdata fallback (itemprop/itemscope) ────────────────────────

def _extract_microdata_products(soup, website: str, default_model: str,
                                keyword: str | None, base_url: str) -> list[dict]:
    """Extract products from Schema.org microdata attributes (itemprop/itemscope).
    Used as a second fallback when both CSS selectors and JSON-LD fail."""
    deals = []
    # Find elements with itemtype containing "Product"
    product_scopes = soup.select('[itemtype*="schema.org/Product"], [itemtype*="Product"]')
    if not product_scopes:
        return deals

    for scope in product_scopes:
        try:
            name_el = scope.select_one('[itemprop="name"]')
            price_el = scope.select_one('[itemprop="price"]')
            url_el = scope.select_one('[itemprop="url"]') or scope.select_one('a[href]')
            img_el = scope.select_one('[itemprop="image"]') or scope.select_one('img')

            if not name_el:
                continue
            title = (name_el.get('content') or name_el.get_text(' ', strip=True)).strip()
            if not title or len(title) < 5 or _is_unwanted(title, None, keyword):
                continue

            price = None
            if price_el:
                price_str = price_el.get('content') or price_el.get_text(strip=True)
                price = _parse_price(price_str)
            if price is None or price < 100:
                continue

            href = None
            if url_el:
                href = url_el.get('href') or url_el.get('content')
            href = _abs_url(href or '', base_url)
            if not href:
                continue

            img_url = None
            if img_el:
                img_url = img_el.get('src') or img_el.get('content') or img_el.get('data-src')

            deals.append({
                'title':        title[:255],
                'price':        price,
                'url':          href,
                'website':      website,
                'model':        _detect_model(title) or default_model,
                'ram':          _extract_ram(title),
                'ssd':          _extract_ssd(title),
                'image_url':    img_url,
                'description':  None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'{website} microdata item error: {e}')

    if deals:
        logger.info(f'{website}: Schema.org microdata fallback found {len(deals)} products')
    return deals


# ── Amazon.de ────────────────────────────────────────────────────────────────

def _parse_amazon(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for item in soup.select('[data-component-type="s-search-result"]'):
        try:
            title_el = item.select_one('h2 span, h2 a span')
            link_el  = item.select_one('h2 a, a.a-link-normal[href*="/dp/"]')
            price_el = item.select_one('.a-price .a-offscreen, .a-price-whole')
            img_el   = item.select_one('img.s-image, img')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href  = _abs_url(link_el.get('href', ''), 'https://www.amazon.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Amazon', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'amazon item error: {e}')
    return deals


# DEPRECATED: Amazon is handled by scrape_anti_bot_batch().
# Kept for isolated testing only.
def _scrape_amazon_standalone(targets):
    return _generic_scrape(
        'Amazon', targets,
        lambda kw: f'https://www.amazon.de/s?k={kw.replace(" ", "+")}',
        _parse_amazon,
    )


# ── Otto.de ──────────────────────────────────────────────────────────────────

def _parse_otto(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for item in soup.select('[data-test="productListing"] article, .productList__item, article.product, [class*="product-tile"]'):
        try:
            title_el = item.select_one('p.find_tile__name, [class*="productName"], [class*="title"], h3')
            price_el = item.select_one('[class*="price"], strong.price')
            link_el  = item.select_one('a')
            img_el   = item.select_one('img')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = _abs_url(link_el.get('href', ''), 'https://www.otto.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Otto', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'otto item error: {e}')
    if not deals:
        deals = _extract_jsonld_products(html, 'Otto', default_model, keyword)
    return deals


def scrape_otto(targets):
    return _generic_scrape(
        'Otto', targets,
        lambda kw: f'https://www.otto.de/suche/{kw.replace(" ", "%20")}/',
        _parse_otto,
    )


# ── Kaufland.de ──────────────────────────────────────────────────────────────

def _parse_kaufland(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for item in soup.select('article.product, [data-testid="product-card"], [class*="product-card"], [class*="product-tile"]'):
        try:
            title_el = item.select_one('[class*="title"], [class*="name"], h3, h2')
            price_el = item.select_one('[class*="price"]')
            link_el  = item.select_one('a')
            img_el   = item.select_one('img')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = _abs_url(link_el.get('href', ''), 'https://www.kaufland.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Kaufland', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'kaufland item error: {e}')
    if not deals:
        deals = _extract_jsonld_products(html, 'Kaufland', default_model, keyword)
    return deals


# DEPRECATED: Kaufland is handled by scrape_anti_bot_batch().
def _scrape_kaufland_standalone(targets):
    return _generic_scrape(
        'Kaufland', targets,
        lambda kw: f'https://www.kaufland.de/item/search/?search_value={kw.replace(" ", "+")}',
        _parse_kaufland,
    )


# ── Idealo.de (price aggregator) ────────────────────────────────────────────

def _parse_idealo(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    # Idealo uses React; try multiple known selector patterns
    selectors = (
        '[data-testid="resultItem"]',
        '[data-testid="product-card"]',
        'div[class*="resultItem"]',
        'div[class*="ProductCard"]',
        'div[class*="productCard"]',
        '.offerList-item',
        'article[class*="result"]',
        'li[class*="result"]',
    )
    items = []
    for sel in selectors:
        items = soup.select(sel)
        if items:
            logger.debug(f'Idealo: matched selector "{sel}" with {len(items)} items')
            break
    if not items:
        # Broad fallback: any element containing both a link and price-like text
        items = soup.select('article, [role="listitem"], li[class]')

    for item in items:
        try:
            title_el = item.select_one(
                '[class*="title"], [class*="name"], [class*="Title"], [class*="Name"], '
                'h2, h3, a[title], [itemprop="name"], [data-testid*="title"]'
            )
            price_el = item.select_one(
                '[class*="price"], [class*="Price"], [itemprop="price"], '
                '[data-testid*="price"]'
            )
            link_el  = item.select_one('a[href]')
            img_el   = item.select_one('img')
            if not title_el and link_el:
                title_el = link_el
            if not title_el or not link_el:
                continue
            title = (title_el.get('title') or title_el.get_text(' ', strip=True)).strip()
            if not title or len(title) < 5 or _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = _abs_url(link_el.get('href', ''), 'https://www.idealo.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Idealo', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'idealo item error: {e}')

    # JSON-LD fallback
    if not deals:
        deals = _extract_jsonld_products(html, 'Idealo', default_model, keyword)
    return deals


# DEPRECATED: Idealo is handled by scrape_anti_bot_batch().
def _scrape_idealo_standalone(targets):
    return _generic_scrape(
        'Idealo', targets,
        lambda kw: f'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={kw.replace(" ", "+")}',
        _parse_idealo,
    )


# ── Mindfactory.de (PC components) ──────────────────────────────────────────

def _parse_mindfactory(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for item in soup.select('div.pcontent, article.product, [class*="product-box"], [class*="pproduct"]'):
        try:
            title_el = item.select_one('p.pname, .product-name, [class*="title"], h2')
            price_el = item.select_one('span.pprice, [class*="price"]')
            link_el  = item.select_one('a')
            img_el   = item.select_one('img')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = _abs_url(link_el.get('href', ''), 'https://www.mindfactory.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Mindfactory', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'mindfactory item error: {e}')
    if not deals:
        deals = _extract_jsonld_products(html, 'Mindfactory', default_model, keyword)
    return deals


def scrape_mindfactory(targets):
    return _generic_scrape(
        'Mindfactory', targets,
        lambda kw: f'https://www.mindfactory.de/search_result.php?search_query={kw.replace(" ", "+")}',
        _parse_mindfactory,
    )


# ── Alternate.de ────────────────────────────────────────────────────────────

def _parse_alternate(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
    soup  = BeautifulSoup(html, 'lxml')
    deals = []
    for item in soup.select('div.product, article.product, .listingProductBox, [class*="productBox"], [data-product]'):
        try:
            title_el = item.select_one('[class*="productLink"] [class*="name"], .product-name, [class*="title"], h2, h3')
            price_el = item.select_one('[class*="price"]')
            link_el  = item.select_one('a')
            img_el   = item.select_one('img')
            if not title_el or not link_el:
                continue
            title = title_el.get_text(' ', strip=True)
            if _is_unwanted(title, None, keyword):
                continue
            price = _parse_price(price_el.get_text(strip=True)) if price_el else None
            if price is None or price < 100:
                continue
            href = _abs_url(link_el.get('href', ''), 'https://www.alternate.de')
            if not href:
                continue
            deals.append({
                'title': title[:255], 'price': price, 'url': href,
                'website': 'Alternate', 'model': _detect_model(title) or default_model,
                'ram': _extract_ram(title), 'ssd': _extract_ssd(title),
                'image_url': _best_image(img_el), 'description': None,
                'pickup_only':  False,
                'listing_type': 'fixed',
            })
        except Exception as e:
            logger.debug(f'alternate item error: {e}')
    if not deals:
        deals = _extract_jsonld_products(html, 'Alternate', default_model, keyword)
    return deals


def scrape_alternate(targets):
    return _generic_scrape(
        'Alternate', targets,
        lambda kw: f'https://www.alternate.de/listing.xhtml?q={kw.replace(" ", "+")}',
        _parse_alternate,
    )


# ── Generic shop parser (works for many sites with similar structure) ────────

_PRICE_TEXT_RE = re.compile(
    r'(?:EUR|€|\$|CHF)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{1,2})?)'
    r'|([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(?:EUR|€|\$|CHF|,-|,–|,\-)',
    re.IGNORECASE,
)


def _extract_price_from_text(text: str) -> float | None:
    """Iter. 37: Fallback-Price-Extraction aus reinem Text fuer Sites wie
    Galaxus die hash-Klassen ohne 'price' im Namen nutzen, aber EUR/€-Marker
    im Text haben."""
    if not text:
        return None
    m = _PRICE_TEXT_RE.search(text)
    if not m:
        return None
    raw = (m.group(1) or m.group(2) or '').strip()
    if not raw:
        return None
    # German-format: 1.234,56 -> 1234.56
    if ',' in raw and '.' in raw:
        # 1.234,56 (thousands . + decimal ,)
        raw = raw.replace('.', '').replace(',', '.')
    elif ',' in raw:
        # 849,99 or 849, (mit Dash) — wenn nach Komma 0-2 Ziffern -> decimal
        parts = raw.split(',')
        if len(parts[-1]) <= 2:
            raw = parts[0].replace('.', '') + '.' + parts[-1].ljust(2, '0')
        else:
            raw = raw.replace(',', '').replace('.', '')
    else:
        raw = raw.replace('.', '')
    try:
        val = float(raw)
        return val if val >= 1 else None
    except ValueError:
        return None


def _make_generic_parser(website: str, item_selectors: list[str], base_url: str):
    """Build a parser function for a typical product-listing shop.
    Strategy: try CSS selectors first, fall back to JSON-LD structured data."""
    item_sel = ', '.join(item_selectors)

    def parser(html: str, default_model: str, keyword: str | None = None) -> list[dict]:
        soup  = BeautifulSoup(html, 'lxml')
        deals = []
        for item in soup.select(item_sel):
            try:
                # Iter. 36: data-test (ohne 'id') faengt MediaMarkt/Saturn ab;
                # data-cy faengt ReBuy ab.
                title_el = item.select_one(
                    'h2, h3, h4, [class*="title"], [class*="name"], '
                    '[class*="Title"], [class*="Name"], a[title], a[aria-label], '
                    '[data-testid*="title"], [data-testid*="name"], '
                    '[data-test*="title"], [data-test*="name"], '
                    '[data-cy*="title"], [data-cy*="name"], '
                    '[itemprop="name"]'
                )
                price_el = item.select_one(
                    '[class*="price"], [class*="Price"], .price, span.price, '
                    '[data-testid*="price"], [itemprop="price"], '
                    '[data-test*="price"], [data-cy*="price"], '
                    '[class*="cost"], [class*="amount"], '
                    '[class*="Cost"], [class*="Amount"]'
                )
                link_el  = item.select_one('a[href]')
                img_el   = item.select_one('img')

                # Fallback: if no dedicated title element, try the link text
                if not title_el and link_el:
                    title_el = link_el
                if not title_el or not link_el:
                    continue
                title = (title_el.get('aria-label') or title_el.get('title')
                         or title_el.get_text(' ', strip=True)).strip()
                if not title or len(title) < 5 or _is_unwanted(title, None, keyword):
                    continue
                price = _parse_price(price_el.get_text(strip=True)) if price_el else None
                # Iter. 37: Wenn kein price-class, suche EUR/€-Marker im Item-Text
                item_text_full = item.get_text(' ', strip=True)
                if price is None:
                    price = _extract_price_from_text(item_text_full)
                # Iter. 37: filtere Monatsraten — "12 EUR/Monat" ist nicht der Produktpreis
                if price is not None and price < 200:
                    lo = item_text_full.lower()
                    if any(m in lo for m in (
                        '/monat', 'pro monat', '€/mo', 'mtl.', 'monatsrate',
                        'monatlich', '/mo.', '/month', 'per month',
                    )):
                        continue
                if price is None or price < 150:
                    continue
                href = _abs_url(link_el.get('href', ''), base_url)
                if not href:
                    continue
                deals.append({
                    'title':        title[:255],
                    'price':        price,
                    'url':          href,
                    'website':      website,
                    'model':        _detect_model(title) or default_model,
                    'ram':          _extract_ram(title),
                    'ssd':          _extract_ssd(title),
                    'image_url':    _best_image(img_el),
                    'description':  None,
                    'pickup_only':  False,
                    'listing_type': 'fixed',
                })
            except Exception as e:
                logger.debug(f'{website} item error: {e}')

        # ── JSON-LD fallback ──────────────────────────────────────────
        if not deals:
            deals = _extract_jsonld_products(html, website, default_model, keyword)

        # ── Schema.org microdata fallback ─────────────────────────────
        if not deals:
            deals = _extract_microdata_products(soup, website, default_model, keyword, base_url)

        return deals
    return parser


# ── notebooksbilliger.de (DEPRECATED: handled by scrape_anti_bot_batch) ─────
def _scrape_notebooksbilliger_standalone(targets):
    return _generic_scrape(
        'notebooksbilliger', targets,
        lambda kw: f'https://www.notebooksbilliger.de/Search.aspx?q={kw.replace(" ", "+")}',
        _make_generic_parser('notebooksbilliger',
            ['article.product', '.product-tile', '[class*="product-card"]', '[class*="produktbox"]'],
            'https://www.notebooksbilliger.de'),
    )


# ── Cyberport.de (DEPRECATED: handled by scrape_anti_bot_batch) ─────────────
def _scrape_cyberport_standalone(targets):
    return _generic_scrape(
        'Cyberport', targets,
        lambda kw: f'https://www.cyberport.de/suche/?q={kw.replace(" ", "+")}',
        _make_generic_parser('Cyberport',
            ['article.product', '[class*="product-tile"]', '[class*="product-card"]', '[data-product]'],
            'https://www.cyberport.de'),
    )


# ── Gravis.de (Apple Premium Reseller) ──────────────────────────────────────

def scrape_gravis(targets):
    return _generic_scrape(
        'Gravis', targets,
        lambda kw: f'https://www.gravis.de/search?sSearch={kw.replace(" ", "+")}',
        _make_generic_parser('Gravis',
            ['.product--box', '.product-box', 'article.product', '[class*="product-card"]'],
            'https://www.gravis.de'),
    )


# ── Jacob.de (tech retailer) ─────────────────────────────────────────────────

def scrape_jacob(targets):
    return _generic_scrape(
        'Jacob', targets,
        lambda kw: f'https://www.jacob.de/search?q={kw.replace(" ", "+")}',
        _make_generic_parser('Jacob',
            ['article.product', '.product-list-item', '[class*="product"]'],
            'https://www.jacob.de'),
    )


# ── future-x.de (PC components) ─────────────────────────────────────────────

def scrape_future_x(targets):
    return _generic_scrape(
        'future-x', targets,
        lambda kw: f'https://www.future-x.de/search?keyword={kw.replace(" ", "+")}',
        _make_generic_parser('future-x',
            ['article.product', '.product-list-item', '.product', '[class*="product"]'],
            'https://www.future-x.de'),
    )


# ── Conrad.de ────────────────────────────────────────────────────────────────

def scrape_conrad(targets):
    return _generic_scrape(
        'Conrad', targets,
        lambda kw: f'https://www.conrad.de/de/search.html?search={kw.replace(" ", "+")}',
        _make_generic_parser('Conrad',
            ['[data-testid="product"]', 'article.product', '[class*="product-card"]', '[class*="product-tile"]'],
            'https://www.conrad.de'),
    )


# ── Playwright + Stealth: shared browser for anti-bot-protected sites ───────
#
# Idealo / notebooksbilliger / Cyberport are behind Cloudflare. Amazon uses
# Akamai. Kaufland & Backmarket use DataDome. Plain requests gets 403/503 on
# all of these. A headless Chromium with basic stealth patches (mask the
# `webdriver` flag, normalise navigator.plugins/languages, mock the chrome
# runtime, override the Permissions API) defeats most Cloudflare challenges
# and Akamai's lighter checks. DataDome is more aggressive and may still
# block — when it does, we detect the challenge HTML and report status
# 'blocked' honestly instead of pretending to have scraped 0 items.
#
# All 6 sites share ONE browser instance per scrape run (sequential within),
# so we don't pay 6× Chromium startup cost or 6× ~150MB RAM.

_PW_STEALTH_JS = """
() => {
  try {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    // Delete the property from the prototype as well
    delete Navigator.prototype.webdriver;

    // Realistic plugin list
    Object.defineProperty(navigator, 'plugins', {
      get: () => {
        const plugins = [
          { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
          { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
          { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.length = 3;
        plugins.refresh = () => {};
        plugins.item = (i) => plugins[i] || null;
        plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
        return plugins;
      }
    });

    // Languages
    Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });

    // Hardware concurrency (realistic desktop value)
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

    // Device memory
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // Platform
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

    // Max touch points (desktop = 0)
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });

    // Chrome runtime object
    window.chrome = {
      app: { isInstalled: false, InstallState: { INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
      runtime: { OnInstalledReason: { INSTALL: 'install', UPDATE: 'update' }, PlatformOs: { WIN: 'win' }, connect: () => {}, sendMessage: () => {} },
      csi: () => ({}),
      loadTimes: () => ({})
    };

    // Permissions API
    const origQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (origQuery) {
      window.navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : origQuery.call(navigator.permissions, p);
    }

    // WebGL vendor/renderer (realistic NVIDIA values)
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
      if (param === 37445) return 'Google Inc. (NVIDIA)';
      if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)';
      return getParameter.call(this, param);
    };

    // Connection type
    if (navigator.connection) {
      Object.defineProperty(navigator.connection, 'rtt', { get: () => 50 });
    }

    // Disable automation-related features
    if (window.Notification && Notification.permission === 'denied') {
      Object.defineProperty(Notification, 'permission', { get: () => 'default' });
    }
  } catch (e) {}
}
"""

# Substrings that indicate the page is a bot-challenge wall rather than real
# content. Keep these SPECIFIC so we don't false-positive on regular pages
# that happen to mention "captcha" in unrelated content (e.g. a help article).
_BOT_CHALLENGE_MARKERS = (
    'cf-browser-verification',
    'cf_chl_opt',
    '/cdn-cgi/challenge-platform',
    'datadome-captcha',
    'captcha-delivery.com',                # DataDome host
    'geo.captcha-delivery.com',
    'please verify you are a human',
    'unusual traffic from your computer',
    'enter the characters you see below',  # Amazon CAPTCHA
    'api-services-support@amazon',         # Amazon "Sorry!"
    'zur sicherheit unserer kunden',       # Amazon DE
    'press & hold to confirm you are',     # Amazon press-and-hold CAPTCHA
    # ── Akamai / IP-Ban patterns (observed in production, 2026-05) ────────
    # eBay's full deny page: "<title>Access Denied" + reference URL on Akamai
    'errors.edgesuite.net',                # Akamai error tracking URL
    'access denied</h1>',                  # generic Akamai deny body (with close tag to avoid false positives)
    "don't have permission to access",     # Akamai Access Denied body line
    # notebooksbilliger's explicit IP-block page
    'client has been blocked by bot protection',
    '<title>bot detected',
    # Idealo's "Sorry"-page (lighter Akamai variant — generic message page)
    'sorry! something has gone wrong',
    # PerimeterX / HUMAN bot protection
    '_px_ce_',
    'px-captcha',
    # Generic "rate limit" / "too many requests" — last resort
    'rate limit exceeded',
)


def scrape_anti_bot_batch(targets: list[dict]) -> list[dict]:
    """Single Playwright+stealth browser, scrapes all anti-bot-protected sites
    sequentially. Each site re-uses the existing per-site _parse_* function so
    the parsing logic is identical to the (now disabled) plain-requests path.

    Sites that still hit a CAPTCHA or DataDome wall are reported as 'blocked'
    via _set_site_status — we don't silently pretend they returned 0 results.
    """
    # site → (url_builder, parser_fn, settle_ms, wait_selector)
    # wait_selector: comma-separated CSS list — page.wait_for_selector returns
    # as soon as ANY matches. This is the difference between "empty Parser"
    # and real results: most of these sites render product cards via JS
    # AFTER the initial HTML, so domcontentloaded alone isn't enough.
    #
    # ALL e-commerce sites are here now — most render products client-side
    # (React/Vue/Next.js), so plain `requests` only gets empty HTML shells.
    configs = [
        # ── eBay ─────────────────────────────────────────────────────
        ('eBay',
         lambda kw: f'https://www.ebay.de/sch/i.html?_nkw={kw.replace(" ", "+")}&_sop=10',
         _parse_ebay_page, 2000,
         'li.s-card, li.s-item, div.s-item, [class*="s-card"]'),
        # ── Classifieds (may work with requests, but PW is more reliable) ──
        ('markt.de',
         lambda kw: f'https://www.markt.de/suche.htm?query={kw.replace(" ", "+")}',
         _parse_markt_de, 2000,
         'article.classified, [class*="resultlist-item"], [class*="ad-item"], .listing-item'),
        ('quoka',
         lambda kw: f'https://www.quoka.de/anzeigen?q={kw.replace(" ", "+")}',
         _parse_quoka, 2000,
         'article, .ad-list-item, [class*="result-item"], [class*="ad-item"]'),
        # ── Large retailers (all JS-rendered) ────────────────────────
        ('Otto',
         lambda kw: f'https://www.otto.de/suche/{kw.replace(" ", "%20")}/',
         _parse_otto, 3000,
         '[data-test="productListing"] article, .productList__item, article.product, [class*="product-tile"], [class*="find_tile"]'),
        ('Mindfactory',
         lambda kw: f'https://www.mindfactory.de/search_result.php?search_query={kw.replace(" ", "+")}',
         _parse_mindfactory, 2000,
         'div.pcontent, article.product, [class*="product-box"], [class*="pproduct"], .pname'),
        ('Alternate',
         lambda kw: f'https://www.alternate.de/listing.xhtml?q={kw.replace(" ", "+")}',
         _parse_alternate, 2000,
         'div.product, article.product, .listingProductBox, [class*="productBox"], [data-product]'),
        ('Gravis',
         lambda kw: f'https://www.gravis.de/search?sSearch={kw.replace(" ", "+")}',
         _make_generic_parser('Gravis',
            ['.product--box', '.product-box', 'article.product', '[class*="product-card"]', '[class*="product-listing"]'],
            'https://www.gravis.de'),
         2000,
         '.product--box, .product-box, article.product, [class*="product-card"], [class*="product-listing"]'),
        ('future-x',
         lambda kw: f'https://www.future-x.de/search?keyword={kw.replace(" ", "+")}',
         _make_generic_parser('future-x',
            ['article.product', '.product-list-item', '.product', '[class*="product-card"]', '[class*="product-box"]'],
            'https://www.future-x.de'),
         2000,
         'article.product, .product-list-item, [class*="product-card"], [class*="product-box"]'),
        ('Conrad',
         lambda kw: f'https://www.conrad.de/de/search.html?search={kw.replace(" ", "+")}',
         _make_generic_parser('Conrad',
            ['[data-testid="product"]', 'article.product', '[class*="product-card"]', '[class*="product-tile"]', '[class*="productCard"]'],
            'https://www.conrad.de'),
         2500,
         '[data-testid="product"], article.product, [class*="product-card"], [class*="product-tile"], [class*="productCard"]'),
        ('Refurbed',
         lambda kw: f'https://www.refurbed.de/search/?query={kw.replace(" ", "+")}',
         _make_generic_parser('Refurbed',
            ['[data-testid="product-card"]', '[class*="ProductCard"]', '[class*="productCard"]', 'article', '[class*="product-tile"]'],
            'https://www.refurbed.de'),
         2500,
         '[data-testid="product-card"], [class*="ProductCard"], [class*="productCard"], [class*="product-tile"]'),
        # ── Anti-bot heavy (Cloudflare, Akamai, DataDome) ────────────
        ('Idealo',
         lambda kw: f'https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={kw.replace(" ", "+")}',
         _parse_idealo, 2500,
         '[data-testid="resultItem"], .offerList-item, .productOffers-listItemOfferShop, article, [class*="resultItem"]'),
        ('notebooksbilliger',
         lambda kw: f'https://www.notebooksbilliger.de/Search.aspx?q={kw.replace(" ", "+")}',
         _make_generic_parser('notebooksbilliger',
            ['article.product', '.product-tile', '[class*="product-card"]', '[class*="produktbox"]', '[class*="product-listing"]'],
            'https://www.notebooksbilliger.de'),
         2500,
         'article.product, .product-tile, [class*="product-card"], [class*="produktbox"], [class*="product-listing"]'),
        ('Cyberport',
         lambda kw: f'https://www.cyberport.de/suche/?q={kw.replace(" ", "+")}',
         _make_generic_parser('Cyberport',
            ['article.product', '[class*="product-tile"]', '[class*="product-card"]', '[data-product]', '[class*="productCard"]'],
            'https://www.cyberport.de'),
         2500,
         'article.product, [class*="product-tile"], [class*="product-card"], [data-product], [class*="productCard"]'),
        ('Amazon',
         lambda kw: f'https://www.amazon.de/s?k={kw.replace(" ", "+")}',
         _parse_amazon, 2500,
         '[data-component-type="s-search-result"]'),
        ('Kaufland',
         lambda kw: f'https://www.kaufland.de/item/search/?search_value={kw.replace(" ", "+")}',
         _parse_kaufland, 2500,
         'article.product, [data-testid="product-card"], [class*="product-card"], [class*="product-tile"]'),
        ('Backmarket',
         lambda kw: f'https://www.backmarket.de/de-de/search?q={kw.replace(" ", "+")}',
         _make_generic_parser('Backmarket',
            ['[data-test="product-card"]', 'article[data-product]', '[class*="ProductCard"]', '[class*="productcard"]'],
            'https://www.backmarket.de'),
         2500,
         '[data-test="product-card"], article[data-product], [class*="ProductCard"], [class*="productcard"]'),
    ]

    # ── Iter. 36: 40 weitere Shops aus websitenliste.md ─────────────
    # Generic Selektor-Set + Wait-Selektor — _make_generic_parser hat eh
    # JSON-LD + Microdata-Fallback wenn die CSS-Selektoren nichts liefern.
    _generic_card_selectors = [
        'article[data-product]', '[data-testid*="product"]', '[data-test*="product"]',
        '[data-test="product-card"]', '[data-product-id]',
        'article.product', '[class*="ProductCard"]', '[class*="productcard"]',
        '[class*="product-card"]', '[class*="product-tile"]',
        '[class*="ProductTile"]', '[class*="productBox"]', '[class*="product-box"]',
        '[class*="product-item"]', '[class*="ProductItem"]',
        '[class*="article-tile"]', '[class*="ArticleTile"]',
    ]
    _generic_wait_sel = ', '.join(_generic_card_selectors)

    # Iter. 36 Stufe B: Site-spezifische Card-Selektoren — aus debug_html
    # extrahiert (siehe project_iter_36_site_tuning memory).
    # Wenn ein Site-Name hier drin steht, nutzen wir DIESE Selektoren
    # statt der generischen Liste — _make_generic_parser pickt damit nur
    # die echten Produkt-Cards (nicht zufaellige Nav-Elemente).
    _site_specific_selectors: dict[str, list[str]] = {
        # MediaMarkt + Saturn: gleiche Ceconomy-DOM-Struktur
        'MediaMarkt':  ['[data-test="mms-product-card"]'],
        'Saturn':      ['[data-test="mms-product-card"]'],
        # Uhrinstinkt + Gravis: Shopware-Standard
        'Uhrinstinkt': ['.product--box', '[class*="product--box"]', '.product-slider--item'],
        # Uhrzeit.org: eigenes .proBox/.proText/.proPreis-Schema
        'Uhrzeit.org': ['.proBox'],
        # ReBuy: Bootstrap-basiert, .ry-card.product Wrapper
        'ReBuy':       ['.ry-card.product', '[data-cy*="product-card"]'],
        # ── Iter. 37 Phase 2: bot-walled Sites via Persistent-Context ───
        # Galaxus: hash-Klassen aber stable <article>-Wrapper. Titel im
        # aria-label des inneren <a>. Preis im hash-class span (EUR-Marker).
        # Generic-Parser nimmt aria-label + _extract_price_from_text.
        'Galaxus':     ['article'],
        # Coolblue: Search-Cards haben oft data-product-uuid (wenn nicht 0 Ergebnisse)
        'Coolblue':    ['[data-product-uuid]', 'article.product', '[class*="product-card"]'],
        # Idealo: SPA, [data-testid="resultItem"] und Varianten
        'Idealo':      ['[data-testid="resultItem"]', '[data-testid="product-card"]',
                        'div[class*="resultItem"]', 'div[class*="ProductCard"]',
                        '.offerList-item', 'article'],
        # Christ: Shopware-aehnlich, .tile-Wrappers
        'Christ':      ['.tile', 'article[class*="tile"]', '[class*="product-tile"]'],
        # Watchshop: WatchShop UK Shopware. data-fp-tile oder .product
        'Watchshop':   ['[data-fp-tile]', '[data-product]', '.product', 'article'],
        # Chrono24: high-end watch articles
        'Chrono24':    ['.article-item', '[data-article-id]', '[class*="article-info"]',
                        '[class*="ResultListing"]'],
        # Chronext: SPA, brauche grossen Karten-Wrapper
        'Chronext':    ['[data-testid*="product"]', '[class*="ProductCard"]',
                        'article', '[class*="item-tile"]'],
        # Valmano: Shopware
        'Valmano':     ['.product--box', '.product-tile', 'article.product'],
        # Backmarket: React Cards
        'Backmarket':  ['[data-test="product-card"]', 'article[data-product]',
                        '[class*="ProductCard"]'],
        # Cyberport / Gravis / notebooksbilliger: Shopware-Standard
        'Cyberport':         ['article.product', '[class*="product-tile"]',
                              '[class*="product-card"]', '[data-product]'],
        'Gravis':            ['.product--box', '.product-box', 'article.product',
                              '[class*="product-card"]'],
        'notebooksbilliger': ['article.product', '.product-tile',
                              '[class*="product-card"]', '[class*="produktbox"]'],
        'Computeruniverse':  ['article.product', '[class*="product-card"]',
                              '[class*="product-tile"]', '[data-product]'],
        # Kaufland: React, data-testid="product-card"
        'Kaufland':    ['[data-testid="product-card"]', 'article.product',
                        '[class*="product-card"]'],
    }

    # (name, search_url_template, base_url, settle_ms)
    _new_shops = [
        # ── Tech / Elektronik ────────────────────────────────────────
        ('MediaMarkt',       'https://www.mediamarkt.de/de/search.html?query={kw}',                    'https://www.mediamarkt.de',       3000),
        ('Saturn',           'https://www.saturn.de/de/search.html?query={kw}',                        'https://www.saturn.de',           3000),
        ('Galaxus',          'https://www.galaxus.de/de/search?q={kw}',                                'https://www.galaxus.de',          2500),
        ('Coolblue',         'https://www.coolblue.de/zoeken?query={kw}',                              'https://www.coolblue.de',         2500),
        ('Computeruniverse', 'https://www.computeruniverse.net/de/suche?searchtext={kw}',              'https://www.computeruniverse.net', 2500),
        ('Expert',           'https://www.expert.de/shop/search.html?q={kw}',                          'https://www.expert.de',           2500),
        ('Euronics',         'https://www.euronics.de/search?query={kw}',                              'https://www.euronics.de',         2500),
        ('ReBuy',            'https://www.rebuy.de/kaufen/suche?keyword={kw}',                         'https://www.rebuy.de',            2500),
        ('Jacob',            'https://www.jacob.de/suche/?q={kw}',                                     'https://www.jacob.de',            2500),
        # ── Uhren & Accessoires ──────────────────────────────────────
        ('Christ',           'https://www.christ.de/search/?q={kw}',                                   'https://www.christ.de',           2500),
        ('Chrono24',         'https://www.chrono24.de/search/index.htm?query={kw}',                    'https://www.chrono24.de',         3000),
        ('Uhrzeit.org',      'https://www.uhrzeit.org/?query={kw}',                                    'https://www.uhrzeit.org',         2500),
        ('Uhrinstinkt',      'https://www.uhrinstinkt.de/search.html?query={kw}',                      'https://www.uhrinstinkt.de',      2500),
        ('Valmano',          'https://www.valmano.de/search?q={kw}',                                   'https://www.valmano.de',          2500),
        ('Brandfield',       'https://www.brandfield.de/search?q={kw}',                                'https://www.brandfield.de',       2500),
        ('Watchshop',        'https://www.watchshop.com/search.html?q={kw}',                           'https://www.watchshop.com',       2500),
        ('Chronext',         'https://www.chronext.com/de/search?query={kw}',                          'https://www.chronext.com',        2500),
        ('Wardow',           'https://www.wardow.com/search?q={kw}',                                   'https://www.wardow.com',          2500),
        ('Fashionette',      'https://www.fashionette.de/search?q={kw}',                               'https://www.fashionette.de',      2500),
        ('Kapten-Son',       'https://www.kapten-son.com/de/search?q={kw}',                            'https://www.kapten-son.com',      2500),
        ('Fossil',           'https://www.fossil.com/de-de/search/?q={kw}',                            'https://www.fossil.com',          2500),
        ('Skagen',           'https://www.skagen.com/de-de/search/?q={kw}',                            'https://www.skagen.com',          2500),
        ('Liebeskind-Berlin', 'https://www.liebeskind-berlin.com/search?q={kw}',                       'https://www.liebeskind-berlin.com', 2500),
        # ── Parfuem & Beauty ─────────────────────────────────────────
        ('Douglas',          'https://www.douglas.de/de/search.html?q={kw}',                           'https://www.douglas.de',          3000),
        ('Flaconi',          'https://www.flaconi.de/search/?text={kw}',                               'https://www.flaconi.de',          2500),
        ('Notino',           'https://www.notino.de/search/?q={kw}',                                   'https://www.notino.de',           2500),
        ('Parfumdreams',     'https://www.parfumdreams.de/search?q={kw}',                              'https://www.parfumdreams.de',     2500),
        ('Sephora',          'https://www.sephora.de/de/search?q={kw}',                                'https://www.sephora.de',          2500),
        ('Easycosmetic',     'https://www.easycosmetic.de/search?q={kw}',                              'https://www.easycosmetic.de',     2500),
        ('Pieper',           'https://www.parfuemerie-pieper.de/search?q={kw}',                        'https://www.parfuemerie-pieper.de', 2500),
        ('Lookfantastic',    'https://www.lookfantastic.de/elysium.search?search={kw}',                'https://www.lookfantastic.de',    2500),
        ('Beautywelt',       'https://www.beautywelt.de/search?query={kw}',                            'https://www.beautywelt.de',       2500),
        ('Ludwigbeck',       'https://www.ludwigbeck.de/search?q={kw}',                                'https://www.ludwigbeck.de',       2500),
        ('Basler-Beauty',    'https://www.basler-beauty.de/search?q={kw}',                             'https://www.basler-beauty.de',    2500),
        ('Hagel-Shop',       'https://www.hagel-shop.de/search?q={kw}',                                'https://www.hagel-shop.de',       2500),
        ('Shop-Apotheke',    'https://www.shop-apotheke.com/search/?q={kw}',                           'https://www.shop-apotheke.com',   2500),
        ('DocMorris',        'https://www.docmorris.de/search?text={kw}',                              'https://www.docmorris.de',        2500),
        # ── Marktplaetze & Trend-Shops ───────────────────────────────
        ('Zalando',          'https://www.zalando.de/catalog/?q={kw}',                                 'https://www.zalando.de',          2500),
        ('AboutYou',         'https://www.aboutyou.de/suche?term={kw}',                                'https://www.aboutyou.de',         2500),
        ('Asos',             'https://www.asos.com/de/search/?q={kw}',                                 'https://www.asos.com',            2500),
        ('Etsy',             'https://www.etsy.com/de/search?q={kw}',                                  'https://www.etsy.com',            2500),
        ('BestSecret',       'https://www.bestsecret.com/search?text={kw}',                            'https://www.bestsecret.com',      2500),
        ('Veepee',           'https://secure.de.veepee.com/Search/Search.aspx?searchText={kw}',        'https://secure.de.veepee.com',    2500),
        ('Snipes',           'https://www.snipes.com/c/search?q={kw}',                                 'https://www.snipes.com',          2500),
        ('HHV',              'https://www.hhv.com/de/search?keywords={kw}',                            'https://www.hhv.com',             2500),
        ('Breuninger',       'https://www.breuninger.com/de/search/?searchTerm={kw}',                  'https://www.breuninger.com',      2500),
        ('Baur',             'https://www.baur.de/suche/?q={kw}',                                      'https://www.baur.de',             2500),
        ('Lidl',             'https://www.lidl.de/q/query/{kw}',                                       'https://www.lidl.de',             2500),
    ]

    for _name, _tmpl, _base, _settle in _new_shops:
        # Iter. 36 Stufe B: site-spezifische Selektoren wenn vorhanden, sonst generic
        _site_sel = _site_specific_selectors.get(_name, _generic_card_selectors)
        _wait_sel = ', '.join(_site_sel) if _name in _site_specific_selectors else _generic_wait_sel
        # default-arg trick um _tmpl / _base je Iteration einzufrieren
        # (sonst captured Late-Binding ueberschreibt alle Lambdas auf den letzten Wert)
        configs.append((
            _name,
            (lambda kw, t=_tmpl: t.format(kw=kw.replace(' ', '+'))),
            _make_generic_parser(_name, _site_sel, _base),
            _settle,
            _wait_sel,
        ))

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        msg = 'Playwright nicht installiert (pip install playwright && playwright install chromium)'
        logger.error(msg)
        for name, *_ in configs:
            _set_site_status(name, status='error', detail=msg, count=0, ok=False)
        return []

    # Rotate user-agents to reduce fingerprinting
    _USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0',
    ]

    all_deals: list[dict] = []

    # Wiederverwendbarer eligibility-Check (sources-Filter pro Target/Gruppe).
    def _eligible_for(name: str) -> list[dict]:
        return [t for t in targets if not t.get('sources') or name in t['sources']]

    # ── Iter. 37: Persistent-Sites zuerst (eigener sync_playwright-Context) ──
    # WICHTIG: _ensure_persistent_context startet ein eigenes sync_playwright()
    # via _PERSIST_PW. Wenn wir das innerhalb des OUTER `with sync_playwright()`
    # Blocks aufrufen, kollidiert es ("Sync API inside the asyncio loop").
    # Loesung: Persistent-Loop laeuft VOR dem Batch-Browser-Block. Der Persistent-
    # Browser bleibt prozessweit gecached (Iter. 30 Singleton), kein Overhead.
    persist_available_global = _persistent_available()
    for name, url_builder, parser, settle_ms, wait_sel in configs:
        if name not in _PERSIST_SITES:
            continue
        eligible_targets = _eligible_for(name)
        if not eligible_targets:
            _set_site_status(name, status='skipped',
                             detail='Nicht in Gruppen-Quellen', count=0, ok=True)
            continue
        if not persist_available_global:
            # Profile-Path fehlt o.ae. — Site bleibt blocked. Wird unten NICHT
            # mehr vom batch-Browser nachgeholt (steht in _PERSIST_SITES).
            _set_site_status(name, status='blocked',
                             detail='Persistent-Context nicht verfuegbar',
                             count=0, ok=False)
            continue

        site_deals_p: list[dict] = []
        persist_ok = 0
        persist_block = 0
        persist_err: list[str] = []
        for i, target in enumerate(eligible_targets):
            if i > 0:
                time.sleep(random.uniform(2.0, 5.0))
            _set_current(site=name, target=target.get('name'),
                         keyword=target.get('keyword'),
                         group=target.get('group_name'))
            url = url_builder(target['keyword'])
            try:
                html = fetch_search_via_persistent(
                    url, wait_selectors=wait_sel, settle_ms=settle_ms)
            except Exception as e:
                persist_err.append(str(e)[:60])
                logger.warning(f'{name} (persistent, {target["keyword"]}): {e}')
                continue
            if html is None:
                persist_block += 1
                logger.info(f'{name} (persistent, {target["keyword"]}): blocked/empty')
                continue
            persist_ok += 1
            parsed = parser(html, target['name'], target.get('keyword'))
            if not parsed:
                _save_debug_html(name + '_persist', target['keyword'], html)
                logger.info(f'{name} (persistent, {target["keyword"]}): parser 0 — debug HTML saved')
            site_deals_p.extend(parsed)

        if site_deals_p:
            _set_site_status(name, status='ok', detail=None,
                             count=len(site_deals_p), ok=True)
        elif persist_ok > 0:
            _set_site_status(name, status='empty',
                             detail=f'persistent: {persist_ok}/{len(eligible_targets)} Seiten OK, Parser 0',
                             count=0, ok=True)
        elif persist_block:
            _set_site_status(name, status='blocked',
                             detail=f'persistent: {persist_block}/{len(eligible_targets)} Seiten geblockt',
                             count=0, ok=False)
        else:
            _set_site_status(name, status='error',
                             detail=persist_err[0] if persist_err else 'persistent unknown',
                             count=0, ok=False)

        all_deals.extend(site_deals_p)
        logger.info(f'{name} (persistent): {len(site_deals_p)} deals '
                    f'(ok={persist_ok}, blocked={persist_block}, err={len(persist_err)})')
        time.sleep(random.uniform(1.0, 3.0))

    # ── Iter. 37 Bug-Fix: Persistent-Greenlet VOR Batch-sync_playwright killen ──
    # Wenn der Persist-Loop oben gelaufen ist, lebt `_PERSIST_PW` als sync_playwright-
    # Greenlet im selben Thread. Ein zweites `with sync_playwright()` kollidiert
    # damit und wirft "Sync API inside the asyncio loop". Wir killen das persist-
    # Setup VOR dem Batch-Pfad — beim naechsten Group-Scrape wird sauber neu
    # gestartet (5s extra-Overhead pro Group, akzeptabel).
    try:
        _shutdown_persistent_silent()
    except Exception:
        pass

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-infobars',
                    '--window-size=1366,768',
                    '--disable-extensions',
                ],
            )
            for name, url_builder, parser, settle_ms, wait_sel in configs:
                # Iter. 37: Persistent-Sites wurden bereits oben verarbeitet
                if name in _PERSIST_SITES:
                    continue

                # Per-source target filtering: if a target's group has a
                # non-empty sources list, only scrape sources that are in it.
                eligible_targets = _eligible_for(name)
                if not eligible_targets:
                    logger.debug(f'{name}: skipped — no active group allows this source')
                    _set_site_status(name, status='skipped',
                                     detail='Nicht in Gruppen-Quellen', count=0, ok=True)
                    continue

                ua = random.choice(_USER_AGENTS)
                ctx = browser.new_context(
                    user_agent=ua,
                    locale='de-DE',
                    timezone_id='Europe/Berlin',
                    viewport={'width': 1366, 'height': 768},
                    screen={'width': 1920, 'height': 1080},
                    color_scheme='light',
                    extra_http_headers={
                        'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                        'Sec-Ch-Ua-Mobile': '?0',
                        'Sec-Ch-Ua-Platform': '"Windows"',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Upgrade-Insecure-Requests': '1',
                    },
                )
                ctx.add_init_script(_PW_STEALTH_JS)
                page = ctx.new_page()

                site_deals: list[dict] = []
                http_ok = 0
                challenges = 0
                errors: list[str] = []

                def _recreate_page():
                    """Re-create the page after a crash. Some sites (Otto, Quoka)
                    kill the renderer mid-navigation via aggressive anti-bot JS —
                    after which any subsequent call on the dead page raises
                    'Target page... has been closed'. We swap in a fresh page so
                    the rest of the target list can still be attempted."""
                    nonlocal page
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = ctx.new_page()

                for i, target in enumerate(eligible_targets):
                    # Random delay between requests to the same site (2-5s).
                    # Use time.sleep -- page.wait_for_timeout would itself crash
                    # if the previous target killed the page object.
                    if i > 0:
                        time.sleep(random.uniform(2.0, 5.0))

                    # Iter. 36: Live-Progress — UI zeigt "Suche „X" auf <Site> (Gruppe Y)"
                    _set_current(site=name, target=target.get('name'),
                                 keyword=target.get('keyword'),
                                 group=target.get('group_name'))

                    url = url_builder(target['keyword'])
                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=30_000)

                        # Try to wait for network to settle (JS-rendered content)
                        try:
                            page.wait_for_load_state('networkidle', timeout=8_000)
                        except PWTimeout:
                            pass  # some sites never reach networkidle

                        # Dismiss cookie consent banners (common across DE shops)
                        try:
                            for cookie_sel in [
                                'button[id*="accept"], button[id*="Accept"]',
                                'button[class*="accept"], button[class*="consent"]',
                                '[data-testid="accept-cookies"], [data-testid*="cookie"] button',
                                '#onetrust-accept-btn-handler',
                                '.cmpboxbtn.cmpboxbtnyes',
                                '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
                                '[id*="sp-cc-accept"], [id*="sp-cc-rejectall"]',
                                'button:has-text("Alle akzeptieren")',
                                'button:has-text("Akzeptieren")',
                                'button:has-text("Zustimmen")',
                                'button:has-text("Annehmen")',
                                'button:has-text("Accept")',
                                'button:has-text("Alle Cookies akzeptieren")',
                            ]:
                                btn = page.locator(cookie_sel).first
                                if btn.is_visible(timeout=400):
                                    btn.click(timeout=1500)
                                    page.wait_for_timeout(500)
                                    break
                        except Exception:
                            pass   # no cookie banner, or already dismissed

                        # Wait for ANY product selector to render
                        try:
                            page.wait_for_selector(wait_sel, timeout=12_000, state='attached')
                        except PWTimeout:
                            # Fallback: wait for any content that looks like products
                            try:
                                page.wait_for_selector(
                                    'article, [class*="product"], [class*="Product"], '
                                    '[itemtype*="Product"], [data-product]',
                                    timeout=5_000, state='attached')
                            except PWTimeout:
                                pass

                        # Progressive scroll triggers lazy-loaded products
                        try:
                            for scroll_pct in (0.3, 0.5, 0.7, 0.9):
                                page.evaluate(f'window.scrollTo(0, document.body.scrollHeight * {scroll_pct})')
                                page.wait_for_timeout(300 + random.randint(0, 200))
                        except Exception:
                            pass
                        page.wait_for_timeout(settle_ms)
                        html = page.content()
                        lo = html.lower()
                        if any(m in lo for m in _BOT_CHALLENGE_MARKERS):
                            challenges += 1
                            logger.warning(f'{name} ({target["keyword"]}): bot-challenge page detected')
                            _save_debug_html(name, target['keyword'], html)
                            continue
                        http_ok += 1
                        parsed = parser(html, target['name'], target.get('keyword'))
                        if not parsed:
                            # Save full HTML for debugging stale selectors
                            _save_debug_html(name, target['keyword'], html)
                            logger.info(f'{name} ({target["keyword"]}): parser found 0 results — debug HTML saved')
                        site_deals.extend(parsed)
                    except PWTimeout:
                        errors.append('timeout')
                        logger.warning(f'{name} ({target["keyword"]}): timeout')
                        # A timeout shouldn't kill the page, but recreate to be safe
                        # — Playwright sometimes leaves it in a wedged state where
                        # the *next* navigation also times out.
                        _recreate_page()
                    except Exception as e:
                        msg = str(e)
                        errors.append(msg[:60])
                        logger.warning(f'{name} ({target["keyword"]}): {msg}')
                        # If the renderer crashed or the page got closed, every
                        # subsequent target on this site would fail the same way
                        # — recreate so the loop can continue cleanly.
                        if ('crashed' in msg.lower()
                                or 'has been closed' in msg.lower()
                                or 'target page' in msg.lower()):
                            logger.info(f'{name}: recreating page after crash')
                            _recreate_page()

                try:
                    ctx.close()
                except Exception:
                    pass

                if site_deals:
                    _set_site_status(name, status='ok', detail=None,
                                     count=len(site_deals), ok=True)
                elif http_ok > 0:
                    _set_site_status(name, status='empty',
                                     detail=f'{http_ok}/{len(targets)} Seiten OK, Parser fand 0',
                                     count=0, ok=True)
                elif challenges:
                    _set_site_status(name, status='blocked',
                                     detail=f'Bot-Challenge auf {challenges}/{len(targets)} Seiten (Stealth reicht nicht)',
                                     count=0, ok=False)
                else:
                    _set_site_status(name, status='error',
                                     detail=errors[0] if errors else 'unbekannter Fehler',
                                     count=0, ok=False)

                all_deals.extend(site_deals)
                logger.info(f'{name} (PW-stealth): {len(site_deals)} deals '
                            f'(ok={http_ok}, challenges={challenges}, errors={len(errors)})')

                # Random delay between different sites (1-3s).
                # Use time.sleep instead of page.wait_for_timeout -- the context
                # is already closed at this point, so page methods would crash.
                time.sleep(random.uniform(1.0, 3.0))

            browser.close()
    except Exception as e:
        logger.error(f'PW anti-bot batch crashed: {e}')
        for name, *_ in configs:
            if STATUS['sites'].get(name, {}).get('status') is None:
                _set_site_status(name, status='error', detail=str(e)[:100],
                                 count=0, ok=False)

    # ── Iter. 37: Persistent-Context greenlet-recycle ────────────────
    # `with sync_playwright()` Block oben hat den Thread-internen Greenlet-
    # Runner zerstoert. Beim naechsten Group-Scrape wuerde `_PERSIST_CTX`
    # zwar noch existieren, aber jede Methode wuerfe "cannot switch to a
    # different thread (which happens to have exited)". Lazy-Restart funk-
    # tioniert nicht, weil _PERSIST_CTX.pages selbst die Exception wirft
    # — der Cleanup-Pfad triggert dann aber im Aufruf-Stack zu spaet.
    # Loesung: persistent context proaktiv kill nach jedem Batch — beim
    # naechsten fetch_search_via_persistent wird sauber neu gestartet.
    try:
        _shutdown_persistent_silent()
    except Exception:
        pass

    return all_deals


def _shutdown_persistent_silent():
    """Wie _shutdown_persistent aber ohne Lock-Aquire (kann waehrend Lock-
    halter aufgerufen werden) und ohne logging-noise."""
    global _PERSIST_PW, _PERSIST_CTX
    try:
        if _PERSIST_CTX is not None:
            _PERSIST_CTX.close()
    except Exception:
        pass
    _PERSIST_CTX = None
    try:
        if _PERSIST_PW is not None:
            _PERSIST_PW.stop()
    except Exception:
        pass
    _PERSIST_PW = None


# ── Refurbished platforms ─────────────────────────────────────────────────────

# DEPRECATED: Backmarket is handled by scrape_anti_bot_batch().
def _scrape_backmarket_standalone(targets):
    return _generic_scrape(
        'Backmarket', targets,
        lambda kw: f'https://www.backmarket.de/de-de/search?q={kw.replace(" ", "+")}',
        _make_generic_parser('Backmarket',
            ['[data-test="product-card"]', 'article[data-product]', '[class*="ProductCard"]', '[class*="productcard"]'],
            'https://www.backmarket.de'),
    )


def scrape_rebuy(targets):
    return _generic_scrape(
        'Rebuy', targets,
        lambda kw: f'https://www.rebuy.de/kaufen/produkte?searchquery={kw.replace(" ", "+")}',
        _make_generic_parser('Rebuy',
            ['[class*="ProductCard"]', '[class*="product-card"]', '[class*="productcard"]', 'article.product'],
            'https://www.rebuy.de'),
    )


def scrape_refurbed(targets):
    return _generic_scrape(
        'Refurbed', targets,
        lambda kw: f'https://www.refurbed.de/search/?query={kw.replace(" ", "+")}',
        _make_generic_parser('Refurbed',
            ['[data-testid="product-card"]', '[class*="ProductCard"]', '[class*="productCard"]', 'article'],
            'https://www.refurbed.de'),
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────

_DEFAULT_TARGETS = [
    {'name': 'MacBook Air M4', 'keyword': 'macbook air m4'},
    {'name': 'MacBook Pro M4', 'keyword': 'macbook pro m4'},
    {'name': 'Mac mini M4',    'keyword': 'mac mini m4'},
]


def run_scrape(callback=None, targets=None,
               color: str | None = None,
               extra: str | None = None) -> list[dict]:
    STATUS['scraping'] = True
    all_deals: list[dict] = []

    active_targets = targets if targets else _DEFAULT_TARGETS

    def _eligible(source_name: str) -> list[dict]:
        """Targets whose group allows *source_name* (empty sources = all allowed)."""
        return [t for t in active_targets
                if not t.get('sources') or source_name in t['sources']]

    # All e-commerce sites now go through scrape_anti_bot_batch() which uses
    # a real Playwright + stealth browser -- most modern shops render products
    # via JavaScript (React/Vue/Next.js) and return empty HTML to plain requests.
    #
    # Only Kleinanzeigen keeps its own Playwright flow (login-like cookie flow),
    # mac-store24 works with requests (simple SSR page), and Apple is just for
    # reference prices. Status for each site is set inside the batch function.
    #
    # Each standalone scraper gets only the targets whose group allows it.
    # The batch scraper filters internally per source.
    scrapers: dict[str, Callable] = {}

    ka_targets = _eligible('Kleinanzeigen')
    if ka_targets:
        scrapers['Kleinanzeigen'] = lambda: scrape_kleinanzeigen(ka_targets)

    mac_targets = _eligible('mac-store24')
    if mac_targets:
        scrapers['mac-store24'] = lambda: scrape_mac_store24(mac_targets)

    # Apple writes reference prices — run for all targets regardless of group sources
    # (it doesn't return deals, just updates search_targets.apple_price)
    scrapers['Apple'] = lambda: scrape_apple(active_targets)

    # Single Playwright browser covers ALL other sites (filters internally per source).
    scrapers['__PlaywrightBatch'] = lambda: scrape_anti_bot_batch(active_targets)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): name for name, fn in scrapers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                all_deals.extend(future.result())
            except Exception as e:
                logger.error(f'{name} thread error: {e}')

    STATUS['last_scrape'] = datetime.now().isoformat()
    STATUS['scraping']    = False
    _set_current()  # Iter. 36: clear live-progress hint

    # Optional post-collection filters (case-insensitive, applied to title+description)
    if color:
        color_lower = color.lower()
        all_deals = [
            d for d in all_deals
            if color_lower in (d.get('title') or '').lower()
            or color_lower in (d.get('description') or '').lower()
        ]
        logger.info('color filter %r: %d deals remaining', color, len(all_deals))
    if extra:
        extra_lower = extra.lower()
        all_deals = [
            d for d in all_deals
            if extra_lower in (d.get('title') or '').lower()
        ]
        logger.info('extra filter %r: %d deals remaining', extra, len(all_deals))

    if callback:
        try:
            callback(all_deals)
        except Exception as e:
            logger.error(f'Callback error: {e}')

    return all_deals
