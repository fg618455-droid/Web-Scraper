# Prompt für nächsten Chat — Deal Tracker Iter. 36

> Kopiere alles unterhalb der Trennlinie in einen frischen Claude-Code-Chat im Verzeichnis `c:\Users\User\CodingProjekte\Web scraper\`.

---

# Deal Tracker — Iter. 36: Verifikation + Site-Selektoren tunen + nächste Polish-Welle

## 📚 Lies ZUERST (Pflicht, nicht überspringen)

Im Memory-Vault `C:\Users\User\.claude\projects\c--Users-User-CodingProjekte-Web-scraper\memory\` diese Files in dieser Reihenfolge:

1. `project_iter_35_frameless_titlebar.md` — was Iter. 35 gebaut hat: DWM Dark Title-Bar, Frameless+Custom-Title-Bar, **CSS-Klasse `.pywebview-drag-region`** (nicht data-Attribut!), Taskbar-Icon-Fix, SetForegroundWindow, Per-Gruppe-UX-Fix, last_scrape-Hydratation, 48 neue Sites, Splash-Screen, TEMPLATES_AUTO_RELOAD. Plus die 7 Files aus `MEMORY.md`-Index — speziell `project_iter_34_pywebview_native.md`, `project_iter_30_akamai_bypass_solved.md`, `feedback_app_does_it_itself.md`, `user_preferences.md`.

**Lies KEINE Code-Files bevor du diese Memory-Files durch hast.**

## 🩸 Wo Iter. 35 aufgehört hat (Stand 2026-05-26)

**Was lokal im Source-Mode live wurde (aber noch NICHT von Felix visuell verifiziert / NICHT committet)**:

- ✅ DWM Dark Title-Bar via `DwmSetWindowAttribute` ctypes — verifiziert per Read-back `attr20=1` auf beiden Windows
- ✅ Frameless + Custom HTML Title-Bar mit `WindowAPI` js_api (Min/Max/Close) — Maximize-Icon-Toggle via JS `resize`-Listener
- ✅ **Drag-Region Fix**: `class="pywebview-drag-region"` (vorher fälschlicherweise `data-pywebview-drag-region` Attribut) — Felix muss noch live-verifizieren dass Drag jetzt geht
- ✅ Taskbar-Icon via `SetCurrentProcessExplicitAppUserModelID("fg618455.DealTracker.App")` + `WM_SETICON` ctypes
- ✅ Hauptfenster-Bring-To-Front via Win32 `SetForegroundWindow` (pywebview-`show()` un-hidet nur)
- ✅ Per-Gruppe-Scrape Overlay-Bug: `pollUntilDone({noOverlay: true})`
- ✅ `STATUS['last_scrape']` aus DB beim Import lesen (MAX(last_seen))
- ✅ Lint-Noise abgeschaltet: `.hintrc` + `.vscode/settings.json` + `.markdownlint.json` (auch im Obsidian-Vault)
- ✅ Garmin-Target in Source-DB neu angelegt (id=9, Sport Uhr)
- ✅ Stufe C — Splash-Screen während Flask-Bootstrap via `webview.create_window(html=SPLASH_HTML)` + `window.load_url(URL)` sobald `/api/health` antwortet
- ✅ `app.config['TEMPLATES_AUTO_RELOAD'] = True` — Template-Edits ohne App-Restart sichtbar
- ✅ **48 neue Webseiten** aus `websitenliste.md` in `scraper.py:_SITE_NAMES` + `scrape_anti_bot_batch.configs` via `_make_generic_parser` mit generic Card-Selektoren. `script.js:_ensureScrapePill()` legt fehlende Overlay-Pillen dynamisch an. 67 Sites total.

**Was NICHT committet ist** (Felix soll erst live verifizieren):
- Alle Code-Änderungen in `main.py`, `scraper.py`, `app.py`, `static/script.js`, `static/style.css`, `templates/index.html`, `templates/scrape_window.html`, `.hintrc`, `.vscode/settings.json`, `version.json` (26.0.0 → 34.1.0 als Dev-Quickfix gegen Update-Dialog-Loop), `PROMPT_ITER36.md`

## 🎯 Iter. 36 Ziele

### A — Verifikation (~10 min)

1. **App starten** via `python main.py` (oder die installierte v34.x.exe killen wenn auf Port 5001).
2. **Splash sichtbar** beim Start (~1-2s lila Tag-Icon + Spinner) → wechselt zu echtem UI
3. **Drag funktioniert** — Title-Bar mit Maus festhalten + bewegen verschiebt das Fenster
4. **Min/Max/Close-Buttons** klickbar — Maximize-Icon wechselt zwischen Quadrat ↔ Doppel-Quadrat
5. **Taskleisten-Icon** ist unser lila Tag-Icon, nicht Python-generisch
6. **„Hauptfenster"-Button** im Scrape-Window holt das Hauptfenster auch wenn's hinter anderen Apps liegt nach vorn
7. **Per-Gruppe-Scrape** zeigt nur Toast + Button-Spinner, kein globales 19/67-Quellen-Overlay mehr
8. **last_scrape**-Pille zeigt „fertig vor X" statt „Noch nicht gescrapt"
9. **Garmin Forerunner 265** sichtbar als Target in Sport-Uhr-Gruppe
10. **67 Sites in `/api/status`** (`curl http://127.0.0.1:5001/api/status | jq '.sites | length'`)

Wenn ALLE 10 grün: `git add -A && git commit -m "Iter. 35: frameless + custom title-bar + drag-fix + splash + 48 sites"` + `git tag v35` + `git push origin main --tags`.

### B — Site-Selektoren tunen (~1-2h)

Die 48 neuen Sites laufen mit `_make_generic_parser` und generischen Card-Selektoren. **Wahrscheinlich** liefern viele 0 Treffer (selectors stale) oder werden mit Anti-Bot blockiert. Realistisch:

- Mit Glück: 10-15 der neuen Sites liefern echte Deals direkt.
- Akamai-/Cloudflare-Sites (MediaMarkt, Saturn, Douglas, Notino, Zalando, Asos, Lidl): wahrscheinlich `status='blocked'`. Brauchen vielleicht residential proxy oder dedizierten Per-Site-Parser.
- Login-walled (BestSecret, evtl. Veepee): brauchen Storage-State wie eBay-Session.

**Vorgehen pro Site mit `status='empty'` oder 0-Treffer:**

1. `python play/scrape_one_site.py <SiteName> <keyword>` (Script schreiben falls noch nicht da) — startet einen sichtbaren Chromium, navigiert zur Search-URL, druckt die ersten 5 detected items mit Selektoren
2. DevTools/Right-Click → "Inspect" auf Produktkarten → echte Card-Selektoren herausfinden
3. Site-Entry in `scrape_anti_bot_batch._new_shops` → spezifischen Wait-Selector ergänzen (z.B. statt generic: `'[data-testid="product"]'`)
4. Live-Test, danach nächste Site

**Priorität nach Felix' Targets**:
- Apple/Mac → MediaMarkt, Saturn, Galaxus, Coolblue, Computeruniverse, Jacob, ReBuy
- Garmin Sport Uhr → Chrono24, Christ, Uhrzeit.org, Uhrinstinkt, Watchshop, Fossil, Skagen
- Parfum/Beauty/Fashion → low priority (keine Targets dafür)

### C — Polish-Backlog (wenn Zeit übrig)

- **`websitenliste.md` als markdown UI im Settings-Drawer** — Felix kann pro Gruppe Sites aktivieren/deaktivieren ohne JSON-Edit
- **Site-Health-Dashboard**: 7-Tages-Erfolgsquote pro Site, sortiert → Felix sieht welche Sites grade kaputt sind
- **DataDome-Bypass für Kaufland/Backmarket** (ScrapeFly/ZenRows Trial)
- **Stufe Z**: 7 deals.db-Kopien-Cleanup (`Downloads\DealScraper-v*` legacy)
- **In-App-Update-Progress-Bar** (Updater zieht jetzt unsichtbar via `CREATE_NO_WINDOW`, ohne Download-Feedback)
- **`/bfl/viewbids/` Bypass via DPAPI-Cookie-Klau** (eBay-Bid-History bleibt sonst manueller Paste)
- **Mobile-LAN-Zugang** mit QR-Code im Tray-Menü

## 🛠 Tech-Constraints (aktualisiert nach Iter. 35)

- Stack: Python 3.14, Flask, Playwright, SQLite, **pywebview 6.2.1 + Edge-WebView2 v148**
- UI-Container: PyWebView native window, **frameless=True**, Custom HTML Title-Bar
- Drag-Region: `.pywebview-drag-region` CSS-Klasse (**NICHT** `data-pywebview-drag-region` Attribut)
- Title-Bar-Dark: DWM-API als Backup (Frameless macht es eh dunkel)
- Taskbar-Identity: `_APP_USER_MODEL_ID = "fg618455.DealTracker.App"` + WM_SETICON ctypes
- DB-Pfad: `%APPDATA%\DealScraper\deals.db` (frozen) / Projekt-Wurzel (dev) — `paths.py`
- Scraper-Browser-Profile: `%LOCALAPPDATA%\DealScraper\ScraperProfile\` (Iter. 30)
- Tray: pystray in worker-thread, callbacks an main-thread für `webview.windows[0].show()/hide()/destroy()`
- Splash: `webview.create_window(html=SPLASH_HTML, ...)` → `main_win.load_url(URL)` via `_switch_to_flask()` Background-Thread
- Aktueller Stand: Iter. 35 lokal fertig, NICHT committet, NICHT getagt

## 🚫 Anti-Pattern (nicht wiederholen)

- **NICHT** `data-pywebview-drag-region`-Attribut für Drag-Region verwenden — pywebview erkennt das nicht, der korrekte Selektor ist die CSS-KLASSE `.pywebview-drag-region`. (Tauri/Neutralino-Folklore stimmt nicht für pywebview.)
- **NICHT** `-webkit-app-region: drag` für Drag — funktioniert in WebView2 nicht (Electron-only-Feature).
- **NICHT** auf `<meta name="theme-color">` für Title-Bar-Färbung verlassen — WebView2 ignoriert das.
- **NICHT** zurück zu Chrome `--app` migrieren als "schnelle Lösung" (Iter. 34 hat das aus gutem Grund weggekriegt).
- **NICHT** `headless=True` für eBay (Akamai 303 Access Denied) — Persistent ist headless=False (Iter. 30).
- **NICHT** `__file__`-Dirname für persistente User-Daten in frozen Builds (paths.py-Saga seit Iter. 28).
- **NICHT** für jede neue Site einen kompletten Custom-Parser schreiben wenn `_make_generic_parser` mit JSON-LD-Fallback reicht.
- **NICHT** Splash + Window-URL beim `create_window` mischen — `html=` oder `URL` als 2. positional arg, nicht beides.

## 🔁 Failure-Modes & wie man damit umgeht

1. **Drag funktioniert immer noch nicht**:
   - Verify im DevTools console (`webview.start(debug=True)` temporär): `document.querySelector('.pywebview-drag-region')` muss ein Element returnen
   - Pywebview's customize.js loggt Events nicht — als Sanity-Check `easy_drag=True` temporär setzen (sollte ganze Window draggable machen, bestätigt dass pywebview-Drag generell funktioniert)
   - Falls Drag mit `easy_drag=True` ginge aber mit Klasse nicht: pointer-events checken, alle children der drag-region müssen `pointer-events: none` haben

2. **Splash bleibt sichtbar (load_url failt)**:
   - `_switch_to_flask` printet `[Splash] Flask antwortete nicht in 20s` — heißt Flask hängt. Check `/api/health` mit `curl`. Wenn Flask läuft aber load_url failed → pywebview-API-Problem, `webview.windows[0].load_url(URL)` direkt probieren

3. **48 Sites alle empty/blocked**:
   - Logge die response-HTML einer pending Site: `python -c "from playwright.sync_api import sync_playwright; ..."` → manuell die Card-Selektoren herausfinden
   - JSON-LD-Fallback in `_make_generic_parser` greift wenn CSS-Selektoren leer — wenn auch das leer ist, ist die Site wirklich blocked oder hat zero Treffer für das Keyword

4. **Window-Title weiterhin "python.exe" in Taskbar nach Restart**:
   - Windows cached AppUserModelID per Process-Identity — kompletter App-Neustart nötig (kein Soft-Reload)
   - Verifizieren: `Get-Process python | Select Id, MainWindowTitle` — wenn MainWindowTitle="Deal Tracker" aber Taskbar zeigt Python: Icon-Cache von Windows; `ie4uinit.exe -show` kann den Cache rebuild

5. **last_scrape weiterhin "Noch nicht gescrapt"**:
   - `python -c "import scraper; print(scraper.STATUS['last_scrape'])"` — wenn None: DB-Lese hat gefailt, check Pfad via `paths.resolve_user_file('deals.db')`
   - Wenn Source-DB leer ist, dann ist `MAX(last_seen)` natürlich None — Fix: einmal scrapen, dann beim nächsten Restart greift's

6. **TEMPLATES_AUTO_RELOAD wirkt nicht**:
   - `app.jinja_env.auto_reload = True` zusätzlich zu `app.config[...]` setzen (gewöhnt sich nicht jede Flask-Version daran)
   - Browser-Cache: Strg+Shift+R hard-reload

## 📋 Backlog für später (NICHT in Iter. 36)

- 7 deals.db-Kopien aufräumen (Backlog seit Iter. 28)
- In-App-Update-Progress-Bar
- `/bfl/viewbids/` Bypass via DPAPI-Cookie-Klau
- Mobile-LAN-Zugang mit QR-Code im Tray-Menü
- `webview.start(menu=...)` mit eigenem Menüleisten-Eintrag (Datei/Ansicht/Hilfe)
- DataDome-Bypass für Kaufland/Backmarket (Proxy-Service nötig)
- Color/Extra-Filter pro Target (Backlog seit Iter. 6)
- iOS/Mac-Build (wartet auf Mac mini M4)
- Auto-Update von WebView2 selbst → NIEMALS (Microsoft hat eigenen Service)

## 🎬 Erfolgs-Test (vor `git tag v35`)

**Sofort-Check beim Source-Mode-Start:**
- App startet, sieht Splash für ~1-2s (Lila-Tag-Icon + Spinner + „Deal Tracker startet…")
- Splash wechselt nahtlos zu echtem UI
- Taskbar zeigt unser Tag-Icon, nicht Python
- Title-Bar ist dunkel, eigene Min/Max/Close-Buttons rechts
- Title-Bar draggable
- Doppelklick auf Title-Bar = max/restore
- 67 Pillen im Scrape-Overlay nach `/api/status` (vorher 19)
- last_scrape-Pille zeigt „fertig vor X" beim Start
- Garmin-Target sichtbar in Sport-Uhr-Gruppe

**Wenn Felix grünes Licht gibt:**
```
git add -A
git commit -m "Iter. 35: frameless window + custom title-bar + DWM + drag-class-fix + 48 new sites + splash + lint-config"
git tag v35
git push origin main --tags
```

GitHub Actions baut die .exe.

## 📝 Memory-Update am Schluss

`project_iter_35_frameless_titlebar.md` ist schon angelegt. Nach erfolgreichem Iter. 36:
- Update mit „live verifiziert"-Status und ggf. neuen Failure-Modes aus der Verifikation
- Neue Memory-File `project_iter_36_site_tuning.md` mit welche der 48 Sites tatsächlich Daten liefern + welche Selektoren funktionieren
- MEMORY.md-Index-Eintrag

---

**Frage am Anfang nicht**, sondern: **erst die 10 Verifikations-Punkte abklappern** (Stufe A), **dann Felix kurz die Erfolge bestätigen lassen**, **dann erst Site-Selektoren-Tuning** (Stufe B).

Felix möchte schnelle visuelle Wins. Stufe A in ~10 min liefern, dann Pause-Punkt für ihn zur Bestätigung. Wenn er offline ist: einfach Stufe B starten und so weit kommen wie möglich.
