/* ═══════════════════════════════════════════════════════════════════
   DEAL TRACKER · Frontend
   ═══════════════════════════════════════════════════════════════════ */

// Cache of distinct group names — refreshed on every loadDashboard().
// Powers the inline "Gruppe" dropdown on each category row.
let _groupsList = [];

/* ── PLZ-Umkreis-Filter state (Iter. 24) ───────────────────────────
   Loaded from /api/settings/filter on boot, persisted on change.
   When active (plz set + radius > 0), every /api/deals & /api/top-deals
   call gets ?plz=…&radius_km=… appended via withFilterParams(). */
let _filterPlz    = '';
let _filterRadius = 0;  // km, 0 = filter disabled
let _filterSaveTimer = null;

function withFilterParams(url) {
  if (!_filterPlz || _filterRadius <= 0) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}plz=${encodeURIComponent(_filterPlz)}&radius_km=${_filterRadius}`;
}

async function loadFilterSettings() {
  try {
    const s = await api('/api/settings/filter');
    _filterPlz    = s.plz || '';
    _filterRadius = Number(s.radius_km) || 0;
    syncFilterUI();
  } catch { /* silent — backend may be old */ }
}

function syncFilterUI() {
  const plzIn  = document.getElementById('plz-input');
  const slider = document.getElementById('radius-slider');
  const label  = document.getElementById('radius-label');
  const status = document.getElementById('plz-status');
  if (plzIn  && plzIn.value  !== _filterPlz)      plzIn.value  = _filterPlz;
  if (slider && Number(slider.value) !== _filterRadius) slider.value = String(_filterRadius);
  if (label)  label.textContent = _filterRadius > 0 ? `${_filterRadius} km` : 'aus';
  if (status) {
    const active = _filterPlz && _filterRadius > 0;
    status.textContent = active ? 'aktiv' : 'aus';
    status.style.color = active ? 'var(--accent, #8b5cf6)' : '';
  }
}

function scheduleFilterSave() {
  syncFilterUI();
  if (_filterSaveTimer) clearTimeout(_filterSaveTimer);
  _filterSaveTimer = setTimeout(saveFilterSettings, 500);
}

async function saveFilterSettings() {
  try {
    await api('/api/settings/filter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plz: _filterPlz, radius_km: _filterRadius }),
    });
    // Reload visible data so the filter immediately reflects.
    loadDashboard();
  } catch {
    toast('Filter konnte nicht gespeichert werden', 'error');
  }
}

/* ── eBay-Login-Session (Iter. 25) ─────────────────────────────────
   Opens a real browser window on the user's PC for the eBay login,
   saves the session cookies for the background bid-history scraper. */
let _ebayLoginPollTimer = null;

async function loadEbaySessionStatus() {
  try {
    const s = await api('/api/ebay-session/status');
    renderEbaySessionStatus(s);
    return s;
  } catch {
    return null;
  }
}

function renderEbaySessionStatus(s) {
  const statusEl = document.getElementById('ebay-session-status');
  const loginBtn = document.getElementById('ebay-login-btn');
  const logoutBtn = document.getElementById('ebay-logout-btn');
  if (!statusEl || !loginBtn) return;

  if (s.in_progress) {
    statusEl.textContent = '⏳ Browser-Fenster geöffnet — bitte einloggen…';
    statusEl.style.color = 'var(--amber, #fbbf24)';
    loginBtn.disabled = true;
    loginBtn.querySelector('span').textContent = 'Login läuft…';
    logoutBtn?.classList.add('hidden');
  } else if (s.has_session) {
    statusEl.textContent = '✅ Eingeloggt — Gebot-Auto-Import aktiv';
    statusEl.style.color = 'var(--green, #4ade80)';
    loginBtn.disabled = false;
    loginBtn.querySelector('span').textContent = 'Neu einloggen';
    logoutBtn?.classList.remove('hidden');
  } else {
    statusEl.textContent = 'Nicht eingeloggt — Gebote müssen manuell eingefügt werden';
    statusEl.style.color = '';
    loginBtn.disabled = false;
    loginBtn.querySelector('span').textContent = 'Bei eBay einloggen';
    logoutBtn?.classList.add('hidden');
  }
}

async function startEbayLogin() {
  try {
    const res = await api('/api/ebay-session/login', { method: 'POST' });
    if (res.status === 'started') {
      toast('Browser-Fenster öffnet sich — logge dich bei eBay ein', 'info', 6000);
    } else if (res.status === 'already_running') {
      toast('Login läuft schon — prüfe das Browser-Fenster', 'warning');
    }
    // Poll status every 2s until in_progress flips false.
    if (_ebayLoginPollTimer) clearInterval(_ebayLoginPollTimer);
    _ebayLoginPollTimer = setInterval(async () => {
      const s = await loadEbaySessionStatus();
      if (s && !s.in_progress) {
        clearInterval(_ebayLoginPollTimer);
        _ebayLoginPollTimer = null;
        if (s.last_result === 'ok')        toast(s.last_message || 'Login gespeichert', 'success');
        else if (s.last_result === 'error') toast(s.last_message || 'Login fehlgeschlagen', 'error');
      }
    }, 2000);
  } catch {
    toast('Login konnte nicht gestartet werden', 'error');
  }
}

async function logoutEbay() {
  if (!confirm('Gespeicherten eBay-Login löschen?\n(Gebot-Auto-Import wird wieder deaktiviert.)')) return;
  try {
    await api('/api/ebay-session/logout', { method: 'POST' });
    toast('Login gelöscht', 'info');
    loadEbaySessionStatus();
  } catch {
    toast('Fehler beim Löschen', 'error');
  }
}

/* ── Icon helper ─────────────────────────────────────────────────── */
function icon(name, cls = '') {
  return `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
}

/* ── Versand/Abholung-Badge (Iter. 25) ───────────────────────────────
   Returns {icon, label, cls} or null. Online-Shops bekommen NICHTS
   ("Versand" ist da implizit) — Badge nur fuer Kleinanzeigen / markt /
   quoka / eBay wo es echt eine Wahl gibt. */
const _CLASSIFIED_SITES = new Set(['Kleinanzeigen', 'markt.de', 'quoka', 'eBay']);
function shippingBadge(d) {
  const isClassified = _CLASSIFIED_SITES.has(d.website);
  const pickup  = d.pickup_only === 1 || d.pickup_only === true;
  const ships   = d.shipping_available === 1;
  const noShip  = d.shipping_available === 0;
  if (pickup && ships)       return { icon: 'package',   label: 'Abholung + Versand', cls: 'tag-both' };
  if (pickup || noShip)      return { icon: 'truck-off', label: 'Nur Abholung',       cls: 'tag-pickup' };
  if (ships && isClassified) return { icon: 'package',   label: 'Versand',            cls: 'tag-ship' };
  return null;
}
function shippingBadgeHtml(d, wrapperClass = 'deal-tag') {
  const b = shippingBadge(d);
  if (!b) return '';
  return `<span class="${wrapperClass} ${b.cls}">${icon(b.icon)} ${b.label}</span>`;
}

/* ── Toast ───────────────────────────────────────────────────────── */
const TOAST_ICONS = {
  success: 'check',
  error:   'alert',
  warning: 'alert',
  info:    'spark',
};
function toast(msg, type = 'info', ms = 3000) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `${icon(TOAST_ICONS[type] || 'spark')}<span>${esc(msg)}</span>`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }, ms);
}

/* ── API helper with retry ───────────────────────────────────────── */
async function api(path, opts = {}, retries = 1) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) {
      const txt = await r.text().catch(() => '');
      throw new Error(`HTTP ${r.status}${txt ? ' · ' + txt.slice(0, 100) : ''}`);
    }
    return await r.json();
  } catch (err) {
    if (retries > 0 && /Failed to fetch|NetworkError/i.test(err.message)) {
      await sleep(500);
      return api(path, opts, retries - 1);
    }
    console.error(`[api] ${path}`, err);
    throw err;
  }
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function fmt(n) { return n != null ? n.toLocaleString('de-DE') + ' €' : '–'; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function siteBadge(site) {
  const map = {
    Kleinanzeigen:       's-ka',
    eBay:                's-eb',
    'markt.de':          's-mk',
    quoka:               's-qk',
    Amazon:              's-am',
    Otto:                's-ot',
    Kaufland:            's-kl',
    Idealo:              's-id',
    Mindfactory:         's-mf',
    Alternate:           's-al',
    notebooksbilliger:   's-nb',
    Cyberport:           's-cy',
    Gravis:              's-gr',
    Jacob:               's-jc',
    'future-x':          's-fx',
    Conrad:              's-cr',
    Backmarket:          's-bm',
    Rebuy:               's-rb',
    Refurbed:            's-rf',
    'mac-store24':       's-ms',
    asgoodasnew:         's-ag',
  };
  return `<span class="site-badge ${map[site] ?? 's-xx'}">${esc(site)}</span>`;
}

/* Replace broken image with a package placeholder. */
function imgFallback(img, cls = 'deal-card-img-ph') {
  const ph = document.createElement('div');
  ph.className = cls;
  ph.innerHTML = '<svg><use href="#i-package"/></svg>';
  const wrap = img.closest('.deal-card-img-wrap, .top-cat-img-wrap');
  (wrap || img).replaceWith(ph);
}

/* ═══ DASHBOARD ═════════════════════════════════════════════════════ */
async function loadDashboard() {
  const main = document.getElementById('dashboard');
  try {
    const [data, topDealsRaw, groupsRaw] = await Promise.allSettled([
      api('/api/dashboard'),
      api(withFilterParams('/api/top-deals')),
      api('/api/groups'),
    ]).then(results => [
      results[0].status === 'fulfilled' ? results[0].value : [],
      results[1].status === 'fulfilled' ? results[1].value : [],
      results[2].status === 'fulfilled' ? results[2].value : [],
    ]);
    _groupsList = Array.isArray(groupsRaw) ? groupsRaw : [];

    if (!data.length) {
      main.innerHTML = `
        <div class="empty">
          ${icon('empty')}
          <div>Noch keine Suchbegriffe.</div>
          <div class="dim text-xs" style="margin-top:.5rem">Oben „Suche" klicken um einen hinzuzufügen.</div>
        </div>`;
      return;
    }

    // Mark the cheapest non-empty top deal as "best"
    const validTopDeals = topDealsRaw.filter(d => !d.empty && d.price != null);
    const cheapest = validTopDeals.length
      ? validTopDeals.reduce((a, b) => (a.price < b.price ? a : b))
      : null;
    const topDeals = topDealsRaw.map(d => ({
      ...d,
      _isBest: cheapest && !d.empty && d.price === cheapest.price && d.url === cheapest.url,
    }));

    const topHtml = topDeals.length ? buildTopSection(topDeals) : '';
    main.innerHTML = topHtml + buildGroupedSections(data);
    populateAlertModelSelect(data);
    applySearchFilter();
  } catch (err) {
    console.error('loadDashboard error:', err);
    main.innerHTML = `
      <div class="panel-error">
        ${icon('alert')}
        <div>Fehler beim Laden: ${esc(err?.message ?? '')}</div>
        <button class="btn btn-secondary" onclick="loadDashboard()">${icon('refresh')}<span>Erneut versuchen</span></button>
      </div>`;
  }
}

/* ── Group targets by group_name and render with group headers ───── */
function buildGroupedSections(targets) {
  const groups = new Map();   // groupKey -> { name, targets: [] }
  for (const t of targets) {
    const key = (t.group_name && t.group_name.trim()) || `__solo_${t.id}`;
    if (!groups.has(key)) {
      groups.set(key, {
        name: t.group_name && t.group_name.trim() ? t.group_name : null,
        targets: [],
      });
    }
    groups.get(key).targets.push(t);
  }

  return [...groups.values()].map(g => {
    if (!g.name) {
      // Solo target — no group header
      return buildSection(g.targets[0]);
    }
    // Group with header
    const totalDeals = g.targets.reduce((sum, t) => sum + (t.stats?.count ?? 0), 0);

    // Source badge: first target carries the group's source list
    const sources = g.targets[0]?.sources ?? [];
    const sourceBadge = sources.length
      ? `<span class="group-sources-badge" title="${sources.join(', ')}">${icon('link')} ${sources.length} Quelle${sources.length > 1 ? 'n' : ''}</span>`
      : `<span class="group-sources-badge group-sources-all" title="Alle verfügbaren Quellen">${icon('link')} alle Quellen</span>`;

    const groupMinPrice = g.targets[0]?.group_min_price ?? null;
    const minPriceInput = `<label class="group-min-price-wrap" title="Mindestpreis für die Gruppe – Deals darunter werden ausgeblendet" onclick="event.stopPropagation()">
      <span class="group-min-price-pfx">ab €</span>
      <input class="group-min-price-input" type="number" min="0" step="10"
             value="${groupMinPrice ?? ''}" placeholder="–"
             onblur="saveGroupMinPrice(${esc(JSON.stringify(g.name))}, this.value)"
             onkeydown="if(event.key==='Enter'){this.blur()}" />
    </label>`;

    return `
<section class="group-block" data-group="${esc(g.name)}">
  <header class="group-head">
    <span class="group-icon">${icon('folder')}</span>
    <span class="group-name">${esc(g.name)}</span>
    <span class="group-meta">${g.targets.length} ${g.targets.length === 1 ? 'Produkt' : 'Produkte'} · ${totalDeals} Deals</span>
    <span class="group-head-spacer"></span>
    ${minPriceInput}
    ${sourceBadge}
    <button class="btn-icon group-scrape-btn" title="Alle Produkte dieser Gruppe scrapen"
            onclick="event.stopPropagation(); scrapeGroup(${esc(JSON.stringify(g.name))}, this)">
      ${icon('refresh')}
    </button>
    <button class="btn-icon group-sources-btn" title="Quellen für diese Gruppe bearbeiten"
            onclick="event.stopPropagation(); openGroupSourcePicker(${esc(JSON.stringify(g.name))}, this)">
      ${icon('settings')}
    </button>
    <button class="btn-icon group-purchase-btn" title="Alle aktiven Deals dieser Gruppe als gekauft markieren"
            onclick="event.stopPropagation(); purchaseGroup(${esc(JSON.stringify(g.name))}, this)">
      ${icon('check')}
    </button>
  </header>
  <div class="group-body">${g.targets.map(buildSection).join('')}</div>
</section>`;
  }).join('');
}

/* ── Top deals (3-card podium) ───────────────────────────────────── */
function buildTopSection(deals) {
  return `
<div class="top-section">
  <div class="top-section-head">
    <span class="top-section-title">${icon('spark')} Top Deals</span>
    <span class="top-section-sub">bestes Angebot je Kategorie</span>
  </div>
  <div class="top-deals-grid">${deals.map(buildTopCard).join('')}</div>
</div>`;
}

function buildTopCard(d) {
  if (d.empty) {
    return `
<div class="top-cat-card top-cat-empty">
  <div class="top-cat-body">
    <div class="top-cat-head-line"><span class="top-cat-name">${esc(d.category)}</span></div>
    <div class="top-cat-no-deal">${icon('empty')}<span>Noch keine Deals</span></div>
  </div>
</div>`;
  }
  const proxied = d.image_url ? `/api/image-proxy?url=${encodeURIComponent(d.image_url)}` : null;
  const imgHtml = proxied
    ? `<div class="top-cat-img-wrap"><img class="top-cat-img" src="${esc(proxied)}" alt="" loading="lazy" onerror="imgFallback(this,'top-cat-img-ph')"></div>`
    : `<div class="top-cat-img-ph">${icon('package')}</div>`;

  const savingsAvg = d.avg_price && d.avg_price > d.price
    ? Math.round((1 - d.price / d.avg_price) * 100) : 0;
  const savingsUVP = d.retail_price && d.retail_price > d.price
    ? Math.round((1 - d.price / d.retail_price) * 100) : 0;
  const savingsApple = d.apple_price && d.apple_price > d.price
    ? Math.round((1 - d.price / d.apple_price) * 100) : 0;
  // Prefer UVP-based savings if known, else fall back to market avg
  const primary = savingsUVP || savingsAvg;
  const primaryLabel = savingsUVP
    ? `unter UVP (${Math.round(d.retail_price).toLocaleString('de-DE')} €)`
    : (savingsAvg ? `unter Ø ${Math.round(d.avg_price).toLocaleString('de-DE')} €` : '');

  // Context tags & meta — gives the user at-a-glance specs without opening the link
  const tags = [];
  const isNew = d.found_at && (Date.now() - new Date(d.found_at).getTime()) < 86_400_000;
  if (isNew) tags.push(`<span class="top-cat-tag tag-new">${icon('spark')} Neu</span>`);
  tags.push(`<span class="top-cat-tag tag-best">${icon('crown')} Bestpreis</span>`);
  { const b = shippingBadge(d); if (b) tags.push(`<span class="top-cat-tag ${b.cls}">${icon(b.icon)} ${b.label}</span>`); }
  if (d.listing_type === 'auction')
    tags.push(`<span class="top-cat-tag tag-auction">${icon('gavel')} Auktion</span>`);
  else if (d.listing_type === 'fixed')
    tags.push(`<span class="top-cat-tag tag-fixed">${icon('cash')} Festpreis</span>`);
  if (savingsApple > 0)
    tags.push(`<span class="top-cat-tag tag-apple">${icon('apple')} −${savingsApple}% Apple</span>`);

  const specs = [d.ram, d.ssd].filter(Boolean).join(' · ');
  const specsHtml  = specs ? `<div class="top-cat-specs">${esc(specs)}</div>` : '';
  const locHtml    = d.location ? `<div class="top-cat-loc">${icon('pin')}<span>${esc(d.location)}${d.distance_km != null ? ` · ${d.distance_km} km` : ''}</span></div>` : '';
  const descSnippet = d.description ? esc(d.description.slice(0, 95)) : '';
  const descHtml   = descSnippet ? `<div class="top-cat-desc">${descSnippet}</div>` : '';
  const appleHtml  = savingsApple > 0
    ? `<div class="top-cat-apple-line">${icon('apple')} −${savingsApple}% vs. Apple (${Math.round(d.apple_price).toLocaleString('de-DE')} €)</div>`
    : '';

  const menuHtml = blockMenuHtml(d);

  return `
<div class="top-cat-card-wrap">
  ${menuHtml}
  <a class="top-cat-card ${d._isBest ? 'best' : ''}" href="${esc(d.url)}" target="_blank" rel="noopener" title="${esc(d.title)}">
    ${imgHtml}
    <div class="top-cat-body">
      <div class="top-cat-head-line">
        <span class="top-cat-name">
          <span class="top-cat-crown">${icon('crown')}</span>
          ${esc(d.category)}
        </span>
        ${d.total_count ? `<span class="top-cat-count">${d.total_count} Deals</span>` : ''}
      </div>
      <div class="top-cat-tags">${tags.join('')}</div>
      <div class="top-cat-title">${esc(d.title)}</div>
      ${specsHtml}
      ${descHtml}
      <div class="top-cat-price-row">
        ${d.price != null
          ? `<span class="top-cat-price">${d.price.toLocaleString('de-DE')} €</span>`
          : '<span class="top-cat-price-na">k.A.</span>'}
        ${primary > 0 ? `<span class="top-cat-save">−${primary}%</span>` : ''}
      </div>
      ${primaryLabel ? `<div class="top-cat-savings-label">${primaryLabel}</div>` : ''}
      ${appleHtml}
      ${locHtml}
      ${auctionInlineHtml(d)}
      <div class="top-cat-footer">
        ${siteBadge(d.website)}
        <span class="top-cat-cta">Öffnen ${icon('external')}</span>
      </div>
    </div>
  </a>
</div>`;
}

/* Build the "Gruppe" dropdown for a category row.
   Options: current group (selected) + every distinct group + "Keine" + "Neue Gruppe…".
   onChange routes to changeTargetGroup() which handles both existing-pick and __new__. */
function buildGroupSelect(cat) {
  const current = cat.group_name || '';
  const others  = _groupsList.filter(g => g && g !== current);
  const opts = [
    `<option value="">— keine Gruppe —</option>`,
    current ? `<option value="${esc(current)}" selected>📁 ${esc(current)}</option>` : '',
    ...others.map(g => `<option value="${esc(g)}">📁 ${esc(g)}</option>`),
    `<option value="__new__">+ neue Gruppe…</option>`,
  ].filter(Boolean).join('');
  return `<select class="cat-group-select" onclick="event.stopPropagation()"
            onchange="changeTargetGroup(${cat.id}, this)"
            title="${current ? 'Aktuelle Gruppe: ' + current : 'Keiner Gruppe zugeordnet'}">
    ${opts}
  </select>`;
}

/* ── Compact category row ────────────────────────────────────────── */
function buildSection(cat) {
  const s = cat.stats;
  const count    = s.count ?? 0;
  const minLabel = s.min_price != null ? 'ab ' + fmt(s.min_price) : '';
  const avgLabel = s.avg_price != null ? 'Ø ' + fmt(s.avg_price)  : '';

  return `
<div class="cat-row" data-id="${cat.id}" data-model="${esc(cat.name)}"
     data-min="${s.min_price ?? ''}" data-avg="${s.avg_price ?? ''}"
     data-wish="${cat.wish_price ?? ''}">
  <div class="cat-row-head" onclick="toggleRow(this)">
    <span class="chevron">${icon('chevron')}</span>
    <span class="cat-name">${esc(cat.name)}</span>
    <div class="cat-meta">
      <span class="meta-badge ${count === 0 ? 'zero' : ''}">${count} ${count === 1 ? 'Deal' : 'Deals'}</span>
      ${minLabel ? `<span class="meta-min">${minLabel}</span>` : ''}
      ${avgLabel ? `<span class="meta-avg">${avgLabel}</span>` : ''}
    </div>
    ${buildGroupSelect(cat)}
    <button class="cat-scrape-btn" onclick="event.stopPropagation();scrapeTarget(${cat.id}, this)" title="Nur diese Kategorie scrapen">
      ${icon('refresh')}
    </button>
    <label onclick="event.stopPropagation()" style="cursor:pointer;display:flex;align-items:center">
      <span class="toggle">
        <input type="checkbox" ${cat.active ? 'checked' : ''} onchange="toggleTarget(${cat.id})">
        <span class="toggle-track"></span>
      </span>
    </label>
  </div>
  <div class="cat-panel"><div class="cat-panel-inner" id="panel-${cat.id}"></div></div>
</div>`;
}

/* Inline group change handler — wired up from each cat-row's <select>. */
async function changeTargetGroup(targetId, selectEl) {
  let value = selectEl.value;
  if (value === '__new__') {
    const name = (prompt('Name für die neue Gruppe?') || '').trim();
    if (!name) { selectEl.value = ''; return; }   // user cancelled
    value = name;
  }
  try {
    await updateTarget(targetId, { group_name: value || null });
    toast(value ? `In Gruppe „${value}" verschoben` : 'Gruppe entfernt', 'success', 1500);
    loadDashboard();
  } catch {
    toast('Fehler beim Speichern', 'error');
  }
}

/* ── Expand / load panel ─────────────────────────────────────────── */
async function toggleRow(head) {
  const row   = head.closest('.cat-row');
  const inner = row.querySelector('.cat-panel-inner');
  const model = row.dataset.model;
  const stats = {
    min_price:  parseFloat(row.dataset.min)  || null,
    avg_price:  parseFloat(row.dataset.avg)  || null,
    wish_price: parseFloat(row.dataset.wish) || null,
  };

  row.classList.toggle('open');

  if (row.classList.contains('open') && !inner.dataset.loaded) {
    loadPanel(inner, model, stats);
  }
}

async function loadPanel(panel, model, stats) {
  panel.innerHTML = `<div class="loading-skel" style="padding:.9rem 1rem">
    <div class="skel-row" style="height:88px"></div></div>`;
  try {
    const deals = await api(withFilterParams(`/api/deals?model=${encodeURIComponent(model)}&available_only=1&sort=price&order=ASC`));
    panel.innerHTML = buildPanel(deals, stats);
    panel.dataset.loaded = '1';
    const tableWrap = panel.querySelector('.panel-table');
    if (tableWrap) initTableSort(tableWrap, model);
    panel.querySelector('.expand-btn')?.addEventListener('click', function () {
      togglePanelTable(this, tableWrap);
    });
    hydrateSparklines();
  } catch (err) {
    panel.innerHTML = `
      <div class="panel-error">
        ${icon('alert')}
        <div>Fehler: ${esc(err?.message ?? 'Laden fehlgeschlagen')}</div>
        <button class="btn btn-secondary panel-retry">
          ${icon('refresh')}<span>Erneut versuchen</span>
        </button>
      </div>`;
    panel.querySelector('.panel-retry')?.addEventListener('click', () => loadPanel(panel, model, stats));
    delete panel.dataset.loaded;
  }
}

function buildPanel(deals, stats) {
  if (!deals.length) {
    return `<div class="empty" style="padding:1.8rem">${icon('empty')}<div>Keine Angebote gefunden.</div></div>`;
  }
  const count     = deals.length;
  const tableHtml = buildTable(deals);

  // Group by website/source
  const byWebsite = {};
  for (const d of deals) {
    (byWebsite[d.website] = byWebsite[d.website] || []).push(d);
  }
  const websites = Object.keys(byWebsite).sort((a, b) => {
    const aMin = Math.min(...byWebsite[a].map(d => d.price ?? Infinity));
    const bMin = Math.min(...byWebsite[b].map(d => d.price ?? Infinity));
    return aMin - bMin;
  });

  // Single source → flat card grid (no sub-group overhead)
  if (websites.length <= 1) {
    const top = deals.slice(0, 6);
    return `
<div class="panel-cards">${top.map(d => dealCard(d, stats)).join('')}</div>
<div class="panel-expand">
  <button class="expand-btn">${icon('chevron')}<span>Alle ${count} Angebote anzeigen</span></button>
</div>
<div class="panel-table">${tableHtml}</div>`;
  }

  // Multiple sources → sub-groups per website
  const groupsHtml = websites.map(site => {
    const sd   = byWebsite[site];
    const sMin = Math.min(...sd.map(d => d.price ?? Infinity));
    const top3 = sd.slice(0, 4);
    return `<div class="src-group">
  <div class="src-group-hd">
    ${siteBadge(site)}
    <span class="src-group-name">${esc(site)}</span>
    <span class="src-group-meta">${sd.length} Deal${sd.length !== 1 ? 's' : ''} · ab ${fmt(sMin)}</span>
  </div>
  <div class="src-group-cards">${top3.map(d => dealCard(d, stats)).join('')}</div>
</div>`;
  }).join('');

  return `
<div class="src-groups">${groupsHtml}</div>
<div class="panel-expand">
  <button class="expand-btn">${icon('chevron')}<span>Alle ${count} Angebote anzeigen</span></button>
</div>
<div class="panel-table">${tableHtml}</div>`;
}

function togglePanelTable(btn, tableWrap) {
  const open = tableWrap.classList.toggle('open');
  btn.classList.toggle('open', open);
  const lbl = btn.querySelector('span');
  if (lbl) {
    const n = tableWrap.querySelectorAll('tbody tr').length;
    lbl.textContent = open ? 'Einklappen' : `Alle ${n} Angebote anzeigen`;
  }
}

/* ── Deal card ───────────────────────────────────────────────────── */
function dealCard(d, stats = {}) {
  const proxied = d.image_url ? `/api/image-proxy?url=${encodeURIComponent(d.image_url)}` : null;
  const imgHtml = proxied
    ? `<div class="deal-card-img-wrap"><img src="${esc(proxied)}" alt="" loading="lazy" onerror="imgFallback(this,'deal-card-img-ph')"></div>`
    : `<div class="deal-card-img-ph">${icon('package')}</div>`;

  const tags = [];
  const isNew = d.found_at && (Date.now() - new Date(d.found_at).getTime()) < 86_400_000;
  if (isNew) tags.push(`<span class="deal-tag tag-new">${icon('spark')} Neu</span>`);
  // Iter. 26: Wunschpreis-Treffer — Felix' Zielpreis wird unterschritten
  const belowWish = (stats.wish_price != null && d.price != null && d.price <= stats.wish_price);
  if (belowWish)
    tags.push(`<span class="deal-tag tag-wish">${icon('crown')} unter Wunsch (${stats.wish_price.toLocaleString('de-DE')} €)</span>`);
  if (d.price != null && stats.min_price != null && d.price <= stats.min_price)
    tags.push(`<span class="deal-tag tag-best">${icon('crown')} Bestpreis</span>`);
  if (d.price != null && stats.avg_price != null && d.price < stats.avg_price * 0.85)
    tags.push(`<span class="deal-tag tag-deal">${icon('flame')} Deal</span>`);
  { const b = shippingBadge(d); if (b) tags.push(`<span class="deal-tag ${b.cls}">${icon(b.icon)} ${b.label}</span>`); }
  // Auktion vs Festpreis — always show so user knows at a glance
  if (d.listing_type === 'auction')
    tags.push(`<span class="deal-tag tag-auction">${icon('gavel')} Auktion</span>`);
  else if (d.listing_type === 'fixed')
    tags.push(`<span class="deal-tag tag-fixed">${icon('cash')} Festpreis</span>`);
  // Apple-comparison tag — only when Apple reference price is known AND higher
  const savingsApple = d.apple_price && d.apple_price > d.price
    ? Math.round((1 - d.price / d.apple_price) * 100) : 0;
  if (savingsApple > 0)
    tags.push(`<span class="deal-tag tag-apple">${icon('apple')} −${savingsApple}% Apple</span>`);

  let priceHtml;
  if (d.price == null) {
    priceHtml = `<span class="no-price">Preis unbekannt</span>`;
  } else {
    const savings = stats.avg_price && stats.avg_price > d.price
      ? Math.round((1 - d.price / stats.avg_price) * 100) : 0;
    priceHtml = `
      <span class="deal-price-main">${d.price.toLocaleString('de-DE')} €</span>
      ${stats.avg_price && d.price < stats.avg_price
        ? `<span class="deal-price-orig">statt Ø ${stats.avg_price.toLocaleString('de-DE')} €</span>`
        : ''}
      ${savings > 0 ? `<span class="deal-savings">${savings}% günstiger</span>` : ''}`;
  }

  const descHtml = d.description
    ? `<div class="deal-desc">${esc(d.description.slice(0, 120))}</div>`
    : '';

  const locationHtml = d.location
    ? `<div class="deal-location">${icon('pin')}<span>${esc(d.location)}${d.distance_km != null ? ` · ${d.distance_km} km` : ''}</span></div>`
    : '';

  const appleHtml = savingsApple > 0
    ? `<div class="deal-apple-line">${icon('apple')} −${savingsApple}% vs. Apple (${Math.round(d.apple_price).toLocaleString('de-DE')} €)</div>`
    : '';

  return `
<div class="deal-card-wrap">
  ${blockMenuHtml(d)}
  <a class="deal-card${belowWish ? ' below-wish' : ''}" href="${esc(d.url)}" target="_blank" rel="noopener" title="${esc(d.title)}">
    ${imgHtml}
    <div class="deal-card-body">
      ${tags.length ? `<div class="deal-tags">${tags.join('')}</div>` : ''}
      <div class="deal-title">${esc(d.title)}</div>
      ${descHtml}
      <div>${priceHtml}</div>
      ${appleHtml}
      ${locationHtml}
      ${auctionInlineHtml(d)}
      <div class="deal-card-footer">
        ${siteBadge(d.website)}
        <span class="deal-link-hint">Öffnen ${icon('external')}</span>
      </div>
    </div>
  </a>
</div>`;
}

/* ── Block menu (X-button) ─────────────────────────────────────────
   Returns the menu trigger + popup (popup is hidden until clicked).
   Wrapper is a sibling of the <a class="deal-card">, so clicks here
   don't bubble through to the card's link. */
function blockMenuHtml(d) {
  // Inline onclick handlers run after the HTML parser unescapes attribute
  // entities — so we wrap JSON.stringify(x) in esc(...) to turn its inner
  // double quotes into &quot;. The browser puts them back to " before eval.
  const sellerOpt = d.seller
    ? `<button class="danger" onclick="event.stopPropagation();event.preventDefault();blockSellerFromCard(${esc(JSON.stringify(d.website))}, ${esc(JSON.stringify(d.seller))}, this)">
         ${icon('ban')} Verkäufer „${esc(d.seller).slice(0, 20)}…" blockieren
       </button>`
    : '';
  return `
<div class="card-menu-wrap">
  <button class="card-menu-btn" title="Ausblenden / blockieren"
          onclick="event.stopPropagation();event.preventDefault();toggleCardMenu(this)">
    ${icon('x')}
  </button>
  <div class="card-menu-popup hidden">
    <button onclick="event.stopPropagation();event.preventDefault();purchaseDealFromCard(${d.id}, this)">
      ${icon('check')} Als gekauft markieren
    </button>
    <button class="danger" onclick="event.stopPropagation();event.preventDefault();blockDealFromCard(${d.id}, this)">
      ${icon('ban')} Angebot ausblenden
    </button>
    ${sellerOpt}
  </div>
</div>`;
}

/* ── Auction sparkline inline on the card ──────────────────────────
   Renders a 32px-high SVG of the price-history points + a meta line
   with bid count and remaining time. Clicking either opens the modal. */
function auctionInlineHtml(d) {
  if (d.listing_type !== 'auction') return '';
  // Bid count: distinguish "0 Gebote" (no bidders yet) from null (unknown).
  let bidHtml;
  if (d.bid_count === 0) {
    bidHtml = `<span class="auction-meta-zero">${icon('gavel')} Noch keine Gebote</span>`;
  } else if (d.bid_count != null) {
    bidHtml = `<span>${icon('gavel')} ${d.bid_count} ${d.bid_count === 1 ? 'Gebot' : 'Gebote'}</span>`;
  } else {
    bidHtml = `<span class="auction-meta-unknown">${icon('gavel')} Gebote …</span>`;
  }
  const remaining = formatRemaining(d.auction_ends_at);
  const absEnd    = formatAbsoluteEnd(d.auction_ends_at);
  // eBay doesn't expose end-time in the search listing — only show countdown
  // when we actually have it. Otherwise show a neutral "läuft" marker so the
  // user doesn't get a misleading "beendet".  Iter. 27 B8: zusatzlich
  // absolute Endzeit als Hover-Tooltip damit Felix die genaue Uhrzeit sieht
  // ohne erst das Modal oeffnen zu muessen.
  const remHtml = remaining
    ? `<span title="${absEnd ? esc('Endet ' + absEnd) : ''}">${icon('clock')} ${esc(remaining)}</span>`
    : `<span class="auction-meta-running">${icon('clock')} läuft</span>`;
  const seller = d.seller
    ? `<span class="auction-meta-seller" title="${esc(d.seller)}">@${esc(d.seller).slice(0, 18)}</span>`
    : '';
  // Iter. 27 E16: deal-Preis + last_seen als data-Attrs damit hydrateSparklines
  // einen Shadow-Endpunkt anhaengen kann wenn die Bid-History aelter ist als
  // der Karten-Preis. So endet die Sparkline immer beim sichtbaren Karten-
  // Preis statt 'unter' ihm.
  const dataAttrs = `data-deal-price="${d.price != null ? d.price : ''}"`
                  + ` data-deal-lastseen="${d.last_seen || ''}"`;
  return `
<div class="auction-spark-wrap" ${dataAttrs}
     onclick="event.stopPropagation();event.preventDefault();openAuctionModal(${d.id})"
     title="Preisübersicht öffnen">
  <span class="auction-spark-label">${icon('trend')} Preisübersicht</span>
  <svg class="auction-sparkline" data-deal-id="${d.id}"
       viewBox="0 0 100 32" preserveAspectRatio="none" aria-label="Preisentwicklung Auktion">
    <!-- path is filled in by hydrateSparklines() once /api/price-history responds -->
  </svg>
</div>
<div class="auction-meta-inline">${bidHtml}${remHtml}${seller}</div>`;
}

function formatRemaining(iso) {
  if (!iso) return null;
  const end = new Date(iso).getTime();
  const ms  = end - Date.now();
  if (ms <= 0) return 'beendet';
  const days = Math.floor(ms / 86_400_000);
  const hrs  = Math.floor((ms % 86_400_000) / 3_600_000);
  const mins = Math.floor((ms % 3_600_000) / 60_000);
  if (days > 0) return `${days}d ${hrs}h`;
  if (hrs  > 0) return `${hrs}h ${mins}m`;
  return `${mins}m`;
}

/* Iter. 27 B8: Absolute Endzeit als "heute 17:35" oder "Mi 17:35" oder
   "25.05. 17:35" (je nach Naehe). Zusatz zum relativen Countdown damit
   Felix auch bei kurzen Auktionen direkt sieht WANN sie endet. */
function formatAbsoluteEnd(iso) {
  if (!iso) return null;
  const end  = new Date(iso);
  const ms   = end.getTime() - Date.now();
  if (ms <= 0) return null;
  const hhmm = end.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const endDay = new Date(end); endDay.setHours(0, 0, 0, 0);
  const dayDelta = Math.round((endDay - today) / 86_400_000);
  if (dayDelta === 0) return `heute ${hhmm}`;
  if (dayDelta === 1) return `morgen ${hhmm}`;
  if (dayDelta <= 6) {
    const wd = end.toLocaleDateString('de-DE', { weekday: 'short' });
    return `${wd} ${hhmm}`;
  }
  const dm = end.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' });
  return `${dm} ${hhmm}`;
}

/* Time delta from `tMs` to now, in human-readable German.
   Used in the auction modal's "Zuletzt erfasst" card so the user can see
   at a glance whether the snapshot is fresh ("vor 12 s") or stale. */
function formatRelative(tMs) {
  if (!tMs) return '–';
  const ms = Date.now() - tMs;
  if (ms < 0)            return 'gerade eben';
  if (ms < 60_000)       return `vor ${Math.floor(ms / 1000)} s`;
  if (ms < 3_600_000)    return `vor ${Math.floor(ms / 60_000)} min`;
  if (ms < 86_400_000)   return `vor ${Math.floor(ms / 3_600_000)} h`;
  return `vor ${Math.floor(ms / 86_400_000)} d`;
}

/* ── Table ──────────────────────────────────────────────────────── */
function buildTable(deals) {
  if (!deals.length) return `<div class="empty">${icon('empty')}<div>Keine Angebote gefunden.</div></div>`;

  const rows = deals.map(d => {
    const price = d.price != null
      ? `<span class="price-val">${d.price.toLocaleString('de-DE')} €</span>`
      : `<span class="price-na">k.A.</span>`;
    const specs = [d.ram, d.ssd].filter(Boolean)
      .map(s => `<span class="pill">${esc(s)}</span>`).join('');
    const date  = d.found_at ? d.found_at.slice(0, 10) : '–';
    const cls   = d.available ? '' : 'unavailable';

    const meta = [
      d.location ? `${icon('pin')}<span>${esc(d.location)}${d.distance_km != null ? ` · ${d.distance_km} km` : ''}</span>` : '',
      (() => { const b = shippingBadge(d); return b ? `<span class="td-pickup ${b.cls}">${icon(b.icon)} ${b.label}</span>` : ''; })(),
    ].filter(Boolean).join('');

    return `<tr class="${cls}">
      <td class="td-title">
        <a href="${esc(d.url)}" target="_blank" rel="noopener" title="${esc(d.title)}">${esc(d.title)}</a>
        ${specs ? `<div class="td-specs">${specs}</div>` : ''}
        ${meta ? `<div class="td-meta">${meta}</div>` : ''}
      </td>
      <td>${price}</td>
      <td>${siteBadge(d.website)}</td>
      <td style="color:var(--text-dim);font-size:.74rem;white-space:nowrap">${date}</td>
      <td>${d.available ? '<span class="avail-y">✓</span>' : '<span class="avail-n">✗</span>'}</td>
    </tr>`;
  }).join('');

  return `<table>
    <thead>
      <tr>
        <th data-col="title">Titel</th>
        <th data-col="price">Preis</th>
        <th data-col="website" class="no-sort">Website</th>
        <th data-col="found_at">Datum</th>
        <th class="no-sort">Verf.</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function initTableSort(wrap, model) {
  let sortCol = 'price', sortOrder = 'ASC';
  wrap.querySelectorAll('thead th[data-col]').forEach(th => {
    th.addEventListener('click', async () => {
      const col = th.dataset.col;
      sortOrder = (sortCol === col && sortOrder === 'ASC') ? 'DESC' : 'ASC';
      sortCol = col;
      wrap.querySelectorAll('thead th').forEach(h => h.classList.remove('asc', 'desc'));
      th.classList.add(sortOrder === 'ASC' ? 'asc' : 'desc');
      try {
        const deals = await api(withFilterParams(`/api/deals?model=${encodeURIComponent(model)}&available_only=1&sort=${col}&order=${sortOrder}`));
        const tbody = wrap.querySelector('tbody');
        if (tbody) {
          const match = buildTable(deals).match(/<tbody>([\s\S]*)<\/tbody>/);
          tbody.outerHTML = `<tbody>${match?.[1] ?? ''}</tbody>`;
        }
      } catch { /* silent */ }
    });
  });
}

/* ═══ SEARCH (client-side filter) ═══════════════════════════════════ */
function applySearchFilter() {
  const term = (document.getElementById('search-input').value || '').trim().toLowerCase();
  document.querySelectorAll('.cat-row').forEach(row => {
    const name = (row.dataset.model || '').toLowerCase();
    row.style.display = (!term || name.includes(term)) ? '' : 'none';
  });
  document.querySelectorAll('.top-cat-card').forEach(card => {
    const name = (card.querySelector('.top-cat-name')?.textContent || '').toLowerCase();
    card.style.display = (!term || name.includes(term)) ? '' : 'none';
  });
}

/* ═══ TARGETS ═══════════════════════════════════════════════════════ */
async function addTargetFromModal() {
  const name    = document.getElementById('modal-name').value.trim();
  const keyword = document.getElementById('modal-keyword').value.trim() || name.toLowerCase();
  const group   = document.getElementById('modal-group').value.trim() || null;
  const retailRaw = document.getElementById('modal-retail').value.trim();
  const retail  = retailRaw ? parseFloat(retailRaw) : null;
  if (!name) {
    toast('Bitte einen Namen eingeben', 'warning');
    return;
  }
  try {
    await api('/api/targets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, keyword, group_name: group, retail_price: retail }),
    });
    closeAddModal();
    toast(`„${name}" hinzugefügt`, 'success');
    loadDashboard();
    loadDrawerTargets();
  } catch (err) {
    toast('Fehler: ' + (err?.message ?? 'unbekannt'), 'error');
  }
}

async function updateTarget(id, body) {
  return api(`/api/targets/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function populateGroupSuggestions() {
  try {
    const groups = await api('/api/groups');
    const dl = document.getElementById('group-suggestions');
    dl.innerHTML = groups.map(g => `<option value="${esc(g)}"></option>`).join('');
  } catch { /* silent */ }
}

async function toggleTarget(id) {
  try {
    await api(`/api/targets/${id}/toggle`, { method: 'POST' });
    loadDashboard();
    loadDrawerTargets();
  } catch { toast('Fehler beim Umschalten', 'error'); }
}

async function deleteTarget(id) {
  try {
    await api(`/api/targets/${id}`, { method: 'DELETE' });
    toast('Suchbegriff gelöscht', 'success');
    loadDashboard();
    loadDrawerTargets();
  } catch { toast('Fehler beim Löschen', 'error'); }
}

async function loadDrawerTargets() {
  try {
    const targets = await api('/api/targets');
    const list = document.getElementById('target-list');
    if (!targets.length) {
      list.innerHTML = '<p class="dim text-xs">Noch keine Suchbegriffe.</p>';
      return;
    }
    list.innerHTML = targets.map(t => `
      <div class="target-item ${t.active ? '' : 'inactive'}" data-id="${t.id}">
        <div class="target-row-main">
          <label class="toggle">
            <input type="checkbox" ${t.active ? 'checked' : ''} onchange="toggleTarget(${t.id})">
            <span class="toggle-track"></span>
          </label>
          <div class="target-info">
            <div class="target-name">${esc(t.name)}</div>
            <div class="target-kw">${esc(t.keyword)}${t.apple_price ? ` · Apple ${Math.round(t.apple_price).toLocaleString('de-DE')} €` : ''}</div>
          </div>
          <button class="btn-icon" onclick="deleteTarget(${t.id})" title="Löschen"
                  style="color:var(--rose);border-color:transparent;background:transparent">
            ${icon('trash')}
          </button>
        </div>
        <div class="target-edit-row">
          <input class="target-edit-input" data-field="group_name" value="${esc(t.group_name || '')}"
                 placeholder="Gruppe…" list="group-suggestions" />
          <div class="input-with-suffix">
            <input class="target-edit-input" data-field="retail_price" type="number" min="0" step="1"
                   value="${t.retail_price ?? ''}" placeholder="UVP" />
            <span class="input-suffix">€</span>
          </div>
          <div class="input-with-suffix">
            <input class="target-edit-input" data-field="min_price" type="number" min="0" step="1"
                   value="${t.min_price ?? ''}" placeholder="Min" title="Mindestpreis – Angebote darunter gelten als Fake und werden ausgeblendet" />
            <span class="input-suffix">€</span>
          </div>
          <div class="input-with-suffix">
            <input class="target-edit-input" data-field="wish_price" type="number" min="0" step="1"
                   value="${t.wish_price ?? ''}" placeholder="Wunsch" title="Wunschpreis – Angebote darunter werden mit Gold-Rand markiert" />
            <span class="input-suffix">€</span>
          </div>
        </div>
      </div>`).join('');

    // Wire up inline edits (blur → PATCH)
    list.querySelectorAll('.target-edit-input').forEach(inp => {
      inp.addEventListener('change', async (e) => {
        const item   = e.target.closest('.target-item');
        const id     = item.dataset.id;
        const field  = e.target.dataset.field;
        const raw    = e.target.value.trim();
        let body;
        if (field === 'retail_price') {
          body = { retail_price: raw === '' ? null : parseFloat(raw) };
        } else if (field === 'min_price') {
          body = { min_price: raw === '' ? null : parseFloat(raw) };
        } else if (field === 'wish_price') {
          body = { wish_price: raw === '' ? null : parseFloat(raw) };
        } else {
          body = { group_name: raw || null };
        }
        try {
          await updateTarget(id, body);
          toast('Gespeichert', 'success', 1500);
          loadDashboard();
          populateGroupSuggestions();
        } catch { toast('Fehler beim Speichern', 'error'); }
      });
    });
  } catch { /* silent */ }
}

function populateAlertModelSelect(data) {
  const sel = document.getElementById('alert-model');
  const val = sel.value;
  sel.innerHTML = '<option value="">Kategorie wählen…</option>' +
    data.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join('');
  if (val) sel.value = val;
}

/* ═══ ALERTS ════════════════════════════════════════════════════════ */
async function saveAlert() {
  const model     = document.getElementById('alert-model').value;
  const threshold = parseFloat(document.getElementById('alert-threshold').value);
  if (!model || isNaN(threshold) || threshold <= 0) {
    toast('Bitte Kategorie und Preis eingeben', 'warning');
    return;
  }
  try {
    await api('/api/alerts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, threshold }),
    });
    toast(`Alarm: ${model} unter ${threshold} €`, 'success');
    document.getElementById('alert-threshold').value = '';
    loadDrawerAlerts();
  } catch { toast('Fehler beim Speichern', 'error'); }
}

async function loadDrawerAlerts() {
  try {
    const alerts = await api('/api/alerts');
    const list = document.getElementById('alert-list');
    if (!alerts.length) { list.innerHTML = ''; return; }
    list.innerHTML = alerts.map(a => `
      <div class="alert-item">
        <div>
          <div class="alert-model">${esc(a.model)}</div>
          <div class="alert-thresh">unter ${a.threshold.toLocaleString('de-DE')} €</div>
        </div>
        <button class="btn-icon" onclick="deleteAlert(${a.id})" title="Löschen"
                style="color:var(--rose);border-color:transparent;background:transparent">
          ${icon('trash')}
        </button>
      </div>`).join('');
  } catch { /* silent */ }
}

async function deleteAlert(id) {
  try {
    await api(`/api/alerts/${id}`, { method: 'DELETE' });
    toast('Alarm gelöscht', 'success');
    loadDrawerAlerts();
  } catch { toast('Fehler beim Löschen', 'error'); }
}

/* ═══ SCRAPE ════════════════════════════════════════════════════════ */
async function scrapeTarget(id, btn) {
  if (btn?.disabled) return;
  if (btn) { btn.disabled = true; btn.classList.add('spinning'); }
  try {
    const res = await api(`/api/scrape/${id}`, { method: 'POST' });
    if (res.status === 'queued') {
      toast(`„${res.target}" wartet (Position ${res.queue_position})…`, 'info');
    } else if (res.status === 'already_running') {
      toast('Scraping läuft bereits…', 'warning');
    } else {
      toast(`„${res.target}" wird gescrapt…`, 'info');
      pollUntilDone({ noOverlay: true, refreshOnly: id });
    }
  } catch (err) {
    toast('Fehler: ' + (err?.message ?? 'unbekannt'), 'error');
  } finally {
    if (btn) {
      // keep spinning until pollUntilDone resets via reload; safety timeout
      setTimeout(() => { btn.disabled = false; btn.classList.remove('spinning'); }, 1500);
    }
  }
}

async function saveGroupMinPrice(groupName, rawValue) {
  const min_price = rawValue === '' ? null : parseFloat(rawValue);
  try {
    await api(`/api/groups/${encodeURIComponent(groupName)}/min-price`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_price }),
    });
    toast(`Mindestpreis für „${groupName}": ${min_price != null ? '€' + min_price : 'kein Limit'}`, 'success');
  } catch (e) {
    toast('Fehler beim Speichern des Mindestpreises', 'error');
  }
}


async function scrapeGroup(groupName, btn) {
  if (btn?.disabled) return;
  if (btn) { btn.disabled = true; btn.classList.add('spinning'); }
  try {
    const res = await api(
      `/api/scrape/group/${encodeURIComponent(groupName)}`,
      { method: 'POST' },
    );
    if (res.status === 'queued') {
      const n = (res.targets || []).length;
      toast(`Gruppe „${groupName}" wartet (${n} Produkt${n === 1 ? '' : 'e'}, Position ${res.queue_position})…`, 'info');
    } else if (res.status === 'already_running') {
      toast('Scraping läuft bereits…', 'warning');
    } else {
      const n = (res.targets || []).length;
      toast(`Gruppe „${groupName}" wird gescrapt (${n} Produkt${n === 1 ? '' : 'e'})…`, 'info');
      // Iter. 35 fix: kein globales 19-Quellen-Overlay fuer Per-Gruppe-Scrape —
      // das wirkte so als wuerde alles gescrapt, obwohl Backend nur die Gruppe
      // scrapt. Spinner am Group-Button + Status-Pill geben weiter Feedback.
      pollUntilDone({ noOverlay: true });
    }
  } catch (err) {
    toast('Fehler: ' + (err?.message ?? 'unbekannt'), 'error');
  } finally {
    if (btn) {
      setTimeout(() => { btn.disabled = false; btn.classList.remove('spinning'); }, 1500);
    }
  }
}


async function triggerScrape() {
  const btn     = document.getElementById('scrape-btn');
  const overlay = document.getElementById('scrape-overlay');
  btn.disabled  = true;
  document.querySelectorAll('#scrape-progress li').forEach(li => {
    li.classList.remove('prog-done', 'prog-err');
    const cnt = li.querySelector('.prog-count');
    if (cnt) cnt.textContent = '';
  });
  overlay.classList.remove('hidden', 'minimized');   // Iter. 26: bei Start volle Ansicht
  try {
    const res = await api('/api/scrape', { method: 'POST' });
    if (res.status === 'queued') {
      toast(`Globaler Scrape wartet (Position ${res.queue_position})…`, 'info');
      overlay.classList.add('hidden');
    } else if (res.status === 'already_running') {
      toast('Scraping läuft bereits…', 'warning');
    } else {
      toast('Scraping gestartet', 'info');
      pollUntilDone();
    }
  } catch (err) {
    toast('Fehler: ' + (err?.message ?? 'unbekannt'), 'error');
    overlay.classList.add('hidden');
  } finally {
    btn.disabled = false;
  }
}

async function pollUntilDone(opts = {}) {
  const overlay = document.getElementById('scrape-overlay');
  while (true) {
    await sleep(2000);
    try {
      const s = await api('/api/status');
      updateStatusUI(s);
      if (!opts.noOverlay) updateScrapeProgress(s);
      if (!s.scraping) {
        await sleep(400);
        if (!opts.noOverlay) overlay.classList.add('hidden');
        overlay.classList.remove('minimized');     // reset for next run
        toast('Scraping abgeschlossen', 'success');
        loadDashboard();
        break;
      }
    } catch { break; }
  }
}

/* Status label per scraper outcome (German, human-readable). */
const _PROG_LABEL = {
  ok:      (info) => `${info.count} Deals`,
  empty:   ()     => 'leer (Seite ok, 0 Treffer)',
  blocked: (info) => `geblockt${info.detail ? ' · ' + info.detail.replace(/^HTTP /, '') : ''}`,
  error:   (info) => `Fehler${info.detail ? ' · ' + info.detail.replace(/^HTTP /, '') : ''}`,
};

function _ensureScrapePill(siteName) {
  // Iter. 36: dynamisch fehlende Pills im Scrape-Overlay anlegen — sonst
  // tauchen die 48 neuen Sites aus websitenliste.md gar nicht im Progress auf.
  const list = document.getElementById('scrape-progress');
  if (!list) return null;
  let li = list.querySelector(`[data-site="${CSS.escape(siteName)}"]`);
  if (li) return li;
  li = document.createElement('li');
  li.dataset.site = siteName;
  const state = document.createElement('span'); state.className = 'prog-state';
  const name  = document.createElement('span'); name.className  = 'prog-name'; name.textContent = siteName;
  const count = document.createElement('span'); count.className = 'prog-count';
  li.append(state, name, count);
  list.appendChild(li);
  return li;
}

function updateScrapeProgress(s) {
  if (!s.sites) return;

  // Iter. 36: Live-Progress-Banner — zeigt aktuell laufende Site/Target/Gruppe.
  const curBox = document.getElementById('scrape-current');
  if (curBox) {
    const c = s.current || {};
    if (s.scraping && (c.site || c.target)) {
      const kwEl    = document.getElementById('scrape-current-keyword');
      const siteEl  = document.getElementById('scrape-current-site');
      const grpEl   = document.getElementById('scrape-current-group');
      if (kwEl)   kwEl.textContent   = c.keyword || c.target || '…';
      if (siteEl) siteEl.textContent = c.site || '…';
      if (grpEl)  grpEl.textContent  = c.group ? `· Gruppe „${c.group}"` : '';
      curBox.classList.remove('hidden');
    } else {
      curBox.classList.add('hidden');
    }
  }

  for (const [site, info] of Object.entries(s.sites)) {
    // Iter. 36: Sites die in keiner Gruppen-Quellen-Liste sind oder
    // explizit als 'skipped' markiert wurden - aus der Pillen-Liste raus.
    if (info.eligible === false || info.status === 'skipped') {
      const existing = document.querySelector(`#scrape-progress [data-site="${CSS.escape(site)}"]`);
      if (existing) existing.classList.add('prog-skipped');
      continue;
    }
    const li = _ensureScrapePill(site);
    if (!li) continue;
    li.classList.remove('prog-done', 'prog-err', 'prog-empty', 'prog-blocked', 'prog-skipped');
    const cnt = li.querySelector('.prog-count');
    if (info.status === 'ok') {
      li.classList.add('prog-done');
      if (cnt) cnt.textContent = _PROG_LABEL.ok(info);
    } else if (info.status === 'empty') {
      li.classList.add('prog-empty');
      if (cnt) cnt.textContent = _PROG_LABEL.empty(info);
    } else if (info.status === 'blocked') {
      li.classList.add('prog-blocked');
      if (cnt) cnt.textContent = _PROG_LABEL.blocked(info);
    } else if (info.status === 'error' || info.ok === false) {
      li.classList.add('prog-err');
      if (cnt) cnt.textContent = _PROG_LABEL.error(info);
    } else if (info.ok === true) {
      // Backward-compat (older scraper before iter 9 status semantics)
      li.classList.add('prog-done');
      if (cnt) cnt.textContent = `${info.count ?? 0} Deals`;
    } else if (cnt) {
      cnt.textContent = '';
    }
    if (info.detail) li.title = info.detail;
  }
}

/* ═══ STATUS POLL ═══════════════════════════════════════════════════ */
async function updateStatus() {
  try { updateStatusUI(await api('/api/status')); } catch { /* silent */ }
}

function updateStatusUI(s) {
  // Sync the auto-scrape interval dropdown to the backend's value
  if (s.interval_minutes != null) {
    const sel = document.getElementById('interval-input');
    if (sel && !sel.matches(':focus') && sel.value !== String(s.interval_minutes)) {
      sel.value = String(s.interval_minutes);
    }
  }
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (s.scraping) {
    dot.className = 'dot busy';
    text.textContent = 'Scraping…';
  } else {
    dot.className = 'dot ok';
    text.textContent = s.last_scrape
      ? s.last_scrape.slice(0, 16).replace('T', ' ')
      : 'Noch nicht gescrapt';
  }
}

/* ═══ INTERVAL ══════════════════════════════════════════════════════ */
async function saveInterval() {
  const input = document.getElementById('interval-input');
  const mins  = parseInt(input.value) || 0;
  try {
    await api('/api/interval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minutes: mins }),
    });
    const hours = mins / 60;
    const label = mins === 0 ? 'aus' : (hours === 1 ? '1 h' : `${hours} h`);
    toast(mins === 0 ? 'Auto-Scrape deaktiviert' : `Auto-Scrape: alle ${label}`, 'success');
  } catch { toast('Fehler beim Speichern', 'error'); }
}

/* ═══ DRAWER & MODAL ═══════════════════════════════════════════════ */
function openDrawer() {
  document.getElementById('drawer').classList.remove('hidden');
  document.getElementById('drawer-overlay').classList.remove('hidden');
  loadDrawerTargets();
  loadDrawerAlerts();
  loadDrawerPurchased();
  loadDrawerBlocked();
}
function closeDrawer() {
  document.getElementById('drawer').classList.add('hidden');
  document.getElementById('drawer-overlay').classList.add('hidden');
}

function openAddModal() {
  document.getElementById('add-modal').classList.remove('hidden');
  populateGroupSuggestions();
  setTimeout(() => document.getElementById('modal-name').focus(), 50);
}
function closeAddModal() {
  document.getElementById('add-modal').classList.add('hidden');
  ['modal-name', 'modal-keyword', 'modal-group', 'modal-retail'].forEach(id => {
    document.getElementById(id).value = '';
  });
}

/* ═══ BLOCK ACTIONS (X-button popup) ════════════════════════════════ */
function toggleCardMenu(btn) {
  const popup = btn.parentElement.querySelector('.card-menu-popup');
  // Close every other open popup first
  document.querySelectorAll('.card-menu-popup').forEach(p => {
    if (p !== popup) p.classList.add('hidden');
  });
  popup?.classList.toggle('hidden');
}

// Click-away closes any open popup
document.addEventListener('click', (e) => {
  if (!e.target.closest('.card-menu-wrap')) {
    document.querySelectorAll('.card-menu-popup').forEach(p => p.classList.add('hidden'));
  }
});

async function blockDealFromCard(dealId, btn) {
  try {
    await api(`/api/deals/${dealId}/block`, { method: 'POST' });
    toast('Angebot ausgeblendet', 'success');
    loadDashboard();
    loadDrawerBlocked();
  } catch {
    toast('Fehler beim Blockieren', 'error');
  }
}

async function purchaseDealFromCard(dealId, btn) {
  try {
    await api(`/api/deals/${dealId}/purchase`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purchased: true }),
    });
    toast('Als gekauft markiert', 'success');
    loadDashboard();
    loadDrawerPurchased();
  } catch {
    toast('Fehler beim Markieren', 'error');
  }
}

async function purchaseGroup(groupName, btn) {
  if (!confirm(`Alle aktiven Deals in „${groupName}" als gekauft markieren?\n(Versteckt sie aus dem Dashboard, Preis-Historie bleibt erhalten.)`)) return;
  if (btn) btn.disabled = true;
  try {
    const res = await api(`/api/groups/${encodeURIComponent(groupName)}/purchase`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purchased: true }),
    });
    toast(`${res.updated ?? 0} Deals als gekauft markiert`, 'success');
    loadDashboard();
    loadDrawerPurchased();
  } catch {
    toast('Fehler beim Markieren der Gruppe', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function unpurchaseDeal(dealId) {
  try {
    await api(`/api/deals/${dealId}/purchase`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ purchased: false }),
    });
    toast('Wieder im Dashboard', 'success');
    loadDrawerPurchased();
    loadDashboard();
  } catch {
    toast('Fehler beim Rueckgaengig-Machen', 'error');
  }
}

async function blockSellerFromCard(website, seller, btn) {
  if (!confirm(`Alle Angebote von „${seller}" auf ${website} blockieren?`)) return;
  try {
    await api('/api/sellers/block', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ website, seller }),
    });
    toast(`Verkäufer „${seller}" blockiert`, 'success');
    loadDashboard();
    loadDrawerBlocked();
  } catch {
    toast('Fehler beim Blockieren', 'error');
  }
}

async function unblockDeal(dealId) {
  try {
    await api(`/api/deals/${dealId}/unblock`, { method: 'POST' });
    toast('Angebot wieder sichtbar', 'success');
    loadDrawerBlocked();
    loadDashboard();
  } catch { toast('Fehler', 'error'); }
}

async function unblockSeller(website, seller) {
  try {
    await api('/api/sellers/unblock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ website, seller }),
    });
    toast(`Verkäufer „${seller}" entsperrt`, 'success');
    loadDrawerBlocked();
    loadDashboard();
  } catch { toast('Fehler', 'error'); }
}

async function loadDrawerPurchased() {
  const list = document.getElementById('purchased-list');
  if (!list) return;
  try {
    const deals = await api('/api/purchased');
    if (!deals.length) {
      list.innerHTML = '<p class="blocked-list-empty">Noch nichts als gekauft markiert.</p>';
      return;
    }
    const fmt = new Intl.DateTimeFormat('de-DE', {
      day: '2-digit', month: '2-digit', year: 'numeric',
    });
    list.innerHTML = deals.map(d => {
      const when = d.purchased_at ? fmt.format(new Date(d.purchased_at)) : '–';
      const price = d.price != null ? d.price.toLocaleString('de-DE') + ' €' : '–';
      return `
        <div class="blocked-item">
          <div class="blocked-item-info">
            <div class="blocked-item-title">${esc(d.title)}</div>
            <div class="blocked-item-meta">${siteBadge(d.website)}
              <span>${price}</span>
              <span class="dim">· gekauft ${when}</span>
            </div>
          </div>
          <button class="blocked-item-unblock" onclick="unpurchaseDeal(${d.id})"
                  title="Wieder ins Dashboard zurueckholen">
            Zurueck
          </button>
        </div>`;
    }).join('');
  } catch { /* silent */ }
}


async function loadDrawerBlocked() {
  const list = document.getElementById('blocked-list');
  if (!list) return;
  try {
    const data = await api('/api/blocked');
    const sellers = data.sellers || [];
    const deals   = data.deals   || [];
    if (!sellers.length && !deals.length) {
      list.innerHTML = '<p class="blocked-list-empty">Nichts blockiert.</p>';
      return;
    }
    let html = '';
    if (sellers.length) {
      html += `<div class="blocked-list-group-head">Verkäufer (${sellers.length})</div>`;
      html += sellers.map(s => `
        <div class="blocked-item">
          <div class="blocked-item-info">
            <div class="blocked-item-title">${esc(s.seller)}</div>
            <div class="blocked-item-meta">${siteBadge(s.website)}</div>
          </div>
          <button class="blocked-item-unblock"
                  onclick="unblockSeller(${esc(JSON.stringify(s.website))}, ${esc(JSON.stringify(s.seller))})">
            Entsperren
          </button>
        </div>`).join('');
    }
    if (deals.length) {
      html += `<div class="blocked-list-group-head">Einzelne Angebote (${deals.length})</div>`;
      html += deals.map(d => `
        <div class="blocked-item">
          <div class="blocked-item-info">
            <div class="blocked-item-title">${esc(d.title)}</div>
            <div class="blocked-item-meta">${siteBadge(d.website)}
              <span>${d.price != null ? d.price.toLocaleString('de-DE') + ' €' : '–'}</span>
            </div>
          </div>
          <button class="blocked-item-unblock" onclick="unblockDeal(${d.id})">
            Entsperren
          </button>
        </div>`).join('');
    }
    list.innerHTML = html;
  } catch { /* silent */ }
}

/* ═══ GROUP SOURCE PICKER ════════════════════════════════════════════ */

let _allSourcesMeta = null;   // cached from /api/sources

async function _ensureSourcesMeta() {
  if (!_allSourcesMeta) {
    _allSourcesMeta = await api('/api/sources');
  }
  return _allSourcesMeta;
}

async function openGroupSourcePicker(groupName, btn) {
  // Close any other open picker first
  document.querySelectorAll('.source-picker-popup').forEach(p => p.remove());

  const [allSources, currentSources] = await Promise.all([
    _ensureSourcesMeta(),
    api(`/api/groups/${encodeURIComponent(groupName)}/sources`),
  ]);

  // Group sources by category for the UI
  const byCategory = {};
  for (const s of allSources) {
    const cat = s.category || 'Sonstige';
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(s);
  }

  const currentSet = new Set(currentSources);
  const allUnchecked = currentSet.size === 0;  // empty = all allowed

  let html = `
  <div class="source-picker-popup" id="sp-${esc(groupName)}" data-group-name="${esc(groupName)}">
    <div class="sp-head">
      <span class="sp-title">${icon('link')} Quellen für „${esc(groupName)}"</span>
      <button class="sp-close" onclick="this.closest('.source-picker-popup').remove()">✕</button>
    </div>
    <p class="sp-hint">Leer lassen = alle Quellen verwenden</p>`;

  for (const [cat, sources] of Object.entries(byCategory)) {
    html += `<div class="sp-cat-head">${esc(cat)}</div>`;
    for (const s of sources) {
      const checked = allUnchecked ? false : currentSet.has(s.name);
      const noteHtml = s.note ? ` <span class="sp-note">${esc(s.note)}</span>` : '';
      html += `
      <label class="sp-item">
        <input type="checkbox" class="sp-cb" value="${esc(s.name)}" ${checked ? 'checked' : ''}>
        <span>${esc(s.name)}${noteHtml}</span>
      </label>`;
    }
  }

  html += `
    <div class="sp-actions">
      <button class="btn-sm btn-ghost" onclick="clearGroupSources(${esc(JSON.stringify(groupName))})">
        Alle Quellen
      </button>
      <button class="btn-sm btn-primary" onclick="saveGroupSources(${esc(JSON.stringify(groupName))})">
        Speichern
      </button>
    </div>
  </div>`;

  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  const popup = wrapper.firstElementChild;

  // Append first so we can measure actual height — needed for flip logic below.
  popup.style.position = 'fixed';
  popup.style.visibility = 'hidden';
  popup.style.zIndex = '9999';
  document.body.appendChild(popup);

  // Smart placement: prefer below the button, but flip above when the bottom
  // half doesn't fit. Cap height to available space so the action buttons
  // ("Speichern" / "Alle Quellen") never get clipped at the bottom of the
  // viewport — that was the "unten rechts sieht man nd alle optionen" bug.
  const rect = btn.getBoundingClientRect();
  const vh = window.innerHeight;
  const vw = window.innerWidth;
  const margin = 12;            // breathing room top + bottom
  const popupW = popup.offsetWidth;
  const popupH = popup.offsetHeight;
  const spaceBelow = vh - rect.bottom - margin;
  const spaceAbove = rect.top - margin;

  let top, maxH;
  if (popupH <= spaceBelow || spaceBelow >= spaceAbove) {
    // Place below
    top = rect.bottom + 6;
    maxH = spaceBelow;
  } else {
    // Place above — bottom-anchor the popup
    maxH = spaceAbove;
    top  = Math.max(margin, rect.top - 6 - Math.min(popupH, maxH));
  }

  // Horizontal: keep aligned to the right edge of the button, but never let
  // the popup overflow the viewport on the right.
  let left = Math.max(8, rect.right - popupW);
  if (left + popupW > vw - 8) left = vw - popupW - 8;

  popup.style.top    = top + 'px';
  popup.style.left   = left + 'px';
  popup.style.maxHeight  = Math.max(160, maxH) + 'px';
  popup.style.visibility = 'visible';

  // Click-outside closes
  setTimeout(() => {
    document.addEventListener('click', function handler(e) {
      if (!popup.contains(e.target) && e.target !== btn) {
        popup.remove();
        document.removeEventListener('click', handler);
      }
    });
  }, 0);
}

async function saveGroupSources(groupName) {
  // Use querySelector with attribute selector to avoid ID-escaping issues
  const popup = document.querySelector(`.source-picker-popup[data-group-name="${esc(groupName)}"]`)
             || document.getElementById(`sp-${groupName}`);
  if (!popup) return;
  const sources = [...popup.querySelectorAll('.sp-cb:checked')].map(cb => cb.value);
  try {
    await api(`/api/groups/${encodeURIComponent(groupName)}/sources`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sources }),
    });
    const label = sources.length
      ? `${sources.length} Quelle${sources.length > 1 ? 'n' : ''} gespeichert`
      : 'Alle Quellen verwendet (keine Einschränkung)';
    toast(label, 'success');
    popup.remove();
    // Refresh group badge in header
    loadDashboard();
  } catch { toast('Fehler beim Speichern', 'error'); }
}

async function clearGroupSources(groupName) {
  try {
    await api(`/api/groups/${encodeURIComponent(groupName)}/sources`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sources: [] }),
    });
    toast('Alle Quellen werden verwendet', 'success');
    document.querySelectorAll('.source-picker-popup').forEach(p => p.remove());
    loadDashboard();
  } catch { toast('Fehler', 'error'); }
}

/* ═══ AUCTION MODAL (price-history chart) ═══════════════════════════ */
async function openAuctionModal(dealId) {
  const modal = document.getElementById('auction-modal');
  const meta  = document.getElementById('auction-modal-meta');
  const chart = document.getElementById('auction-modal-chart');
  const table = document.getElementById('auction-modal-table');
  const link  = document.getElementById('auction-modal-link');
  const titleEl = document.getElementById('auction-modal-title');

  meta.innerHTML  = '<div class="dim text-xs">Lade…</div>';
  chart.innerHTML = '';
  table.innerHTML = '';
  modal.classList.remove('hidden');

  try {
    // Fetch deal by ID + price history in parallel
    const [historyRows, d] = await Promise.all([
      api(`/api/price-history/${dealId}`),
      api(`/api/deals/${dealId}`),
    ]);
    if (!d || d.error) { titleEl.textContent = 'Angebot nicht gefunden'; return; }
    titleEl.textContent = d.title;
    link.href = d.url;

    // Iter. 27 D12: Direkt-Link zur eBay-Gebotsuebersicht. Reduziert die
    // Reibung des Paste-Flows: Klick → Tab geht auf der Bid-History-Seite
    // auf, Felix markiert Tabelle, kopiert, klickt zurueck "Gebote einfuegen".
    const viewBidsBtn = document.getElementById('auction-modal-viewbids');
    if (viewBidsBtn) {
      const idMatch = d.url ? d.url.match(/\/itm\/(?:[^/?#]*-)?(\d{9,15})|[?&]item=(\d{9,15})/) : null;
      const itemId = idMatch ? (idMatch[1] || idMatch[2]) : null;
      if (itemId) {
        const host = (d.url.match(/^https?:\/\/[^/]+/) || ['https://www.ebay.de'])[0];
        viewBidsBtn.href = `${host}/bfl/viewbids/${itemId}?item=${itemId}&rt=nc`;
        viewBidsBtn.style.display = '';
      } else {
        viewBidsBtn.style.display = 'none';
      }
    }
    const hasBidHistory = historyRows.some(r => r.source === 'ebay_bid');
    const rowLabel = hasBidHistory ? 'Gebote' : 'Snapshots';

    // Build meta cards. eBay doesn't expose end-time in the search listing
    // so we show "läuft" instead of pretending to know.
    const remaining = formatRemaining(d.auction_ends_at);
    const absoluteEnd = formatAbsoluteEnd(d.auction_ends_at);
    // Iter. 27 B8: Wenn wir das absolute Ende kennen, zeigen wir BEIDES:
    // grosser Countdown + kleinere absolute Zeit darunter ("Noch 2h 14m /
    // heute 17:35"). Macht klar wann die Auktion endet, ohne dass Felix
    // rechnen muss.
    const endsLabel = remaining ?? 'läuft';
    meta.innerHTML = `
      <div class="auction-meta-card">
        <div class="auction-meta-card-label">Aktuelles Gebot</div>
        <div class="auction-meta-card-value accent-pink">${d.price != null ? d.price.toLocaleString('de-DE') + ' €' : '–'}</div>
      </div>
      <div class="auction-meta-card">
        <div class="auction-meta-card-label">Gebote</div>
        <div class="auction-meta-card-value">${
          d.bid_count == null ? '–'
          : d.bid_count === 0 ? '0 (noch keine)'
          : d.bid_count}</div>
      </div>
      ${d.seller ? `<div class="auction-meta-card">
        <div class="auction-meta-card-label">Verkäufer</div>
        <div class="auction-meta-card-value" style="font-size:.82rem">@${esc(d.seller)}</div>
      </div>` : ''}
      <div class="auction-meta-card">
        <div class="auction-meta-card-label">Endet</div>
        <div class="auction-meta-card-value">${esc(endsLabel)}</div>
        ${absoluteEnd ? `<div class="auction-meta-card-sub">${esc(absoluteEnd)}</div>` : ''}
      </div>
      <div class="auction-meta-card">
        <div class="auction-meta-card-label">${rowLabel}</div>
        <div class="auction-meta-card-value">${historyRows.length}${
          !hasBidHistory && d.bid_count != null && d.bid_count > historyRows.length
            ? ` <span style="color:var(--text-dim);font-size:.7rem">von ${d.bid_count}</span>`
            : ''
        }</div>
      </div>
      <div class="auction-meta-card">
        <div class="auction-meta-card-label">${hasBidHistory ? 'Letztes Gebot' : 'Zuletzt erfasst'}</div>
        <div class="auction-meta-card-value" id="auction-meta-lastcap">${
          historyRows.length
            ? (hasBidHistory
                ? new Date(historyRows[historyRows.length - 1].changed_at).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' })
                : formatRelative(new Date(historyRows[historyRows.length - 1].changed_at).getTime()))
            : '–'
        }</div>
      </div>`;

    // Hint-Banner-Logik (Iter. 27 A5 erweitert)
    //   stale   = Bid-History importiert, aber Karten-Preis ist NEUER (Felix
    //             muss neu einfuegen, sonst stimmt Chart und Karte nicht)
    //   success = Bid-History importiert + konsistent mit Karten-Preis
    //   warning = keine Bid-History, eBay zeigt mehr Gebote als wir Snapshots haben
    //   hidden  = nichts auffaelliges
    const hint = document.getElementById('auction-modal-hint');
    const maxBidPrice = hasBidHistory
      ? Math.max(...historyRows.filter(r => r.source === 'ebay_bid').map(r => r.price))
      : null;
    const isStale = hasBidHistory && d.price != null && maxBidPrice != null
                  && d.price > maxBidPrice + 0.01;

    if (isStale) {
      const diff = (d.price - maxBidPrice).toFixed(2).replace('.', ',');
      hint.innerHTML = `${icon('alert')}
        <span>Karten-Preis <b>${d.price.toLocaleString('de-DE')} €</b> ist neuer als das letzte importierte Gebot (<b>${maxBidPrice.toLocaleString('de-DE')} €</b>, Differenz +${diff} €).
        Auf eBay sind seit dem letzten Import neue Gebote gefallen — Tabelle neu kopieren und <b>„Gebote einfügen"</b> klicken.</span>`;
      hint.dataset.mode = 'warning';
      hint.classList.remove('hidden');
    } else if (!hasBidHistory && d.bid_count != null && d.bid_count > historyRows.length) {
      hint.innerHTML = `${icon('alert')}
        <span>eBay zeigt <b>${d.bid_count} Gebote</b>, hier liegen aber nur <b>${historyRows.length}</b> Scrape-Snapshots.
        Öffne bei eBay die Gebotsübersicht, kopiere die Tabelle und nutze <b>„Gebote einfügen"</b>.</span>`;
      hint.dataset.mode = 'warning';
      hint.classList.remove('hidden');
    } else if (hasBidHistory) {
      // Iter. 27 D13: zeige zusaetzlich wann der Import passierte.
      const importedAt = d.bid_history_imported_at
        ? formatRelative(new Date(d.bid_history_imported_at).getTime())
        : null;
      const sub = importedAt ? ` <span class="dim">· eingespielt ${esc(importedAt)}</span>` : '';
      hint.innerHTML = `${icon('check')}
        <span>Echte eBay-Gebotsübersicht importiert: Beträge und Uhrzeiten kommen aus der eBay-Tabelle.${sub}</span>`;
      hint.dataset.mode = 'success';
      hint.classList.remove('hidden');
    } else {
      hint.classList.add('hidden');
      hint.dataset.mode = '';
    }

    // Wire up refresh button — re-fetches the eBay item page and adds a snapshot.
    // We stash the current price + bid_count in the button's dataset so the
    // next refresh can diff and emit a meaningful toast ("Preis unverändert"
    // vs "Neues Gebot: +5 €") — gives the user real feedback that the click
    // did something even when nothing actually changed.
    const refreshBtn = document.getElementById('auction-modal-refresh');
    refreshBtn.dataset.dealId   = dealId;
    refreshBtn.dataset.lastPrice = d.price == null ? '' : String(d.price);
    refreshBtn.dataset.lastBids  = d.bid_count == null ? '' : String(d.bid_count);
    refreshBtn.disabled = false;
    const importBtn = document.getElementById('auction-modal-import');
    importBtn.dataset.dealId = dealId;
    importBtn.disabled = false;

    // Build chart series from price_history.
    // The DB stores ISO timestamps WITHOUT a "Z" suffix, which modern JS
    // interprets as LOCAL time — matches what database.py wrote with
    // datetime.now().isoformat(). So no TZ conversion needed.
    const series = historyRows.map(r => ({
      t: new Date(r.changed_at).getTime(),
      p: r.price,
      bidder: r.bidder || null,
      source: r.source || 'snapshot',
    }));
    // Iter. 27 A1: For fallback snapshots we used to append a synthetic point
    // stamped with Date.now() and labeled "jetzt (live)". That was a lie: when
    // the last scrape ran 4 h ago, the price shown was 4 h old, not live. Now
    // we stamp the appended point with d.last_seen (when the scrape actually
    // confirmed the price) and label the row "zuletzt erfasst" so the user
    // knows the freshness of that value. Still skipped for imported eBay bids
    // because the bid timeline is already authoritative.
    if (!hasBidHistory && d.price != null) {
      const lastSeenMs = d.last_seen ? new Date(d.last_seen).getTime() : Date.now();
      const lastT = series.length ? series[series.length - 1].t : 0;
      if (lastSeenMs - lastT > 30_000) {
        series.push({ t: lastSeenMs, p: d.price, synthetic: true });
      }
    }
    let chartSeries = hasBidHistory ? buildAuctionProgressSeries(series) : series;

    // Iter. 27 A4: Wenn die importierte Bid-History aelter ist als der aktuelle
    // Suchergebnis-Preis (typisch: Scrape lief NACH dem Bid-History-Paste, eBay
    // hat ein neueres Gebot, das wir nicht in der Tabelle haben), zeigen wir
    // einen explizit gestrichelten "Suche"-Punkt am Karten-Preis. Visuell
    // klar abgegrenzt von echten Geboten, damit Felix sieht: "hier liegt der
    // aktuelle Stand laut Suche, die Bid-History endet aber weiter unten."
    if (hasBidHistory && chartSeries.length && d.price != null) {
      const lastPoint = chartSeries[chartSeries.length - 1];
      if (d.price > lastPoint.p + 0.01) {
        const lastSeenMs = d.last_seen ? new Date(d.last_seen).getTime() : Date.now();
        chartSeries = [...chartSeries, {
          t: Math.max(lastSeenMs, lastPoint.t + 1),
          p: d.price,
          shadow: true,
        }];
      }
    }

    const endsMs = d.auction_ends_at ? new Date(d.auction_ends_at).getTime() : null;
    chart.innerHTML = renderAuctionChart(chartSeries, { endsMs });  // SVG, kein User-Input

    // History table — 4-digit year, no ambiguity (DD.MM.YYYY HH:MM).
    if (series.length) {
      const fmt = new Intl.DateTimeFormat('de-DE', {
        day: '2-digit', month: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
      const rows = [...series].reverse().map(r => `
        <tr${r.synthetic ? ' class="snapshot-now"' : ''}>
          <td>${fmt.format(new Date(r.t))}${r.synthetic ? ' <span class="dim text-xs">· zuletzt erfasst</span>' : ''}</td>
          ${hasBidHistory ? `<td>${esc(r.bidder || '–')}</td>` : ''}
          <td class="price-cell">${r.p.toLocaleString('de-DE')} €</td>
        </tr>`).join('');
      table.innerHTML = `<table>
        <thead><tr><th>Zeitpunkt</th>${hasBidHistory ? '<th>Bieter</th>' : ''}<th>${hasBidHistory ? 'Gebotsbetrag' : 'Preis'}</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    } else {
      table.innerHTML = '';
    }
  } catch (err) {
    meta.innerHTML = `<div class="dim">Fehler: ${esc(err?.message || '')}</div>`;
  }
}

function closeAuctionModal() {
  document.getElementById('auction-modal').classList.add('hidden');
  stopAuctionLive();  // never leave a polling timer running after modal close
}

/* ── Live-Mode: poll the currently-open auction every 30s ────────────
   eBay doesn't expose bid history without login, so we can't backfill
   missed bids. The best we can do is fetch MORE often while the user
   is actively watching — every 30s gives a fighting chance of catching
   bids that drop between global scrapes (every 4h by default). */
let _auctionLiveTimer = null;
let _auctionLiveDealId = null;
const AUCTION_LIVE_INTERVAL_MS = 30_000;

function startAuctionLive(dealId) {
  stopAuctionLive();
  _auctionLiveDealId = dealId;
  // First tick after the interval, not immediately — the modal already has
  // fresh data from openAuctionModal.
  _auctionLiveTimer = setInterval(async () => {
    // Guard: bail out if modal closed externally or focus shifted.
    const modal = document.getElementById('auction-modal');
    if (!modal || modal.classList.contains('hidden')) {
      stopAuctionLive();
      return;
    }
    // Snapshot the pre-refresh state from the refresh button's dataset so
    // we can detect an actual new bid landing during the live poll.
    const refBtn = document.getElementById('auction-modal-refresh');
    const prevP  = refBtn?.dataset.lastPrice ? parseFloat(refBtn.dataset.lastPrice) : null;
    const prevB  = refBtn?.dataset.lastBids  ? parseInt(refBtn.dataset.lastBids, 10) : null;
    try {
      const res = await api(`/api/deals/${_auctionLiveDealId}/refresh`, { method: 'POST' });
      _sparkCache.delete(_auctionLiveDealId);

      // Toast ONLY on real change — silent otherwise so we don't spam every 30s.
      const newP = res?.deal?.price ?? null;
      const newB = res?.deal?.bid_count ?? null;
      if (prevP != null && newP != null && Math.abs(newP - prevP) >= 0.01) {
        toast(`Live: neues Gebot ${newP.toLocaleString('de-DE')} €`, 'success');
      } else if (prevB != null && newB != null && newB > prevB) {
        toast(`Live: ${newB - prevB} neue${newB - prevB === 1 ? 's' : ''} Gebot · ${newP?.toLocaleString('de-DE') ?? '?'} €`, 'success');
      }

      // Re-render meta + chart + table; openAuctionModal also resets the
      // refresh-button label, which is harmless here.
      await openAuctionModal(_auctionLiveDealId);
      // Re-mark live as active because openAuctionModal resets the toggle.
      _markLiveActive(true);
    } catch (err) {
      // Don't toast on every failure (could be a temporary 502) — log only.
      console.warn('[live] refresh failed', err);
    }
  }, AUCTION_LIVE_INTERVAL_MS);
  _markLiveActive(true);
}

function stopAuctionLive() {
  if (_auctionLiveTimer) {
    clearInterval(_auctionLiveTimer);
    _auctionLiveTimer = null;
  }
  _auctionLiveDealId = null;
  _markLiveActive(false);
}

function toggleAuctionLive() {
  const dealId = parseInt(
    document.getElementById('auction-modal-refresh')?.dataset.dealId || '0',
    10
  );
  if (!dealId) return;
  if (_auctionLiveTimer) stopAuctionLive();
  else startAuctionLive(dealId);
}

function _markLiveActive(on) {
  const btn   = document.getElementById('auction-modal-live');
  const label = btn?.querySelector('.live-label');
  if (!btn) return;
  btn.classList.toggle('live-on', !!on);
  if (label) label.textContent = on ? 'Live · alle 30 s' : 'Live: aus';
}

/* Live-refresh a single auction: hits POST /api/deals/<id>/refresh which
   fetches the eBay item page now, writes a new price_history snapshot,
   and returns the updated deal. We then re-render the modal contents.
   Lets the user poll a hot auction without waiting for the next scrape. */
async function refreshAuctionDeal(btn) {
  const dealId = parseInt(btn.dataset.dealId, 10);
  if (!dealId) return;
  const lbl  = btn.querySelector('span');
  const orig = lbl?.textContent;
  // Captured BEFORE the refresh so we can diff against the response and
  // show a "Preis unverändert" / "Neues Gebot" toast — without that the
  // chart looks frozen when consecutive snapshots land on the same y-level
  // (which is the norm: bids can be hours apart on a slow auction).
  const oldPrice = btn.dataset.lastPrice ? parseFloat(btn.dataset.lastPrice) : null;
  const oldBids  = btn.dataset.lastBids  ? parseInt(btn.dataset.lastBids, 10) : null;

  btn.disabled = true;
  if (lbl) lbl.textContent = 'Lädt…';
  btn.querySelector('svg')?.classList.add('spin');
  try {
    const res = await api(`/api/deals/${dealId}/refresh`, { method: 'POST' });
    _sparkCache.delete(dealId);            // invalidate sparkline cache

    // Iter. 26: auction ended (Verkaeufer-Cancel oder Endzeit erreicht) → eBay
    // sagt es ist vorbei, Backend hat available=0 gesetzt. Modal schliessen +
    // Dashboard reloaden damit der Deal aus der Liste verschwindet.
    if (res?.ended) {
      toast('Auktion ist beendet — wurde aus der Liste entfernt', 'info');
      closeAuctionModal();
      loadDashboard();
      return;
    }

    const newPrice    = res?.deal?.price ?? null;
    const newBids     = res?.deal?.bid_count ?? null;
    const bidsImported = res?.bids_imported || 0;
    if (bidsImported > 0) {
      // We just pulled the FULL eBay bid history — chart will show every
      // increment, not a sampled subset. This is the "good" outcome.
      toast(`Komplette Gebots-Historie geladen: ${bidsImported} Einträge`, 'success');
    } else {
      _emitRefreshToast(oldPrice, newPrice, oldBids, newBids);
      // Iter. 27 D14: Wenn der Preis gestiegen ist UND wir frueher mal eine
      // Bid-History importiert hatten, ist die Tabelle jetzt veraltet (das
      // neue Gebot fehlt). Sanfter Hinweis im Toast statt stiller Inkonsistenz.
      const importedPrice = res?.deal?.bid_history_imported_price;
      if (res?.deal?.bid_history_imported_at && newPrice != null
          && importedPrice != null && newPrice > importedPrice + 0.01) {
        setTimeout(() => {
          toast('Karten-Preis neuer als importierte Gebote — bei eBay Gebotsübersicht neu kopieren', 'warning');
        }, 600);
      }
    }

    if (lbl) lbl.textContent = 'Aktualisiert ✓';
    await openAuctionModal(dealId);         // re-render with fresh data
    setTimeout(() => loadDashboard(), 300); // pull fresh bid_count onto the cards
  } catch (err) {
    // Iter. 26: api() throws on non-2xx — surface eBay-block specifically so
    // Felix knows it's not our bug and to try again later.
    const msg = err?.message || 'unbekannt';
    if (/eBay-Block/i.test(msg) || /503/.test(msg)) {
      toast('eBay blockt die Item-Seite gerade — versuch es in ein paar Minuten nochmal', 'warning');
    } else {
      toast(`Fehler: ${msg}`, 'error');
    }
    if (lbl) lbl.textContent = orig;
    btn.disabled = false;
  } finally {
    btn.querySelector('svg')?.classList.remove('spin');
  }
}

async function importAuctionBidHistory(btn) {
  const dealId = parseInt(btn.dataset.dealId, 10);
  if (!dealId) return;
  let text = '';
  try {
    text = await navigator.clipboard.readText();
  } catch {
    text = window.prompt(
      'eBay-Gebotstabelle einfügen: Auf der eBay-Gebotsseite Tabelle markieren, kopieren, hier einfügen.'
    ) || '';
  }
  if (!text.trim()) {
    toast('Zwischenablage leer oder nicht freigegeben', 'warning');
    return;
  }

  const lbl = btn.querySelector('span');
  const orig = lbl?.textContent;
  btn.disabled = true;
  if (lbl) lbl.textContent = 'Importiert...';
  try {
    const res = await api(`/api/deals/${dealId}/bid-history/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    _sparkCache.delete(dealId);
    toast(`Gebotsverlauf importiert: ${res?.bids_imported || 0} Einträge`, 'success');
    await openAuctionModal(dealId);
    setTimeout(() => loadDashboard(), 300);
  } catch (err) {
    toast(`Import fehlgeschlagen: ${err?.message || 'keine Gebote erkannt'}`, 'error');
    if (lbl) lbl.textContent = orig;
    btn.disabled = false;
  }
}

/* Pick the right toast for a refresh result. The user just clicked a button
   and needs to know what changed — a silent no-op feels like the button is
   broken even though we wrote a snapshot. */
function _emitRefreshToast(oldP, newP, oldB, newB) {
  const fmtEur = v => v.toLocaleString('de-DE') + ' €';
  if (newP == null) {
    toast('Refresh OK, aber kein Preis ablesbar', 'warning');
    return;
  }
  if (oldP != null && Math.abs(newP - oldP) >= 0.01) {
    const diff = newP - oldP;
    const sign = diff > 0 ? '+' : '−';
    toast(`Neues Gebot: ${fmtEur(newP)} (${sign}${fmtEur(Math.abs(diff)).replace(' €','')} €)`, 'success');
    return;
  }
  if (oldB != null && newB != null && newB > oldB) {
    const added = newB - oldB;
    toast(`${added} neue${added === 1 ? 's' : ''} Gebot erfasst · Preis ${fmtEur(newP)}`, 'success');
    return;
  }
  toast(`Preis unverändert bei ${fmtEur(newP)} — Snapshot trotzdem gespeichert`, 'info');
}

function buildAuctionProgressSeries(rawSeries) {
  const sorted = [...rawSeries].sort((a, b) => {
    const dt = a.t - b.t;
    if (dt) return dt;
    return a.p - b.p;
  });
  let current = null;
  return sorted.map(s => {
    current = current == null ? s.p : Math.max(current, s.p);
    return { ...s, rawPrice: s.p, p: current };
  });
}

/* Render an SVG line chart for the auction price history.
   series: [{ t: ms, p: euros }] sorted ascending by t.
   opts: { endsMs }  optional auction-end-timestamp; if provided and after
                     the last data point, the x-axis extends to it and a red
                     "Ende"-marker line is drawn (Iter. 27 A3). */
function renderAuctionChart(series, opts = {}) {
  if (!series.length) {
    return '<div class="auction-chart-empty">Noch keine Daten — die Historie wächst bei jedem Scrape.</div>';
  }
  // series.length === 1 ist mit dem synthetischen "jetzt"-Punkt aus
  // openAuctionModal eigentlich unreachable. Fallback fuer den absurden
  // Edge-Case (DB-Snapshot existiert, d.price ist null) bleibt drin.
  if (series.length === 1) {
    return `<div class="auction-chart-empty">
      Nur 1 Datenpunkt: ${series[0].p.toLocaleString('de-DE')} € am
      ${new Date(series[0].t).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' })}.
    </div>`;
  }
  const W = 680, H = 220, pad = { l: 50, r: 16, t: 18, b: 30 };
  const tMin = series[0].t;
  let tMax = series[series.length - 1].t;
  // Iter. 27 A3: x-axis extends to auction-end so the gap between "last bid"
  // and "auction closes" is visible. Otherwise the chart always ends at the
  // last data point and Felix has no visual cue how much time is left.
  const endsMs = opts.endsMs && opts.endsMs > tMin ? opts.endsMs : null;
  if (endsMs && endsMs > tMax) tMax = endsMs;
  const tSpan = Math.max(1, tMax - tMin);
  const ps = series.map(s => s.p);
  const pMin = Math.min(...ps);
  const pMax = Math.max(...ps);
  const pSpan = Math.max(1, pMax - pMin);
  const padY = pSpan * 0.12;
  const yMin = Math.max(0, pMin - padY);
  const yMax = pMax + padY;
  const ySpan = Math.max(1, yMax - yMin);

  const x = (t) => pad.l + ((t - tMin) / tSpan) * (W - pad.l - pad.r);
  const y = (p) => H - pad.b - ((p - yMin) / ySpan) * (H - pad.t - pad.b);

  // Iter. 27 A2: Step-line statt diagonaler Verbindung. Bei Auktionen aendert
  // sich der Preis nur bei echten Geboten, dazwischen ist er konstant. Eine
  // diagonale Linie suggeriert kontinuierlichen Anstieg und macht Sampling-
  // Snapshots traegerisch unsichtbar. Treppen-Profil (H-Move, dann V-Move)
  // zeigt ehrlich an: "Preis blieb konstant bis zum naechsten Datenpunkt".
  const pts = series.map(s => ({ x: x(s.t), y: y(s.p) }));
  let path = `M ${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    path += ` H ${pts[i].x.toFixed(1)} V ${pts[i].y.toFixed(1)}`;
  }
  // Area-Fill folgt dem gleichen Step-Profil bis zur Baseline.
  let area = `M ${pad.l.toFixed(1)},${(H - pad.b).toFixed(1)} L ${pts[0].x.toFixed(1)},${pts[0].y.toFixed(1)}`;
  for (let i = 1; i < pts.length; i++) {
    area += ` H ${pts[i].x.toFixed(1)} V ${pts[i].y.toFixed(1)}`;
  }
  area += ` L ${pts[pts.length - 1].x.toFixed(1)},${(H - pad.b).toFixed(1)} Z`;

  // Y axis: 4 horizontal gridlines
  let grid = '';
  let yLabels = '';
  for (let i = 0; i <= 3; i++) {
    const py = pad.t + ((H - pad.t - pad.b) / 3) * i;
    const pp = yMax - (ySpan / 3) * i;
    grid += `<line class="grid-line" x1="${pad.l}" y1="${py}" x2="${W - pad.r}" y2="${py}"/>`;
    yLabels += `<text class="axis-label" x="${pad.l - 6}" y="${py + 3}" text-anchor="end">${Math.round(pp).toLocaleString('de-DE')} €</text>`;
  }
  // X axis: start/end only. Dense bid histories often have many points within
  // minutes, and middle labels become unreadable fast.
  const xTicks = [0, series.length - 1];
  let xLabels = '';
  for (const idx of xTicks) {
    const s = series[idx];
    const lbl = new Date(s.t).toLocaleString('de-DE', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
    const isFirst = idx === 0;
    const isLast = idx === series.length - 1;
    const anchor = isFirst ? 'start' : (isLast ? 'end' : 'middle');
    const tx = isFirst ? pad.l : (isLast ? W - pad.r : x(s.t));
    xLabels += `<text class="axis-label" x="${tx}" y="${H - 8}" text-anchor="${anchor}">${lbl}</text>`;
  }
  // Data points — last one gets a bigger ring + pulse so the user can see
  // that a manual refresh / live tick actually added something, even when
  // the new sample lands at the same price level as the previous ones
  // (which is the normal case for auctions between bids).
  const lastIdx = series.length - 1;
  const dots = series.map((s, i) => {
    const isLast    = i === lastIdx;
    const isShadow  = s.shadow === true;       // Iter. 27 A4: dashed "Suche"-Punkt
    const r         = isLast ? 5.5 : 3.5;
    const cls       = isShadow ? 'price-point price-point-shadow'
                    : isLast   ? 'price-point price-point-latest'
                    : 'price-point';
    return `<circle class="${cls}" cx="${pts[i].x.toFixed(1)}" cy="${pts[i].y.toFixed(1)}" r="${r}"/>`;
  }).join('');

  // Iter. 27 A3: vertikale Ende-Marker-Linie + Beschriftung. Nur sichtbar wenn
  // wir das Auktions-Ende kennen UND es nicht direkt mit dem letzten Datenpunkt
  // zusammenfaellt (sonst ueberlappt die Linie mit dem Latest-Punkt).
  let endMarker = '';
  if (endsMs && endsMs > series[series.length - 1].t) {
    const ex   = x(endsMs);
    const elbl = new Date(endsMs).toLocaleString('de-DE', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });
    endMarker = `
      <line class="end-marker-line" x1="${ex.toFixed(1)}" y1="${pad.t}" x2="${ex.toFixed(1)}" y2="${H - pad.b}"/>
      <text class="end-marker-label" x="${ex.toFixed(1)}" y="${pad.t - 4}" text-anchor="middle">Ende ${esc(elbl)}</text>`;
  }

  return `
<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
  <defs>
    <linearGradient id="auctionGrad" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%"   stop-color="#f472b6" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#f472b6" stop-opacity="0"/>
    </linearGradient>
  </defs>
  ${grid}
  ${yLabels}
  ${xLabels}
  <path class="price-area" d="${area}"/>
  <path class="price-path" d="${path}"/>
  ${dots}
  ${endMarker}
</svg>`;
}

/* Hydrate sparklines on the dashboard after it renders.
   For each auction card on screen, fetch its price history once
   (or use cached) and draw a tiny line in the inline SVG. */
const _sparkCache = new Map();
async function hydrateSparklines() {
  const sparks = document.querySelectorAll('svg.auction-sparkline[data-deal-id]:empty');
  for (const svg of sparks) {
    const id = parseInt(svg.dataset.dealId, 10);
    // Iter. 27 E16: Karten-Preis als finalen Sparkline-Punkt anhaengen damit
    // Sparkline-Ende und Karten-Preis garantiert uebereinstimmen. Der Wrap-
    // Container traegt data-deal-price + data-deal-lastseen, geschrieben in
    // auctionInlineHtml() (siehe unten).
    const wrap = svg.closest('.auction-spark-wrap');
    const cardPrice = wrap?.dataset.dealPrice ? parseFloat(wrap.dataset.dealPrice) : null;
    const lastSeenMs = wrap?.dataset.dealLastseen
      ? new Date(wrap.dataset.dealLastseen).getTime() : null;

    let series = _sparkCache.get(id);
    if (!series) {
      try {
        const rows = await api(`/api/price-history/${id}`);
        series = rows.map(r => ({
          t: new Date(r.changed_at).getTime(),
          p: r.price,
          source: r.source || 'snapshot',
        }));
        // Iter. 27 E15: buildAuctionProgressSeries IMMER anwenden (auch fuer
        // reine Snapshots) damit die Sparkline-Linie monoton steigend ist.
        // Auktionen koennen niemals fallen — fallende Linie waere ein
        // Render-Bug, kein Datenzustand.
        series = buildAuctionProgressSeries(series);
        _sparkCache.set(id, series);
      } catch { continue; }
    }

    // E16: Wenn Karten-Preis > letzter Series-Punkt, haenge einen Shadow-Punkt
    // an (eBay-Suche meldet hoeheren Preis als unsere Bid-History/Snapshots).
    // So endet die Sparkline IMMER auf dem aktuellen Karten-Preis.
    let displaySeries = series;
    if (cardPrice != null && series.length
        && cardPrice > series[series.length - 1].p + 0.01) {
      const tEnd = Math.max(
        lastSeenMs || Date.now(),
        series[series.length - 1].t + 1,
      );
      displaySeries = [...series, { t: tEnd, p: cardPrice, shadow: true }];
    }

    if (displaySeries.length < 2) {
      // First scrape only — show a labeled hint instead of an empty box.
      svg.innerHTML = `
        <line x1="2" y1="16" x2="98" y2="16" stroke="rgba(244,114,182,0.35)" stroke-width="1" stroke-dasharray="2 3"/>
        <text x="50" y="20" text-anchor="middle" fill="rgba(244,114,182,0.85)"
              font-size="9" font-weight="600" style="letter-spacing:.5px">
          Verlauf entsteht beim nächsten Scrape
        </text>`;
      continue;
    }
    const ps = displaySeries.map(s => s.p);
    const pMin = Math.min(...ps), pMax = Math.max(...ps);
    const span = Math.max(1, pMax - pMin);
    const ts   = displaySeries.map(s => s.t);
    const tMin = ts[0], tMax = ts[ts.length - 1];
    const tSpan = Math.max(1, tMax - tMin);
    const xy = displaySeries.map(s => ({
      x: ((s.t - tMin) / tSpan) * 100,
      y: 28 - ((s.p - pMin) / span) * 24,
      shadow: s.shadow === true,
    }));
    // Step-line passend zum Modal-Chart (Iter. 27 A2-Parität).
    let path = `M ${xy[0].x.toFixed(1)},${xy[0].y.toFixed(1)}`;
    for (let i = 1; i < xy.length; i++) {
      path += ` H ${xy[i].x.toFixed(1)} V ${xy[i].y.toFixed(1)}`;
    }
    let area = `M 0,32 L ${xy[0].x.toFixed(1)},${xy[0].y.toFixed(1)}`;
    for (let i = 1; i < xy.length; i++) {
      area += ` H ${xy[i].x.toFixed(1)} V ${xy[i].y.toFixed(1)}`;
    }
    area += ` L ${xy[xy.length - 1].x.toFixed(1)},32 Z`;
    const last = xy[xy.length - 1];
    const endpointCls = last.shadow ? 'sparkline-end-shadow' : '';
    svg.innerHTML = `<path class="auction-sparkline-area" d="${area}"/>
                     <path d="${path}"/>
                     <circle class="${endpointCls}" cx="${last.x.toFixed(1)}" cy="${last.y.toFixed(1)}" r="1.6"/>`;
  }
}

// Re-hydrate after dashboard loads (loadDashboard rewrites innerHTML).
// Wrap loadDashboard so we don't have to thread the call through every code path.
const _origLoadDashboard = loadDashboard;
loadDashboard = async function (...args) {
  await _origLoadDashboard.apply(this, args);
  hydrateSparklines();
};

/* ═══ ITER. 29: Bookmarklet-Sync ═══════════════════════════════════════
   eBay blockt unsere Direkt-Requests mit Akamai. Workaround: ein Bookmarklet
   das Felix EINMAL in seiner Lesezeichen-Leiste speichert. Klick darauf im
   eBay-Tab → fetch() schickt das geladene HTML (inkl. Login-Cookie-Auth) an
   unsere localhost-API → wir parsen Gebote + Preis serverseitig.

   Der Code muss kompakt sein (Bookmarklet-Limit ~2 KB). 'text/plain' als
   Content-Type macht's zum "simple CORS request" — Browser sendet keinen
   Preflight, was viele CSP-Probleme umgeht. */
function _buildBookmarkletCode() {
  // Backend-Origin dynamisch — funktioniert auch wenn Felix den Port aendert.
  const apiOrigin = `${location.protocol}//${location.host}`;
  // IIFE-Body — bewusst kurz, ohne unnoetigen Whitespace.
  // Item-ID-Extraktion: ?item=NNN ODER /NNN/ ODER /itm/NNN ODER itemId="NNN".
  const src = `(async()=>{try{const u=new URL(location.href);` +
    `const q=u.searchParams.get('item');` +
    `const p=(u.pathname.match(/\\b(\\d{10,15})\\b/)||[])[1];` +
    `const m=(document.documentElement.outerHTML.match(/itemId["':\\s]+(\\d{10,15})/)||[])[1];` +
    `const id=q||p||m||'';` +
    `const r=await fetch('${apiOrigin}/api/ebay-paste-html'+(id?'?item='+id:''),` +
    `{method:'POST',headers:{'Content-Type':'text/plain'},body:document.documentElement.outerHTML,mode:'cors'});` +
    `const d=await r.json();` +
    `if(d.ok){const f=(d.fields_updated||[]).join(', ')||'-';` +
    `alert('OK DealScraper-Sync\\n\\nGebote: '+(d.bids_imported||0)+'\\nFelder: '+f+'\\nDeal-ID: '+d.deal_id)}` +
    `else{alert('FEHLER DealScraper-Sync\\n\\n'+(d.error||'Unbekannt'))}` +
    `}catch(e){alert('FEHLER DealScraper-Sync\\n\\n'+e.message+` +
    `'\\n\\nLaeuft die App? Bei CSP-Block den manuellen Paste-Pfad nutzen.')}})()`;
  return 'javascript:' + encodeURI(src);
}

function _initBookmarkletUI() {
  const a = document.getElementById('sync-bookmarklet');
  const copyBtn = document.getElementById('sync-copy-btn');
  if (!a) return;
  const code = _buildBookmarkletCode();
  a.setAttribute('href', code);
  a.addEventListener('click', e => {
    // Browser laesst javascript: hrefs aus normalen Klicks oft nicht zu
    // (security feature). Drag in die Lesezeichen-Leiste funktioniert immer.
    e.preventDefault();
    toast('Ziehe diesen Button in die Lesezeichen-Leiste — Klick ist hier deaktiviert.', 'info');
  });
  if (copyBtn) {
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(code);
        toast('Bookmarklet-Code kopiert. In Chrome: Strg+D → URL ersetzen.', 'success');
      } catch {
        // Fallback fuer geblockte Clipboard-API: in textarea + manuelles Copy
        const ta = document.createElement('textarea');
        ta.value = code; document.body.appendChild(ta);
        ta.select(); document.execCommand('copy'); ta.remove();
        toast('Bookmarklet-Code kopiert (Fallback).', 'success');
      }
    });
  }
}

/* ═══ INIT ══════════════════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
  loadDashboard();
  updateStatus();
  setInterval(updateStatus, 15_000);

  // Search
  const searchInput = document.getElementById('search-input');
  searchInput.addEventListener('input', applySearchFilter);

  // Top buttons
  document.getElementById('add-target-btn').addEventListener('click', openAddModal);
  document.getElementById('scrape-btn').addEventListener('click', triggerScrape);
  document.getElementById('interval-input').addEventListener('change', saveInterval);

  // Iter. 36: Klick auf Status-Pille oeffnet das Scrape-Status-Fenster
  document.getElementById('status-pill')?.addEventListener('click', async () => {
    try {
      await fetch('/api/scrape-window/show', { method: 'POST' });
    } catch (e) { /* silent */ }
  });

  // Iter. 26: Scrape-Overlay minimieren — Felix wollte waehrend des Scrapings
  // weiter mit der App arbeiten. Toggle via Header-Button, Click auf Backdrop
  // (ausserhalb der Card), oder Click auf das minimierte Pillchen.
  const _scrapeOverlay = document.getElementById('scrape-overlay');
  const _scrapeMinBtn  = document.getElementById('scrape-overlay-min');
  function minimizeScrapeOverlay() { _scrapeOverlay?.classList.add('minimized'); }
  function maximizeScrapeOverlay() { _scrapeOverlay?.classList.remove('minimized'); }
  _scrapeMinBtn?.addEventListener('click', e => {
    e.stopPropagation();
    if (_scrapeOverlay.classList.contains('minimized')) maximizeScrapeOverlay();
    else minimizeScrapeOverlay();
  });
  _scrapeOverlay?.addEventListener('click', e => {
    // Click direkt auf das Overlay (Backdrop) → minimieren. Click in der Card
    // im minimierten Modus → maximieren.
    if (e.target === _scrapeOverlay && !_scrapeOverlay.classList.contains('minimized')) {
      minimizeScrapeOverlay();
    } else if (_scrapeOverlay.classList.contains('minimized')) {
      maximizeScrapeOverlay();
    }
  });

  // Modal
  document.getElementById('add-modal-close').addEventListener('click', closeAddModal);
  document.getElementById('modal-cancel').addEventListener('click', closeAddModal);
  document.getElementById('modal-submit').addEventListener('click', addTargetFromModal);
  document.getElementById('add-modal').addEventListener('click', e => {
    if (e.target.id === 'add-modal') closeAddModal();
  });
  ['modal-name', 'modal-keyword'].forEach(id => {
    document.getElementById(id).addEventListener('keydown', e => {
      if (e.key === 'Enter') addTargetFromModal();
    });
  });

  // Drawer
  document.getElementById('settings-btn').addEventListener('click', openDrawer);
  document.getElementById('drawer-close').addEventListener('click', closeDrawer);
  document.getElementById('drawer-overlay').addEventListener('click', closeDrawer);

  // Auction price-history modal
  document.getElementById('auction-modal-close')?.addEventListener('click', closeAuctionModal);
  document.getElementById('auction-modal')?.addEventListener('click', e => {
    if (e.target.id === 'auction-modal') closeAuctionModal();
  });
  _initBookmarkletUI();   // Iter. 29: Bookmarklet-Sync ein-Klick-Aktualisierung
  document.getElementById('auction-modal-refresh')?.addEventListener('click', e => {
    refreshAuctionDeal(e.currentTarget);
  });
  document.getElementById('auction-modal-live')?.addEventListener('click', toggleAuctionLive);
  document.getElementById('auction-modal-import')?.addEventListener('click', e => {
    importAuctionBidHistory(e.currentTarget);
  });

  // Alerts
  document.getElementById('alert-save-btn').addEventListener('click', saveAlert);
  document.getElementById('alert-threshold').addEventListener('keydown', e => {
    if (e.key === 'Enter') saveAlert();
  });
  document.getElementById('export-btn').addEventListener('click', () => {
    window.location.href = '/api/export/csv';
    toast('CSV wird heruntergeladen…', 'info');
  });

  // PLZ-Umkreis-Filter (Iter. 24)
  loadFilterSettings();
  document.getElementById('plz-input')?.addEventListener('input', e => {
    _filterPlz = e.target.value.replace(/\D/g, '').slice(0, 5);
    if (e.target.value !== _filterPlz) e.target.value = _filterPlz;
    scheduleFilterSave();
  });
  document.getElementById('radius-slider')?.addEventListener('input', e => {
    _filterRadius = Number(e.target.value) || 0;
    scheduleFilterSave();
  });

  // eBay-Login-Session (Iter. 25)
  loadEbaySessionStatus();
  document.getElementById('ebay-login-btn')?.addEventListener('click', startEbayLogin);
  document.getElementById('ebay-logout-btn')?.addEventListener('click', logoutEbay);

  // Keyboard shortcuts
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
    }
    if (e.key === 'Escape') {
      closeAddModal();
      closeDrawer();
      closeAuctionModal();
    }
  });
});
