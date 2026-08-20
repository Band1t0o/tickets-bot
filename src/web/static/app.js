import { lineChart } from '/chart.js';

const $ = (id) => document.getElementById(id);
const state = {
  scenario: null,
  viability: null,
  sweeps: [],
  stamp: null,
  frequent: { origins: [], destinations: [] },
  // A trip being created has no file behind it yet, so Save must POST and the
  // sweep buttons have nothing to run against until it does.
  isNew: false,
  // True between seeing a run start and seeing it end, so the page can show the
  // sweep it just watched rather than whichever one was selected before.
  watching: false,
  // Which run the Explore tab reads verdicts from — its own selection, since
  // the best sweep to judge airports by is rarely the one you are pricing.
  exploreStamp: null,
  exploreCost: null,
};

/* Must equal `API_CONTRACT` in src/web/app.py; a test fails if it does not.

   Static files are served from disk on every request, but the Python is frozen
   at import time — so a server left running from an older commit hands you this
   very file and then 404s the endpoints it asks for. That is how an afternoon
   was lost: the page rendered an empty trip picker and empty charts, which is
   exactly what a deleted database looks like, when in fact nothing on disk had
   changed and the answer was `make ui` again. */
const EXPECTED_CONTRACT = 5;

const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return response.json();
};

/* Replaces the whole app with a stated reason. Used only where the alternative
   is drawing something misleading. */
const block = (title, detail, { retry = null } = {}) => {
  $('blocker-title').textContent = title;
  $('blocker-detail').innerHTML = detail;
  $('blocker').hidden = false;
  $('tabs').hidden = true;
  for (const section of document.querySelectorAll('section[data-panel]')) section.hidden = true;
  const button = $('blocker-retry');
  button.hidden = retry === null;
  button.onclick = retry;
};

const unblock = () => {
  $('blocker').hidden = true;
  $('tabs').hidden = false;
  const active = document.querySelector('#tabs button.is-active')?.dataset.tab ?? 'search';
  for (const section of document.querySelectorAll('section[data-panel]')) {
    section.hidden = section.dataset.panel !== active;
  }
};

/* True when the server is the same generation as this page. */
async function contractMatches() {
  let contract = null;
  try {
    contract = (await api('/api/version')).contract;
  } catch { /* an old server has no such endpoint at all */ }

  if (contract === EXPECTED_CONTRACT) return true;

  const missing = contract === null
    ? 'It is old enough that it has no version endpoint at all.'
    : `It reports contract ${escapeHtml(String(contract))}; this page needs ${EXPECTED_CONTRACT}.`;
  block(
    'The server is running older code than this page',
    `${missing} Restarting it fixes this: stop the running server and run ` +
    '<code>make ui</code> again. Your trips and sweep history are files on disk ' +
    'and are untouched by any of this.',
    { retry: () => window.location.reload() },
  );
  return false;
}

const money = (n, currency = 'CZK') => `${Math.round(n).toLocaleString()} ${currency}`;

/* A price is a measurement, and one taken three days ago may no longer be
   buyable. Every total on screen says when it was read off the site. */

const observedAt = (iso) => {
  if (!iso) return 'time not recorded';
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return 'time not recorded';
  const stamp = when.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
  return `measured ${stamp} · ${relativeTime(when)}`;
};

const relativeTime = (when) => {
  const minutes = Math.round((Date.now() - when.getTime()) / 60000);
  if (minutes < 2) return 'just now';
  if (minutes < 90) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
};

// A trip is chained from legs priced minutes or hours apart, so a total can be
// assembled from prices that were never all true at the same moment.
const spanNote = (minutes) =>
  minutes >= 20 ? ` · legs priced up to ${minutes >= 90 ? `${Math.round(minutes / 60)} h` : `${minutes} min`} apart` : '';

/* ------------------------------------------------------------------ theme */

const savedTheme = localStorage.getItem('theme');
if (savedTheme === 'dark') document.documentElement.dataset.theme = 'dark';
$('theme-toggle').onclick = () => {
  const dark = document.documentElement.dataset.theme === 'dark';
  if (dark) delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = 'dark';
  localStorage.setItem('theme', dark ? 'light' : 'dark');
  renderPrices();  // charts read theme tokens at draw time
};

/* ------------------------------------------------------------------- tabs */

function showTab(name) {
  for (const b of $('tabs').children) b.classList.toggle('is-active', b.dataset.tab === name);
  for (const section of document.querySelectorAll('section[data-panel]')) {
    section.hidden = section.dataset.panel !== name;
  }
  if (name === 'explore') renderExplore();
  if (name === 'results') renderResults();
  if (name === 'prices') { renderPrices(); renderFocusControls(); }
  if (name === 'sources') { renderSources(); renderNotifyTarget(); }
}

$('tabs').onclick = (event) => {
  const button = event.target.closest('button[data-tab]');
  if (button) showTab(button.dataset.tab);
};

/* --------------------------------------------------------- route editor --
   A trip is an ordered chain of stops, so the form is generated from the
   scenario rather than hardcoded. The previous version had `jp-min`/`ph-max`
   inputs and a ['europe','japan','philippines'] loop, which is why adding a
   third destination meant editing HTML, JS, a Pydantic model and the schema.

   Airports are chosen through a typeahead over the whole catalogue. A checkbox
   grid was fine for nine hardcoded European airports; there are now ~4,000 with
   scheduled service.
*/

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

// Everything below `route` is the editable trip; `state.scenario` stays the
// last saved version so an unsaved edit can be discarded by reloading.
const route = { origins: [], stops: [], returnTo: [], preferredTiers: [] };

const airportCache = new Map();
const cacheAirports = (list) => {
  for (const airport of list) airportCache.set(airport.iata, airport);
  return list;
};

function airportLabel(code) {
  const airport = airportCache.get(code);
  return airport ? `${airport.city || airport.name}` : '';
}

function describeAirport(airport) {
  const where = [airport.city || airport.name, airport.country_name].filter(Boolean).join(', ');
  return where ? `${airport.iata} — ${where}` : airport.iata;
}

function viabilityOf(code) {
  const stats = state.viability?.airports?.[code];
  if (!stats) return null;
  if (stats.verdict === 'no_inventory' || stats.verdict === 'no_return') return stats;
  return null;
}

/* ------------------------------------------------------------- typeahead --

   Enter used to be a no-op at human typing speed. `onkeydown` opened with
   `if (menu.hidden) return`, and the menu needs a debounce plus a round trip,
   so typing "BCN" (~200 ms) and pressing Enter did nothing at all: no chip, no
   message, the text just sat there. Enter now waits for the query it needs
   rather than giving up, and a query that finds nothing says so instead of
   closing the menu.
*/

const searchAirports = (query) =>
  api(`/api/airports/search?q=${encodeURIComponent(query)}`);

function typeahead(onPick, placeholder = '+ airport or city') {
  const wrap = document.createElement('span');
  wrap.className = 'typeahead';

  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = placeholder;
  input.autocomplete = 'off';
  input.spellcheck = false;

  const menu = document.createElement('div');
  menu.className = 'typeahead__menu';
  menu.hidden = true;

  let items = [];
  let active = -1;
  let timer;
  // The query currently in flight, so Enter can await it instead of racing it.
  let inFlight = null;
  let inFlightFor = '';

  const close = () => { menu.hidden = true; active = -1; };

  const highlight = () => {
    for (const [index, button] of [...menu.querySelectorAll('.typeahead__item')].entries()) {
      button.classList.toggle('is-active', index === active);
    }
  };

  const pick = (airport) => {
    if (!airport) return false;
    cacheAirports([airport]);
    input.value = '';
    input.classList.remove('is-unresolved');
    close();
    onPick(airport.iata);
    return true;
  };

  const message = (text, tone = 'muted') => {
    items = [];
    active = -1;
    menu.innerHTML = `<div class="typeahead__note ${tone}">${escapeHtml(text)}</div>`;
    menu.hidden = false;
  };

  const render = (result, query) => {
    items = cacheAirports(result.airports ?? []);
    menu.innerHTML = '';
    if (!items.length) {
      message(`No airport matches “${query}”`);
      return;
    }
    for (const airport of items) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'typeahead__item';
      // Catalogue text is third-party data; never interpolate it raw.
      button.innerHTML =
        `<strong>${escapeHtml(airport.iata)}</strong>` +
        `<span class="muted">${escapeHtml(airport.city || airport.name)}` +
        `${airport.country_name ? ', ' + escapeHtml(airport.country_name) : ''}</span>`;
      button.onmousedown = (event) => { event.preventDefault(); pick(airport); };
      menu.appendChild(button);
    }
    // A country match is usually longer than the list shown. Say what was cut
    // rather than truncating in silence.
    if (result.total > items.length) {
      const note = document.createElement('div');
      note.className = 'typeahead__note muted';
      note.textContent = result.country
        ? `${items.length} of ${result.total} in ${result.country} — biggest first, keep typing to narrow`
        : `${items.length} of ${result.total} matches — keep typing to narrow`;
      menu.appendChild(note);
    }
    menu.hidden = false;
    active = 0;
    highlight();
  };

  // Returns the results, so Enter can act on the same promise the menu shows.
  const run = (query) => {
    inFlightFor = query;
    inFlight = searchAirports(query).then(
      (result) => {
        if (input.value.trim() === query) render(result, query);
        return result;
      },
      (error) => {
        // This used to be `catch { close(); }`, which made a failing lookup
        // indistinguishable from an airport that does not exist.
        if (input.value.trim() === query) message(`Search failed — ${error.message}`, 'is-error');
        return { airports: [], total: 0, country: null };
      },
    );
    return inFlight;
  };

  input.oninput = () => {
    clearTimeout(timer);
    input.classList.remove('is-unresolved');
    const query = input.value.trim();
    if (!query) { close(); return; }
    timer = setTimeout(() => run(query), 160);
  };

  const commit = async () => {
    const query = input.value.trim();
    if (!query) return;
    clearTimeout(timer);

    // A code you already know should not wait on the network.
    const known = airportCache.get(query.toUpperCase());
    if (known && pick(known)) return;

    const result = inFlightFor === query && inFlight ? await inFlight : await run(query);
    if (!pick(result.airports?.[active >= 0 ? active : 0])) {
      input.classList.add('is-unresolved');
    }
  };

  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    else if (event.key === 'Escape') { close(); }
    else if (event.key === 'ArrowDown' && !menu.hidden) {
      event.preventDefault(); active = Math.min(active + 1, items.length - 1); highlight();
    } else if (event.key === 'ArrowUp' && !menu.hidden) {
      event.preventDefault(); active = Math.max(active - 1, 0); highlight();
    }
  };

  input.onblur = () => setTimeout(() => {
    close();
    // Text left behind after clicking away looks like a chip that is about to
    // appear. Mark it so it is obvious nothing was added.
    if (input.value.trim()) input.classList.add('is-unresolved');
  }, 120);

  wrap.append(input, menu);
  return wrap;
}

/* ----------------------------------------------------------------- chips --

   Every picker carries a stable `data-picker` key. Choosing an airport
   re-renders the route, which destroys the input that was being typed into -
   so focus landed on <body> and adding five airports meant five trips back to
   the mouse. The key is what lets focus be put back afterwards.
*/

const focusPicker = (key) =>
  document.querySelector(`[data-picker="${key}"] .typeahead input`)?.focus();

function renderChips(host, codes, onChange, { key, suggest = [] } = {}) {
  host.innerHTML = '';
  if (key) host.dataset.picker = key;
  const change = (next) => { onChange(next); if (key) focusPicker(key); };

  for (const code of codes) {
    const dead = viabilityOf(code);
    const chip = document.createElement('span');
    chip.className = `chip${dead ? ' chip--dead' : ''}`;
    if (dead) chip.title = dead.note || `No inventory found on ${dead.dead_routes.join(', ')}`;

    const city = airportLabel(code);
    chip.innerHTML =
      `<span class="chip__code">${escapeHtml(code)}</span>` +
      (city ? `<span class="chip__city">${escapeHtml(city)}</span>` : '') +
      (dead ? '<span class="badge badge--warning">no inventory</span>' : '');

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chip__remove';
    remove.textContent = '×';
    remove.title = `Remove ${code}`;
    remove.onclick = () => change(codes.filter((c) => c !== code));
    chip.appendChild(remove);
    host.appendChild(chip);
  }

  host.appendChild(typeahead((code) => {
    if (!codes.includes(code)) change([...codes, code]);
  }));

  // One-click chips for airports already in use. A checkbox grid genuinely beat
  // typing for the departure airports, which barely change; this keeps that
  // without hardcoding a list, because it is derived from the saved trips.
  const unused = suggest.filter((airport) => !codes.includes(airport.iata));
  if (unused.length) {
    const row = document.createElement('div');
    row.className = 'chips chips--suggest';
    row.innerHTML = '<span class="muted small">Yours:</span>';
    for (const airport of unused.slice(0, 8)) {
      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'chip chip--add';
      add.title = `Add ${describeAirport(airport)}`;
      add.innerHTML =
        `<span class="chip__code">+ ${escapeHtml(airport.iata)}</span>` +
        `<span class="chip__city">${escapeHtml(airport.city || airport.name)}</span>`;
      add.onclick = () => change([...codes, airport.iata]);
      row.appendChild(add);
    }
    // Inside the host, not after it: a sibling would survive `innerHTML = ''`
    // and a second row would appear on every re-render.
    host.appendChild(row);
  }
}

/* ----------------------------------------------------------------- stops -- */

function renderStops() {
  const host = $('stops');
  host.innerHTML = '';

  route.stops.forEach((stop, index) => {
    const card = document.createElement('div');
    card.className = 'stop';

    const head = document.createElement('div');
    head.className = 'stop__head';

    const badge = document.createElement('span');
    badge.className = 'stop__index';
    badge.textContent = index + 1;

    const label = document.createElement('input');
    label.className = 'stop__label';
    label.value = stop.label || '';
    label.placeholder = `Stop ${index + 1}`;
    label.oninput = () => { stop.label = label.value; };

    const stay = document.createElement('span');
    stay.className = 'stop__stay';
    stay.innerHTML = '<span class="muted">stay</span>';
    const [min, max] = [0, 1].map((slot) => {
      const field = document.createElement('input');
      field.type = 'number';
      field.min = '1';
      field.value = stop.stay_days[slot];
      field.onchange = () => {
        stop.stay_days[slot] = Number(field.value);
        scheduleEstimate();
      };
      return field;
    });
    const dash = document.createElement('span');
    dash.className = 'muted';
    dash.textContent = '–';
    const days = document.createElement('span');
    days.className = 'muted';
    days.textContent = 'days';
    stay.append(min, dash, max, days);

    const up = document.createElement('button');
    up.className = 'small';
    up.textContent = '↑';
    up.title = 'Move earlier';
    up.disabled = index === 0;
    up.onclick = () => moveStop(index, -1);

    const down = document.createElement('button');
    down.className = 'small';
    down.textContent = '↓';
    down.title = 'Move later';
    down.disabled = index === route.stops.length - 1;
    down.onclick = () => moveStop(index, 1);

    const remove = document.createElement('button');
    remove.className = 'small';
    remove.textContent = 'Remove';
    remove.disabled = route.stops.length === 1;
    remove.title = route.stops.length === 1 ? 'A trip needs at least one stop' : '';
    remove.onclick = () => {
      route.stops.splice(index, 1);
      renderStops();
      scheduleEstimate();
    };

    head.append(badge, label, stay, up, down, remove);

    const chips = document.createElement('div');
    chips.className = 'chips';
    renderChips(chips, stop.airports, (codes) => {
      stop.airports = codes;
      renderStops();
      scheduleEstimate();
    }, { key: `stop-${index}`, suggest: state.frequent.destinations });

    card.append(head, chips);
    host.appendChild(card);
  });
}

function moveStop(index, delta) {
  const target = index + delta;
  if (target < 0 || target >= route.stops.length) return;
  [route.stops[index], route.stops[target]] = [route.stops[target], route.stops[index]];
  renderStops();
  scheduleEstimate();
}

$('add-stop-btn').onclick = () => {
  const previous = route.stops[route.stops.length - 1];
  route.stops.push({ label: '', airports: [], stay_days: [...(previous?.stay_days ?? [7, 10])] });
  renderStops();
  scheduleEstimate();
};

/* ---------------------------------------------------------------- return --

   There used to be a "One-way" and a "Return somewhere else" checkbox, and the
   shape of the trip lived in those two booleans rather than in the route you
   could see. The chain now says it: leave the Return row matching the origins
   for a normal round trip, change it for an open jaw, empty it for a one-way.
   `formToScenario` derives `one_way` and `return_to` from the row, so the
   stored schema is untouched.
*/

const sameSet = (a, b) =>
  a.length === b.length && [...a].sort().join() === [...b].sort().join();

function renderReturn() {
  const mirrors = sameSet(route.returnTo, route.origins) && route.origins.length > 0;
  $('return-note').textContent = route.returnTo.length === 0
    ? '— empty, so this is a one-way with no leg home'
    : mirrors
      ? '— same as departure'
      : '— open jaw, flying home to somewhere else';
  renderChips($('return-to'), route.returnTo, (codes) => {
    route.returnTo = codes;
    renderReturn();
    scheduleEstimate();
  }, { key: 'return', suggest: state.frequent.origins });
}

function renderOrigins() {
  renderChips($('origins'), route.origins, (codes) => {
    // While the return row mirrors the origins, keep it mirroring: adding a
    // departure airport to a round trip should not quietly make it an open jaw.
    if (sameSet(route.returnTo, route.origins)) route.returnTo = [...codes];
    route.origins = codes;
    renderOrigins();
    renderReturn();
    scheduleEstimate();
  }, { key: 'origins', suggest: state.frequent.origins });
}

/* ------------------------------------------------------ preferred airports */

/* Ranked tiers, not a flat list: PRG and VIE are not interchangeable with KTW
   just because all three beat FRA. Built from the same chip/typeahead machinery
   as the route rows, so an airport is chosen the same way everywhere. */

const ORDINALS = ['1st choice', '2nd choice', '3rd choice', '4th choice', '5th choice'];

function renderPreferred() {
  const host = $('preferred-tiers');
  host.innerHTML = '';

  route.preferredTiers.forEach((tier, index) => {
    const block = document.createElement('div');
    block.style.marginBottom = '12px';

    const heading = document.createElement('div');
    heading.className = 'row';
    heading.innerHTML = `<span class="small muted">${ORDINALS[index] ?? `choice ${index + 1}`}</span>`;
    const drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'small';
    drop.textContent = 'Remove';
    drop.title = `Remove ${ORDINALS[index] ?? 'this tier'}`;
    drop.onclick = () => {
      route.preferredTiers.splice(index, 1);
      renderPreferred();
    };
    heading.appendChild(drop);
    block.appendChild(heading);

    const chips = document.createElement('div');
    chips.className = 'chips';
    block.appendChild(chips);
    host.appendChild(block);

    renderChips(chips, tier, (next) => {
      // An airport in two tiers makes "the best tier holding it" ambiguous, and
      // the scenario rejects it on save. Drop it from the others rather than
      // letting a save fail on something the UI could see coming.
      route.preferredTiers = route.preferredTiers.map((other, i) =>
        (i === index ? next : other.filter((code) => !next.includes(code))));
      renderPreferred();
    }, { key: `tier-${index}`, suggest: state.frequent.origins });
  });

  if (!route.preferredTiers.length) {
    host.innerHTML =
      '<p class="empty small">No preference — only the cheapest trip is ever reported.</p>';
  }
}

$('add-tier-btn').onclick = () => {
  route.preferredTiers.push([]);
  renderPreferred();
};

function renderRoute() {
  renderOrigins();
  renderStops();
  renderReturn();
  renderPreferred();
}

/* -------------------------------------------------------------- scenarios */

function fillForm(scenario) {
  route.origins = [...scenario.origins];
  route.stops = scenario.stops.map((stop) => ({
    label: stop.label,
    airports: [...stop.airports],
    stay_days: [...stop.stay_days],
  }));
  // `return_to: null` on a return trip means "back where you started", which is
  // shown as the row mirroring the origins rather than as an empty row.
  route.returnTo = scenario.one_way ? [] : [...(scenario.return_to ?? scenario.origins)];
  route.preferredTiers = (scenario.preferred_origins ?? []).map((tier) => [...tier]);

  const notify = scenario.notify ?? ['cheapest', 'preferred'];
  $('notify-cheapest').checked = notify.includes('cheapest');
  $('notify-preferred').checked = notify.includes('preferred');
  $('notify-quiet').checked = scenario.notify_quiet !== false;

  $('trip-name').value = scenario.name ?? '';
  $('window-start').value = scenario.window_start;
  $('window-end').value = scenario.window_end;
  $('adults').value = scenario.adults;
  $('currency').value = scenario.currency ?? 'CZK';
  $('depth').value = scenario.depth;
  $('enabled').checked = scenario.enabled !== false;

  renderRoute();
}

function formToScenario() {
  const oneWay = route.returnTo.length === 0;
  return {
    ...state.scenario,
    name: $('trip-name').value.trim() || derivedName(),
    origins: route.origins,
    stops: route.stops.map((stop) => ({
      label: stop.label,
      airports: stop.airports,
      stay_days: stop.stay_days,
    })),
    return_to: oneWay || sameSet(route.returnTo, route.origins) ? null : route.returnTo,
    one_way: oneWay,
    window_start: $('window-start').value,
    window_end: $('window-end').value,
    adults: Number($('adults').value),
    currency: ($('currency').value || 'CZK').toUpperCase(),
    depth: $('depth').value,
    enabled: $('enabled').checked,
    // An empty tier is a ranking with a hole in it and the scenario rejects it,
    // so a row you added and never filled is simply dropped on save.
    preferred_origins: route.preferredTiers.filter((tier) => tier.length),
    notify: [
      ...($('notify-cheapest').checked ? ['cheapest'] : []),
      ...($('notify-preferred').checked ? ['preferred'] : []),
    ],
    notify_quiet: $('notify-quiet').checked,
  };
}

/* A trip's name is a consequence of its route, not a category chosen up front.
   "Europe → Japan → Philippines" and "Tokyo round trip" were two saved trips,
   but reading them in a dropdown made them look like two kinds of trip. */
function derivedName() {
  const where = (codes) =>
    codes.map((code) => airportLabel(code) || code).filter(Boolean)[0] ?? '?';
  const parts = [where(route.origins)];
  for (const stop of route.stops) parts.push(stop.label.trim() || where(stop.airports));
  if (route.returnTo.length) {
    parts.push(sameSet(route.returnTo, route.origins) ? where(route.origins) : where(route.returnTo));
  }
  return parts.join(' → ');
}

/* --------------------------------------------------------------- estimate */

let estimateTimer;
const scheduleEstimate = () => {
  // The cost is debounced because it costs a round trip; the unsaved marker is
  // not, because it is the answer to "what would that button search?" and has
  // to be true the moment the trip stops matching what is saved.
  renderDirty();
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(refreshEstimate, 300);
};

async function refreshEstimate() {
  // A trip that has never been saved has no file to estimate against, and the
  // endpoint is keyed on the id. Say what is missing instead of 404ing.
  if (state.isNew) {
    $('estimate').textContent = 'Save the trip to price the sweep';
    $('estimate').className = 'badge badge--muted';
    $('leg-breakdown').textContent = '';
    $('explore-cost').textContent = 'a few minutes';
    return;
  }
  renderDirty();
  refreshExploreCost();
  try {
    const body = await api(
      `/api/scenarios/${state.scenario.id}/estimate?depth=${$('depth').value}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formToScenario()) },
    );
    $('estimate').textContent = `${body.searches} searches · ~${body.minutes} min`;
    $('estimate').className = 'badge badge--muted';
    $('leg-breakdown').innerHTML = (body.leg_labels ?? [])
      .map((label, index) => `${escapeHtml(label)} — ${body.per_leg[index] ?? 0} searches`)
      .join('<br>');
  } catch (error) {
    $('estimate').textContent = error.message;
    $('estimate').className = 'badge badge--error';
    $('leg-breakdown').textContent = '';
  }
}

/* The probe's own price, asked of the same endpoint the run will use. A number
   in the sentence is what makes "explore first" an obvious trade rather than
   another button of unknown cost. */
async function refreshExploreCost() {
  try {
    const body = await api(
      `/api/scenarios/${state.scenario.id}/estimate?mode=explore`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formToScenario()) },
    );
    state.exploreCost = `${body.searches} searches, ~${body.minutes} min`;
  } catch {
    state.exploreCost = 'a few minutes';
  }
  $('explore-cost').textContent = state.exploreCost;
  $('explore-run-note').textContent = state.exploreCost;
}

/* -------------------------------------------------------------- save/run */

/* Whether the form holds edits the saved trip does not. A run searches the
   saved trip, so this is the difference between what you are looking at and
   what would actually be searched. */
function isDirty() {
  if (state.isNew || !state.scenario) return false;
  return JSON.stringify(formToScenario()) !== JSON.stringify(state.scenario);
}

function renderDirty() {
  const note = $('dirty-note');
  const dirty = isDirty();
  note.hidden = !dirty;
  note.textContent = dirty ? 'Unsaved changes — a run will save them first' : '';
  const inline = $('explore-dirty');
  if (inline) inline.textContent = dirty ? ' · unsaved changes will be saved first' : '';
}

/* Where a message about the trip belongs: the panel you are looking at. The
   Search panel's box is on a tab you cannot see from Explore, so a refused save
   used to render into hidden markup and the button read as broken.

   `tone` exists because not everything said here is a failure — "I took CEB
   out, now save it" is an instruction, and printing it in red reads as
   something having gone wrong. */
const clearError = () => {
  for (const box of document.querySelectorAll('.panel-error')) box.hidden = true;
};

const showError = (message, tone = 'error') => {
  clearError();
  const panel = document.querySelector('section[data-panel]:not([hidden])');
  const box = panel?.querySelector('.panel-error') ?? $('save-error');
  box.textContent = message;
  box.className = `panel-error badge badge--${tone}`;
  box.hidden = false;
  box.scrollIntoView({ block: 'nearest' });
  return box;
};

/* Write the edited trip back. Shared with the Explore tab's pending-changes
   bar, which drops airports and then has to save them the same way. Returns
   true when it stuck, so a caller can clear its own state only if it did. */
async function saveTrip() {
  clearError();
  const payload = formToScenario();
  try {
    let saved;
    if (state.isNew) {
      // The id comes from the name, so two trips can collide. The server
      // answers 409 rather than overwriting; try the next free suffix.
      payload.id = await freeId(slug(payload.name));
      saved = await api('/api/scenarios', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      state.isNew = false;
      await reloadScenarioList(saved.id);
    } else {
      saved = await api(`/api/scenarios/${state.scenario.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const option = [...$('scenario-select').options].find((o) => o.value === saved.id);
      if (option) option.textContent = saved.name;
    }
    state.scenario = saved;
    fillForm(saved);
    state.frequent = await api('/api/airports/frequent').catch(() => state.frequent);
    renderRoute();
    // Whatever was waiting in the pending bar is on disk now, however the save
    // was triggered — the bar itself, or a run that saved on the way past.
    pending.length = 0;
    renderPending();
    await refreshEstimate();
    // Editing the trip is exactly what turns an existing run into a run of a
    // different trip, so the flags in the sweep picker are stale the instant a
    // save lands. Re-read them rather than waiting for the next poll.
    await pollStatus();
    $('save-btn').textContent = 'Saved';
    setTimeout(() => ($('save-btn').textContent = 'Save trip'), 1500);
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  }
}

$('save-btn').onclick = saveTrip;

$('delete-trip-btn').onclick = async () => {
  clearError();
  if (state.isNew) { await reloadScenarioList(); return; }
  if (!confirm(`Delete “${state.scenario.name}”? Sweep results already gathered are kept.`)) return;
  try {
    await api(`/api/scenarios/${state.scenario.id}`, { method: 'DELETE' });
    await reloadScenarioList();
  } catch (error) {
    showError(error.message);
  }
};

$('new-trip-btn').onclick = () => {
  clearError();
  state.isNew = true;
  state.stamp = null;
  state.sweeps = [];
  state.scenario = blankScenario();
  fillForm(state.scenario);
  $('scenario-select').value = '';
  updateNewTripUi();
  $('estimate').textContent = 'Add airports, then save';
  $('estimate').className = 'badge badge--muted';
  $('leg-breakdown').textContent = '';
  $('status-text').textContent = 'New trip — not saved yet';
  $('status-strip').className = 'status-strip';
  focusPicker('origins');
};

function blankScenario() {
  // Three months out is roughly where fares settle; the previous session's
  // probe found them moving over days rather than hours at that range.
  const day = (offset) => {
    const date = new Date();
    date.setDate(date.getDate() + offset);
    return date.toISOString().slice(0, 10);
  };
  const origins = state.frequent.origins.slice(0, 2).map((airport) => airport.iata);
  return {
    id: '',
    name: '',
    origins,
    stops: [{ label: '', airports: [], stay_days: [7, 10] }],
    window_start: day(90),
    window_end: day(120),
    return_to: null,
    one_way: false,
    adults: 1,
    depth: 'quick',
    currency: 'CZK',
    alert_threshold: null,
    bag_estimate: 1500,
    // Off until you have run it once and believe the numbers.
    enabled: false,
    notes: '',
    preferred_origins: [],
    notify: ['cheapest', 'preferred'],
    notify_quiet: true,
  };
}

const slug = (name) =>
  (name || 'trip')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')   // "Zürich" -> "Zurich"
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'trip';

async function freeId(base) {
  const taken = new Set([...$('scenario-select').options].map((option) => option.value));
  if (!taken.has(base)) return base;
  for (let n = 2; n < 100; n += 1) if (!taken.has(`${base}-${n}`)) return `${base}-${n}`;
  return `${base}-${Date.now()}`;
}

function updateNewTripUi() {
  // Nothing to sweep until the trip exists on disk.
  for (const id of ['run-local-btn', 'run-cloud-btn', 'explore-btn']) $(id).disabled = state.isNew;
  $('delete-trip-btn').textContent = state.isNew ? 'Discard' : 'Delete';
}

/* A run searches the trip on disk, so the edits on screen have to reach disk
   before it starts. Without this the app happily spent two 25-minute probes
   searching the previous day's airports and reported on them as if they were
   the trip — the tool being confidently wrong, which is worse than no tool.

   A trip that will not save does not run: the reason appears on whichever tab
   the button was pressed from. */
async function startRun(mode) {
  clearError();
  if (isDirty() && !(await saveTrip())) return false;
  try {
    await api(
      `/api/scenarios/${state.scenario.id}/run?depth=${$('depth').value}&mode=${mode}`,
      { method: 'POST' },
    );
    pollStatus();
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  }
}

$('run-local-btn').onclick = () => startRun('sweep');
$('explore-btn').onclick = () => startRun('explore');

$('stop-btn').onclick = async () => {
  $('stop-btn').disabled = true;
  try {
    await api(`/api/scenarios/${state.scenario.id}/stop`, { method: 'POST' });
  } catch (error) {
    // Most often 409: it finished between the render and the click.
    $('status-text').textContent = error.message;
  }
  pollStatus();
};

$('run-cloud-btn').onclick = async () => {
  clearError();
  // The cloud reads the trip out of the repo, so an unsaved edit is even
  // further from what would be searched than it is locally.
  if (isDirty() && !(await saveTrip())) return;
  try {
    await api(`/api/scenarios/${state.scenario.id}/run-cloud?depth=${$('depth').value}`, { method: 'POST' });
    $('status-text').textContent = 'Dispatched to GitHub Actions';
  } catch (error) {
    showError(error.message);
  }
};

$('depth').onchange = scheduleEstimate;
for (const id of ['window-start', 'window-end', 'adults', 'currency', 'trip-name', 'enabled']) {
  $(id).onchange = scheduleEstimate;
}

/* ----------------------------------------------------------------- status */

async function pollStatus() {
  if (state.isNew) return;   // no file, so no sweeps to report on
  const strip = $('status-strip');
  try {
    const body = await api(`/api/sweeps/${state.scenario.id}`);
    state.sweeps = body.sweeps;
    const latest = body.sweeps[0];

    // Only offered while there is something to stop, and disabled once asked -
    // a second click cannot make it stop any sooner.
    $('stop-btn').hidden = !body.running;
    $('stop-btn').disabled = Boolean(body.stopping);
    if (body.running) state.watching = true;

    // A sweep thread that died has no status.json to show, so without this the
    // strip would sit on "No sweeps yet" forever after a failed launch.
    if (body.error) {
      strip.className = 'status-strip is-error';
      $('status-text').textContent = `Sweep failed — ${body.error}`;
      populateSweepSelect();
      return;
    }

    if (body.stopping && latest) {
      // The search in flight has to finish or hit the site's 120s timeout.
      // Saying "stopped" here would be a lie the next poll exposes.
      strip.className = 'status-strip is-running';
      $('status-text').textContent =
        `Stopping — finishing the search in flight · ${latest.completed}/${latest.total} done, ` +
        `${latest.legs_found ?? 0} flights kept`;
      setTimeout(pollStatus, 2000);
    } else if (body.running && latest) {
      // Pace constants come from the server. Keeping local copies is how the
      // countdown ended up claiming half the real wait after the sweep was
      // slowed from 4 workers to 2.
      const perSearch = body.seconds_per_search ?? 19;
      const workers = body.workers ?? 2;
      const left = latest.total
        ? Math.max(0, Math.round(((latest.total - latest.completed) * perSearch) / workers / 60))
        : '?';
      strip.className = 'status-strip is-running';
      $('status-text').textContent =
        `${latest.current || 'starting'} · ${latest.completed}/${latest.total} · ~${left} min left`;
      setTimeout(pollStatus, 2000);
    } else if (latest) {
      const broken = latest.state === 'unhealthy';
      const what = latest.mode === 'explore' ? 'probe' : 'sweep';
      strip.className = `status-strip${broken ? ' is-error' : ''}`;
      if (broken) {
        $('status-text').textContent = `Last ${what} looked broken — ${latest.legs_found ?? 0} flights found`;
      } else if (latest.state === 'stopped') {
        // Stopped is neither success nor breakage, and reading it as either
        // would be wrong: it is exactly as much of a sweep as you asked for.
        $('status-text').textContent =
          `Stopped at ${latest.completed}/${latest.total} · ${latest.legs_found ?? 0} flights kept`;
      } else {
        $('status-text').textContent =
          `Last ${what} ${latest.stamp.replace('T', ' ').replace('Z', '')} · ${latest.legs_found ?? 0} flights`;
      }
      // A run this page watched start should be the one it shows when it ends.
      // Otherwise you explore, open Results, and read yesterday's deep sweep.
      if (state.watching) {
        state.watching = false;
        state.stamp = latest.stamp;
        state.exploreStamp = latest.stamp;
        populateSweepSelect();
        // A probe answers in the Explore tab, a sweep in Results. Show whichever
        // one the run you just watched actually filled in.
        showTab(latest.mode === 'explore' ? 'explore' : 'results');
        return;
      }
      populateSweepSelect();
    } else {
      strip.className = 'status-strip';
      $('status-text').textContent = 'No sweeps yet';
    }
  } catch (error) {
    strip.className = 'status-strip is-error';
    $('status-text').textContent = error.message;
  }
}

function populateSweepSelect() {
  const select = $('sweep-select');
  select.innerHTML = '';
  for (const sweep of state.sweeps) {
    const option = document.createElement('option');
    option.value = sweep.stamp;
    // Depth and search count are part of the label because a 20-search smoke
    // test and a 204-search real sweep are otherwise indistinguishable here -
    // and reading the wrong one produced a headline price 7,000 Kč too high.
    const dark = (sweep.routes_with_no_results || []).length;
    // A probe and a stopped run are both "not a sweep of this trip", and
    // reading either as one is how a headline price ends up 7,000 Kč wrong.
    const kind = sweep.mode === 'explore' ? 'probe' : (sweep.depth ?? '?');
    option.textContent =
      `${sweep.stamp.replace('T', ' ').replace('Z', '')} · ` +
      `${kind} · ${sweep.total ?? 0} searches · ${sweep.legs_found ?? 0} flights` +
      (sweep.state === 'stopped' ? ` · stopped at ${sweep.completed ?? '?'}` : '') +
      (dark ? ` · ⚠ ${dark} dead route(s)` : '');
    select.appendChild(option);
  }
  if (!state.stamp && state.sweeps.length) state.stamp = state.sweeps[0].stamp;
  if (state.stamp) select.value = state.stamp;
}

$('sweep-select').onchange = (event) => {
  state.stamp = event.target.value;
  renderResults();
  renderPrices();
};

/* ---------------------------------------------------------------- results */

/* A second opinion on the shortlist, from letuska. Never on all 900 legs — it
   has no deep link, so a search costs a minute — but the five or six legs the
   decision rests on are worth a few minutes once, after the sweep. */

const VERDICTS = {
  agrees: ['good', 'letuska agrees on every leg it could price.'],
  cheaper_elsewhere: ['warning', 'letuska quotes one of these legs materially cheaper.'],
  partial: ['muted', 'letuska agreed where it could, but could not price every leg.'],
  unavailable: ['error', 'letuska could not be reached, so nothing was confirmed.'],
  nothing_to_check: ['muted', 'nothing to check.'],
};

function renderVerification(report) {
  const host = $('verification');
  if (!report) {
    // Not checked is not the same as checked and fine, and must not read as it.
    host.innerHTML =
      '<span class="muted">Not cross-checked. Run ' +
      `<code>python -m src.cli verify --scenario ${escapeHtml(state.scenario.id)}</code>` +
      ' to re-price the shortlist on letuska.</span>';
    return;
  }
  const [tone, text] = VERDICTS[report.verdict] ?? ['muted', report.verdict];
  const best = report.cheapest_elsewhere;
  host.innerHTML =
    `<span class="badge badge--${tone}">${escapeHtml(text)}</span> ` +
    `<span class="muted">${Number(report.legs_checked)} leg(s) checked · ` +
    `${escapeHtml(observedAt(report.checked_at))}</span>` +
    (best
      ? `<div class="small" style="margin-top:6px">${escapeHtml(best.route)} on ` +
        `${escapeHtml(best.depart_date)}: ours ${money(best.ours)} · theirs ` +
        `${money(best.theirs)} — <strong>${Number(best.saving_pct).toFixed(1)}% cheaper</strong> there.</div>`
      : '') +
    (report.unpriced?.length
      ? `<div class="small muted" style="margin-top:6px">Could not be priced there: ` +
        `${escapeHtml(report.unpriced.join(', '))}.</div>`
      : '');
}

/* --------------------------------------------------------- explore report --

   What a reconnaissance pass has to say. Not itineraries — three dates a leg
   rarely chain into a whole trip — but a verdict per airport, and a button to
   act on it.

   The verdicts deliberately separate "measured and bad" from "not measured".
   With the site throttling this client into mass timeouts, an airport with no
   price is far more often unanswered than genuinely empty, and dropping one on
   that basis would be throwing away a good route on four timeouts.
*/

const VERDICT_TEXT = {
  best: ['good', 'cheapest here'],
  close: ['good', 'within reach'],
  worse: ['warning', 'dearer'],
  poor: ['error', 'not worth it'],
  no_offers: ['error', 'nothing sold'],
  unproven: ['muted', 'not measured'],
};

const DROPPABLE = new Set(['poor', 'no_offers']);

/* Airports dropped here but not yet saved. Removals accumulate rather than
   taking effect one at a time: you are deciding about a pool of airports at
   once, and six removals should be one review and one save. */
const pending = [];

function removeFromTrip(pool, code) {
  if (pool.role === 'origins') route.origins = route.origins.filter((c) => c !== code);
  else if (pool.role === 'return_to') route.returnTo = route.returnTo.filter((c) => c !== code);
  else if (pool.role === 'stop') {
    const stop = route.stops[pool.stop_index];
    if (stop) stop.airports = stop.airports.filter((c) => c !== code);
  }
  pending.push(code);
  renderRoute();
  scheduleEstimate();
  renderPending();
  renderExplore();   // the dropped airport should leave the table it was in
}

function renderPending() {
  const bar = $('explore-pending');
  bar.hidden = pending.length === 0;
  if (pending.length) {
    // Named rather than counted: "dropping 2 airports" is not something you can
    // check, and this is the last screen before the trip changes.
    $('explore-pending-text').innerHTML =
      `Dropping <strong>${escapeHtml([...new Set(pending)].join(', '))}</strong> — ` +
      'not saved yet.';
  }
}

$('explore-undo').onclick = async () => {
  // Reload the saved trip rather than re-adding codes: whatever else was edited
  // in the meantime goes back too, which is what "undo" has to mean here.
  pending.length = 0;
  fillForm(state.scenario);
  renderPending();
  await refreshEstimate();
  renderExplore();
};

$('explore-save').onclick = async () => {
  if (await saveTrip()) {
    pending.length = 0;
    renderPending();
    renderExplore();
  }
};

$('explore-run-btn').onclick = () => startRun('explore');

/* Whether the *edited* trip still contains this airport. The report describes
   a sweep, so it keeps listing airports you have just dropped; without this
   they would sit there still offering a Remove button that does nothing. */
function stillInTrip(pool, code) {
  if (pool.role === 'origins') return route.origins.includes(code);
  if (pool.role === 'return_to') return route.returnTo.includes(code);
  return Boolean(route.stops[pool.stop_index]?.airports.includes(code));
}

function exploreRow(pool, row, currency) {
  const [tone, text] = VERDICT_TEXT[row.verdict] ?? ['muted', row.verdict];
  // Struck through only for an airport *you* took out just now, in this
  // session. An airport that was simply never in your trip is a different
  // statement, and reading a whole table as "dropped" — which is what a report
  // of somebody else's trip looked like — implied six decisions never made.
  const dropped = pending.includes(row.iata) && !stillInTrip(pool, row.iata);
  const outside = !dropped && !stillInTrip(pool, row.iata);
  const tr = document.createElement('tr');
  if (dropped) tr.className = 'is-dropped';
  else if (outside) tr.className = 'is-outside';

  const price = (amount, stops) =>
    amount === null || amount === undefined
      ? '<span class="muted">—</span>'
      : `${escapeHtml(money(amount, currency))}` +
        (stops === null || stops === undefined
          ? ''
          : ` <span class="muted small">${stops === 0 ? 'direct' : `${stops} stop${stops === 1 ? '' : 's'}`}</span>`);

  const gap = row.vs_best_pct
    ? `<span class="trend trend--up">+${Number(row.vs_best_pct).toFixed(0)}%</span>`
    : (row.verdict === 'best' ? '<span class="muted">benchmark</span>' : '<span class="muted">—</span>');

  tr.innerHTML =
    `<td><strong>${escapeHtml(row.iata)}</strong> ` +
    `<span class="muted small">${escapeHtml(airportLabel(row.iata))}</span></td>` +
    `<td><span class="badge badge--${tone}">${escapeHtml(text)}</span></td>` +
    `<td class="num">${price(row.in_min_price, row.in_min_stops)}</td>` +
    `<td class="num">${price(row.out_min_price, row.out_min_stops)}</td>` +
    `<td class="num">${row.total_min === null ? '<span class="muted">—</span>' : escapeHtml(money(row.total_min, currency))}</td>` +
    `<td class="num">${gap}</td>` +
    // Sweeps written before per-route accounting existed have no attempt counts
    // at all. "0 asked" would read as a measurement; it is an absence.
    `<td class="num small muted">${row.searches
      ? `${row.searches} asked · ${row.errors} failed`
      : 'attempts not recorded'}</td>`;

  const action = document.createElement('td');
  if (dropped) {
    action.innerHTML = '<span class="muted small">dropped</span>';
  } else if (outside) {
    action.innerHTML = '<span class="muted small">not in this trip</span>';
  } else if (DROPPABLE.has(row.verdict)) {
    const drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'small';
    drop.textContent = 'Remove from trip';
    drop.onclick = () => removeFromTrip(pool, row.iata);
    action.appendChild(drop);
  } else if (row.verdict === 'unproven') {
    // The one case where the right advice is to do nothing yet.
    action.innerHTML = '<span class="muted small">probe again</span>';
  }
  tr.appendChild(action);
  return tr;
}

/* Which run the verdicts are read from. Deliberately every sweep, not only
   probes: a real sweep has priced the same routes on many more dates, so its
   verdict is the better one whenever you have it. */
function populateExploreSelect() {
  const select = $('explore-select');
  const previous = state.exploreStamp;
  select.innerHTML = '';
  // `has_legs` and not `legs_found`: the latter is what a run found, and a run
  // killed before legs were written to disk still reports thousands of them.
  // Offering one of those would draw a report of nothing but "not measured".
  const usable = state.sweeps.filter((sweep) => sweep.has_legs);
  for (const sweep of usable) {
    const option = document.createElement('option');
    option.value = sweep.stamp;
    const kind = sweep.mode === 'explore' ? 'probe' : `${sweep.depth ?? '?'} sweep`;
    option.textContent =
      `${sweep.stamp.replace('T', ' ').replace('Z', '')} · ${kind} · ` +
      `${sweep.total ?? 0} searches · ${sweep.legs_found ?? 0} flights` +
      (sweep.state === 'throttled' ? ' · site refused' : '') +
      (sweep.state === 'stopped' ? ' · stopped early' : '') +
      // Before it is opened, not after: a run of other airports reads exactly
      // like a run of yours once its verdicts are on screen.
      (sweep.searched_another_trip ? ' · different trip' : '');
    select.appendChild(option);
  }
  state.exploreStamp = usable.some((s) => s.stamp === previous) ? previous : usable[0]?.stamp;
  if (state.exploreStamp) select.value = state.exploreStamp;
  return usable.length > 0;
}

$('explore-select').onchange = (event) => {
  state.exploreStamp = event.target.value;
  renderExplore();
};

async function renderExplore() {
  if (state.isNew) return;
  const anything = populateExploreSelect();
  $('explore-empty').hidden = anything;
  $('explore-report').hidden = !anything;
  $('explore-run-note').textContent = state.exploreCost ?? '';
  if (!anything) {
    $('explore-report').innerHTML = '';
    $('explore-intro').textContent = '';
    $('explore-mismatch').hidden = true;
    return;
  }
  try {
    renderExploreReport(
      await api(`/api/sweeps/${state.scenario.id}/${state.exploreStamp}/explore`),
    );
  } catch (error) {
    $('explore-report').innerHTML = '';
    $('explore-mismatch').hidden = true;
    $('explore-empty').hidden = false;
    $('explore-empty').textContent = `Could not read that run — ${error.message}`;
  }
}

function renderExploreReport(body) {
  const host = $('explore-report');
  host.innerHTML = '';
  host.hidden = false;

  const probe = body.mode === 'explore';
  $('explore-intro').innerHTML =
    (probe
      ? 'A probe, not a sweep: every route on three spread-out dates. '
      : 'Read from a full sweep, so these prices come from many more dates than a probe. ') +
    'Prices are the cheapest seen, so they are a floor to compare airports by, never a fare ' +
    'to book. <strong>Not measured</strong> means the site never answered — run it again ' +
    'rather than ruling that airport out.' +
    (body.state === 'stopped' ? ' <strong>This run was stopped early</strong>, so some routes went unasked.' : '') +
    (body.state === 'throttled' ? ' <strong>The site stopped answering during this run</strong>, so treat thin rows as unmeasured.' : '');

  renderMismatch(body, probe);

  for (const pool of body.pools) {
    const block = document.createElement('div');
    block.className = 'panel__section';

    const heading = document.createElement('h3');
    heading.textContent = pool.role === 'origins' && pool.index === 0
      ? 'Flying from'
      : `${pool.label}`;
    block.appendChild(heading);

    const scroll = document.createElement('div');
    scroll.className = 'table-scroll';
    const table = document.createElement('table');
    table.className = 'data';
    table.innerHTML =
      '<thead><tr><th>Airport</th><th>Verdict</th><th class="num">Cheapest in</th>' +
      '<th class="num">Cheapest out</th><th class="num">Together</th>' +
      '<th class="num">vs best</th><th class="num">Searches</th><th></th></tr></thead>';
    const tbody = document.createElement('tbody');
    for (const row of pool.airports) tbody.appendChild(exploreRow(pool, row, body.currency));
    table.appendChild(tbody);
    scroll.appendChild(table);
    block.appendChild(scroll);

    // Answered rather than left to inference. An airport missing from a table
    // says nothing at all, and the question you came here with is "what about
    // this one?" — the absence of a row is not an answer to it.
    if (pool.not_searched?.length) {
      const missing = document.createElement('p');
      missing.className = 'panel__hint small';
      missing.innerHTML =
        `<strong>${escapeHtml(pool.not_searched.join(', '))}</strong> ` +
        'never searched in this run — run a probe to price them.';
      block.appendChild(missing);
    }
    host.appendChild(block);
  }
}

/* Says, before any table is read, that this run is about a different trip.

   This is the whole failure being fixed: two probes searched the previous
   day's airports, and the tab drew their verdicts as though they were the
   answer for the trip on screen. Nothing here is subtle by design — the number
   in a table is believed, and a wrong one is worse than a blank tab. */
function renderMismatch(body, probe) {
  const notice = $('explore-mismatch');
  if (body.matches_current_trip) { notice.hidden = true; return; }

  const kind = probe ? 'probe' : 'sweep';
  const missing = [...new Set((body.pools ?? []).flatMap((pool) => pool.not_searched ?? []))];
  notice.innerHTML =
    `<strong>This ${kind} searched a different trip.</strong> ` +
    (body.shape_changed
      ? 'Your trip has gained or lost a stop since it ran, so its airports cannot be lined '
        + 'up against yours at all. '
      : '') +
    (missing.length
      ? `Nothing here priced <strong>${escapeHtml(missing.join(', '))}</strong>. `
      : '') +
    'The rows below are the airports that run actually searched.';

  const again = document.createElement('button');
  again.type = 'button';
  again.className = 'small primary';
  again.textContent = 'Run a probe for this trip';
  again.onclick = () => startRun('explore');
  notice.appendChild(document.createElement('br'));
  notice.appendChild(again);
  notice.hidden = false;
}

async function renderResults() {
  const tbody = $('results-table').querySelector('tbody');
  tbody.innerHTML = '';
  $('headline').innerHTML = '';

  if (!state.stamp) {
    $('results-empty').hidden = false;
    $('results-empty').textContent = 'Run a sweep to see itineraries.';
    return;
  }

  // A probe prices three dates a leg, so it almost never chains into a whole
  // trip. An empty table here would read as "the probe found nothing" rather
  // than "that is not what it is for", so say where its answer actually is.
  if (state.sweeps.find((sweep) => sweep.stamp === state.stamp)?.mode === 'explore') {
    $('verification').innerHTML = '';
    $('results-scroll').hidden = true;
    $('results-empty').hidden = false;
    $('results-empty').textContent =
      'That run was a probe — it prices a few dates per leg to compare airports, not to '
      + 'build trips. Its verdicts are in the Explore tab.';
    return;
  }
  $('results-scroll').hidden = false;

  // Every other call site catches; these two did not, so a 500 left a blank
  // table and an explanation only in the browser console.
  let body;
  try {
    body = await api(`/api/sweeps/${state.scenario.id}/${state.stamp}/results`);
  } catch (error) {
    $('results-empty').hidden = false;
    $('results-empty').textContent = `Could not load results — ${error.message}`;
    return;
  }
  $('results-empty').hidden = body.itineraries.length > 0;
  $('results-empty').textContent = body.legs_found
    ? 'This sweep found flights, but none of them chain into a complete trip.'
    : 'Run a sweep to see itineraries.';

  renderVerification(body.verification);

  const same = body.best_same_airport;
  const jaw = body.best_open_jaw;
  // Exactly one card is the headline. On a tie the same-airport option wins,
  // since it is the one preferred when the price is equal.
  // Compare on the bag-inclusive total, not the headline fare: a low-cost
  // carrier's bagless price is not comparable to a bag-inclusive one.
  const withBags = (i) => (i ? i.total_with_bags ?? i.total_price : Infinity);
  const headline = !same ? jaw : !jaw ? same : (withBags(jaw) < withBags(same) ? jaw : same);

  for (const [label, itinerary] of [
    ['Cheapest, same airport', same],
    ['Cheapest, open jaw', jaw],
  ]) {
    const card = document.createElement('div');
    card.className = `stat${itinerary && itinerary === headline ? ' is-headline' : ''}`;
    const saving = itinerary && itinerary !== headline && headline
      ? `<div class="stat__sub trend trend--up">${money(withBags(itinerary) - withBags(headline), '')}more</div>`
      : '';
    // The bookable number leads; the headline fare stays visible underneath so
    // the estimated bag fees are never hidden inside a single figure.
    const bagNote = itinerary && itinerary.bags_needed
      ? `<div class="stat__sub">${money(itinerary.total_price, itinerary.currency)} fare + est. ${itinerary.bags_needed} bag(s)</div>`
      : '<div class="stat__sub">bags included</div>';
    // When the price was true. Without it a figure from a sweep three days ago
    // reads exactly like one from ten minutes ago, and only one of them is
    // still worth clicking through on.
    const measured = itinerary
      ? `<div class="stat__sub muted">${escapeHtml(observedAt(itinerary.observed_at))}` +
        `${escapeHtml(spanNote(itinerary.observed_span_minutes ?? 0))}</div>`
      : '';
    card.innerHTML =
      `<div class="stat__label">${label}</div>` +
      (itinerary
        ? `<div class="stat__value">${money(withBags(itinerary), itinerary.currency)}</div>
           <div class="stat__sub">${itinerary.route}</div>${bagNote}${measured}${saving}`
        : '<div class="stat__value muted">—</div><div class="stat__sub">none found</div>');
    $('headline').appendChild(card);
  }

  // Airline codes, routes and URLs are all scraped from a third-party page, so
  // every one of them is escaped before it reaches innerHTML. A link is built
  // as a node with .href so a `javascript:` URL cannot be injected either.
  for (const itinerary of body.itineraries) {
    const row = document.createElement('tr');
    const airlines = [...new Set(itinerary.legs.map((l) => l.airline))].filter(Boolean).join(', ');
    row.innerHTML =
      `<td>${escapeHtml(itinerary.route)}</td>` +
      `<td>${escapeHtml(itinerary.legs[0].depart_date)}</td>` +
      `<td>${escapeHtml(itinerary.legs[itinerary.legs.length - 1].depart_date)}</td>` +
      `<td>${escapeHtml(airlines)}</td>` +
      `<td>${itinerary.same_airport ? '<span class="badge badge--good">same airport</span>' : '<span class="badge badge--muted">open jaw</span>'}</td>` +
      `<td>${itinerary.bags_needed
        ? `<span class="badge badge--warning">${Number(itinerary.bags_needed)} bag(s) extra</span>`
        : '<span class="badge badge--good">bags included</span>'}</td>` +
      `<td class="num">${money(withBags(itinerary), itinerary.currency)}</td>`;

    const detail = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 7;
    const disclosure = document.createElement('details');
    disclosure.className = 'disclosure';
    disclosure.innerHTML = '<summary class="small muted">Legs</summary>';

    for (const leg of itinerary.legs) {
      const line = document.createElement('div');
      line.className = 'small';
      line.innerHTML =
        `${escapeHtml(leg.origin)}→${escapeHtml(leg.destination)} · ${escapeHtml(leg.depart_date)}` +
        (leg.depart_time ? ` ${escapeHtml(leg.depart_time)}→${escapeHtml(leg.arrive_time)}` : '') +
        ` · ${escapeHtml(leg.airline)} · ${leg.stops ?? '?'} stop(s) · ` +
        `${money(leg.price_amount, leg.price_currency)}` +
        (leg.checked_bag === true
          ? ' · <span class="badge badge--good">bag incl.</span>'
          : ' · <span class="badge badge--warning">bag extra</span>') +
        ` · <span class="muted">${escapeHtml(observedAt(leg.observed_at))}</span>`;
      if (leg.url && /^https?:\/\//i.test(leg.url)) {
        const link = document.createElement('a');
        link.href = leg.url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = 'open';
        line.append(' · ', link);
      }
      disclosure.appendChild(line);
    }

    cell.appendChild(disclosure);
    detail.appendChild(cell);
    tbody.append(row, detail);
  }
}

/* ----------------------------------------------------------------- prices */

/* Why a sweep is or is not comparable, in the tooltip. "0 errors" is not
   health: the 06 Aug standard sweep reported exactly that while averaging 2.9
   legs a search — roughly 70% silent failure — and read 7% dearer than a quick
   sweep that actually worked. */
const sweepQuality = (row) => {
  const parts = [`${row.depth ?? '?'} · ${row.searches ?? '?'} searches`];
  if (row.legs_per_search != null) parts.push(`${row.legs_per_search} legs/search`);
  if (row.routes_planned) parts.push(`${row.routes_covered}/${row.routes_planned} routes`);
  if (!row.comparable) parts.push('not comparable');
  return parts.join(' · ');
};

const historyNote = (history, comparable) => {
  if (!history.length) return '';
  const dropped = history.length - comparable.length;
  if (!dropped) return `<span class="muted">All ${history.length} sweeps are complete enough to compare.</span>`;

  // Name the two failure modes separately: they call for different fixes. A
  // starved sweep means the scraper or the site; short coverage usually means
  // the sweep predates a widening of the trip and measured a smaller one.
  const starved = history.filter((r) => !r.comparable && r.legs_per_search != null
    && r.legs_per_search < 6).length;
  const short = dropped - starved;
  const reasons = [
    starved ? `${starved} ran too thin to trust (under 6 legs per search)` : '',
    short ? `${short} did not cover every route this trip needs` : '',
  ].filter(Boolean).join(', and ');

  // Said plainly, because a single solid point among dimmed ones still reads as
  // a trend at a glance, and it is not one.
  const trend = comparable.length >= 2
    ? ''
    : ` ${comparable.length === 1
      ? 'That leaves one sweep complete enough to compare, so there is no trend here yet'
      : 'No sweep yet is complete enough to compare against another'}.`;

  return `<span class="muted">${dropped} of ${history.length} sweeps are dimmed: ${escapeHtml(reasons)}. ` +
    'Their totals are drawn but not joined or labelled — a line through them would track how well ' +
    `the scraper was working rather than what flights cost.${trend}</span>`;
};

/* Depth sets the resolution of this curve, and the curve is steep — 29% between
   the cheapest and dearest day sampled — so a coarse grid can miss the best day
   entirely. Say what the grid is instead of drawing a smooth line through it. */
const byDateNote = (byDate) => {
  if (byDate.length < 2) return '';
  const days = byDate.map((row) => Date.parse(row.depart_date) / 86400000);
  const step = Math.round(Math.min(...days.slice(1).map((d, i) => d - days[i])));
  const values = byDate.map((row) => row.cheapest_total);
  // The saving from picking the right day, which is the decision this chart
  // supports. Not max/min - 1: that is how far the dearest day sits *above*
  // the cheapest, a larger number that answers a question nobody asked.
  const saving = Math.round((1 - Math.min(...values) / Math.max(...values)) * 100);
  const slack = Math.floor(step / 2);
  return '<span class="muted">' +
    `Sampled every ${step} day${step === 1 ? '' : 's'}` +
    (slack ? `, so the true cheapest day could be up to ${slack} day${slack === 1 ? '' : 's'} either side of a point` : '') +
    `. The cheapest day sampled is ${saving}% below the dearest` +
    (step > 1 ? ' — sweep deeper to find the day itself.' : '.') +
    '</span>';
};

async function renderPrices() {
  if (!state.stamp) return;

  // Independent endpoints, fetched together. Awaiting them one after another
  // made the Prices tab about three round trips slower than it needed to be.
  // `allSettled` so one failing panel does not blank the other two.
  const [byDateResult, historyResult, probeResult] = await Promise.allSettled([
    api(`/api/sweeps/${state.scenario.id}/${state.stamp}/by-date`),
    api(`/api/history/${state.scenario.id}`),
    api('/api/probe'),
  ]);

  // The chart used to hardcode " CZK" regardless of what the legs were priced
  // in. The scenario says what the currency is.
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const byDate = byDateResult.status === 'fulfilled' ? byDateResult.value : [];
  const history = historyResult.status === 'fulfilled' ? historyResult.value : [];

  const byDateHost = $('chart-by-date');
  const width = Math.max(420, byDateHost.clientWidth - 4);
  byDateHost.innerHTML = '';
  byDateHost.appendChild(lineChart(
    byDate.map((row) => ({ label: row.depart_date, value: row.cheapest_total, note: row.route })),
    { width, valueSuffix: suffix, ariaLabel: 'Cheapest total by departure date',
      // A range being picked wins over the one already saved, so a click shows
      // its effect immediately rather than after a save.
      band: focusRange() ?? (state.scenario.focus_start
        ? [state.scenario.focus_start, state.scenario.focus_end]
        : null),
      onPick: pickFocusDate,
      emptyText: byDateResult.status === 'rejected'
        ? `Could not load — ${byDateResult.reason.message}`
        : 'No itineraries in this sweep yet.' },
  ));
  $('by-date-note').innerHTML = byDateNote(byDate);

  $('chart-history').innerHTML = '';
  // Every sweep is drawn, including the ones too incomplete to trust: the gaps
  // in the record are worth seeing, and a chart that silently dropped them
  // would be its own kind of lie. What comparability buys is the *line* — only
  // trustworthy points are joined solid and eligible for the cheapest label.
  // Drawn through starved sweeps, this chart tracked how well the scraper was
  // working rather than what flights cost.
  const comparable = history.filter((row) => row.comparable);
  $('chart-history').appendChild(lineChart(
    history.length >= 2
      ? history.map((row) => ({
        label: row.swept_at.slice(0, 10),
        value: row.best_total,
        muted: !row.comparable,
        note: sweepQuality(row),
      }))
      : [],
    { width, valueSuffix: suffix, ariaLabel: 'Best total over time',
      emptyText: history.length === 1
        ? 'Only one sweep so far — a trend needs at least two.'
        : 'Needs a few sweeps before a trend means anything.' },
  ));
  $('history-note').innerHTML = historyNote(history, comparable);

  const probe = probeResult.status === 'fulfilled' ? probeResult.value : { routes: {}, recommendation: '' };
  const routes = Object.entries(probe.routes);
  $('probe-body').className = routes.length ? '' : 'empty';
  // Net movement leads, because it is what decides whether to book. The panel
  // used to lead with "median move" and "biggest drop", and reported 24 and 20
  // for FRA→NRT during the four days it climbed 2,724 Kč — both true, both
  // useless, since `largest_drop` reads only negative steps.
  $('probe-body').innerHTML = routes.length
    ? '<div class="table-scroll"><table class="data"><thead><tr>' +
      '<th>Route</th><th class="num">Observations</th><th class="num">Net move</th>' +
      '<th class="num">High to low</th><th class="num">Steps that moved</th>' +
      '<th class="num">…by more than 1%</th></tr></thead><tbody>' +
      routes.map(([name, r]) => {
        const net = Number(r.net_change_pct ?? 0);
        const trend = net > 0 ? 'trend trend--up' : net < 0 ? 'trend trend--down' : 'muted';
        return `<tr><td>${escapeHtml(name)}</td><td class="num">${Number(r.n_observations)}</td>` +
          `<td class="num ${trend}">${net > 0 ? '+' : ''}${net.toFixed(1)}%</td>` +
          `<td class="num">${Number(r.range_pct ?? 0).toFixed(1)}%</td>` +
          `<td class="num muted">${Math.round(r.change_rate * 100)}%</td>` +
          `<td class="num">${Math.round((r.meaningful_change_rate ?? 0) * 100)}%</td></tr>`;
      }).join('') +
      '</tbody></table></div>' +
      `<p class="panel__hint" style="margin-top:12px">${escapeHtml(probe.recommendation)}</p>`
    : 'No observations yet.';
}

/* ------------------------------------------------------------------ focus

   Once a broad sweep has shown which departure dates are cheap, a focus
   narrows the next one onto them. It bounds the *first* leg only; the later
   legs are derived from it through the stay ranges, so the three can never
   contradict each other and a focused sweep can still complete a trip.

   Picked here rather than typed into two date boxes because the decision is
   made by looking at the chart, and a date box beside a chart is a second
   place to get the same answer wrong. */

// Which two days are picked, before anything is saved. Kept out of
// `state.scenario` so an abandoned pick never reaches disk.
const focusPick = { start: null, end: null };

function focusRange() {
  const { start, end } = focusPick;
  if (!start) return null;
  if (!end) return [start, start];
  return start <= end ? [start, end] : [end, start];
}

function pickFocusDate(label) {
  // First click starts a range, second closes it, third starts over. A picker
  // that could only ever extend would need a Clear button to correct a misclick.
  if (!focusPick.start || (focusPick.start && focusPick.end)) {
    focusPick.start = label;
    focusPick.end = null;
  } else {
    focusPick.end = label;
  }
  renderFocusControls();
  renderPrices();
}

async function renderFocusControls() {
  const saved = state.scenario.focus_start && state.scenario.focus_end
    ? [state.scenario.focus_start, state.scenario.focus_end]
    : null;
  const badge = $('focus-state');
  badge.className = `badge badge--${saved ? 'good' : 'muted'}`;
  badge.textContent = saved
    ? `watching ${saved[0]} to ${saved[1]}`
    : 'watching the whole window';

  const range = focusRange();
  const save = $('focus-save');
  const note = $('focus-pick');
  $('focus-clear').disabled = !saved && !range;

  if (!range) {
    save.disabled = true;
    note.textContent = saved
      ? 'Click two points to move the window you are watching.'
      : 'Click two points to pick the days worth watching closely.';
    return;
  }

  const [from, to] = range;
  save.disabled = false;
  note.textContent = focusPick.end
    ? `Picked ${from} to ${to}. `
    : `Picked ${from}. Click a second day to close the range. `;

  // What it would actually cost, priced against the trip on screen rather than
  // guessed. The estimate endpoint takes an unsaved trip for exactly this.
  try {
    const trip = { ...state.scenario, focus_start: from, focus_end: to };
    const [narrow, broad] = await Promise.all([
      api(`/api/scenarios/${state.scenario.id}/estimate?depth=deep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(trip),
      }),
      api(`/api/scenarios/${state.scenario.id}/estimate?depth=deep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...state.scenario, focus_start: null, focus_end: null }),
      }),
    ]);
    note.textContent +=
      `${narrow.searches} searches (~${narrow.minutes} min) against ` +
      `${broad.searches} for the whole window.`;
  } catch (error) {
    note.textContent += `Could not price that range — ${error.message}`;
  }
}

$('focus-save').onclick = async () => {
  const range = focusRange();
  if (!range) return;
  await saveFocus(range[0], range[1]);
};

$('focus-clear').onclick = async () => {
  focusPick.start = focusPick.end = null;
  await saveFocus(null, null);
};

async function saveFocus(from, to) {
  const note = $('focus-pick');
  try {
    const saved = await api(`/api/scenarios/${state.scenario.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...state.scenario, focus_start: from, focus_end: to }),
    });
    state.scenario = saved;
    focusPick.start = focusPick.end = null;
    await renderFocusControls();
    note.textContent = from
      ? `Saved. The afternoon sweep will price ${from} to ${to}; the morning one still ` +
        'covers the whole window.'
      : 'Cleared. Both sweeps cover the whole window again.';
  } catch (error) {
    note.className = 'small badge badge--error';
    note.textContent = error.message;
  }
}

/* ---------------------------------------------------------------- sources */

/* One card per source, and the card answers the only question people arrive
   here with: is this still working. The panel used to open on four text fields
   and six CSS selectors for one site, which is the answer to a question you
   only have once the answer to this one is "no" — so the selectors moved behind
   Repair, and Repair opens itself when a check has failed. */

const SELECTOR_HELP = {
  card: 'One offer. Everything else is read inside it.',
  price: 'The block holding the price and its currency symbol.',
  date: 'The departure date printed on the card — read rather than assumed, because the site substitutes nearby dates.',
  time: 'Departure and arrival times.',
  baggage_icon: 'The bag icon, whose filename says included or not.',
  baggage_label: 'The text beside it — "Ano" / "Ne", or an invitation to click through.',
};

const FIELD_HELP = {
  base_url: 'Everything before the search parameters.',
  url_template: 'Comma-joined parameters. {origin}, {destination}, {depart}, {adults} and {trip_type} are filled in; a return date is appended for a round trip.',
  no_results_marker: 'The site’s own wording for "no flights on this route". Without it, an empty route and a broken search look identical.',
  result_timeout_s: 'How long to wait for results before calling the search failed.',
};

/* What a broken one actually costs you. Stated on the card, because "source"
   flattens three very different stakes into one word. */
const ROLE_NOTE = {
  sweep: 'Every price in this app. If this breaks, the nightly sweep goes quiet.',
  check: 'A second opinion, run by hand. If this breaks you lose the cross-check, not the prices.',
  none: 'Not connected. Nothing in this app reads it.',
};

/* A check with no date on it is a claim about now that may be a fortnight old,
   so `checked_at` is always shown alongside the verdict. Reuses the same
   relative wording as the results headline - two phrasings for "how long ago"
   in one app is one too many. */
function checkedAgo(iso) {
  const then = new Date(iso);
  return Number.isNaN(then.getTime()) ? iso : relativeTime(then);
}

function checkBadge(source) {
  const check = source.last_check;
  if (source.role === 'none') return { kind: 'muted', text: 'not connected' };
  if (!check) return { kind: 'muted', text: 'never checked' };
  const when = check.checked_at ? ` · ${checkedAgo(check.checked_at)}` : '';
  return check.ok
    ? { kind: 'good', text: `working${when}` }
    : { kind: 'error', text: `not working${when}` };
}

async function renderSources() {
  const host = $('sources-body');
  let sources;
  try {
    sources = await api('/api/sources');
  } catch (error) {
    host.className = 'empty';
    host.textContent = `Could not load sources — ${error.message}`;
    return;
  }
  host.className = 'source-list';
  host.innerHTML = '';
  for (const [name, source] of Object.entries(sources)) {
    host.appendChild(sourceCard(name, source));
  }
}

function sourceCard(name, source) {
  const card = document.createElement('div');
  card.className = 'source-card';
  card.dataset.source = name;

  const badge = checkBadge(source);
  const header = document.createElement('div');
  header.className = 'source-card__header';
  header.innerHTML =
    `<h3>${escapeHtml(source.label || name)}</h3>` +
    `<span class="badge badge--${badge.kind}" data-role="state">${escapeHtml(badge.text)}</span>`;
  card.appendChild(header);

  if (source.note) {
    const note = document.createElement('p');
    note.className = 'panel__hint small';
    note.style.margin = '0 0 8px';
    note.textContent = source.note;
    card.appendChild(note);
  }

  const stake = document.createElement('p');
  stake.className = 'muted small';
  stake.style.margin = '0 0 12px';
  stake.textContent = ROLE_NOTE[source.role] ?? '';
  card.appendChild(stake);

  const details = repairForm(name, source);

  const row = document.createElement('div');
  row.className = 'row';
  if (source.role !== 'none') {
    const check = document.createElement('button');
    check.className = 'primary small';
    check.dataset.role = 'check';
    check.textContent = 'Check now';
    check.onclick = () => testSource(name, check, card);
    row.appendChild(check);
  }
  if (source.repairable) {
    const repair = document.createElement('button');
    repair.className = 'small';
    repair.dataset.role = 'repair';
    repair.textContent = 'Repair';
    repair.onclick = () => toggleRepair(card);
    row.appendChild(repair);
  }
  card.appendChild(row);

  const outcome = document.createElement('div');
  outcome.className = 'small';
  outcome.style.marginTop = '10px';
  outcome.dataset.role = 'outcome';
  card.appendChild(outcome);
  if (source.last_check) renderCheck(outcome, source.last_check);

  // Opened for you when the last check failed, because that is the one moment
  // these fields are worth looking at — and closed otherwise, because the rest
  // of the time they are six CSS selectors in the way of a yes/no answer.
  card.appendChild(details);
  setRepairOpen(card, Boolean(source.last_check && source.last_check.ok === false));
  return card;
}

function setRepairOpen(card, open) {
  const details = card.querySelector('.source-card__repair');
  const button = card.querySelector('[data-role="repair"]');
  if (!details) return;
  details.toggleAttribute('hidden', !open);
  if (button) button.textContent = open ? 'Hide repair' : 'Repair';
}

function toggleRepair(card) {
  const details = card.querySelector('.source-card__repair');
  setRepairOpen(card, details.hasAttribute('hidden'));
}

function repairForm(name, source) {
  const details = document.createElement('div');
  details.className = 'source-card__repair';
  details.hidden = true;
  if (!source.repairable) return details;

  details.insertAdjacentHTML(
    'beforeend',
    '<p class="panel__hint small">Edit the string that broke, then save — it re-checks itself. ' +
      'Zero cards matched means the markup changed; a page that will not load at all means the ' +
      'URL did.</p>'
  );

  const grid = document.createElement('div');
  grid.className = 'form-grid';
  for (const key of ['base_url', 'url_template', 'no_results_marker', 'result_timeout_s']) {
    const label = document.createElement('label');
    label.className = key === 'result_timeout_s' ? 'field' : 'field field--wide';
    label.innerHTML =
      `${escapeHtml(key)}<input type="${key === 'result_timeout_s' ? 'number' : 'text'}" ` +
      `data-field="${key}" value="${escapeHtml(source[key])}">` +
      `<span class="muted small">${escapeHtml(FIELD_HELP[key] ?? '')}</span>`;
    grid.appendChild(label);
  }
  details.appendChild(grid);

  details.insertAdjacentHTML(
    'beforeend',
    '<h3 class="muted small" style="margin-top:16px">Selectors</h3>'
  );
  const selectors = document.createElement('div');
  selectors.className = 'form-grid';
  for (const [key, value] of Object.entries(source.selectors)) {
    const label = document.createElement('label');
    label.className = 'field field--wide';
    label.innerHTML =
      `${escapeHtml(key)}<input type="text" data-selector="${escapeHtml(key)}" ` +
      `value="${escapeHtml(value)}">` +
      `<span class="muted small">${escapeHtml(SELECTOR_HELP[key] ?? '')}</span>`;
    selectors.appendChild(label);
  }
  details.appendChild(selectors);

  const row = document.createElement('div');
  row.className = 'row';
  row.style.marginTop = '14px';
  const save = document.createElement('button');
  save.className = 'small';
  save.textContent = 'Save and check again';
  save.onclick = () => saveSources(name, details.closest('.source-card'), source);
  row.appendChild(save);
  details.appendChild(row);
  return details;
}

$('sources-check-all').onclick = async () => {
  const button = $('sources-check-all');
  const label = button.textContent;
  button.disabled = true;
  button.textContent = 'Checking…';
  try {
    // One at a time, not in parallel: these are real searches against real
    // sites, and the whole project exists because this client has been
    // throttled for asking too fast.
    for (const card of document.querySelectorAll('.source-card')) {
      const check = card.querySelector('[data-role="check"]');
      if (check) await testSource(card.dataset.source, check, card);
    }
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
};

/* ------------------------------------------------------- the notify target

   The webhook never comes back from the server, so this panel is written
   around that: the field shows a masked placeholder and an empty field means
   "leave it alone", never "clear it". Removing is its own button. */

async function renderNotifyTarget() {
  const field = $('webhook-url');
  const badge = $('notify-origin');
  let body;
  try {
    body = await api('/api/notify');
  } catch (error) {
    badge.textContent = 'unknown';
    $('webhook-result').textContent = `Could not read the current setting — ${error.message}`;
    return;
  }

  field.value = '';
  field.placeholder = body.masked || 'https://discord.com/api/webhooks/…';
  $('webhook-path').textContent = body.path;
  badge.className = `badge badge--${body.configured ? 'good' : 'muted'}`;
  badge.textContent = {
    environment: 'set by DISCORD_WEBHOOK_URL',
    file: 'saved on this machine',
    none: 'not set — nothing is being sent',
  }[body.origin];

  // Editing the box would be silently pointless while the variable overrides it.
  const overridden = body.origin === 'environment';
  field.disabled = overridden;
  $('webhook-save').disabled = overridden;
  $('webhook-clear').disabled = overridden;
  if (overridden) {
    $('webhook-result').className = 'small muted';
    $('webhook-result').textContent =
      'DISCORD_WEBHOOK_URL is set in this server’s environment and takes precedence, so ' +
      'there is nothing to edit here. Unset it to manage the webhook from this page.';
  }
}

function notifyOutcome(message, kind = 'good') {
  const host = $('webhook-result');
  host.className = `small badge badge--${kind}`;
  host.textContent = message;
}

$('webhook-save').onclick = async () => {
  const url = $('webhook-url').value.trim();
  if (!url) return notifyOutcome('Paste the webhook URL first.', 'error');
  try {
    await api('/api/notify', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    await renderNotifyTarget();
    notifyOutcome('Saved. Send a test message to prove it reaches the channel.');
  } catch (error) {
    notifyOutcome(error.message, 'error');
  }
};

$('webhook-clear').onclick = async () => {
  try {
    await api('/api/notify', { method: 'DELETE' });
    await renderNotifyTarget();
    notifyOutcome('Removed. Sweeps will run as before and report nowhere.', 'muted');
  } catch (error) {
    notifyOutcome(error.message, 'error');
  }
};

$('webhook-test').onclick = async () => {
  const button = $('webhook-test');
  button.disabled = true;
  try {
    const body = await api('/api/notify/test', { method: 'POST' });
    notifyOutcome(body.message, body.sent ? 'good' : 'error');
  } catch (error) {
    notifyOutcome(error.message, 'error');
  } finally {
    button.disabled = false;
  }
};

function readSourceForm(card, source) {
  const next = { ...source, selectors: { ...source.selectors } };
  delete next.last_check;
  for (const input of card.querySelectorAll('[data-field]')) {
    next[input.dataset.field] =
      input.dataset.field === 'result_timeout_s' ? Number(input.value) : input.value;
  }
  for (const input of card.querySelectorAll('[data-selector]')) {
    next.selectors[input.dataset.selector] = input.value;
  }
  return next;
}

async function saveSources(name, card, source) {
  const outcome = card.querySelector('[data-role="outcome"]');
  try {
    // Only the source being edited is sent. The others are left as they stand
    // on disk, so repairing one can never revert another to its defaults — and
    // two of the three have no selectors to send in the first place.
    await api('/api/sources', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [name]: readSourceForm(card, source) }),
    });
  } catch (error) {
    outcome.className = 'small badge badge--error';
    outcome.textContent = error.message;
    return;
  }
  // Saved is not working, and the gap between the two is the whole reason this
  // panel exists — so the save proves itself instead of claiming success.
  await testSource(name, card.querySelector('[data-role="check"]'), card);
}

function renderCheck(host, body) {
  host.className = 'small';
  const link = body.url && /^https?:\/\//i.test(body.url)
    ? `<a href="${escapeHtml(body.url)}" target="_blank" rel="noopener">open the exact URL it used</a>`
    : '';
  host.innerHTML =
    `<span class="badge badge--${body.ok ? 'good' : 'error'}">` +
    `${body.cards_found ?? 0} found, ${body.legs_parsed ?? 0} priced</span> ` +
    `${escapeHtml(body.message ?? '')}<br>` +
    `<span class="muted">${escapeHtml(body.route ?? '')}</span> ${link}` +
    (body.sample
      ? `<br><span class="muted">First offer: ${escapeHtml(body.sample.airline ?? '')} · ` +
        `${escapeHtml(body.sample.depart_date ?? '')} · ` +
        `${money(body.sample.price_amount, body.sample.price_currency)}</span>`
      : '');
}

async function testSource(name, button, card) {
  const outcome = card.querySelector('[data-role="outcome"]');
  const state = card.querySelector('[data-role="state"]');
  const label = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = 'Checking…';
  }
  outcome.className = 'small muted';
  outcome.textContent = 'Asking the site for one real price — this takes 15–30 seconds.';
  state.className = 'badge badge--muted';
  state.textContent = 'checking…';
  try {
    const body = await api(`/api/sources/${name}/test`, { method: 'POST' });
    renderCheck(outcome, body);
    state.className = `badge badge--${body.ok ? 'good' : 'error'}`;
    state.textContent = body.ok ? 'working · just now' : 'not working · just now';
    // A failed check is the one moment the repair fields are worth looking at.
    if (!body.ok) setRepairOpen(card, true);
  } catch (error) {
    outcome.className = 'small badge badge--error';
    outcome.textContent = error.message;
    state.className = 'badge badge--error';
    state.textContent = 'check failed';
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

/* -------------------------------------------------------------------- init */

async function loadScenario(id) {
  state.isNew = false;
  updateNewTripUi();
  state.scenario = await api(`/api/scenarios/${id}`);
  // Cache the labels for every airport the scenario names, so chips read
  // "PRG Prague" on first paint instead of filling in after a search.
  const codes = [...new Set([
    ...state.scenario.origins,
    ...state.scenario.stops.flatMap((stop) => stop.airports),
    ...(state.scenario.return_to ?? []),
  ])];
  await Promise.allSettled(codes.map(async (code) => {
    if (!airportCache.has(code)) cacheAirports([await api(`/api/airports/${code}`)]);
  }));
  fillForm(state.scenario);
}

/* Reloads the saved-trip list and opens `preferred`, or the first trip, or an
   empty draft when nothing is saved at all. Called after create and delete so
   the dropdown never names a file that is no longer there. */
async function reloadScenarioList(preferred = null) {
  let body;
  try {
    body = await api('/api/scenarios');
  } catch (error) {
    // "I could not ask" and "you have no trips" both used to end at an empty
    // picker, and only one of them means anything is missing.
    block(
      'Could not read your saved trips',
      `The server answered: <code>${escapeHtml(error.message)}</code>. The trips ` +
      'themselves are JSON files under <code>scenarios/</code> and are still there.',
      { retry: () => window.location.reload() },
    );
    return;
  }
  unblock();

  const scenarios = body.trips;
  const select = $('scenario-select');
  select.innerHTML = '';
  for (const scenario of scenarios) {
    const option = document.createElement('option');
    option.value = scenario.id;
    option.textContent = scenario.name;
    select.appendChild(option);
  }

  // Named rather than swallowed: a trip missing from the picker because its
  // file will not parse should say so, not simply be absent.
  if (body.problems.length) {
    showError(
      `${body.problems.length} trip file(s) could not be read and are missing from the ` +
      `list: ${body.problems.map((p) => `${p.file} (${p.error})`).join('; ')}`,
    );
  }

  if (!scenarios.length) {
    // A fresh checkout has no trips. Opening straight into a draft beats an
    // empty page telling you to go and write JSON.
    $('new-trip-btn').onclick();
    return;
  }

  const id = scenarios.some((s) => s.id === preferred) ? preferred : scenarios[0].id;
  select.value = id;
  state.stamp = null;
  await loadScenario(id);
  await refreshEstimate();
  await pollStatus();
}

async function init() {
  // Asked first and on its own: everything after it assumes the server can
  // answer the calls this page makes, and a stale one cannot.
  if (!(await contractMatches())) return;

  try {
    const [viability, frequent] = await Promise.all([
      api('/api/viability').catch(() => ({ airports: {} })),
      api('/api/airports/frequent').catch(() => ({ origins: [], destinations: [] })),
    ]);
    state.viability = viability;
    state.frequent = frequent;
    cacheAirports([...frequent.origins, ...frequent.destinations]);

    $('scenario-select').onchange = async () => {
      const id = $('scenario-select').value;
      if (!id) return;
      state.stamp = null;
      await loadScenario(id);
      await refreshEstimate();
      await pollStatus();
    };

    await reloadScenarioList();
  } catch (error) {
    $('status-strip').className = 'status-strip is-error';
    $('status-text').textContent = `Could not start — ${error.message}`;
  }
}

init();
