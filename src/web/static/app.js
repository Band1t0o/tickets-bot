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
};

const api = async (path, options) => {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return response.json();
};

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

$('tabs').onclick = (event) => {
  const button = event.target.closest('button[data-tab]');
  if (!button) return;
  for (const b of $('tabs').children) b.classList.toggle('is-active', b === button);
  for (const section of document.querySelectorAll('section[data-panel]')) {
    section.hidden = section.dataset.panel !== button.dataset.tab;
  }
  if (button.dataset.tab === 'results') renderResults();
  if (button.dataset.tab === 'prices') renderPrices();
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
const route = { origins: [], stops: [], returnTo: [] };

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

function renderRoute() {
  renderOrigins();
  renderStops();
  renderReturn();
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
    return;
  }
  // The estimate is served for the *saved* scenario, so an unsaved edit shows
  // the last saved cost. Say so rather than implying the number is live.
  const dirty = JSON.stringify(formToScenario()) !== JSON.stringify(state.scenario);
  try {
    const body = await api(
      `/api/scenarios/${state.scenario.id}/estimate?depth=${$('depth').value}`,
      { method: 'POST' },
    );
    $('estimate').textContent =
      `${body.searches} searches · ~${body.minutes} min` + (dirty ? ' (saved version)' : '');
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

/* -------------------------------------------------------------- save/run */

const showError = (message) => {
  $('save-error').textContent = message;
  $('save-error').hidden = false;
};

$('save-btn').onclick = async () => {
  $('save-error').hidden = true;
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
    await refreshEstimate();
    $('save-btn').textContent = 'Saved';
    setTimeout(() => ($('save-btn').textContent = 'Save trip'), 1500);
  } catch (error) {
    showError(error.message);
  }
};

$('delete-trip-btn').onclick = async () => {
  $('save-error').hidden = true;
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
  $('save-error').hidden = true;
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
  for (const id of ['run-local-btn', 'run-cloud-btn']) $(id).disabled = state.isNew;
  $('delete-trip-btn').textContent = state.isNew ? 'Discard' : 'Delete';
}

$('run-local-btn').onclick = async () => {
  $('save-error').hidden = true;
  try {
    await api(`/api/scenarios/${state.scenario.id}/run?depth=${$('depth').value}`, { method: 'POST' });
    pollStatus();
  } catch (error) {
    showError(error.message);
  }
};

$('run-cloud-btn').onclick = async () => {
  $('save-error').hidden = true;
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

    // A sweep thread that died has no status.json to show, so without this the
    // strip would sit on "No sweeps yet" forever after a failed launch.
    if (body.error) {
      strip.className = 'status-strip is-error';
      $('status-text').textContent = `Sweep failed — ${body.error}`;
      populateSweepSelect();
      return;
    }

    if (body.running && latest) {
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
      strip.className = `status-strip${broken ? ' is-error' : ''}`;
      $('status-text').textContent = broken
        ? `Last sweep looked broken — ${latest.legs_found ?? 0} flights found`
        : `Last sweep ${latest.stamp.replace('T', ' ').replace('Z', '')} · ${latest.legs_found ?? 0} flights`;
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
    option.textContent =
      `${sweep.stamp.replace('T', ' ').replace('Z', '')} · ` +
      `${sweep.depth ?? '?'} · ${sweep.total ?? 0} searches · ${sweep.legs_found ?? 0} flights` +
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

async function renderResults() {
  const tbody = $('results-table').querySelector('tbody');
  tbody.innerHTML = '';
  $('headline').innerHTML = '';

  if (!state.stamp) {
    $('results-empty').hidden = false;
    $('results-empty').textContent = 'Run a sweep to see itineraries.';
    return;
  }

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
  $('probe-body').innerHTML = routes.length
    ? '<div class="table-scroll"><table class="data"><thead><tr>' +
      '<th>Route</th><th class="num">Observations</th><th class="num">Changed</th>' +
      '<th class="num">Median move</th><th class="num">Biggest drop</th></tr></thead><tbody>' +
      routes.map(([name, r]) =>
        `<tr><td>${escapeHtml(name)}</td><td class="num">${Number(r.n_observations)}</td>` +
        `<td class="num">${Math.round(r.change_rate * 100)}%</td>` +
        `<td class="num">${Math.round(r.median_change).toLocaleString()}</td>` +
        `<td class="num">${Math.round(r.largest_drop).toLocaleString()}</td></tr>`).join('') +
      '</tbody></table></div>' +
      `<p class="panel__hint" style="margin-top:12px">${escapeHtml(probe.recommendation)}</p>`
    : 'No observations yet.';
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
  const scenarios = await api('/api/scenarios');
  const select = $('scenario-select');
  select.innerHTML = '';
  for (const scenario of scenarios) {
    const option = document.createElement('option');
    option.value = scenario.id;
    option.textContent = scenario.name;
    select.appendChild(option);
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
