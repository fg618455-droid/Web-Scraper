# Prompt für nächsten Chat — Deal Tracker Iter. 37 (Autonom-Modus, bis zu 9h)

> Felix lässt seinen PC über Nacht an. **Du hast bis zu 9 Stunden** und sollst alle Aufgaben **selbständig** abarbeiten. **Keine Rückfragen.** Wenn ein Schritt nicht zu 100% bestimmbar ist, triff eine begründete Entscheidung und arbeite weiter.

---

# Deal Tracker — Iter. 37: Bot-Bypass-Welle für die blockierten Sites + Stufe-C-Polish

## 📚 Pflicht-Lesen ZUERST (~5 min)

Memory-Vault `C:\Users\User\.claude\projects\c--Users-User-CodingProjekte-Web-scraper\memory\` in dieser Reihenfolge:

1. `project_iter_36_queue_progress_sources.md` — was Iter. 36 (gestern Nacht) gebaut hat: Queue, Live-Banner, eligible-Flag, 48 Sites in ALL_SOURCES, Status-Pille-Button, Site-Tuning (MediaMarkt/Saturn/Uhrinstinkt/Uhrzeit.org/ReBuy). Inkl. **Liste der bot-walled Sites** die für Iter. 37 offen sind.
2. `project_iter_35_frameless_titlebar.md` — Drag-Region-Klasse, frameless+js_api, Custom-Title-Bar Setup
3. `project_iter_34_pywebview_native.md` — pywebview Setup, why no Chrome --app
4. `project_iter_30_akamai_bypass_solved.md` — **WICHTIG**: persistent-Chromium-Pattern für eBay/Akamai. Dieses Pattern erweiterst du in Iter. 37 auf andere Sites.
5. `feedback_app_does_it_itself.md` — App soll's selbst machen. Kein „du musst X machen"
6. `user_preferences.md` — compact/minimal, dark, German

**Keine Code-Files lesen bevor diese Memory-Files durch sind.**

## 🎯 Plan (in dieser Reihenfolge abarbeiten)

### Phase 1 — Akamai/DataDome-Bypass auf weitere Sites ausweiten (2-3h)

Iter. 30 hat `fetch_ebay_via_persistent` gebaut: off-screen headed Chromium mit eigenem user-data-dir, kommt durch Akamai. Die gleiche Technik kann für **alle bot-walled Sites** in Iter. 36 funktionieren — die Anti-Bot-Walls sind meist Akamai/Cloudflare/DataDome und alle erkennen `playwright.chromium.launch(headless=True)` als bot, lassen aber persistent-headed durch.

**Sites die in Iter. 36 bot-walled waren** (alle Status='blocked' oder 'empty' mit „robot"/„captcha" in HTML):
- **Priorität A (Apple/Mac Targets):** Saturn, Coolblue, Galaxus, Computeruniverse, Cyberport, Gravis, notebooksbilliger, Idealo, Kaufland, Backmarket
- **Priorität B (Garmin/Sport Uhr):** Christ, Chrono24, Chronext, Valmano, Watchshop
- **Priorität C (Iphone):** eBay-Batch (klappt schon für Item-Detail via persistent), Cyberport, etc.

**Implementierungs-Plan:**

1. **Refactor `scrape_anti_bot_batch`** in `scraper.py`:
   - Aktuell: macht selbst `pw.chromium.launch(headless=True)` mit eigenem context
   - Neu: für Sites in einer `_PERSIST_SITES`-Allowlist nutzt sie `fetch_ebay_via_persistent`-Style persistent-context (existing helper `_ensure_persistent_context` / `fetch_ebay_via_persistent`)
   - Generalisiere `fetch_ebay_via_persistent` → `fetch_via_persistent(url)` (Site-agnostisch)
   - Threading-Lock bleibt (1 Browser, serialisiert)
   - Off-screen window-position bleibt (-3000,-3000)

2. **Per-Site-Allowlist** für persistent context:
   ```python
   _PERSIST_SITES = {
       'Saturn', 'Coolblue', 'Galaxus', 'Computeruniverse',
       'Cyberport', 'Gravis', 'notebooksbilliger', 'Idealo',
       'Kaufland', 'Backmarket', 'Christ', 'Chrono24',
       'Chronext', 'Valmano', 'Watchshop',
   }
   ```

3. **Verify-Loop** je Site:
   - Trigger Scrape via curl: `curl -s -X POST "http://127.0.0.1:5001/api/scrape/group/Computer%20Apple"`
   - Wait via `until [ "$(curl -s /api/status | jq -r .scraping)" = "false" ]; do sleep 5; done`
   - Check welche Sites von „blocked" zu „ok"/„empty" wechseln
   - Für jede Site die nicht von blocked → ok/empty wechselt: nochmal Debug-HTML angucken in `debug_html/<Site>_<Keyword>.html`

4. **Erfolg = mindestens 4 von 15 bot-walled Sites liefern danach Daten**. Realistisch: Saturn (gleiche DOM wie MediaMarkt → direkt parser-ready), Coolblue, Galaxus, Christ.

### Phase 2 — Site-Selektoren weiter tunen (1-2h)

Nachdem Phase 1 läuft, sind mehr Sites in `status='empty'` statt `'blocked'`. Das heißt: HTML ist da, aber Parser findet nichts.

**Analyse-Loop pro empty-Site:**
1. Trigger Scrape mit Garmin + MacBook Keywords
2. Schau in `debug_html/<Site>_<Keyword>.html`:
   ```bash
   grep -oE 'class="[^"]{0,80}(product|tile|card|box)[^"]{0,80}"' debug_html/<Site>.html | sort | uniq -c | sort -rn | head -10
   grep -oE 'data-[a-z-]*="[^"]{0,40}"' debug_html/<Site>.html | sort | uniq -c | sort -rn | head -10
   ```
3. Top-Selektor in `_site_specific_selectors`-Dict in `scrape_anti_bot_batch` eintragen (siehe scraper.py)
4. Restart App + neuen Scrape, prüfen ob status='ok' mit count>0

**Top-Sites für Phase 2:**
- Galaxus (.productmain?), Coolblue ([data-product-uuid]?), Jacob (article.box?), notebooksbilliger
- Christ — hat 0 Produkte aber `data-product-count` Attribut. Vermutlich `[data-product]` oder Shopware-Style
- Chrono24 — anders strukturiert, hochpreisige Uhren. `.article-info` o.ä.
- Watchshop — UK-Site, vermutlich ähnlich zu Frasers Group `[data-fp-tile]`
- Brandfield + Wardow + Liebeskind-Berlin: Shopify-Standard `[data-product-id]`, `.product-card`, `.grid__item`

### Phase 3 — Memory + Commit + Tag v37 + Push (30 min)

Wenn Phase 1+2 abgeschlossen (oder gut weitergekommen):

1. **Neue Memory-File** `project_iter_37_persist_expansion.md`:
   - Welche Sites erfolgreich auf persistent-context migriert
   - Welche Sites mit echten Selektoren liefern (count > 0)
   - Welche weiter blocked (DataDome braucht residential proxy, Backlog)
   - Per-Site Selektoren-Liste die du genutzt hast

2. **MEMORY.md-Index** mit Iter. 37 Eintrag ergänzen

3. **Commit + Tag + Push**:
   ```bash
   git add -A
   git commit -m "Iter. 37: Persistent-Context auf bot-walled Sites + Selektoren-Tuning Phase 2"
   git tag v37
   git push origin main --tags
   ```
   GitHub Actions baut die .exe.

### Phase 4 — Polish (wenn Zeit übrig, je 30 min)

In dieser Reihenfolge:

a) **Site-Health-Dashboard** im Settings-Drawer: 7-Tages-Erfolgsquote pro Site, sortiert nach Misserfolgs-Quote. Felix sieht welche Sites grade kaputt sind.
   - Backend: `/api/site-health` → `[{name, last_7d_ok_count, last_7d_total_count, status_today}]`
   - DB-Schema: `scrape_attempts` Tabelle mit `(site, ts, status, count)` schon vorhanden? Falls nicht: Migration einbauen + `_set_site_status` schreibt in beides
   - Frontend: neuer Drawer-Tab oder Modal mit Sortier-Liste

b) **deals.db-Cleanup**:
   - 7 alte Kopien in `c:/Users/User/Downloads/DealScraper-v*` (v25, v26, v27, v31)
   - Datei für Datei prüfen ob deals.db darin „verfügbares Material" hat
   - Wenn nichts Besonderes drin: löschen. Sonst in `Backups/` archivieren
   - Felix' Wunsch seit Iter. 28

c) **In-App-Update-Progress-Bar**:
   - Iter. 33 Updater zieht .exe unsichtbar via CREATE_NO_WINDOW
   - Felix' Wunsch: Progress-Bar in App während Update lädt
   - Implementierung: WebSocket oder polling-endpoint `/api/update/progress` während Download
   - Frontend: Toast mit Fortschrittsbalken

d) **websitenliste.md als UI im Settings-Drawer**: Felix kann pro Gruppe Sites aktivieren/deaktivieren ohne JSON. Existiert teilweise schon via `/api/groups/<g>/sources` PUT — nur das Frontend muss schöner sein.

### Phase 5 — Was du NICHT tun sollst

- **NIE** force-push auf main
- **NIE** ScrapeFly/ZenRows API-Keys eintragen ohne Felix-Approval (die kosten Geld)
- **NIE** Browser-Cookie-Klau ohne Backup-Mechanismus
- **NIE** an Group-Sources direkt in DB schreiben — immer via API `/api/groups/<g>/sources` PUT
- **NIE** den Git-Commit-Hook mit `--no-verify` umgehen
- **NIE** die laufende App killen ohne vorher zu checken ob Felix noch interagiert (er ist offline → ok)

## 🛠 Tech-Constraints (Stand 2026-05-27)

- Stack: Python 3.14, Flask, Playwright, SQLite, pywebview 6.2.1 + Edge-WebView2 v148
- App-Process: `python main.py` aus `c:/Users/User/CodingProjekte/Web scraper/`, Source-Mode
- DB-Pfad: `%APPDATA%\DealScraper\deals.db` (frozen) / Projekt-Wurzel (dev) — `paths.py`
- Scraper-Browser-Profile: `%LOCALAPPDATA%\DealScraper\ScraperProfile\`
- App-Start dauert ~1-2s bis Flask antwortet (`curl http://127.0.0.1:5001/api/health`)
- TEMPLATES_AUTO_RELOAD ist on, aber Python-Code-Änderungen brauchen Server-Restart
- Aktueller Stand: Iter. 36 lokal **wahrscheinlich committet** wenn ich's heut Nacht noch geschafft habe (siehe `git log --oneline -3`)

## 🚦 Workflow-Pattern (befolgen!)

1. **Vor jedem Code-Edit**: Read der Datei (verboten Edit ohne Read in dieser Session)
2. **Nach Edit**: Server-Restart wenn Python-Code betroffen
   ```bash
   # Standard-Pattern
   $c = Get-NetTCPConnection -LocalPort 5001 -State Listen; if ($c) { Stop-Process -Id $c.OwningProcess -Force }
   cd "c:/Users/User/CodingProjekte/Web scraper" && python main.py > app_iter37_runN.log 2>&1 &
   for i in 1 2 3 4 5; do out=$(curl -s -m 1 http://127.0.0.1:5001/api/health); if [ -n "$out" ]; then break; fi; sleep 1; done
   ```
3. **Verify-Pattern**: nach jedem Site-Tuning curl /api/status, schau ob die Site status='ok' mit count > 0 hat
4. **Background-Scrapes**: nutze `run_in_background: true` für lange Scrapes, dann arbeite parallel weiter
5. **debug_html ist Gold**: jede empty/blocked Site speichert ihre HTML in `debug_html/`. **Immer dort schauen**, niemals raten

## 🔁 Failure-Modes & wie du damit umgehst

1. **Persistent-Chromium startet nicht**:
   - Check `_persistent_available()` Status
   - Check `DEALSCRAPER_PROFILE_PATH` env var
   - Wenn nicht gesetzt: rufe `_setup_dealscraper_chrome_profile()` aus main.py manuell

2. **Site zeigt immer noch 'blocked' nach persistent-context**:
   - Akamai-Browser-Fingerprint zu schwach — schau ob `_PW_STEALTH_JS` (Iter. 30) in den persistent context geladen wird
   - Wenn ja: User-Agent rotieren oder Chrome-Version updaten
   - Wenn nein: Backlog

3. **Site liefert ok aber count=0**:
   - Parser-Selektor stale. Debug-HTML schauen, neue Selektoren. Mehr als 5 Tries pro Site → Backlog für Felix manuelle Inspektion

4. **Git-Hook failed beim Commit**:
   - Niemals --no-verify. Fix das Underlying-Problem. Hooks sind meistens Lint-Checks die durch Iter. 35 .hintrc deaktiviert sein sollten

5. **App crasht nach Restart**:
   - Schau `app_iter37_runN.log` für Stacktrace
   - Häufig: Import-Reihenfolge (paths.py, database.py, scraper.py — paths zuerst!)
   - Wenn unklar: revert letzten Edit, commit + push als Sicherheit

6. **Du bist nicht sicher ob ein Feature komplett ist**:
   - Default: Commit als „WIP: Iter. 37 — phase X" mit beschreibender Message, dann weiter
   - Felix sieht morgen die Commits und kann re-base/squash

## ✅ Success-Metriken am Ende der 9h

**Minimal:**
- [ ] Mindestens 4 weitere Sites von 'blocked' → 'ok' mit count>0
- [ ] Iter. 37 Memory geschrieben
- [ ] Commit + Tag v37 + Push (oder bei Unsicherheit: nur Commit, kein Tag, Felix taggt morgen)
- [ ] App läuft sauber nach finalem Restart

**Stretch:**
- [ ] 10+ Sites bot-walled → working
- [ ] Site-Health-Dashboard im Drawer
- [ ] deals.db-Cleanup gemacht
- [ ] Aktiv ge-runde Sources erweitert basierend auf welchen Sites jetzt liefern

**Wenn du fertig bist:**
- Schreib einen kurzen Status-Bericht ans Ende dieses File als Markdown-Block oder in ein neues `STATUS_ITER37_DONE.md`
- Felix liest das morgen früh als erstes

## 📋 Backlog (NIEMALS in Iter. 37 anfangen)

- ScrapeFly/ZenRows Trial (kostet, Felix-Approval nötig)
- DPAPI-Cookie-Klau (`/bfl/viewbids/`)
- Mobile-LAN-Zugang mit QR-Code
- iOS/Mac-Build (wartet auf Mac mini)
- `webview.start(menu=...)` Menüleiste

---

**Letzter Hinweis**: Felix' Mantra ist „die App macht's selbst". Vermeide jeden Lösungsansatz der von Felix einen Klick/Setup/Login erfordert. Wenn du auf solche Brücke stößt — versuche zuerst alle programmatischen Alternativen (CDP, Cookie-Import, browser-cookie3, persistent-context, Stealth-JS-Update).

**Du hast 9 Stunden. Sei mutig, dokumentier deine Entscheidungen in Commits, mach Felix am Morgen stolz.**
