"""eBay login session via Playwright storage_state (Iter. 25).

eBay's /bfl/viewbids/ page started requiring login around 2026-05.
Workaround: user logs in ONCE via an interactive browser window, we
persist cookies + localStorage to ebay_session.json, and subsequent
eBay-scrape calls load that storage_state so the bid-history endpoint
returns real data instead of a sign-in wall.

The login flow blocks the calling thread — call start_login_flow_async()
to spawn it in the background so HTTP handlers return immediately.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(BASE_DIR, 'ebay_session.json')

# Tracks the most recent login attempt so the UI can poll status.
_state_lock = threading.Lock()
_login_state: dict[str, Any] = {
    'in_progress': False,
    'last_result': None,        # 'ok' | 'error' | None
    'last_message': '',
    'last_finished_at': None,   # epoch seconds
}


def has_session() -> bool:
    """True if a session file exists on disk. Does NOT validate liveness —
    eBay sessions expire after weeks of inactivity; caller must handle
    the case where stored cookies are no longer accepted."""
    return os.path.isfile(SESSION_PATH)


def session_path_for_playwright() -> str | None:
    """Return the path to pass to browser.new_context(storage_state=...).
    Returns None if no session is stored."""
    return SESSION_PATH if has_session() else None


def delete_session() -> bool:
    if has_session():
        try:
            os.remove(SESSION_PATH)
            return True
        except OSError as exc:
            logger.warning('Konnte ebay_session.json nicht loeschen: %s', exc)
    return False


def get_login_status() -> dict[str, Any]:
    """Snapshot of current login flow state — used by /api/ebay-session/status."""
    with _state_lock:
        return {
            'has_session':      has_session(),
            'in_progress':      _login_state['in_progress'],
            'last_result':      _login_state['last_result'],
            'last_message':     _login_state['last_message'],
            'last_finished_at': _login_state['last_finished_at'],
        }


def _set_state(**kwargs) -> None:
    with _state_lock:
        _login_state.update(kwargs)


def _run_login_flow() -> None:
    """Blocking implementation — open a real browser window, let the user
    log in, save storage_state once the page navigates away from /signin."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_state(
            in_progress=False, last_result='error',
            last_message='Playwright nicht installiert',
            last_finished_at=time.time(),
        )
        return

    try:
        with sync_playwright() as pw:
            # Non-headless so the user can actually log in.
            browser = pw.chromium.launch(headless=False, args=['--start-maximized'])
            ctx = browser.new_context(no_viewport=True, locale='de-DE')
            page = ctx.new_page()
            page.goto('https://www.ebay.de/signin/', wait_until='domcontentloaded')
            logger.info('eBay signin window opened — waiting for user to log in (max 5 min)...')

            # eBay redirects away from /signin once login succeeds. We allow
            # up to 5 minutes; if the user cancels we land here on a Timeout.
            # Case-insensitive: eBay sometimes uses /SignIn / /Signin variants.
            try:
                page.wait_for_url(
                    lambda u: ('/signin' not in u.lower()
                               and 'login' not in u.lower()
                               and 'identityweb' not in u.lower()),
                    timeout=300_000,
                )
            except Exception:
                _set_state(
                    in_progress=False, last_result='error',
                    last_message='Login-Timeout (5 min). Fenster wurde geschlossen oder Login nicht abgeschlossen.',
                    last_finished_at=time.time(),
                )
                try: browser.close()
                except Exception: pass
                return

            # Verify by hitting an authenticated page — Mein eBay redirects
            # unauthenticated users back to /signin.
            try:
                page.goto('https://www.ebay.de/mye/myebay/summary',
                          wait_until='domcontentloaded', timeout=15_000)
            except Exception as exc:
                logger.warning('Login validation page load failed: %s', exc)

            current_url = page.url.lower()
            if '/signin' in current_url or 'login' in current_url:
                _set_state(
                    in_progress=False, last_result='error',
                    last_message='Login nicht erkannt — bitte nochmal versuchen.',
                    last_finished_at=time.time(),
                )
                try: browser.close()
                except Exception: pass
                return

            # Persist
            ctx.storage_state(path=SESSION_PATH)
            try: browser.close()
            except Exception: pass

            # Iter. 26: Reset bid-history cooldown — a fresh login means we
            # should immediately retry, even if the previous unauthenticated
            # attempts had triggered the 30-min Akamai cooldown.
            try:
                import scraper as _scr
                _scr._EBAY_BLOCKED_UNTIL = 0
                logger.info('bid-history cooldown reset after login save')
            except Exception:
                pass

            _set_state(
                in_progress=False, last_result='ok',
                last_message='Login gespeichert. Gebot-Auto-Import ist jetzt aktiv.',
                last_finished_at=time.time(),
            )
            logger.info('eBay session saved to %s', SESSION_PATH)
    except Exception as exc:
        logger.exception('eBay login flow crashed')
        _set_state(
            in_progress=False, last_result='error',
            last_message=f'Fehler: {exc}',
            last_finished_at=time.time(),
        )


def start_login_flow_async() -> dict[str, Any]:
    """Spawn the login flow in a background thread and return immediately.
    Returns the dict that should be JSON-serialised back to the client."""
    with _state_lock:
        if _login_state['in_progress']:
            return {'status': 'already_running',
                    'message': 'Login-Flow laeuft bereits — Browser-Fenster pruefen'}
        _login_state.update({
            'in_progress':      True,
            'last_result':      None,
            'last_message':     '',
            'last_finished_at': None,
        })

    t = threading.Thread(target=_run_login_flow, daemon=True, name='ebay-login')
    t.start()
    return {'status': 'started',
            'message': 'Browser-Fenster oeffnet sich. Logge dich bei eBay ein.'}
