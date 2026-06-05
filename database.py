import sqlite3
import os
from datetime import datetime

from paths import resolve_user_file

# deals.db landet im User-Daten-Ordner (%APPDATA%\DealScraper bei frozen,
# Projekt-Wurzel bei dev). Migration aus Legacy-Pfaden passiert beim ersten
# Aufruf von resolve_user_file. Siehe paths.py.
DB_PATH = resolve_user_file('deals.db')


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 60000')
    return conn


def _create_schema(c):
    """Create all tables (idempotent)."""
    c.execute('''CREATE TABLE IF NOT EXISTS deals (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        price       REAL,
        url         TEXT    UNIQUE NOT NULL,
        website     TEXT    NOT NULL,
        found_at    TEXT    NOT NULL,
        ram         TEXT,
        ssd         TEXT,
        model       TEXT,
        image_url   TEXT,
        description TEXT,
        available   INTEGER DEFAULT 1,
        favorite    INTEGER DEFAULT 0,
        last_seen   TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id     INTEGER NOT NULL,
        price       REAL    NOT NULL,
        changed_at  TEXT    NOT NULL,
        bidder      TEXT,
        source      TEXT DEFAULT 'snapshot',
        FOREIGN KEY (deal_id) REFERENCES deals(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        model       TEXT    NOT NULL,
        threshold   REAL    NOT NULL,
        active      INTEGER DEFAULT 1
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS alert_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id     INTEGER,
        deal_id      INTEGER,
        price        REAL,
        triggered_at TEXT    NOT NULL,
        FOREIGN KEY (alert_id) REFERENCES alerts(id),
        FOREIGN KEY (deal_id)  REFERENCES deals(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS search_targets (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        name    TEXT    NOT NULL,
        keyword TEXT    NOT NULL,
        active  INTEGER DEFAULT 1
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS blocked_sellers (
        website TEXT NOT NULL,
        seller  TEXT NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (website, seller)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS group_sources (
        group_name TEXT NOT NULL,
        source     TEXT NOT NULL,
        PRIMARY KEY (group_name, source)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS group_settings (
        group_name TEXT PRIMARY KEY,
        min_price  REAL
    )''')

    # Geocoding cache: maps a location string ("80331 München", "10115 Berlin")
    # to lat/lon. status='ok' = resolved, status='notfound' = Nominatim returned
    # nothing (negative cache, retry after a week).
    c.execute('''CREATE TABLE IF NOT EXISTS geocache (
        query       TEXT PRIMARY KEY,
        lat         REAL,
        lon         REAL,
        status      TEXT NOT NULL DEFAULT 'ok',
        fetched_at  TEXT NOT NULL
    )''')


def _run_migrations(c):
    """Add columns that may be missing in older DBs."""
    # deals table migrations
    for col, typedef in [
        ('image_url',        'TEXT'),
        ('description',      'TEXT'),
        ('location',         'TEXT'),
        ('pickup_only',      'INTEGER DEFAULT 0'),
        ('listing_type',     'TEXT'),
        ('seller',           'TEXT'),
        ('blocked',          'INTEGER DEFAULT 0'),
        ('bid_count',        'INTEGER'),
        ('auction_ends_at',  'TEXT'),
        # PLZ radius filter (Iter. 24): geocoded coords for deals with a location.
        # Backfilled lazily by the background geocode thread in app.py.
        ('lat',              'REAL'),
        ('lon',              'REAL'),
        # "Gekauft" marker (Iter. 25): hides the deal from all panels but
        # keeps it in DB for the future "Was hab ich gekauft"-panel.
        ('purchased',        'INTEGER DEFAULT 0'),
        ('purchased_at',     'TEXT'),
        # Versand-Verfügbarkeit (Iter. 25). NULL = unknown, 0 = no, 1 = yes.
        # Combined with existing pickup_only this gives a tri-state badge:
        # "Nur Abholung", "Versand", "Beides", or nothing if both unknown.
        ('shipping_available', 'INTEGER'),
        # Iter. 27 D13: Zeitstempel des letzten Bid-History-Imports. Wird von
        # replace_price_history() gesetzt und vom Modal als "Eingespielt: vor
        # 12 min" angezeigt damit Felix sieht ob die Chart-Daten noch frisch
        # sind oder ob nach dem Paste schon wieder Gebote gefallen sind.
        ('bid_history_imported_at', 'TEXT'),
        # Iter. 27 D14: Karten-Preis zum Zeitpunkt des letzten Imports. Wenn
        # spaeter ein Scrape einen hoeheren Preis liefert, koennen wir das
        # ehrlich als "veraltet" markieren ohne die echte Karten-Preis-Spalte
        # zu manipulieren.
        ('bid_history_imported_price', 'REAL'),
    ]:
        try:
            c.execute(f'ALTER TABLE deals ADD COLUMN {col} {typedef}')
        except Exception:
            pass

    # Backfill listing_type for legacy rows
    c.execute(
        "UPDATE deals SET listing_type = 'fixed' "
        "WHERE listing_type IS NULL AND website != 'eBay'"
    )

    # price_history migrations
    for col, typedef in [
        ('bidder', 'TEXT'),
        ('source', "TEXT DEFAULT 'snapshot'"),
    ]:
        try:
            c.execute(f'ALTER TABLE price_history ADD COLUMN {col} {typedef}')
        except Exception:
            pass
    c.execute("UPDATE price_history SET source = 'snapshot' WHERE source IS NULL")

    # search_targets migrations
    for col, typedef in [
        ('group_name',   'TEXT'),
        ('retail_price', 'REAL'),
        ('min_price',    'REAL'),
        ('apple_price',  'REAL'),
        ('wish_price',   'REAL'),     # Iter. 26: Felix' Zielpreis - Deals darunter werden in UI hervorgehoben
    ]:
        try:
            c.execute(f'ALTER TABLE search_targets ADD COLUMN {col} {typedef}')
        except Exception:
            pass


def _cleanup_data(c):
    """Remove garbage / accessory / mis-matched deals from older scrapes."""
    import re as _re

    # Remove absurd prices (parser bug artefacts)
    c.execute('UPDATE deals SET price = NULL WHERE price > 100000')

    # Mark garbage listings as unavailable
    garbage_patterns = [
        'defekt', 'kaputt', 'ersatzteil', 'bastler', 'reparatur',
        'wasserschaden', 'displaybruch', 'displayschaden',
        'ankauf', 'gesucht', 'suche ', 'kaufe ', 'tausch',
        'sonstige', 'leergehäuse', 'gestohlen',
    ]
    for pat in garbage_patterns:
        c.execute('UPDATE deals SET available = 0 WHERE LOWER(title) LIKE ?', (f'%{pat}%',))

    # Mark accessory listings as unavailable
    accessory_patterns = [
        'dockingstation', 'docking station', 'schutzfolie', 'schutzglas',
        'displayschutz', 'panzerglas', 'ladekabel', 'netzteil',
        'magsafe kabel', 'magsafe-kabel', 'powerbank', 'usb-c hub', 'usb c hub',
        'tastaturhülle', 'laptophülle', 'laptoptasche', 'laptopsleeve',
        'notebooktasche', 'notebooksleeve', 'handyhülle', 'smartphonehülle',
        'schutzhülle für', 'hülle für', 'tasche für', 'cover für',
        'adapter für', 'halterung für', 'kabel für', 'ständer für',
        # iPhone/Mac case patterns flooding "iphone 17 pro" results
        'magsafe-rückseite', 'magsafe rückseite', 'magsafe-cover', 'magsafe cover',
        'magsafe-case', 'magsafe case',
        'iphone-case', 'smartphone-case',
        'backplate', 'back cover', 'back-cover',
        'hard case', 'silikon case', 'silikon-case', 'silicon case',
        'gel case', 'tpu case', 'leder case', 'leather case',
        'bumper', 'kameralinse', 'kameraschutz', 'linsenschutz',
        'objektivschutz', 'kameraglas',
        'displayfolie',
    ]
    for pat in accessory_patterns:
        c.execute('UPDATE deals SET available = 0 WHERE LOWER(title) LIKE ?', (f'%{pat}%',))

    # Hide deals with no or absurdly low price
    c.execute('UPDATE deals SET available = 0 WHERE price IS NULL OR price < 100')

    # Mark deals whose title doesn't match the target keyword (legacy mis-matches)
    _STOP = {'der', 'die', 'das', 'und', 'mit', 'für', 'fuer', 'in', 'on', 'a', 'and'}
    _GAP  = r'(?:[\s\-,/().\'"]+\w{1,5}){0,2}[\s\-,/().\'"]*'
    targets_for_cleanup = c.execute(
        'SELECT name, keyword FROM search_targets'
    ).fetchall()
    for t_name, t_kw in targets_for_cleanup:
        if not t_kw:
            continue
        words = [w for w in t_kw.lower().split() if len(w) >= 2 and w not in _STOP]
        if not words:
            continue
        parts = [(rf'{_re.escape(w)}\b' if w.isdigit() else _re.escape(w)) for w in words]
        phrase_re = _re.compile(r'\b' + _GAP.join(parts))
        rows = c.execute(
            'SELECT id, title FROM deals WHERE model = ? AND available = 1',
            (t_name,),
        ).fetchall()
        bad_ids = [r_id for r_id, r_title in rows
                   if not phrase_re.search((r_title or '').lower())]
        if bad_ids:
            qs = ','.join('?' for _ in bad_ids)
            c.execute(f'UPDATE deals SET available = 0 WHERE id IN ({qs})', bad_ids)


def _seed_defaults(c):
    """Insert default search targets and settings if tables are empty."""
    if c.execute('SELECT COUNT(*) FROM search_targets').fetchone()[0] == 0:
        c.executemany(
            'INSERT INTO search_targets (name, keyword) VALUES (?, ?)',
            [
                ('MacBook Air M4', 'macbook air m4'),
                ('MacBook Pro M4', 'macbook pro m4'),
                ('Mac mini M4',    'mac mini m4'),
            ]
        )
    # Default settings
    defaults = {
        'scrape_interval_minutes': '240',
    }
    for key, value in defaults.items():
        c.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, value),
        )


# ── Available scraping sources (used for group source picker) ────────────────

ALL_SOURCES: list[dict] = [
    # Marktplätze — für fast alles geeignet
    {'name': 'Kleinanzeigen',      'category': 'Marktplatz',  'note': ''},
    {'name': 'eBay',               'category': 'Marktplatz',  'note': ''},
    {'name': 'markt.de',           'category': 'Marktplatz',  'note': ''},
    {'name': 'quoka',              'category': 'Marktplatz',  'note': ''},
    # Tech-Shops
    {'name': 'mac-store24',        'category': 'Tech',        'note': ''},
    {'name': 'Mindfactory',        'category': 'Tech',        'note': ''},
    {'name': 'Alternate',          'category': 'Tech',        'note': ''},
    {'name': 'Gravis',             'category': 'Tech',        'note': ''},
    {'name': 'future-x',           'category': 'Tech',        'note': ''},
    {'name': 'Conrad',             'category': 'Tech',        'note': ''},
    {'name': 'notebooksbilliger',  'category': 'Tech',        'note': ''},
    {'name': 'Cyberport',          'category': 'Tech',        'note': ''},
    {'name': 'Refurbed',           'category': 'Tech',        'note': 'Refurbished'},
    # Preisvergleich / Große Shops
    {'name': 'Idealo',             'category': 'Preisvergleich', 'note': ''},
    {'name': 'Amazon',             'category': 'Großmarkt',   'note': ''},
    {'name': 'Otto',               'category': 'Großmarkt',   'note': ''},
    # Schwer erreichbar (DataDome)
    {'name': 'Kaufland',           'category': 'Großmarkt',   'note': 'oft geblockt'},
    {'name': 'Backmarket',         'category': 'Tech',        'note': 'Refurbished · oft geblockt'},
    # ── Iter. 36: 48 weitere Shops (matchen scraper.py _new_shops) ───
    # Tech / Elektronik
    {'name': 'MediaMarkt',         'category': 'Tech',        'note': ''},
    {'name': 'Saturn',             'category': 'Tech',        'note': ''},
    {'name': 'Galaxus',            'category': 'Tech',        'note': ''},
    {'name': 'Coolblue',           'category': 'Tech',        'note': ''},
    {'name': 'Computeruniverse',   'category': 'Tech',        'note': ''},
    {'name': 'Expert',             'category': 'Tech',        'note': ''},
    {'name': 'Euronics',           'category': 'Tech',        'note': ''},
    {'name': 'ReBuy',              'category': 'Tech',        'note': 'Refurbished'},
    {'name': 'Jacob',              'category': 'Tech',        'note': ''},
    # Uhren & Accessoires
    {'name': 'Christ',             'category': 'Uhren',       'note': ''},
    {'name': 'Chrono24',           'category': 'Uhren',       'note': ''},
    {'name': 'Uhrzeit.org',        'category': 'Uhren',       'note': ''},
    {'name': 'Uhrinstinkt',        'category': 'Uhren',       'note': ''},
    {'name': 'Valmano',            'category': 'Uhren',       'note': ''},
    {'name': 'Brandfield',         'category': 'Uhren',       'note': ''},
    {'name': 'Watchshop',          'category': 'Uhren',       'note': ''},
    {'name': 'Chronext',           'category': 'Uhren',       'note': ''},
    {'name': 'Wardow',             'category': 'Uhren',       'note': 'Taschen+Uhren'},
    {'name': 'Fashionette',        'category': 'Uhren',       'note': 'Taschen+Uhren'},
    {'name': 'Kapten-Son',         'category': 'Uhren',       'note': ''},
    {'name': 'Fossil',             'category': 'Uhren',       'note': ''},
    {'name': 'Skagen',             'category': 'Uhren',       'note': ''},
    {'name': 'Liebeskind-Berlin',  'category': 'Uhren',       'note': ''},
    # Parfum & Beauty
    {'name': 'Douglas',            'category': 'Beauty',      'note': ''},
    {'name': 'Flaconi',            'category': 'Beauty',      'note': ''},
    {'name': 'Notino',             'category': 'Beauty',      'note': ''},
    {'name': 'Parfumdreams',       'category': 'Beauty',      'note': ''},
    {'name': 'Sephora',            'category': 'Beauty',      'note': ''},
    {'name': 'Easycosmetic',       'category': 'Beauty',      'note': ''},
    {'name': 'Pieper',             'category': 'Beauty',      'note': ''},
    {'name': 'Lookfantastic',      'category': 'Beauty',      'note': ''},
    {'name': 'Beautywelt',         'category': 'Beauty',      'note': ''},
    {'name': 'Ludwigbeck',         'category': 'Beauty',      'note': ''},
    {'name': 'Basler-Beauty',      'category': 'Beauty',      'note': ''},
    {'name': 'Hagel-Shop',         'category': 'Beauty',      'note': ''},
    {'name': 'Shop-Apotheke',      'category': 'Beauty',      'note': ''},
    {'name': 'DocMorris',          'category': 'Beauty',      'note': ''},
    # Marktplätze & Trend-Shops
    {'name': 'Zalando',            'category': 'Fashion',     'note': ''},
    {'name': 'AboutYou',           'category': 'Fashion',     'note': ''},
    {'name': 'Asos',               'category': 'Fashion',     'note': ''},
    {'name': 'Etsy',               'category': 'Marktplatz',  'note': ''},
    {'name': 'BestSecret',         'category': 'Fashion',     'note': 'Login-walled'},
    {'name': 'Veepee',             'category': 'Fashion',     'note': 'Login-walled'},
    {'name': 'Snipes',             'category': 'Fashion',     'note': ''},
    {'name': 'HHV',                'category': 'Fashion',     'note': ''},
    {'name': 'Breuninger',         'category': 'Fashion',     'note': ''},
    {'name': 'Baur',               'category': 'Fashion',     'note': ''},
    {'name': 'Lidl',               'category': 'Großmarkt',   'note': ''},
]

ALL_SOURCE_NAMES: list[str] = [s['name'] for s in ALL_SOURCES]


def get_group_sources(group_name: str) -> list[str]:
    """Return the list of allowed source names for a group.
    An empty list means *all sources* (no restriction set yet)."""
    conn = get_connection()
    rows = conn.execute(
        'SELECT source FROM group_sources WHERE group_name = ? ORDER BY source',
        (group_name,),
    ).fetchall()
    conn.close()
    return [r['source'] for r in rows]


def set_group_sources(group_name: str, sources: list[str]) -> None:
    """Replace the source list for a group atomically.
    Pass an empty list to remove all restrictions (= use all sources)."""
    valid = [s for s in sources if s in ALL_SOURCE_NAMES]
    conn = get_connection()
    conn.execute('DELETE FROM group_sources WHERE group_name = ?', (group_name,))
    if valid:
        conn.executemany(
            'INSERT OR IGNORE INTO group_sources (group_name, source) VALUES (?, ?)',
            [(group_name, s) for s in valid],
        )
    conn.commit()
    conn.close()


def get_group_min_price(group_name: str):
    """Return the configured min_price for a group, or None if not set."""
    conn = get_connection()
    row = conn.execute(
        'SELECT min_price FROM group_settings WHERE group_name = ?',
        (group_name,),
    ).fetchone()
    conn.close()
    return row['min_price'] if row else None


def set_group_min_price(group_name: str, min_price) -> None:
    """Set (or clear) the group-level min_price floor.
    Pass None to remove the group's min_price restriction."""
    conn = get_connection()
    if min_price is None:
        conn.execute('DELETE FROM group_settings WHERE group_name = ?', (group_name,))
    else:
        conn.execute(
            'INSERT INTO group_settings (group_name, min_price) VALUES (?, ?)'
            ' ON CONFLICT(group_name) DO UPDATE SET min_price = excluded.min_price',
            (group_name, float(min_price)),
        )
    conn.commit()
    conn.close()


def get_all_group_settings() -> dict:
    """Return a dict of group_name -> { min_price } for all groups with settings."""
    conn = get_connection()
    rows = conn.execute('SELECT group_name, min_price FROM group_settings').fetchall()
    conn.close()
    return {r['group_name']: {'min_price': r['min_price']} for r in rows}


def init_db():
    """Initialize the database: create schema, run migrations, clean data, seed defaults."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('PRAGMA journal_mode = WAL')
    c.execute('PRAGMA synchronous = NORMAL')

    _create_schema(c)
    _run_migrations(c)
    _cleanup_data(c)
    _seed_defaults(c)

    conn.commit()
    conn.close()


# ── Settings helpers ─────────────────────────────────────────────────────────

def backup_db() -> str:
    """Erstellt ein Zeitstempel-Backup von deals.db unter %APPDATA%\\DealScraper\\backups\\
    und gibt den Pfad zurueck. Haelt maximal 10 Backups (aelteste werden geloescht)."""
    import shutil
    backup_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "DealScraper", "backups"
    )
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(backup_dir, f"deals_{ts}.db")
    shutil.copy2(DB_PATH, dest)
    # Maximal 10 Backups behalten — aelteste loeschen
    try:
        existing = sorted(
            (f for f in os.listdir(backup_dir) if f.startswith("deals_") and f.endswith(".db")),
        )
        for old in existing[:-10]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except Exception:
                pass
    except Exception:
        pass
    return dest


def list_backups() -> list[dict]:
    """Gibt alle vorhandenen Backups als Liste zurück."""
    backup_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "DealScraper", "backups"
    )
    if not os.path.isdir(backup_dir):
        return []
    result = []
    for fname in sorted(os.listdir(backup_dir), reverse=True):
        if fname.startswith("deals_") and fname.endswith(".db"):
            path = os.path.join(backup_dir, fname)
            try:
                size_kb = round(os.path.getsize(path) / 1024)
            except Exception:
                size_kb = 0
            result.append({"filename": fname, "path": path, "size_kb": size_kb})
    return result


def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a value from the settings table."""
    conn = get_connection()
    row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key: str, value: str):
    """Write a value to the settings table (upsert)."""
    conn = get_connection()
    conn.execute(
        'INSERT INTO settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, str(value)),
    )
    conn.commit()
    conn.close()


_ONLINE_SHIPS_BY_DEFAULT = {
    'Mindfactory', 'Alternate', 'Gravis', 'future-x', 'Conrad',
    'Refurbed', 'mac-store24', 'asgoodasnew', 'Apple',
    'Idealo', 'notebooksbilliger', 'Cyberport', 'Amazon',
}


def insert_or_update_deal(deal):
    """Returns (is_new, deal_id, old_price). old_price is the previous DB
    price for existing deals (may be None) or None for new inserts — callers
    use it to detect price drops without a second SELECT."""
    conn = get_connection()
    c = conn.cursor()
    now = datetime.now().isoformat()

    # Fallback: pure online shops always ship — set unless the scraper
    # explicitly said otherwise. Classifieds (KA / markt.de / quoka / eBay)
    # are heterogeneous so we leave the value to the scraper.
    if deal.get('shipping_available') is None and deal.get('website') in _ONLINE_SHIPS_BY_DEFAULT:
        deal['shipping_available'] = 1

    existing = c.execute('SELECT * FROM deals WHERE url = ?', (deal['url'],)).fetchone()
    is_new    = False
    deal_id   = None
    old_price = existing['price'] if existing else None
    is_auction = deal.get('listing_type') == 'auction'

    if existing:
        # Snapshot ONLY on real change. Two triggers:
        #   - price changed
        #   - bid_count went up (auction got a new bidder at same proxy max)
        # Unchanged state produces no snapshot. The chart should show bid
        # events, not sampling clicks — a row of 5 identical 450 € points
        # stamped with the user's "Aktualisieren" times is noise that masks
        # the real price trajectory.
        if deal.get('price') is not None:
            price_changed = (existing['price'] is None
                             or existing['price'] != deal['price'])
            bids_changed  = (is_auction
                             and deal.get('bid_count') is not None
                             and (existing['bid_count'] or 0) < deal.get('bid_count'))
            if price_changed or bids_changed:
                c.execute(
                    '''INSERT INTO price_history
                       (deal_id, price, changed_at, source) VALUES (?, ?, ?, ?)''',
                    (existing['id'], deal['price'], now, 'snapshot')
                )
        c.execute(
            '''UPDATE deals
               SET title=?, price=?, website=?, ram=?, ssd=?, model=?,
                   image_url=?, description=?, location=?, pickup_only=?,
                   shipping_available=COALESCE(?, shipping_available),
                   listing_type=?, seller=COALESCE(?, seller),
                   bid_count=?, auction_ends_at=COALESCE(?, auction_ends_at),
                   available=1, last_seen=?
               WHERE url=?''',
            (deal['title'], deal['price'], deal['website'],
             deal.get('ram'), deal.get('ssd'), deal.get('model'),
             deal.get('image_url'), deal.get('description'),
             deal.get('location'), 1 if deal.get('pickup_only') else 0,
             deal.get('shipping_available'),
             deal.get('listing_type'), deal.get('seller'),
             deal.get('bid_count'), deal.get('auction_ends_at'),
             now, deal['url'])
        )
        deal_id = existing['id']
    else:
        c.execute(
            '''INSERT INTO deals
               (title, price, url, website, found_at, ram, ssd, model,
                image_url, description, location, pickup_only, shipping_available,
                listing_type, seller, bid_count, auction_ends_at, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (deal['title'], deal['price'], deal['url'], deal['website'], now,
             deal.get('ram'), deal.get('ssd'), deal.get('model'),
             deal.get('image_url'), deal.get('description'),
             deal.get('location'), 1 if deal.get('pickup_only') else 0,
             deal.get('shipping_available'),
             deal.get('listing_type'), deal.get('seller'),
             deal.get('bid_count'), deal.get('auction_ends_at'), now)
        )
        is_new  = True
        deal_id = c.lastrowid
        # Seed price_history for auctions so the first scrape already produces a chart point.
        if is_auction and deal.get('price') is not None:
            c.execute(
                '''INSERT INTO price_history
                   (deal_id, price, changed_at, source) VALUES (?, ?, ?, ?)''',
                (deal_id, deal['price'], now, 'snapshot')
            )

    conn.commit()
    conn.close()
    return is_new, deal_id, old_price


# ── Block-by-deal / block-by-seller helpers ─────────────────────────────────

def set_deal_blocked(deal_id: int, blocked: bool = True) -> None:
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE deals SET blocked = ? WHERE id = ?',
              (1 if blocked else 0, deal_id))
    conn.commit()
    conn.close()


def set_deal_purchased(deal_id: int, purchased: bool = True) -> None:
    """Mark a single deal as purchased (hides from all panels via _VISIBLE_SQL).
    Stamps purchased_at so we can later sort by purchase date."""
    conn = get_connection()
    c = conn.cursor()
    if purchased:
        c.execute(
            'UPDATE deals SET purchased = 1, purchased_at = ? WHERE id = ?',
            (datetime.now().isoformat(), deal_id),
        )
    else:
        c.execute(
            'UPDATE deals SET purchased = 0, purchased_at = NULL WHERE id = ?',
            (deal_id,),
        )
    conn.commit()
    conn.close()


def set_group_purchased(group_name: str, purchased: bool = True) -> int:
    """Mark every active deal in the group's targets as purchased. Returns
    the row count touched. Use case: 'ich hab das iPhone 17 Pro gekauft,
    weg mit allen offenen Treffern'."""
    conn = get_connection()
    c = conn.cursor()
    target_names = [r['name'] for r in c.execute(
        'SELECT name FROM search_targets WHERE COALESCE(group_name, "") = ?',
        (group_name or '',),
    ).fetchall()]
    if not target_names:
        conn.close()
        return 0
    placeholders = ','.join('?' * len(target_names))
    if purchased:
        cur = c.execute(
            f'UPDATE deals SET purchased = 1, purchased_at = ? '
            f'WHERE model IN ({placeholders}) AND COALESCE(purchased, 0) = 0',
            (datetime.now().isoformat(), *target_names),
        )
    else:
        cur = c.execute(
            f'UPDATE deals SET purchased = 0, purchased_at = NULL '
            f'WHERE model IN ({placeholders}) AND purchased = 1',
            target_names,
        )
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def get_purchased_deals(limit: int = 100) -> list[dict]:
    """List recently purchased deals (newest first). For a future
    'Was hab ich gekauft?' panel."""
    conn = get_connection()
    rows = conn.execute(
        'SELECT * FROM deals WHERE purchased = 1 '
        'ORDER BY purchased_at DESC LIMIT ?',
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def block_seller(website: str, seller: str) -> None:
    if not website or not seller:
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        'INSERT OR IGNORE INTO blocked_sellers (website, seller, added_at) VALUES (?, ?, ?)',
        (website, seller, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def unblock_seller(website: str, seller: str) -> None:
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM blocked_sellers WHERE website=? AND seller=?',
              (website, seller))
    conn.commit()
    conn.close()


def get_blocked_sellers() -> list[dict]:
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        'SELECT website, seller, added_at FROM blocked_sellers ORDER BY added_at DESC'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_blocked_deals() -> list[dict]:
    """All deals manually blocked via the X-button (kept around so user can unblock)."""
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        'SELECT * FROM deals WHERE blocked = 1 ORDER BY found_at DESC LIMIT 200'
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_apple_price(target_name: str, price: float | None) -> None:
    """Called by the Apple scraper after it resolves an Apple-shop URL for a target."""
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE search_targets SET apple_price = ? WHERE name = ?',
              (price, target_name))
    conn.commit()
    conn.close()


# SQL fragment: every visible-deals query ANDs this in, after `available = 1`.
# It enforces three things at the DB level so we don't have to remember to add
# them everywhere in Python:
#   1. blocked = 0          (deal not manually X-ed out)
#   2. seller not in blocked_sellers (per-site seller blocklist)
#   3. price >= target.min_price (per-target fake floor; falls back to 100)
_VISIBLE_SQL = (
    "deals.blocked = 0 "
    "AND COALESCE(deals.purchased, 0) = 0 "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM blocked_sellers bs "
    "  WHERE bs.website = deals.website "
    "    AND deals.seller IS NOT NULL "
    "    AND bs.seller = deals.seller"
    ") "
    "AND deals.price >= COALESCE("
    "  (SELECT st.min_price FROM search_targets st WHERE st.name = deals.model),"
    "  (SELECT gs.min_price FROM group_settings gs"
    "   JOIN search_targets st2 ON gs.group_name = st2.group_name"
    "   WHERE st2.name = deals.model LIMIT 1),"
    "  100"
    ")"
)


def mark_expired_auctions() -> int:
    """Set available=0 for eBay auctions whose end time has passed.

    Called periodically by the background auction-refresh loop.  A deal is
    considered expired when auction_ends_at is a non-null ISO-8601 timestamp
    that is in the past.  Returns the number of rows updated.
    """
    conn = get_connection()
    c = conn.cursor()
    # Iter. 27: war datetime.utcnow() — falsch, weil auction_ends_at als
    # Berlin-Local-ISO gespeichert ist (scraper._parse_auction_remaining /
    # _parse_ebay_item_end_time). UTC-Vergleich retired Auktionen 1-2h zu
    # spaet.  Wir muessen Local-now mit Local-end vergleichen.
    now_iso = datetime.now().isoformat()
    c.execute(
        '''UPDATE deals
           SET available = 0
           WHERE listing_type = 'auction'
             AND available    = 1
             AND auction_ends_at IS NOT NULL
             AND auction_ends_at != ''
             AND auction_ends_at < ?''',
        (now_iso,),
    )
    n = c.rowcount
    conn.commit()
    conn.close()
    if n:
        import logging
        logging.getLogger(__name__).info(f'mark_expired_auctions: {n} auctions retired')
    return n


def mark_unavailable(urls_to_keep, website, model: str | None = None):
    """Mark all deals on `website` (optionally restricted to `model`) that are NOT in
    `urls_to_keep` as unavailable. Used after a full or per-target scrape to retire
    listings that disappeared from the source."""
    conn = get_connection()
    c = conn.cursor()
    base   = 'UPDATE deals SET available=0 WHERE website=?'
    params: list = [website]
    if model:
        base += ' AND model=?'
        params.append(model)
    if urls_to_keep:
        placeholders = ','.join(['?' for _ in urls_to_keep])
        c.execute(
            f'{base} AND url NOT IN ({placeholders})',
            params + list(urls_to_keep),
        )
    else:
        c.execute(base, params)
    conn.commit()
    conn.close()


def get_all_deals(filters=None):
    conn = get_connection()
    c = conn.cursor()

    # All `deals.*` qualifiers below match the _VISIBLE_SQL fragment.
    # We also pull the per-target apple_price + retail_price so the panel
    # cards can render the "−X% vs Apple" tag without an extra round-trip.
    query  = (
        f'SELECT deals.*, '
        f'(SELECT apple_price  FROM search_targets WHERE name = deals.model) AS apple_price, '
        f'(SELECT retail_price FROM search_targets WHERE name = deals.model) AS retail_price '
        f'FROM deals WHERE 1=1 AND ({_VISIBLE_SQL})'
    )
    params = []

    if filters:
        if filters.get('available_only'):
            query += ' AND deals.available = 1'
        if filters.get('favorite_only'):
            query += ' AND deals.favorite = 1'
        if filters.get('website'):
            query += ' AND deals.website = ?'
            params.append(filters['website'])
        if filters.get('model'):
            query += ' AND deals.model = ?'
            params.append(filters['model'])
        if filters.get('min_price') is not None:
            query += ' AND deals.price >= ?'
            params.append(filters['min_price'])
        if filters.get('max_price') is not None:
            query += ' AND deals.price <= ?'
            params.append(filters['max_price'])
        if filters.get('search'):
            query += ' AND deals.title LIKE ?'
            params.append(f"%{filters['search']}%")
        if filters.get('color'):
            query += ' AND LOWER(deals.title) LIKE ?'
            params.append(f"%{filters['color'].lower()}%")
        if filters.get('extra'):
            query += ' AND LOWER(deals.title) LIKE ?'
            params.append(f"%{filters['extra'].lower()}%")

        sort_col   = filters.get('sort',  'found_at')
        sort_order = str(filters.get('order', 'DESC')).upper()
        if sort_order not in ('ASC', 'DESC'):
            sort_order = 'DESC'
        if sort_col in ('price', 'found_at', 'website', 'title', 'model'):
            query += f' ORDER BY deals.{sort_col} {sort_order}'
    else:
        query += ' ORDER BY deals.found_at DESC'

    rows = c.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def toggle_favorite(deal_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE deals SET favorite = 1 - favorite WHERE id = ?', (deal_id,))
    conn.commit()
    conn.close()


def get_stats():
    conn = get_connection()
    c = conn.cursor()

    model_rows = c.execute(
        f'SELECT DISTINCT deals.model FROM deals '
        f'WHERE deals.model IS NOT NULL AND deals.available=1 AND ({_VISIBLE_SQL}) '
        f'ORDER BY deals.model'
    ).fetchall()
    model_stats = {}
    for row in model_rows:
        model = row['model']
        r = c.execute(
            f'SELECT AVG(deals.price) as avg, MIN(deals.price) as min, COUNT(*) as count '
            f'FROM deals WHERE deals.model=? AND deals.available=1 AND ({_VISIBLE_SQL})',
            (model,)
        ).fetchone()
        model_stats[model] = dict(r) if r else {'avg': None, 'min': None, 'count': 0}

    website_counts = c.execute(
        f'SELECT deals.website, COUNT(*) as count FROM deals '
        f'WHERE deals.available=1 AND ({_VISIBLE_SQL}) GROUP BY deals.website'
    ).fetchall()

    conn.close()
    return {
        'models':   model_stats,
        'websites': {r['website']: r['count'] for r in website_counts},
    }


def get_alerts():
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute('SELECT * FROM alerts').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_alert(model, threshold):
    conn = get_connection()
    c = conn.cursor()
    existing = c.execute('SELECT id FROM alerts WHERE model=?', (model,)).fetchone()
    if existing:
        c.execute('UPDATE alerts SET threshold=?, active=1 WHERE model=?', (threshold, model))
        alert_id = existing['id']
    else:
        c.execute('INSERT INTO alerts (model, threshold) VALUES (?, ?)', (model, threshold))
        alert_id = c.lastrowid
    conn.commit()
    conn.close()
    return alert_id


def delete_alert(alert_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM alerts WHERE id=?', (alert_id,))
    conn.commit()
    conn.close()


def log_alert(alert_id, deal_id, price):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        'INSERT INTO alert_log (alert_id, deal_id, price, triggered_at) VALUES (?, ?, ?, ?)',
        (alert_id, deal_id, price, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ── Geocode cache + deal coord helpers (Iter. 24 PLZ filter) ────────────────

def geocache_lookup(query: str):
    """Return (lat, lon, status) or None if query never resolved.
    status='notfound' rows older than 7 days are treated as expired (None)."""
    conn = get_connection()
    row = conn.execute(
        'SELECT lat, lon, status, fetched_at FROM geocache WHERE query = ?',
        (query,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if row['status'] == 'notfound':
        try:
            age_days = (datetime.now() - datetime.fromisoformat(row['fetched_at'])).days
        except Exception:
            age_days = 999
        if age_days >= 7:
            return None
    return (row['lat'], row['lon'], row['status'])


def geocache_store(query: str, lat: float | None, lon: float | None, status: str = 'ok'):
    conn = get_connection()
    conn.execute(
        'INSERT INTO geocache (query, lat, lon, status, fetched_at) '
        'VALUES (?, ?, ?, ?, ?) '
        'ON CONFLICT(query) DO UPDATE SET '
        '  lat=excluded.lat, lon=excluded.lon, '
        '  status=excluded.status, fetched_at=excluded.fetched_at',
        (query, lat, lon, status, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def backfill_deal_coords_from_cache() -> int:
    """Copy lat/lon from the geocache into deals for every matching location.
    Cheap (pure SQL) — call this before queueing Nominatim requests so we
    don't waste rate-limit slots on locations another deal already resolved.
    Returns number of rows updated."""
    conn = get_connection()
    cur = conn.execute(
        'UPDATE deals SET '
        '  lat = (SELECT lat FROM geocache WHERE query = deals.location AND status = "ok"), '
        '  lon = (SELECT lon FROM geocache WHERE query = deals.location AND status = "ok") '
        'WHERE deals.location IS NOT NULL AND deals.lat IS NULL '
        '  AND EXISTS (SELECT 1 FROM geocache WHERE query = deals.location AND status = "ok")'
    )
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def get_distinct_locations_needing_geocode(limit: int = 50) -> list[str]:
    """Return distinct location strings that need a fresh Nominatim call:
    appear in deals, no lat yet, not in geocache (or in cache as expired
    'notfound'). DISTINCT avoids 50 deals × same Berlin postcode = 50 calls."""
    conn = get_connection()
    rows = conn.execute(
        'SELECT DISTINCT d.location FROM deals d '
        'LEFT JOIN geocache g ON g.query = d.location '
        'WHERE d.location IS NOT NULL AND TRIM(d.location) != "" '
        '  AND d.lat IS NULL '
        '  AND (g.query IS NULL OR '
        '       (g.status = "notfound" AND julianday("now") - julianday(g.fetched_at) > 7)) '
        'LIMIT ?',
        (limit,),
    ).fetchall()
    conn.close()
    return [r['location'] for r in rows]


def get_alert_log():
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        '''SELECT al.id, al.price, al.triggered_at,
                  a.model, a.threshold,
                  d.title, d.url, d.website
           FROM alert_log al
           JOIN alerts a ON al.alert_id = a.id
           JOIN deals  d ON al.deal_id  = d.id
           ORDER BY al.triggered_at DESC LIMIT 100'''
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_price_history(deal_id):
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        'SELECT * FROM price_history WHERE deal_id=? ORDER BY changed_at', (deal_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def replace_price_history(deal_id: int, bids: list[dict]) -> int:
    """Wipe + rewrite price_history for `deal_id` with authoritative data.

    Used after scraping eBay's /bfl/viewbids page, which exposes the FULL bid
    timeline (every increment, with exact timestamps). Our sampled snapshots
    are a strict subset of that — replacing them with the real series gives
    the chart every bid, not just whatever happened to be live during a
    scrape pass.

    `bids` is a list of {'price': float, 'changed_at': ISO-str, 'bidder'?}.
    Caller is responsible for only invoking this when bids is non-empty
    (we don't want to wipe our snapshots if the bid-history fetch failed).
    Returns the number of rows inserted.

    Iter. 27 D13/D14: Setzt zusaetzlich bid_history_imported_at + _price
    auf der zugehoerigen deals-Row damit die UI Frischheit anzeigen + spaetere
    Karten-Preis-Sprünge als „stale" erkennen kann.
    """
    if not bids:
        return 0
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM price_history WHERE deal_id = ?', (deal_id,))
    rows = [(deal_id, b['price'], b['changed_at'], b.get('bidder'), 'ebay_bid')
            for b in bids
            if b.get('price') is not None and b.get('changed_at')]
    if not rows:
        conn.close()
        return 0
    c.executemany(
        '''INSERT INTO price_history
           (deal_id, price, changed_at, bidder, source) VALUES (?, ?, ?, ?, ?)''',
        rows,
    )
    # Frischheits-Stempel + hoechster importierter Bid als Baseline.
    max_price = max(b['price'] for b in bids if b.get('price') is not None)
    c.execute(
        '''UPDATE deals
           SET bid_history_imported_at    = ?,
               bid_history_imported_price = ?
           WHERE id = ?''',
        (datetime.now().isoformat(), max_price, deal_id),
    )
    conn.commit()
    conn.close()
    return len(rows)


def _price_cutoff(model: str, conn) -> float:
    """5× the median price for the model — filters outliers from stats/top deals."""
    prices = sorted(
        row[0] for row in conn.execute(
            f'SELECT deals.price FROM deals '
            f'WHERE deals.model=? AND deals.available=1 AND ({_VISIBLE_SQL})',
            (model,)
        ).fetchall()
    )
    if not prices:
        return float('inf')
    return prices[len(prices) // 2] * 5


def get_target_summary(model: str) -> dict:
    conn = get_connection()
    c = conn.cursor()
    total = c.execute(
        f'SELECT COUNT(*) FROM deals WHERE deals.model=? AND deals.available=1 AND ({_VISIBLE_SQL})',
        (model,)
    ).fetchone()[0]
    cutoff = _price_cutoff(model, c)
    prices = sorted(
        row[0] for row in c.execute(
            f'SELECT deals.price FROM deals '
            f'WHERE deals.model=? AND deals.available=1 AND deals.price <= ? AND ({_VISIBLE_SQL})',
            (model, cutoff)
        ).fetchall()
    )
    conn.close()
    if not prices:
        return {'count': total, 'avg_price': None, 'min_price': None}
    return {
        'count':     total,
        'avg_price': round(sum(prices) / len(prices)),
        'min_price': prices[0],
    }


def get_top_deals(model: str, limit: int = 6) -> list[dict]:
    conn = get_connection()
    c = conn.cursor()
    cutoff = _price_cutoff(model, c)
    rows = c.execute(
        f'''SELECT deals.*,
                  (SELECT apple_price FROM search_targets WHERE name = deals.model) AS apple_price,
                  (SELECT retail_price FROM search_targets WHERE name = deals.model) AS retail_price
           FROM deals
           WHERE deals.model=? AND deals.available=1
             AND deals.price <= ? AND ({_VISIBLE_SQL})
           ORDER BY deals.price ASC LIMIT ?''',
        (model, cutoff, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_global_top_deals(limit: int = 8) -> list[dict]:
    conn = get_connection()
    c    = conn.cursor()
    # Inner subquery needs its own table alias `d2` but uses the same visibility
    # constraints, so we inline a copy without the deals.* prefix for the alias.
    rows = c.execute(
        f'''SELECT deals.*,
                  (SELECT AVG(d2.price) FROM deals d2
                   WHERE d2.model = deals.model AND d2.available = 1
                     AND d2.blocked = 0
                     AND d2.price >= 100 AND d2.price <= 5000) AS avg_price
           FROM deals
           WHERE deals.available=1 AND deals.price <= 5000 AND ({_VISIBLE_SQL})
           ORDER BY deals.price ASC LIMIT ?''',
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_top_deal_per_model() -> list[dict]:
    """Returns the single cheapest available deal for each active search target."""
    conn = get_connection()
    c    = conn.cursor()
    targets = c.execute(
        'SELECT name FROM search_targets WHERE active = 1 ORDER BY id'
    ).fetchall()
    result = []
    for t in targets:
        model = t['name']
        row = c.execute(
            f'''SELECT deals.*,
                      (SELECT ROUND(AVG(d2.price))
                       FROM deals d2
                       WHERE d2.model = deals.model AND d2.available = 1
                         AND d2.blocked = 0
                         AND d2.price >= 100 AND d2.price <= 5000) AS avg_price,
                      (SELECT COUNT(*)
                       FROM deals d3
                       WHERE d3.model = deals.model AND d3.available = 1
                         AND d3.blocked = 0
                         AND d3.price >= 100 AND d3.price <= 5000) AS total_count
               FROM deals
               WHERE deals.available = 1 AND deals.model = ?
                 AND deals.price <= 5000 AND ({_VISIBLE_SQL})
               ORDER BY deals.price ASC LIMIT 1''',
            (model,)
        ).fetchone()
        if row:
            d = dict(row)
            d['category'] = model
            result.append(d)
        else:
            result.append({'category': model, 'empty': True})
    conn.close()
    return result


def get_top_deal_per_group() -> list[dict]:
    """One top deal per group. Targets without group_name are treated as their own group.
    Picks the single best deal across all targets in the group (by smart score).
    Returns: [{ 'category': <group_or_target_name>, 'group_name': <str|None>,
                'retail_price': <float|None>, 'member_models': [list of model names],
                ...deal fields, 'avg_price', 'total_count' }, ...]"""
    conn = get_connection()
    c    = conn.cursor()
    targets = c.execute(
        'SELECT * FROM search_targets WHERE active = 1 ORDER BY id'
    ).fetchall()
    # Group targets: by group_name if set, else target name is its own group
    groups: dict[str, list] = {}
    group_meta: dict[str, dict] = {}
    for t in targets:
        gn = (t['group_name'] or '').strip() or t['name']
        groups.setdefault(gn, []).append(t['name'])
        meta = group_meta.setdefault(gn, {
            'group_name':   t['group_name'] if (t['group_name'] or '').strip() else None,
            'retail_price': t['retail_price'],
            'apple_price':  t['apple_price'],
        })
        # If multiple targets share group, take the max retail_price as reference
        rp = t['retail_price']
        if rp and (not meta['retail_price'] or rp > meta['retail_price']):
            meta['retail_price'] = rp
        ap = t['apple_price']
        if ap and (not meta['apple_price'] or ap > meta['apple_price']):
            meta['apple_price'] = ap

    result = []
    for group_label, models in groups.items():
        placeholders = ','.join('?' for _ in models)
        avg_count = c.execute(
            f'''SELECT ROUND(AVG(deals.price)) AS avg_price, COUNT(*) AS total_count
                FROM deals
                WHERE deals.model IN ({placeholders}) AND deals.available = 1
                  AND deals.price <= 5000 AND ({_VISIBLE_SQL})
                  AND COALESCE(deals.listing_type, 'fixed') != 'auction' ''',
            models,
        ).fetchone()
        avg_price  = avg_count['avg_price']
        total_count = avg_count['total_count']
        row = c.execute(
            f'''SELECT deals.* FROM deals
                WHERE deals.model IN ({placeholders}) AND deals.available = 1
                  AND deals.price <= 5000 AND ({_VISIBLE_SQL})
                ORDER BY
                  CASE WHEN COALESCE(deals.listing_type, 'fixed') = 'auction' THEN 1 ELSE 0 END ASC,
                  deals.price ASC
                LIMIT 1''',
            models,
        ).fetchone()
        meta = group_meta[group_label]
        if row:
            d = dict(row)
            d.update({
                'category':      group_label,
                'group_name':    meta['group_name'],
                'member_models': models,
                'retail_price':  meta['retail_price'],
                'apple_price':   meta['apple_price'],
                'avg_price':     avg_price,
                'total_count':   total_count,
            })
            result.append(d)
        else:
            result.append({
                'category':      group_label,
                'group_name':    meta['group_name'],
                'member_models': models,
                'retail_price':  meta['retail_price'],
                'apple_price':   meta['apple_price'],
                'empty':         True,
            })
    conn.close()
    return result


def get_targets():
    """Targets ordered by group (alphabetical, NULL group last) then by id.
    Each target dict includes a 'sources' key with the allowed sources for its
    group (empty list = no restriction = all sources allowed)."""
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        "SELECT * FROM search_targets "
        "ORDER BY CASE WHEN group_name IS NULL OR group_name = '' THEN 1 ELSE 0 END, "
        "         LOWER(group_name), id"
    ).fetchall()
    targets = [dict(r) for r in rows]

    # Attach group sources — one query per distinct group to avoid N+1
    group_cache: dict[str, list[str]] = {}
    for t in targets:
        gn = (t.get('group_name') or '').strip()
        if gn not in group_cache:
            src_rows = c.execute(
                'SELECT source FROM group_sources WHERE group_name = ? ORDER BY source',
                (gn,),
            ).fetchall()
            group_cache[gn] = [r['source'] for r in src_rows]
        t['sources'] = group_cache[gn]

    conn.close()
    return targets


def get_deal_by_id(deal_id: int) -> dict | None:
    """Look up a single deal row by primary key. Returns None if not found."""
    conn = get_connection()
    c = conn.cursor()
    row = c.execute('SELECT * FROM deals WHERE id = ?', (deal_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_active_auctions(min_bids: int = 1, limit: int = 50) -> list[dict]:
    """All eBay auctions worth polling between global scrapes.

    Used by the background refresh thread to keep the price-history chart
    growing without waiting for the next 4 h scrape. We restrict to:
      - website='eBay' and listing_type='auction'
      - available=1                 (not retired)
      - blocked=0                   (not user-X'ed)
      - bid_count >= min_bids       (skip dead listings with zero bids —
                                     not worth a request)
    Newest-first; cap at `limit` so the polling thread can't blow up.
    """
    conn = get_connection()
    c = conn.cursor()
    rows = c.execute(
        '''SELECT *
           FROM deals
           WHERE website = 'eBay'
             AND listing_type = 'auction'
             AND available = 1
             AND blocked = 0
             AND COALESCE(bid_count, 0) >= ?
           ORDER BY last_seen DESC
           LIMIT ?''',
        (min_bids, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_target(name: str, keyword: str,
               group_name: str | None = None,
               retail_price: float | None = None) -> int:
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        'INSERT INTO search_targets (name, keyword, group_name, retail_price) VALUES (?, ?, ?, ?)',
        (name, keyword, group_name, retail_price),
    )
    target_id = c.lastrowid
    conn.commit()
    conn.close()
    return target_id


def update_target(target_id: int, *,
                  group_name: str | None | object = ...,
                  retail_price: float | None | object = ...,
                  min_price: float | None | object = ...,
                  wish_price: float | None | object = ...) -> None:
    """Partial update of a target. Pass `...` to skip a field."""
    conn = get_connection()
    c = conn.cursor()
    sets, params = [], []
    if group_name is not ...:
        sets.append('group_name = ?')
        params.append(group_name or None)
    if retail_price is not ...:
        sets.append('retail_price = ?')
        params.append(retail_price)
    if min_price is not ...:
        sets.append('min_price = ?')
        params.append(min_price)
    if wish_price is not ...:
        sets.append('wish_price = ?')
        params.append(wish_price)
    if sets:
        params.append(target_id)
        c.execute(f'UPDATE search_targets SET {", ".join(sets)} WHERE id = ?', params)
        conn.commit()
    conn.close()


def toggle_target(target_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE search_targets SET active = 1 - active WHERE id = ?', (target_id,))
    conn.commit()
    conn.close()


def cleanup_duplicates() -> dict:
    """Findet und bereinigt Duplikate in der deals-Tabelle.

    Zwei Strategien:
    1. eBay-Duplikate: gleiche eBay-Item-ID (10-15 stellige Zahl in der URL)
       → behalte neueren/vollstaendigeren Deal, markiere den anderen als unavailable.
    2. URL-Normalisierung: entferne Tracking-Params (?mkcid=..., ?hash=...) und
       prüfe ob normalisierte URLs kollidieren — markiere den aelteren als unavailable.

    Gibt die Anzahl bereinigter Zeilen zurueck.
    """
    import re as _re
    conn = get_connection()
    c = conn.cursor()

    retired = 0

    # Strategie 1: eBay-Duplikate via Item-ID
    ebay_rows = c.execute(
        "SELECT id, url, price, bid_count, last_seen FROM deals "
        "WHERE website = 'eBay' AND available = 1 "
        "ORDER BY last_seen DESC"
    ).fetchall()

    item_id_map: dict[str, list] = {}
    _EBAY_ID_RE = _re.compile(r'(?:/itm/(?:[^/]+/)?|item=|ebay\.\w+/itm/)(\d{10,15})')
    for row in ebay_rows:
        m = _EBAY_ID_RE.search(row['url'])
        if m:
            eid = m.group(1)
            item_id_map.setdefault(eid, []).append(row)

    for eid, rows in item_id_map.items():
        if len(rows) <= 1:
            continue
        # Behalte den mit mehr Daten (bid_count > 0 bevorzugt, dann neuester)
        rows_sorted = sorted(
            rows,
            key=lambda r: (r['bid_count'] or 0, r['last_seen'] or ''),
            reverse=True,
        )
        keep_id = rows_sorted[0]['id']
        for r in rows_sorted[1:]:
            c.execute('UPDATE deals SET available = 0 WHERE id = ?', (r['id'],))
            retired += 1

    # Strategie 2: URL-Normalisierung (Tracking-Params entfernen)
    _TRACKING_RE = _re.compile(
        r'[?&](?:mkcid|mkrid|campid|toolid|customid|hash|ref|epid|ssPageName'
        r'|mkevt|mkcid|enc|sid)[^&]*'
    )

    def _normalize_url(url: str) -> str:
        url = url.split('#')[0]
        url = _TRACKING_RE.sub('', url)
        url = url.rstrip('?&')
        return url.lower()

    all_rows = c.execute(
        "SELECT id, url, found_at FROM deals WHERE available = 1 "
        "ORDER BY found_at ASC"
    ).fetchall()

    seen_norm: dict[str, int] = {}
    for row in all_rows:
        norm = _normalize_url(row['url'])
        if norm in seen_norm:
            # Aeltere Zeile wird behalten (found_at ASC), neuere retired
            c.execute('UPDATE deals SET available = 0 WHERE id = ?', (row['id'],))
            retired += 1
        else:
            seen_norm[norm] = row['id']

    conn.commit()
    conn.close()
    return {'retired': retired}


def delete_target(target_id: int):
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM search_targets WHERE id = ?', (target_id,))
    conn.commit()
    conn.close()
