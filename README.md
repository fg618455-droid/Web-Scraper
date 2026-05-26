# Deal Tracker

Desktop-App die 19+ Quellen (eBay, Kleinanzeigen, mac-store24, Apple, Amazon, Backmarket, Kaufland, Mindfactory, Saturn, …) im Hintergrund nach Apple-, iPhone- und Smartwatch-Deals scannt, Auktionen live verfolgt und unter dem Apple-Referenzpreis liegende Angebote sofort meldet.

![Screenshot](https://img.shields.io/badge/Platform-Windows-blue)
![Python](https://img.shields.io/badge/Python-3.14-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Was die App macht

- **19+ Shops gleichzeitig scrapen** — parallel via Playwright-Stealth-Browser, Anti-Bot-fest. Resultate landen in einer lokalen SQLite-DB.
- **Auktionen live tracken** — eBay-Auktionen mit Step-Chart, Live-Bid-Counter, Ende-Marker und PT-Cooldown. Akamai-Bypass via off-screen Persistent-Chromium.
- **Preis-Alarme** — Schwellwerte pro Modell, Windows-Toast sobald ein Deal die Grenze unterschreitet.
- **PLZ-Radius-Filter** — Kleinanzeigen nur innerhalb X km um deine PLZ (Nominatim-Geocoding, lokal gecached).
- **System-Tray** — App läuft im Hintergrund weiter wenn das Fenster zu ist. Tray-Menü öffnet Mini-Status-Fenster oder triggert Scrape ad-hoc.
- **In-Place-Auto-Update** — beim Start prüft die App GitHub Releases, lädt + entpackt + ersetzt sich selbst auf Wunsch. Deine `deals.db` bleibt unangetastet (`%APPDATA%\DealScraper\`).

## Schnellstart

### Endnutzer (Windows)

1. Aktuelle Release-`.zip` von [Releases](https://github.com/fg618455-droid/Web-Scraper/releases/latest) laden
2. Entpacken (z.B. nach `Downloads\DealScraper`)
3. `DealScraper.exe` starten — Flask-Backend + Chrome-`--app`-Fenster gehen automatisch auf

Künftige Updates: in der App auf den Update-Knopf klicken — die App ersetzt sich selbst.

### Entwicklung (Source-Mode)

```bash
git clone https://github.com/fg618455-droid/Web-Scraper.git
cd Web-Scraper
setup.bat           # installiert Python-Deps + Playwright-Chromium
python main.py
```

## Architektur

```text
main.py            Bootstrap — Flask-Thread + Chrome --app + System-Tray
├─ app.py          Flask-Routes + Background-Worker (auction-refresh, geocode)
├─ scraper.py      19+ Quellen-Scraper, Akamai-Bypass (Persistent + CDP)
├─ database.py     SQLite-Schema (deals, alerts, search_targets, price_history, geocode_cache)
├─ tray.py         pystray System-Tray-Menü
├─ updater.py      GitHub-Releases In-Place-Updater
├─ ebay_session.py eBay-Login-Cookie-Management (für /bfl/viewbids/ Imports)
├─ geocoder.py     Nominatim mit lokalem Cache
├─ notifier.py     plyer Windows-Toasts
└─ paths.py        User-Daten-Pfade (%APPDATA% frozen / Repo dev)
```

### Daten

| Pfad                                          | Inhalt                                                              |
| --------------------------------------------- | ------------------------------------------------------------------- |
| `%APPDATA%\DealScraper\deals.db`              | Deals, Suchaufträge, Preisverläufe, Alarme, Geocodes                |
| `%APPDATA%\DealScraper\ebay_session.json`     | eBay-Login-Cookies (Klartext-JSON, nicht DPAPI-verschlüsselt)       |
| `%LOCALAPPDATA%\DealScraper\ScraperProfile\`  | Off-screen Chromium-Profil für Akamai-Bypass                        |

Pfade überleben App-Updates — Installations-Ordner kann ohne Datenverlust gelöscht werden.

## Konfiguration

`config.json` neben der `.exe` (optional):

```json
{
  "host": "127.0.0.1",
  "port": 5001,
  "cdp_port": 9222
}
```

`host: "0.0.0.0"` macht die App im LAN sichtbar (Zugriff vom Handy via `http://<PC-IP>:5001`).

## Diagnose

| Endpoint                    | Was er liefert                                                  |
| --------------------------- | --------------------------------------------------------------- |
| `/api/health`               | Lebt Flask?                                                     |
| `/api/status`               | Läuft gerade ein Scrape? Letzter Scrape wann? Status pro Quelle |
| `/api/debug/scraper-state`  | Akamai-Bypass-Status: Persistent/CDP/Login + Profile-Pfad       |

## Tech-Stack

- **Python 3.14**
- **Flask** — Backend + lokales API-UI
- **Playwright** (Chromium) — Stealth-Browser für Anti-Bot-Shops + Akamai-Bypass
- **BeautifulSoup4 / lxml** — HTML-Parsing
- **SQLite** — lokale persistente Speicherung
- **pystray + Pillow** — Windows-Tray-Icon
- **plyer** — native Desktop-Notifications
- **PyInstaller** — Frozen-`.exe`-Build via GitHub-Actions

## Build & Release

Push eines Tags `v*` triggert [`build-release.yml`](.github/workflows/build-release.yml):
PyInstaller baut die `.exe`, packt sie als `DealScraper-v<N>-windows-x64.zip` und hängt sie ans GitHub-Release. Der In-Place-Updater in der laufenden App findet das Asset über die GitHub-API automatisch.

## Lizenz

MIT
