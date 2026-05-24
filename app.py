"""
Flask backend – API endpoints + auto-scrape scheduler.
Launched via main.py (desktop app) or directly: python app.py
"""

import csv
import io
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

import database as db
import scraper
from geocoder import geocode
from notifier import send_notification

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, 'app.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# init_db() is called in main.py for the desktop-app flow.
# For standalone runs (python app.py) we initialise here via __name__ guard at bottom.


@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception('Unhandled exception: %s', e)
    return jsonify({'error': str(e)}), 500


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(BASE_DIR, 'icon.ico', mimetype='image/vnd.microsoft.icon')


@app.route('/api/health')
def api_health():
    return jsonify({'ok': True, 'ts': datetime.now().isoformat()})

# Interval is loaded from DB after init_db() in _init_interval() below.
scrape_interval_minutes: int = 240
_timer: threading.Timer | None = None

# Background auction-refresh thread state
_auction_refresh_thread: threading.Thread | None = None
_auction_refresh_stop = threading.Event()
AUCTION_REFRESH_EVERY_SEC = 30 * 60   # every 30 min
AUCTION_REFRESH_MAX_ITEMS = 25        # cap per pass — avoid hammering eBay

# Background geocode thread state (Iter. 24 PLZ filter)
_geocode_thread: threading.Thread | None = None
_geocode_stop = threading.Event()
GEOCODE_EVERY_SEC = 90                # short loop — Nominatim is cheap when cached
GEOCODE_MAX_PER_PASS = 30             # 30 × 1.1 s rate-limit = ~33 s per pass


def _build_callback(restrict_model: str | set | None = None):
    """Return a callback that processes deals + marks stale listings unavailable.
    If `restrict_model` is set, unavailability marking is limited to that model only."""
    def callback(deals: list[dict]) -> None:
        alerts     = db.get_alerts()
        by_website = defaultdict(set)

        for deal in deals:
            is_new, deal_id, old_price = db.insert_or_update_deal(deal)
            by_website[deal['website']].add(deal['url'])

            price = deal.get('price')
            if price is None:
                continue
            for alert in alerts:
                if not alert['active'] or alert['model'] != deal.get('model'):
                    continue
                threshold = alert['threshold']
                if price > threshold:
                    continue
                # Trigger exactly when the deal CROSSES the threshold:
                #  - brand-new deal already under threshold, OR
                #  - existing deal whose previous price was above threshold
                #    (or unknown) and dropped to/below it.
                # Once old_price <= threshold the condition is false again,
                # so no spam on repeat scrapes.
                crossed = is_new or old_price is None or old_price > threshold
                if not crossed:
                    continue
                db.log_alert(alert['id'], deal_id, price)
                if is_new:
                    title = f"Deal! {deal['model']}"
                else:
                    drop = old_price - price if old_price else 0
                    title = f"Preis gefallen: {deal['model']} -{drop:.0f}€"
                send_notification(
                    title,
                    f"{deal['title'][:80]}\n{price:.0f}€ – {deal['website']}",
                )

        for website, urls in by_website.items():
            # For subset scrapes (set of model names) we have to call
            # mark_unavailable per model so each model only retires URLs
            # within ITS own scope.
            if isinstance(restrict_model, (set, frozenset)):
                for m in restrict_model:
                    model_urls = {d['url'] for d in deals
                                  if d['website'] == website and d.get('model') == m}
                    db.mark_unavailable(model_urls, website, model=m)
            else:
                db.mark_unavailable(urls, website, model=restrict_model)
    return callback


# Kept for backwards compat with anything that imports process_deals
process_deals = _build_callback()


def _do_scrape(target: dict | None = None,
               targets: list | None = None,
               label: str | None = None) -> None:
    """Run a scrape.

    Modes:
      - target=<dict>           → single-target scrape (used by per-deal refresh)
      - targets=<list of dicts> → arbitrary subset (used by per-group scrape)
      - target=None, targets=None → all active targets (global scrape)

    Only the global scrape reschedules the next auto-scrape; partial scrapes
    don't touch the schedule.
    """
    if target:
        logger.info('Scrape triggered (single target: %s)', target['name'])
        scrape_targets = [target]
        restrict       = target['name']
        reschedule     = False
    elif targets:
        names = ', '.join(t['name'] for t in targets[:5])
        if len(targets) > 5:
            names += f' (+{len(targets) - 5} more)'
        logger.info('Scrape triggered (%s: %s)', label or 'subset', names)
        scrape_targets = targets
        # Subset scrapes: only mark stale within these models so other
        # categories aren't accidentally hidden.
        restrict       = {t['name'] for t in targets}
        reschedule     = False
    else:
        logger.info('Scrape triggered (all active targets)')
        all_targets    = db.get_targets()
        scrape_targets = [t for t in all_targets if t['active']] or None
        restrict       = None
        reschedule     = True
    try:
        scraper.run_scrape(callback=_build_callback(restrict), targets=scrape_targets)
    except Exception as e:
        logger.exception('Scrape failed: %s', e)
        scraper.STATUS['scraping'] = False
    finally:
        if reschedule:
            _schedule_next()


def _schedule_next() -> None:
    """Schedule next auto-scrape. interval=0 disables it."""
    global _timer
    if _timer:
        _timer.cancel()
        _timer = None
    if scrape_interval_minutes <= 0:
        logger.info('Auto-scrape disabled (interval=0)')
        return
    _timer = threading.Timer(scrape_interval_minutes * 60, _do_scrape)
    _timer.daemon = True
    _timer.start()
    logger.info(f'Next auto-scrape in {scrape_interval_minutes} min')


def _refresh_one_auction(deal: dict) -> bool:
    """Pull a fresh price for one eBay auction and rewrite price_history with
    the FULL bid timeline (via eBay's public /bfl/viewbids page). Falls back
    to per-scrape snapshot semantics if bid-history is unavailable.

    Returns True if anything was updated. Mirrors the merge logic of the
    /api/deals/<id>/refresh endpoint so behaviour is identical between the
    user-triggered button and this background pass.
    """
    fresh = scraper.refresh_ebay_item(deal['url'])
    if fresh is None:
        return False
    merged = dict(deal)
    merged.update(fresh)
    if 'listing_type' not in fresh and deal.get('listing_type'):
        merged['listing_type'] = deal['listing_type']

    # Auction-ended detection: refresh_ebay_item flags ended=True when the
    # eBay page shows "diese Auktion ist beendet" / "sold for" / itemAvailability
    # OutOfStock. Retire the deal so the UI stops showing it as live.
    if fresh.get('ended'):
        try:
            conn = db.get_connection()
            conn.execute('UPDATE deals SET available=0, last_seen=? WHERE id=?',
                         (datetime.now().isoformat(), deal['id']))
            conn.commit()
            conn.close()
            logger.info(f'auction {deal["id"]} marked ended (eBay page says closed)')
        except Exception as e:
            logger.warning('failed to mark ended auction %s: %s', deal.get('id'), e)
        return True

    db.insert_or_update_deal(merged)

    # Authoritative bid timeline if reachable — replaces our sampled snapshots
    # with every single bid increment eBay records.
    if merged.get('listing_type') == 'auction':
        try:
            bids = scraper.scrape_ebay_bid_history(deal['url'])
            if bids:
                db.replace_price_history(deal['id'], bids)
        except Exception as e:
            logger.warning('bid-history refresh for %s failed: %s', deal.get('id'), e)
    return True


def _auction_refresh_loop() -> None:
    """Background polling for active eBay auctions.

    Every AUCTION_REFRESH_EVERY_SEC seconds, fetch up to AUCTION_REFRESH_MAX_ITEMS
    auctions with bid_count >= 1 and write fresh snapshots. This fills the
    price-history chart with more data points between the (hourly+) global
    scrapes, so auctions accumulate a real time series even when the user
    isn't watching with live-mode. We skip dead listings (no bids) to avoid
    burning quota on listings nobody cares about.

    Refuses to run while a global scrape is in progress (overlapping eBay
    requests would just heat up rate-limits without benefit).
    """
    logger.info('Auction refresh thread started '
                f'(every {AUCTION_REFRESH_EVERY_SEC // 60} min, '
                f'max {AUCTION_REFRESH_MAX_ITEMS} per pass)')
    # First wait, then run — gives the initial scrape time to populate the DB.
    while not _auction_refresh_stop.wait(AUCTION_REFRESH_EVERY_SEC):
        if scraper.STATUS.get('scraping'):
            logger.debug('Auction refresh: skipped (global scrape in progress)')
            continue
        try:
            # Retire any auctions whose end-time has passed before fetching
            # the active list — keeps the UI clean without waiting for a
            # full global scrape.
            expired = db.mark_expired_auctions()
            if expired:
                logger.info(f'Auction refresh: {expired} expired auctions retired')

            auctions = db.get_active_auctions(min_bids=1, limit=AUCTION_REFRESH_MAX_ITEMS)
            if not auctions:
                continue
            ok = 0
            for deal in auctions:
                if _auction_refresh_stop.is_set():
                    break
                try:
                    if _refresh_one_auction(deal):
                        ok += 1
                except Exception as e:
                    logger.warning('auction-refresh item %s: %s', deal.get('id'), e)
                # Polite jitter between item requests so eBay doesn't see a
                # burst. ~1 s × 25 items = ~25 s extra per pass — negligible.
                time.sleep(1.0)
            logger.info(f'Auction refresh: {ok}/{len(auctions)} auctions polled')
        except Exception:
            logger.exception('Auction refresh pass crashed')


def _start_auction_refresh() -> None:
    global _auction_refresh_thread
    if _auction_refresh_thread and _auction_refresh_thread.is_alive():
        return
    _auction_refresh_stop.clear()
    _auction_refresh_thread = threading.Thread(
        target=_auction_refresh_loop, daemon=True, name='auction-refresh'
    )
    _auction_refresh_thread.start()


def _geocode_loop() -> None:
    """Background geocoding for deals that have a location string but no
    coords yet. Two phases per pass:
      1. backfill_deal_coords_from_cache() — cheap SQL, propagates any
         cached geocodes (e.g. a freshly scraped Munich deal inherits the
         coords resolved last week).
      2. For up to GEOCODE_MAX_PER_PASS distinct *new* locations, call
         Nominatim (rate-limited to 1.1 req/s inside the geocoder), cache
         the result, then backfill again so all deals with that location
         pick up the new coords.

    Nominatim is free but throttled — keeping the per-pass cap modest avoids
    starving the rate-limit when other features eventually want geocoding.
    """
    logger.info('Geocode thread started '
                f'(every {GEOCODE_EVERY_SEC}s, max {GEOCODE_MAX_PER_PASS} new locations per pass)')
    while not _geocode_stop.wait(GEOCODE_EVERY_SEC):
        try:
            n_backfilled = db.backfill_deal_coords_from_cache()
            locations = db.get_distinct_locations_needing_geocode(GEOCODE_MAX_PER_PASS)
            if not locations:
                if n_backfilled:
                    logger.info(f'Geocode: backfilled {n_backfilled} deals from cache, no new locations')
                continue
            n_ok = 0
            for loc in locations:
                if _geocode_stop.is_set():
                    break
                if geocode(loc) is not None:
                    n_ok += 1
            n_backfilled2 = db.backfill_deal_coords_from_cache()
            logger.info(
                f'Geocode pass: {n_ok}/{len(locations)} locations resolved, '
                f'{n_backfilled + n_backfilled2} deals updated'
            )
        except Exception:
            logger.exception('Geocode pass crashed')


def _start_geocode_thread() -> None:
    global _geocode_thread
    if _geocode_thread and _geocode_thread.is_alive():
        return
    _geocode_stop.clear()
    _geocode_thread = threading.Thread(
        target=_geocode_loop, daemon=True, name='geocode'
    )
    _geocode_thread.start()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/deals/<int:deal_id>')
def api_deal_by_id(deal_id):
    """Get a single deal by its ID."""
    deal = db.get_deal_by_id(deal_id)
    if deal is None:
        return jsonify({'error': 'Deal not found'}), 404
    return jsonify(deal)


def _apply_plz_radius_filter(deals: list[dict],
                             plz: str | None,
                             radius_km: float) -> list[dict]:
    """Filter a deals list by PLZ + radius (Iter. 24).

    Deals without geocoded coords pass through unfiltered — covers online
    shops (Mindfactory etc.) and classifieds whose location hasn't been
    geocoded yet, so the filter never silently hides anything.

    Adds a 'distance_km' field (rounded float, or None) to each kept deal."""
    if not plz or radius_km <= 0:
        return deals
    user_coords = geocode(plz)
    if not user_coords:
        logger.warning('PLZ filter: could not geocode %r', plz)
        return deals
    from geocoder import haversine_km
    u_lat, u_lon = user_coords
    kept = []
    for d in deals:
        lat, lon = d.get('lat'), d.get('lon')
        if lat is None or lon is None:
            d['distance_km'] = None
            kept.append(d)
            continue
        dist = haversine_km(u_lat, u_lon, lat, lon)
        if dist <= radius_km:
            d['distance_km'] = round(dist, 1)
            kept.append(d)
    return kept


def _read_plz_radius_args() -> tuple[str, float]:
    plz = (request.args.get('plz') or '').strip()
    try:
        radius_km = float(request.args.get('radius_km') or 0)
    except ValueError:
        radius_km = 0.0
    return plz, radius_km


@app.route('/api/deals')
def api_deals():
    try:
        filters = {
            'available_only': request.args.get('available_only') == '1',
            'favorite_only':  request.args.get('favorite_only')  == '1',
            'website':        request.args.get('website')  or None,
            'model':          request.args.get('model')    or None,
            'search':         request.args.get('search')   or None,
            'sort':           request.args.get('sort',  'found_at'),
            'order':          request.args.get('order', 'DESC'),
            'min_price': float(request.args['min_price']) if request.args.get('min_price') else None,
            'max_price': float(request.args['max_price']) if request.args.get('max_price') else None,
        }
        deals = db.get_all_deals(filters)
        plz, radius_km = _read_plz_radius_args()
        deals = _apply_plz_radius_filter(deals, plz, radius_km)
        return jsonify(deals)
    except Exception as e:
        logger.exception('api_deals error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings/filter', methods=['GET'])
def api_get_filter_settings():
    """Return persisted PLZ-Umkreis-Filter settings."""
    return jsonify({
        'plz':       db.get_setting('filter_plz', '') or '',
        'radius_km': int(db.get_setting('filter_radius_km', '0') or 0),
    })


@app.route('/api/settings/filter', methods=['POST'])
def api_save_filter_settings():
    data = request.get_json(force=True, silent=True) or {}
    plz = str(data.get('plz', '')).strip()
    try:
        radius_km = max(0, int(data.get('radius_km', 0)))
    except (ValueError, TypeError):
        radius_km = 0
    db.set_setting('filter_plz',       plz)
    db.set_setting('filter_radius_km', str(radius_km))
    # Warm the geocode cache so the first filtered request is fast.
    if plz and radius_km > 0:
        geocode(plz)
    return jsonify({'ok': True, 'plz': plz, 'radius_km': radius_km})


@app.route('/api/scrape', methods=['POST'])
def api_scrape():
    if scraper.STATUS['scraping']:
        return jsonify({'status': 'already_running'})
    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    return jsonify({'status': 'started'})


@app.route('/api/scrape/<int:target_id>', methods=['POST'])
def api_scrape_target(target_id: int):
    if scraper.STATUS['scraping']:
        return jsonify({'status': 'already_running'})
    targets = [t for t in db.get_targets() if t['id'] == target_id]
    if not targets:
        return jsonify({'error': 'target not found'}), 404
    t = threading.Thread(target=_do_scrape, args=(targets[0],), daemon=True)
    t.start()
    return jsonify({'status': 'started', 'target': targets[0]['name']})


@app.route('/api/scrape/group/<path:group_name>', methods=['POST'])
def api_scrape_group(group_name: str):
    """Scrape every active target in a group with a single click.

    Saves the user from clicking "Aktualisieren" on each product card in a
    group separately (Felix: "ich muss immer einzelne produkte aktualisieren").
    """
    if scraper.STATUS['scraping']:
        return jsonify({'status': 'already_running'})
    group_targets = [t for t in db.get_targets()
                     if t.get('active') and (t.get('group_name') or '') == group_name]
    if not group_targets:
        return jsonify({'error': f'Keine aktiven Targets in Gruppe „{group_name}"'}), 404
    th = threading.Thread(
        target=_do_scrape,
        kwargs={'targets': group_targets, 'label': f'group: {group_name}'},
        daemon=True,
    )
    th.start()
    return jsonify({
        'status':  'started',
        'group':   group_name,
        'targets': [t['name'] for t in group_targets],
    })


@app.route('/api/status')
def api_status():
    return jsonify({**scraper.STATUS, 'interval_minutes': scrape_interval_minutes})


@app.route('/api/stats')
def api_stats():
    return jsonify(db.get_stats())


@app.route('/api/favorite/<int:deal_id>', methods=['POST'])
def api_toggle_favorite(deal_id: int):
    db.toggle_favorite(deal_id)
    return jsonify({'ok': True})


@app.route('/api/alerts', methods=['GET'])
def api_get_alerts():
    return jsonify(db.get_alerts())


@app.route('/api/alerts', methods=['POST'])
def api_save_alert():
    data = request.get_json(force=True)
    db.save_alert(data['model'], float(data['threshold']))
    return jsonify({'ok': True})


@app.route('/api/alerts/<int:alert_id>', methods=['DELETE'])
def api_delete_alert(alert_id: int):
    db.delete_alert(alert_id)
    return jsonify({'ok': True})


@app.route('/api/alert-log')
def api_alert_log():
    return jsonify(db.get_alert_log())


@app.route('/api/price-history/<int:deal_id>')
def api_price_history(deal_id: int):
    return jsonify(db.get_price_history(deal_id))


@app.route('/api/interval', methods=['POST'])
def api_set_interval():
    """Set auto-scrape interval in minutes. 0 disables auto-scrape, otherwise min. 5."""
    global scrape_interval_minutes
    data = request.get_json(force=True)
    raw = int(data.get('minutes', 240))
    scrape_interval_minutes = 0 if raw <= 0 else max(5, raw)
    db.set_setting('scrape_interval_minutes', str(scrape_interval_minutes))
    _schedule_next()
    return jsonify({'ok': True, 'interval': scrape_interval_minutes})


@app.route('/api/targets', methods=['GET'])
def api_get_targets():
    return jsonify(db.get_targets())


@app.route('/api/targets', methods=['POST'])
def api_add_target():
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'error': 'Ungültige JSON-Anfrage'}), 400
        name    = (data.get('name',    '') or '').strip()
        keyword = (data.get('keyword', '') or '').strip()
        if not name or not keyword:
            return jsonify({'error': 'Name und Keyword erforderlich'}), 400
        group_name   = (data.get('group_name', '') or '').strip() or None
        retail_price = data.get('retail_price')
        try:
            retail_price = float(retail_price) if retail_price not in (None, '', 'null') else None
        except (TypeError, ValueError):
            retail_price = None
        target_id = db.add_target(name, keyword, group_name=group_name, retail_price=retail_price)
        return jsonify({'ok': True, 'id': target_id})
    except Exception as e:
        logger.exception('api_add_target error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/targets/<int:target_id>', methods=['DELETE'])
def api_delete_target(target_id: int):
    db.delete_target(target_id)
    return jsonify({'ok': True})


@app.route('/api/targets/<int:target_id>/toggle', methods=['POST'])
def api_toggle_target(target_id: int):
    db.toggle_target(target_id)
    return jsonify({'ok': True})


@app.route('/api/targets/<int:target_id>', methods=['PATCH'])
def api_update_target(target_id: int):
    """Partial update: group_name, retail_price, and/or min_price."""
    data = request.get_json(force=True, silent=True) or {}
    kwargs = {}
    if 'group_name' in data:
        kwargs['group_name'] = (data.get('group_name') or '').strip() or None
    for fld in ('retail_price', 'min_price'):
        if fld in data:
            raw = data.get(fld)
            try:
                kwargs[fld] = float(raw) if raw not in (None, '', 'null') else None
            except (TypeError, ValueError):
                kwargs[fld] = None
    if kwargs:
        db.update_target(target_id, **kwargs)
    return jsonify({'ok': True})


# ── Block-feature endpoints ──────────────────────────────────────────────────

@app.route('/api/deals/<int:deal_id>/block', methods=['POST'])
def api_block_deal(deal_id: int):
    """Hide this single deal from all panels. Reversible via unblock."""
    db.set_deal_blocked(deal_id, True)
    return jsonify({'ok': True})


@app.route('/api/deals/<int:deal_id>/unblock', methods=['POST'])
def api_unblock_deal(deal_id: int):
    db.set_deal_blocked(deal_id, False)
    return jsonify({'ok': True})


@app.route('/api/deals/<int:deal_id>/refresh', methods=['POST'])
def api_refresh_deal(deal_id: int):
    """Re-fetch a single eBay item right now and write the latest price as a
    new price_history snapshot. Used by the auction modal's "Jetzt aktualisieren"
    button so the user can poll a hot auction without waiting for the global
    scrape interval.

    Always writes a snapshot if the fetch succeeded, even when the price is
    unchanged — manual refresh is an explicit user request to capture the
    current state, so giving them no visible feedback (no new chart point)
    would feel like the button is broken.
    """
    deal = db.get_deal_by_id(deal_id)
    if not deal:
        return jsonify({'error': 'Deal nicht gefunden'}), 404
    if deal['website'] != 'eBay':
        return jsonify({'error': 'Live-Refresh nur für eBay-Auktionen'}), 400
    fresh = scraper.refresh_ebay_item(deal['url'])
    if fresh is None:
        return jsonify({'error': 'eBay-Seite nicht erreichbar'}), 502

    # Build a partial-update dict that keeps existing fields. fresh now contains
    # ONLY keys we successfully parsed (no Nones), so .update() is safe.
    merged = dict(deal)
    merged.update(fresh)

    # Preserve auction status: refresh_ebay_item only sets listing_type when it
    # positively detected bids. If it didn't, we keep whatever was already on
    # the deal so an auction doesn't flip to 'fixed' just because the bid-count
    # selector was stale.
    if 'listing_type' not in fresh and deal.get('listing_type'):
        merged['listing_type'] = deal['listing_type']

    db.insert_or_update_deal(merged)

    is_auction      = merged.get('listing_type') == 'auction'
    price_unchanged = (deal.get('price') == merged.get('price'))

    # AUTHORITATIVE PATH for auctions: eBay's public bid-history page
    # (/bfl/viewbids/<id>) returns the full timeline of every increment with
    # exact timestamps. When available, replace our sampled snapshots
    # entirely — they're a strict subset of this data.
    bid_count_imported = 0
    if is_auction:
        try:
            bids = scraper.scrape_ebay_bid_history(deal['url'])
            if bids:
                bid_count_imported = db.replace_price_history(deal_id, bids)
                logger.info(f'manual refresh: imported {bid_count_imported} bids for deal {deal_id}')
        except Exception as e:
            logger.warning('manual bid-history fetch failed for %s: %s', deal_id, e)

    # FALLBACK for fixed-price (or when bid-history was empty): insert_or_update_deal
    # already snapshots on every call for auctions and on price change for fixed.
    # For a MANUAL refresh on a fixed-price item with unchanged price, force one
    # snapshot so the user sees feedback that the click did something.
    if (not is_auction) and price_unchanged and merged.get('price') is not None:
        conn = None
        try:
            from datetime import datetime as _dt
            conn = db.get_connection()
            conn.execute(
                '''INSERT INTO price_history
                   (deal_id, price, changed_at, source) VALUES (?, ?, ?, ?)''',
                (deal_id, merged['price'], _dt.now().isoformat(), 'snapshot'),
            )
            conn.commit()
        except Exception as e:
            logger.warning('manual snapshot write failed: %s', e)
        finally:
            if conn is not None:
                conn.close()

    refreshed = db.get_deal_by_id(deal_id)
    return jsonify({'ok': True, 'deal': refreshed,
                    'bids_imported': bid_count_imported,
                    'bid_history_blocked': bool(is_auction and bid_count_imported == 0)})


@app.route('/api/deals/<int:deal_id>/bid-history/import', methods=['POST'])
def api_import_bid_history(deal_id: int):
    """Import eBay's bid-history table from pasted HTML or copied table text.

    eBay often protects /bfl/viewbids behind the user's logged-in browser
    session. The backend cannot always fetch that page directly, so the modal
    lets the user copy the visible bid table and import the exact rows here.
    """
    deal = db.get_deal_by_id(deal_id)
    if not deal:
        return jsonify({'error': 'Deal nicht gefunden'}), 404
    if deal['website'] != 'eBay':
        return jsonify({'error': 'Gebotsimport nur für eBay-Auktionen'}), 400
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get('html') or data.get('text') or ''
    if not raw.strip():
        return jsonify({'error': 'Leerer Text empfangen. Bitte die eBay-Gebotstabelle markieren und kopieren.'}), 400
    logger.info(f'bid-history import: received {len(raw)} chars for deal {deal_id}')
    logger.debug(f'bid-history import text (first 500 chars): {raw[:500]!r}')
    bids = scraper.parse_ebay_bid_history(raw)
    if not bids:
        # Save the raw text for debugging
        try:
            import os
            debug_dir = os.path.join(os.path.dirname(__file__), 'debug_html')
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, f'bid_import_fail_{deal_id}.txt'), 'w', encoding='utf-8') as f:
                f.write(raw)
            logger.info(f'bid-history import: saved failed import text to debug_html/bid_import_fail_{deal_id}.txt')
        except Exception:
            pass
        return jsonify({'error': 'Keine Gebote erkannt. Bitte die eBay-Gebotstabelle kopieren.'}), 400
    imported = db.replace_price_history(deal_id, bids)
    conn = None
    try:
        highest = max(b['price'] for b in bids if b.get('price') is not None)
        conn = db.get_connection()
        conn.execute('UPDATE deals SET price = ?, last_seen = ? WHERE id = ?',
                     (highest, datetime.now().isoformat(), deal_id))
        conn.commit()
    except Exception as e:
        logger.warning('bid-history deal price update failed for %s: %s', deal_id, e)
    finally:
        if conn is not None:
            conn.close()
    refreshed = db.get_deal_by_id(deal_id)
    return jsonify({'ok': True, 'deal': refreshed, 'bids_imported': imported})


@app.route('/api/sellers/block', methods=['POST'])
def api_block_seller():
    """Block every current and future deal from this seller on this website."""
    data = request.get_json(force=True, silent=True) or {}
    website = (data.get('website') or '').strip()
    seller  = (data.get('seller')  or '').strip()
    if not website or not seller:
        return jsonify({'error': 'website + seller required'}), 400
    db.block_seller(website, seller)
    return jsonify({'ok': True})


@app.route('/api/sellers/unblock', methods=['POST'])
def api_unblock_seller():
    data = request.get_json(force=True, silent=True) or {}
    website = (data.get('website') or '').strip()
    seller  = (data.get('seller')  or '').strip()
    if not website or not seller:
        return jsonify({'error': 'website + seller required'}), 400
    db.unblock_seller(website, seller)
    return jsonify({'ok': True})


@app.route('/api/blocked')
def api_get_blocked():
    """Combined view: blocked sellers + manually-blocked individual deals."""
    return jsonify({
        'sellers': db.get_blocked_sellers(),
        'deals':   db.get_blocked_deals(),
    })


@app.route('/api/groups')
def api_get_groups():
    """Distinct group names from active targets."""
    targets = db.get_targets()
    seen, groups = set(), []
    for t in targets:
        g = (t.get('group_name') or '').strip()
        if g and g not in seen:
            seen.add(g)
            groups.append(g)
    return jsonify(groups)


@app.route('/api/sources')
def api_get_sources():
    """All available scraping sources with metadata."""
    return jsonify(db.ALL_SOURCES)


@app.route('/api/groups/<string:group_name>/sources', methods=['GET'])
def api_get_group_sources(group_name: str):
    """Return the list of allowed sources for a group.
    Empty list = no restriction (all sources used)."""
    return jsonify(db.get_group_sources(group_name))


@app.route('/api/groups/<string:group_name>/sources', methods=['PUT'])
def api_set_group_sources(group_name: str):
    """Replace the source list for a group.
    Body: { "sources": ["Kleinanzeigen", "eBay", ...] }
    Send empty list to remove restrictions."""
    data = request.get_json(force=True, silent=True) or {}
    sources = data.get('sources', [])
    if not isinstance(sources, list):
        return jsonify({'error': 'sources must be a list'}), 400
    db.set_group_sources(group_name, sources)
    return jsonify({'ok': True, 'group': group_name, 'sources': sources})


@app.route('/api/dashboard')
def api_dashboard():
    targets = db.get_targets()
    # Bulk-fetch sources per group ONCE so we don't call get_group_sources()
    # per target (would be N+1 round-trips). Frontend reads sources off
    # each target — without this field the group badge always shows
    # "alle Quellen" no matter what the user saved.
    group_sources_cache: dict[str, list[str]] = {}
    result = []
    for t in targets:
        group = t.get('group_name')
        if group and group not in group_sources_cache:
            try:
                group_sources_cache[group] = db.get_group_sources(group)
            except Exception:
                group_sources_cache[group] = []
        result.append({
            'id':           t['id'],
            'name':         t['name'],
            'keyword':      t['keyword'],
            'active':       t['active'],
            'group_name':   group,
            'retail_price': t.get('retail_price'),
            'min_price':    t.get('min_price'),
            'apple_price':  t.get('apple_price'),
            'sources':      group_sources_cache.get(group, []) if group else [],
            'stats':        db.get_target_summary(t['name']),
            'top_deals':    db.get_top_deals(t['name']) if t['active'] else [],
        })
    return jsonify(result)


# Domain allowlist for image proxy — prevents SSRF by only
# fetching images from known e-commerce / CDN domains.
_IMAGE_PROXY_ALLOWED_DOMAINS = {
    'i.ebayimg.com', 'img.kleinanzeigen.de', 'img.classistatic.de',
    'images.otto.de', 'i.otto.de', 'www.otto.de',
    'assets.mmsrg.com', 'images.mediamarkt.de',
    'www.idealo.de', 'cdn.idealo.com',
    'images-na.ssl-images-amazon.com', 'm.media-amazon.com',
    'www.notebooksbilliger.de', 'nb.img',
    'www.cyberport.de', 'images.cyberport.de',
    'www.gravis.de', 'www.alternate.de',
    'www.mindfactory.de', 'images.mindfactory.de',
    'www.conrad.de', 'asset.conrad.com',
    'store.storeimages.cdn-apple.com', 'www.apple.com',
    'www.backmarket.de', 'images.backmarket.com',
    'www.rebuy.de', 'www.refurbed.de',
    'www.kaufland.de', 'media.kaufland.de',
    'img.markt.de', 'www.markt.de',
    'img.quoka.de', 'www.quoka.de',
    'www.mac-store24.com', 'www.future-x.de',
    'images.asgoodasnew.com', 'www.asgoodasnew.de',
    'www.jacob.de',
}


@app.route('/api/image-proxy')
def image_proxy():
    url = request.args.get('url', '')
    if not url or not url.startswith('http'):
        return '', 400
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ''
        # SSRF protection: only allow known e-commerce image domains
        if not any(hostname == d or hostname.endswith('.' + d)
                   for d in _IMAGE_PROXY_ALLOWED_DOMAINS):
            logger.warning(f'Image proxy blocked domain: {hostname}')
            return '', 403
        referer = f'{parsed.scheme}://{parsed.netloc}/'
        r = requests.get(url, headers={
            'User-Agent': scraper.HEADERS['User-Agent'],
            'Referer':    referer,
            'Accept':     'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        }, timeout=8, allow_redirects=False)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', 'image/jpeg')
        if 'image' not in ct:
            return '', 404
        resp = Response(r.content, mimetype=ct)
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    except Exception:
        return '', 404


@app.route('/api/top-deals')
def api_top_deals():
    deals = db.get_top_deal_per_group()
    plz, radius_km = _read_plz_radius_args()
    return jsonify(_apply_plz_radius_filter(deals, plz, radius_km))


@app.route('/api/export/csv')
def api_export_csv():
    deals  = db.get_all_deals()
    fields = ['id', 'title', 'price', 'url', 'website', 'model',
              'ram', 'ssd', 'found_at', 'available']
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(deals)
    output.seek(0)
    filename = f'deals_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    return Response(
        '﻿' + output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


def _load_persisted_interval():
    """Load scrape_interval_minutes from DB settings (call after init_db)."""
    global scrape_interval_minutes
    try:
        val = db.get_setting('scrape_interval_minutes', '240')
        scrape_interval_minutes = int(val or '240')
    except Exception:
        scrape_interval_minutes = 240


# Background auction-refresh starts as soon as the app module is imported
# (i.e. from main.py too) — daemon thread dies with the process.
_start_auction_refresh()
_start_geocode_thread()


if __name__ == '__main__':
    # Direct run (no main.py) — must init DB ourselves
    db.init_db()
    _load_persisted_interval()
    t = threading.Thread(target=_do_scrape, daemon=True)
    t.start()
    app.run(host='127.0.0.1', port=5001, debug=False, use_reloader=False)
