import { lineChart, multiLineChart } from '/chart.js';

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
  // How the Results table is narrowed. Held here rather than read off the DOM
  // so a re-render cannot lose it, and sent to the server rather than applied
  // here - see the comment on the filter row in index.html.
  filters: { from: '', to: '', bags: false },
};

/* Must equal `API_CONTRACT` in src/web/app.py; a test fails if it does not.

   Static files are served from disk on every request, but the Python is frozen
   at import time — so a server left running from an older commit hands you this
   very file and then 404s the endpoints it asks for. That is how an afternoon
   was lost: the page rendered an empty trip picker and empty charts, which is
   exactly what a deleted database looks like, when in fact nothing on disk had
   changed and the answer was `make ui` again. */
const EXPECTED_CONTRACT = 12;

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
  const active = document.querySelector('#tabs button.is-active')?.dataset.tab ?? 'map';
  for (const section of document.querySelectorAll('section[data-panel]')) {
    section.hidden = section.dataset.step !== active;
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

// Thousands separated. "1618 flights" and "483 searches" sit side by side in the
// sweep picker, and at a glance the first reads as the smaller of the two.
//
// Ambient locale, unlike the dates below, and the difference is deliberate: it
// has to group the same way `money` does or one line reads "25 000 CZK" beside
// "1,618 flights". A date is pinned because it has to line up against a
// workflow log; a thousands separator answers to nothing but the line it is on.
const count = (n) => Number(n ?? 0).toLocaleString();

/* Every time on screen is a Prague time.

   Two separate things stopped that being automatic. Sweep directories are named
   `2026-08-20T18-14-54Z` — a colon is illegal in a path, so the time carries
   dashes and `new Date` rejects the string outright. Three call sites worked
   around that by hacking out the T and the Z, which shipped raw UTC to a reader
   who has never once wanted UTC. And `toLocaleString(undefined, …)` follows
   whatever the machine is set to, so the same sweep read differently on a laptop
   abroad — a stamp you cannot line up against the workflow logs.

   Both are fixed here rather than at each call site. `en-GB` rather than the
   ambient locale for the same reason the timezone is pinned: the rest of this
   page is written in English, and a month abbreviation that changes with the OS
   is one more thing that reads differently depending on where you opened it. */
const PRAGUE = 'Europe/Prague';

const asDate = (stamp) => {
  if (!stamp) return null;
  // Put back the colons a directory name could not hold.
  const when = new Date(String(stamp).replace(/T(\d{2})-(\d{2})-(\d{2})Z$/, 'T$1:$2:$3Z'));
  return Number.isNaN(when.getTime()) ? null : when;
};

/* "20 Aug, 20:14" in Prague. Empty string when the stamp cannot be read, so a
   caller concatenating it degrades to a missing time rather than to "Invalid
   Date" sitting in the middle of a sentence. */
const localStamp = (stamp, opts = {}) => {
  const when = asDate(stamp);
  return when === null ? '' : when.toLocaleString('en-GB', {
    timeZone: PRAGUE,
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
    ...opts,
  });
};

/* A price is a measurement, and one taken three days ago may no longer be
   buyable. Every total on screen says when it was read off the site. */

const observedAt = (iso) => {
  const when = asDate(iso);
  if (when === null) return 'time not recorded';
  return `measured ${localStamp(iso)} · ${relativeTime(when)}`;
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

/* Three steps and a gear, over the same eleven panels.

   The tabs used to be one per panel, which grew to seven and then read as a
   list of screens rather than as the order the work is actually done in. Two of
   them - Prices and Watch - both drew a price history, and nothing on either
   said which question it was answering. They are split by question now: the
   by-date chart says *which days*, so it sits with the narrowing; the history
   chart and the probe say *whether to book now*, so they sit with what you are
   following.

   Sections carry `data-step` and several share one, so a step is a group of
   panels rather than a renamed panel. `data-panel` stays as each one's own
   name, because that is what every renderer, test and error box already
   addresses them by. */
const STEP_OF = {
  search: 'map', explore: 'map',
  narrow: 'narrow', dates: 'narrow', results: 'narrow',
  watch: 'follow', history: 'follow',
  notify: 'setup', night: 'setup', cloud: 'setup', sources: 'setup',
};

// Where a name that is not a step should take you. `showTab` is called with a
// panel name from a few places - a finished sweep opens Results, a save error
// opens Search - and those care about the panel, not about the grouping.
const stepFor = (name) => STEP_OF[name] ?? name;

function showTab(name) {
  const step = stepFor(name);
  for (const b of $('tabs').querySelectorAll('button[data-tab]')) {
    b.classList.toggle('is-active', b.dataset.tab === step);
  }
  for (const section of document.querySelectorAll('section[data-panel]')) {
    section.hidden = section.dataset.step !== step;
  }
  if (step === 'map') renderExplore();
  if (step === 'narrow') {
    // The charts choose the run for the whole step, so they settle before the
    // panels that read `state.stamp`. Fired off in parallel, the table below
    // could render the run the charts were about to move off.
    renderNarrow().then(() => {
      renderByDate();
      renderFocusControls();
      renderResults();
      renderCloudSync();
    });
  }
  if (step === 'follow') { renderWatch(); renderHistory(); }
  if (step === 'setup') {
    renderNightSweep();
    renderCloudRuns();
    renderCloudSync();
    renderSources();
    renderNotifyTarget();
  }
  // A panel named rather than a step: scroll to it, since its step may hold
  // several and the one asked for is not always the first.
  if (name !== step) {
    document.querySelector(`section[data-panel="${name}"]`)
      ?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
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

    card.append(head, chips, overlandControl(stop));
    if (stop.overland) card.appendChild(crossingPins(stop));
    host.appendChild(card);
  });

  // There is no other order of one stop, so the tick would be a control that
  // does nothing. Cleared as well as hidden: a trip cut back to one stop would
  // otherwise keep a setting nothing on screen any longer mentions.
  const swappable = route.stops.length > 1;
  $('both-orders-field').hidden = !swappable;
  if (!swappable && $('probe-both-orders').checked) {
    $('probe-both-orders').checked = false;
    scheduleEstimate();
  }
}

/* Arrive at one of a stop's airports and leave from another, crossing the
   country on the ground in between: Haneda in, Kansai out; Porto in, Lisbon
   out. Everywhere else the airports of a stop are alternatives *because* a trip
   has to leave from the airport it landed at, and this is the one place that
   rule is suspended. It costs no extra searching - every airport pair between
   two stops is already priced - so the only thing standing between you and the
   cheaper trip was that nothing could express it.

   With one airport there is nowhere else to leave from, so the box is disabled
   and says why rather than sitting there doing nothing. */

function overlandControl(stop) {
  const row = document.createElement('label');
  row.className = 'check small stop__overland';

  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = Boolean(stop.overland);
  box.disabled = stop.airports.length < 2;
  box.onchange = () => {
    stop.overland = box.checked;
    // Pins only mean anything on an overland stop, and the server refuses a
    // trip carrying one without it. Clearing them here means unticking the box
    // never produces a trip that cannot be saved.
    if (!box.checked) { stop.arrive_via = null; stop.depart_via = null; }
    renderStops();
    scheduleEstimate();
  };

  const text = document.createElement('span');
  text.textContent = 'Arrive and leave from different airports (you travel overland in between)';

  row.append(box, text);
  row.title = box.disabled
    ? 'Add a second airport to this stop first — there is nowhere else to leave from.'
    : 'Costs no extra searching: every airport pair here is already priced.';
  return row;
}

/* Spend the answer a probe gave you.

   Ticking overland opens every way in against every way out, which is what
   makes the probe able to tell you which crossing is worth having. Once it
   has, the rest are searched for nothing: on the Japan trip, pinning Haneda in
   and Kansai out takes 16 routes to 8 and halves the sweep.

   Only rendered under a ticked box, because that is the only place a pin means
   anything — without overland the chain rule already forces leaving from where
   you landed, and `Scenario.validate` refuses the combination rather than
   quietly ignoring it. */

function crossingPins(stop) {
  const row = document.createElement('div');
  row.className = 'row small stop__pins';

  const lead = document.createElement('span');
  lead.className = 'muted';
  lead.textContent = 'Settled it?';
  row.appendChild(lead);

  for (const [field, label] of [['arrive_via', 'in via'], ['depart_via', 'out via']]) {
    const wrap = document.createElement('label');
    wrap.className = 'check small';

    const caption = document.createElement('span');
    caption.className = 'muted';
    caption.textContent = label;

    const select = document.createElement('select');
    for (const [value, text] of [['', 'any'], ...stop.airports.map((c) => [c, c])]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      select.appendChild(option);
    }
    // An airport removed from the stop leaves a pin pointing at nothing, which
    // the server refuses by name. Shown as the stale value it is rather than
    // silently reset, so the reason a save is refused is on screen.
    if (stop[field] && !stop.airports.includes(stop[field])) {
      const orphan = document.createElement('option');
      orphan.value = stop[field];
      orphan.textContent = `${stop[field]} (no longer a stop airport)`;
      select.appendChild(orphan);
    }
    select.value = stop[field] || '';
    select.onchange = () => {
      stop[field] = select.value || null;
      renderStops();
      scheduleEstimate();
    };

    wrap.append(caption, select);
    row.appendChild(wrap);
  }

  const note = document.createElement('span');
  note.className = 'muted';
  note.textContent = stop.arrive_via || stop.depart_via
    ? 'Only this crossing is searched.'
    : 'Every combination is searched — run a probe to find out which is worth it.';
  row.appendChild(note);
  return row;
}

/* "you get from HND to KIX yourself" - the airports named, because "includes an
   overland leg" does not say whether that is a taxi or half a day on a train. */

function overlandNote(itinerary) {
  const legs = itinerary.legs ?? [];
  const hops = [];
  for (let i = 0; i < legs.length - 1; i += 1) {
    if (legs[i].destination !== legs[i + 1].origin) {
      hops.push(`${legs[i].destination} to ${legs[i + 1].origin}`);
    }
  }
  return hops.length ? `you get from ${hops.join(', and from ')} yourself` : '';
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
  route.stops.push({
    label: '', airports: [], stay_days: [...(previous?.stay_days ?? [7, 10])], overland: false,
    arrive_via: null, depart_via: null,
  });
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
    overland: Boolean(stop.overland),
    arrive_via: stop.arrive_via ?? null,
    depart_via: stop.depart_via ?? null,
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
  $('probe-both-orders').checked = Boolean(scenario.probe_both_orders);

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
      // The stored shape lists a stop's fields by hand, so anything missing
      // here is dropped in silence: the trip comes back off disk chaining
      // Haneda to Haneda, with nothing on screen to say the tick was lost.
      overland: Boolean(stop.overland),
      // Sent only under a ticked box. The server refuses a pin without
      // overland by name, and an unticked stop carrying a stale one would make
      // every save fail with a message about a control that is not on screen.
      arrive_via: stop.overland ? (stop.arrive_via || null) : null,
      depart_via: stop.overland ? (stop.depart_via || null) : null,
    })),
    return_to: oneWay || sameSet(route.returnTo, route.origins) ? null : route.returnTo,
    one_way: oneWay,
    window_start: $('window-start').value,
    window_end: $('window-end').value,
    adults: Number($('adults').value),
    currency: ($('currency').value || 'CZK').toUpperCase(),
    depth: $('depth').value,
    enabled: $('enabled').checked,
    probe_both_orders: $('probe-both-orders').checked,
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

/* ----------------------------------------------------------- cloud runs */

/* What the cloud is doing, has done, and is being held to do.

   The app used to say "Dispatched to GitHub Actions" and stop there. On 22 Aug
   that sentence covered three runs that swept nothing and went green in twelve
   seconds, and two cancelled while pending without starting a job - five
   identical messages for three different outcomes, none of them the one that
   was wanted.

   `known: false` is drawn as a warning rather than an empty list on purpose: no
   `gh` means this app cannot see Actions, which is not remotely the same as
   Actions having stopped. */
async function renderCloudRuns() {
  const badge = $('cloud-badge');
  let body;
  try {
    body = await api('/api/cloud-runs');
  } catch (error) {
    badge.textContent = error.message;
    badge.className = 'badge badge--error';
    return;
  }

  const slots = body.schedule ?? [];
  $('cloud-schedule').innerHTML = slots.length
    ? slots.map((slot) =>
      '<div class="night-list__row">' +
      `<span class="night-list__name">${localStamp(slot.next)}</span>` +
      `<span class="night-list__cost">${slot.focused
        ? 'only the days pinned on the price chart'
        : 'the whole date window'}</span></div>`).join('')
    : '<span class="muted">The schedule could not be read from the workflow file.</span>';

  const warning = $('cloud-unknown');
  warning.hidden = body.known;
  if (!body.known) {
    warning.textContent = `${body.reason} What is scheduled above is read from the ` +
      'workflow file and is still accurate.';
  }

  const held = body.queued ?? [];
  $('cloud-queue').innerHTML = held.length
    ? held.map((entry) =>
      '<div class="night-list__row">' +
      `<span class="night-list__name">${escapeHtml(entry.scenario_id)}` +
      `${entry.depth ? ` · ${escapeHtml(entry.depth)}` : ''}</span>` +
      `<span class="night-list__cost">${entry.error
        ? escapeHtml(entry.error)
        : 'waiting for the cloud to be free'}</span>` +
      `<button class="night-list__drop" data-drop="${escapeHtml(entry.scenario_id)}">Drop</button>` +
      '</div>').join('')
    : '<span class="muted">Nothing waiting. A run asked for now would go straight out.</span>';

  const runs = body.runs ?? [];
  $('cloud-runs').innerHTML = runs.length
    ? runs.map(cloudRunRow).join('')
    : `<span class="muted">${body.known ? 'No runs yet.' : 'Cannot see the runs.'}</span>`;

  badge.className = body.known ? 'badge badge--muted' : 'badge badge--warning';
  badge.textContent = !body.known
    ? 'cannot see Actions'
    : body.busy
      ? `running now${held.length ? ` · ${held.length} waiting` : ''}`
      : held.length ? `${held.length} waiting` : 'idle';
}

/* One run, said in the terms that were missing. "Success" was true of every run
   that failed to sweep anything, so it is never the whole line. */
function cloudRunRow(run) {
  let verdict = run.conclusion || run.status || 'unknown';
  let tone = '';
  if (run.live) {
    verdict = 'running now';
  } else if (run.swept_nothing) {
    verdict = 'swept nothing — finished in seconds';
    tone = ' is-out';
  } else if (run.cancelled_while_waiting) {
    verdict = 'cancelled before it started';
    tone = ' is-out';
  } else if (run.conclusion === 'success') {
    verdict = `swept for ${Math.round((run.seconds ?? 0) / 60)} min`;
  }
  const link = run.url
    ? `<a href="${escapeHtml(run.url)}" target="_blank" rel="noopener">${run.id}</a>`
    : escapeHtml(String(run.id ?? ''));
  return `<div class="night-list__row${tone}">` +
    `<span class="night-list__name">${link} · ${escapeHtml(run.event || '')}` +
    `${run.created_at ? ` · ${localStamp(run.created_at)}` : ''}</span>` +
    `<span class="night-list__cost">${escapeHtml(verdict)}</span></div>`;
}

/* ------------------------------------------------- results on this machine */

/* Cloud runs that are on the branch and not on this disk.

   The Results picker lists directories; the sweep commits to git. Nothing joined
   the two, so on 22 Aug a finished deep run - 64 searches, 638 flights, coverage
   1.0 - sat on the branch while the picker showed the previous day and gave no
   sign that anything was elsewhere. An empty picker and a picker missing three
   runs looked identical, which is the same failure the Cloud tab above exists to
   end: a thing that worked, and no way on screen to tell.

   Shown as a count of *runs*, never of commits. The probe commits every two
   hours, so "6 commits behind" was three sweeps and three observations, and only
   one of those numbers is the one being asked about. */
async function renderCloudSync() {
  let body;
  try {
    body = await api('/api/cloud-sync');
  } catch (error) {
    body = { known: false, reason: error.message, missing_count: 0 };
  }
  const missing = body.missing_count ?? 0;
  // `known: false` is drawn, not hidden. "Cannot tell" and "nothing missing"
  // are different answers and only one of them means the picker is complete.
  const show = missing > 0 || !body.known;
  const message = !body.known
    ? body.reason
    : `${missing} cloud ${missing === 1 ? 'run is' : 'runs are'} on the branch ` +
      'but not on this machine' + describeMissing(body.missing) + '.' +
      (body.can_fast_forward ? '' : ` ${body.blocked_by}`);
  const actionable = Boolean(body.known && missing > 0 && body.can_fast_forward);

  for (const [box, text, button] of [
    ['cloud-sync', 'cloud-sync-text', 'cloud-sync-get'],
    ['cloud-sync-cloud', 'cloud-sync-cloud-text', 'cloud-sync-cloud-get'],
  ]) {
    if (!$(box)) continue;
    $(box).hidden = !show;
    $(text).textContent = message;
    $(button).hidden = !actionable;
  }

  const ok = $('cloud-sync-ok');
  if (ok) {
    ok.hidden = show;
    ok.textContent = 'Every run the branch has is on this machine.';
  }
}

/* Which trips the missing runs belong to, when it is not just one.

   "3 runs missing" on a page showing Tokyo, when two of them are the other trip,
   is the kind of near-miss that gets acted on wrongly. */
function describeMissing(missing) {
  const trips = Object.keys(missing ?? {});
  if (!trips.length) return '';
  // Not escaped, because this is assigned to textContent rather than innerHTML.
  // Escaping it there would show the entities themselves.
  return ` (${trips.map((id) => `${id}: ${missing[id].length}`).join(', ')})`;
}

async function pullCloudResults(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Getting…';
  try {
    const body = await api('/api/cloud-sync', { method: 'POST' });
    // The pull can bring a newer src/web/app.py with it, which this running
    // server has already imported. `contractMatches` is the existing handshake
    // for exactly that, and its message - restart the server - is the right one.
    if (!(await contractMatches())) return;
    await renderCloudSync();

    const gained = Object.values(body.gained ?? {}).reduce((n, v) => n + v.length, 0);
    // Land on the newest run, but only when one actually arrived: moving the
    // picker under someone who is reading a specific sweep is its own bug.
    if (gained) state.stamp = null;
    await pollStatus();
    if (gained) renderResults();
    $('status-text').textContent = gained
      ? `Brought ${gained} cloud run(s) across — newest selected`
      : 'Already up to date with the branch';
  } catch (error) {
    // Verbatim: when git refuses it names the files, and that sentence is the
    // whole point of surfacing this rather than summarising it.
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

for (const id of ['cloud-sync-get', 'cloud-sync-cloud-get']) {
  if ($(id)) $(id).onclick = (event) => pullCloudResults(event.target);
}

$('cloud-queue').onclick = async (event) => {
  const button = event.target.closest('button[data-drop]');
  if (!button) return;
  try {
    await api(`/api/cloud-queue/${button.dataset.drop}`, { method: 'DELETE' });
  } catch (error) {
    showError(error.message);
  }
  await renderCloudRuns();
};

/* ---------------------------------------------------------- night sweep */

/* What the scheduled cloud sweep will do tonight, and to which trip.

   None of this was on the page, and all of it mattered. The nightly run is the
   only place a full-sized trip has ever been swept whole - 483/483 in nineteen
   minutes on 21 Aug, against three throttled local runs the same morning - and
   nothing said which trips it would run, how big they were, when it fires, or
   the one that actually bit: it sweeps the trips committed to the branch, not
   the trip on this screen. A whole day of results was read as answers about a
   trip that had been narrowed hours earlier.

   Read from the saved files, not from the form. The night sweep will run what
   is on disk, so a panel reflecting unsaved edits would describe a run that is
   not going to happen; `isDirty()` is what says the two have parted. */
async function renderNightSweep() {
  const badge = $('night-badge');
  let body;
  try {
    body = await api('/api/night-sweep');
  } catch (error) {
    badge.textContent = error.message;
    badge.className = 'badge badge--error';
    return;
  }

  const trips = body.trips ?? [];
  const scheduled = trips.filter((trip) => trip.included);
  $('night-cap').textContent = count(body.searches_per_runner);
  badge.className = scheduled.length ? 'badge badge--muted' : 'badge badge--warning';
  badge.textContent = scheduled.length
    ? `${scheduled.length} trip${scheduled.length === 1 ? '' : 's'} · ` +
      `${count(scheduled.reduce((n, trip) => n + trip.searches, 0))} searches · ` +
      `${scheduled.reduce((n, trip) => n + trip.runners, 0)} runners`
    : 'nothing scheduled';

  renderNightTrip(body, trips.find((trip) => trip.id === state.scenario?.id));

  $('night-all').innerHTML = trips.length
    ? trips.map((trip) =>
      `<div class="night-list__row${trip.included ? '' : ' is-out'}">` +
      `<span class="night-list__name">${escapeHtml(trip.name)}</span>` +
      `<span class="night-list__cost">${count(trip.searches)} searches · ` +
      `${trip.runners} runner${trip.runners === 1 ? '' : 's'} · ~${trip.minutes} min</span>` +
      '</div>').join('')
    : '<span class="muted">No trips saved.</span>';

  const slots = body.schedule ?? [];
  $('night-when').textContent = slots.length
    ? 'Next ' + slots.map((slot) =>
      `${localStamp(slot.next)}${slot.focused
        ? ' (only the days pinned on the price chart)'
        : ' (the whole date window)'}`).join(', then ')
    : 'The schedule could not be read from the workflow file.';
}

/* The two lines about the trip on screen: what tonight costs it, and whether
   tonight is even about this version of it. */
function renderNightTrip(body, trip) {
  const line = $('night-this-trip');
  const warning = $('night-cloud');
  warning.hidden = true;
  if (!trip) {
    line.textContent = state.isNew
      ? 'Save the trip and it can be swept overnight.'
      : '';
    return;
  }

  const cost = `${count(trip.searches)} searches across ` +
    `${trip.runners} runner${trip.runners === 1 ? '' : 's'}, about ${trip.minutes} min`;
  // The schedule forces a depth, so a trip saved `quick` is still swept every
  // day of the window. Sizing this line from the file reported a plan seven
  // times smaller than the one that would really run.
  const depth = body.forced_depth && body.forced_depth !== trip.saved_depth
    ? ` It prices every night at <em>${escapeHtml(trip.depth)}</em>, whatever the trip is ` +
      `saved as — this one is saved <em>${escapeHtml(trip.saved_depth)}</em>.`
    : '';
  const unsaved = isDirty()
    ? ' <strong>Your unsaved edits are not in this;</strong> save them first.'
    : '';
  line.innerHTML = trip.included
    ? `Tonight this trip is ${escapeHtml(cost)}.${depth}${unsaved}`
    : `<strong>Not swept overnight.</strong> Ticked, it would be ` +
      `${escapeHtml(cost)}.${depth}${unsaved}`;

  const cloud = trip.cloud ?? {};
  const ref = escapeHtml(body.cloud_ref ?? 'the branch');
  if (!cloud.known) {
    warning.hidden = false;
    warning.innerHTML =
      `The night sweep runs the trips committed to <code>${ref}</code>, and this one is ` +
      `not on it. Tonight will not include it whatever the box above says — commit and ` +
      `push <code>scenarios/${escapeHtml(trip.id)}.json</code>.`;
    return;
  }
  if (!cloud.differs.length) return;

  const seen = localStamp(body.cloud_seen_at);
  warning.hidden = false;
  warning.innerHTML =
    `<strong>The night sweep is running a different version of this trip.</strong> ` +
    `It differs in ${escapeHtml(cloud.differs.join(', '))}. Tonight it will make ` +
    `${count(cloud.searches)} searches` +
    `${cloud.included ? '' : ', except it is switched off there'}, not ` +
    `${count(trip.searches)} — so results committed overnight are about that trip, not ` +
    `this one. Commit and push to change it.` +
    (seen ? ` <span class="muted">(<code>${ref}</code> as last fetched, ${escapeHtml(seen)})</span>` : '');
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
  // Where you pressed, not merely what is on screen.
  //
  // This used to take the first visible section, which was the only visible
  // one. A step now shows several at once, so the first is the top of the page
  // and a complaint about the button you just pressed could appear a screen
  // above it - a quieter version of the bug this replaced, where the message
  // went to a hidden panel and the button looked like it had done nothing.
  const acted = document.activeElement?.closest('section[data-panel]:not([hidden])');
  const panel = acted ?? document.querySelector('section[data-panel]:not([hidden])');
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
    // Saving is the moment the night sweep's answer can change: the box above
    // is part of this form, and so is everything the branch is compared on.
    await renderNightSweep();
    // Editing the trip is exactly what turns an existing run into a run of a
    // different trip, so the flags in the sweep picker are stale the instant a
    // save lands. Re-read them rather than waiting for the next poll.
    await pollStatus();
    // Both save buttons, because the same call backs them and only one of
    // them is on screen: the trip's is on Map it out, the notification panel's
    // is behind the gear, and flashing the hidden one confirms nothing.
    for (const [id, label] of [
      ['save-btn', 'Save trip'], ['notify-save-btn', 'Save'], ['night-save-btn', 'Save'],
    ]) {
      const button = $(id);
      if (!button) continue;
      button.textContent = 'Saved';
      setTimeout(() => { button.textContent = label; }, 1500);
    }
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
    // Not awaited: it shells out to git and the page must not wait on it, and
    // nothing already drawn depends on the answer.
    renderCloudSync();
  } catch (error) {
    showError(error.message);
  }
};

$('new-trip-btn').onclick = () => {
  clearError();
  state.isNew = true;
  state.stamp = null;
  state.sweeps = [];
  clearFilters();
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
    stops: [{ label: '', airports: [], stay_days: [7, 10], overland: false, arrive_via: null, depart_via: null }],
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
    probe_both_orders: false,
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

/* Carrying on a run that ended short.

   Offered rather than done automatically, and never hidden behind a warning
   about the throttle: how long a refusal lasts has never been measured here, so
   a cooldown invented to look careful would refuse runs that would have worked.
   What the site did and when is stated; the decision stays with the reader. */
function renderResume(latest, running) {
  const button = $('resume-btn');
  const note = $('resume-note');
  const offer = Boolean(latest && latest.resumable && !running);
  button.hidden = !offer;
  note.hidden = !offer;
  if (!offer) return;

  const left = latest.left_to_ask ?? 0;
  const planned = latest.planned ?? latest.total ?? 0;
  const answered = Math.max(0, planned - left);
  button.textContent = `Carry on that run — ${count(left)} searches left`;
  button.dataset.stamp = latest.stamp;
  note.innerHTML =
    `The ${latest.mode === 'explore' ? 'probe' : 'sweep'} of ` +
    `${escapeHtml(localStamp(latest.stamp))} answered ${count(answered)} of ${count(planned)}. ` +
    'Carrying on asks only for the rest and keeps every flight it already found.' +
    (latest.state === 'throttled'
      ? ' <strong>The site was refusing this client when it ended</strong>, so this may be refused too — ' +
        'nothing is lost if it is, and the answers already in hand are kept either way.'
      : '');
}

$('resume-btn').onclick = async () => {
  clearError();
  const button = $('resume-btn');
  button.disabled = true;
  try {
    await api(
      `/api/scenarios/${state.scenario.id}/resume?stamp=${encodeURIComponent(button.dataset.stamp)}`,
      { method: 'POST' },
    );
    // The finished run should be the one this page shows, exactly as for a run
    // started from scratch.
    state.watching = true;
  } catch (error) {
    showError(error.message);
  }
  button.disabled = false;
  pollStatus();
};

$('run-local-btn').onclick = () => startRun('sweep');
$('explore-btn').onclick = () => startRun('explore');

/* Both stop buttons, driven together so neither can disagree with the other
   about whether a run is going. */
const STOP_BUTTONS = ['stop-btn', 'run-stop-btn'];

const stopButtons = (fn) => { for (const id of STOP_BUTTONS) fn($(id)); };

async function askToStop() {
  stopButtons((button) => { button.disabled = true; });
  try {
    await api(`/api/scenarios/${state.scenario.id}/stop`, { method: 'POST' });
  } catch (error) {
    // Most often 409: it finished between the render and the click.
    $('status-text').textContent = error.message;
  }
  pollStatus();
}

stopButtons((button) => { button.onclick = askToStop; });

/* "Dispatched to GitHub Actions" used to be the last thing this app ever said
   about a cloud run, and it said it whether the run swept a trip, swept nothing,
   or was cancelled ten minutes later without starting a job. Three outcomes, one
   sentence. Now a run is either refused with a reason, held with a reason, or
   sent - and the Cloud tab carries it from there. */
$('run-cloud-btn').onclick = async () => {
  clearError();
  // The cloud reads the trip out of the repo, so an unsaved edit is even
  // further from what would be searched than it is locally.
  if (isDirty() && !(await saveTrip())) return;
  await dispatchCloudRun(false);
};

async function dispatchCloudRun(force) {
  const query = `depth=${$('depth').value}${force ? '&force=true' : ''}`;
  try {
    const body = await api(`/api/scenarios/${state.scenario.id}/run-cloud?${query}`,
      { method: 'POST' });
    $('status-text').textContent = body.queued
      ? 'Held until the cloud is free — see the Cloud tab'
      : 'Dispatched to GitHub Actions';
  } catch (error) {
    // The refusal is about the branch, and it is worth overriding on purpose:
    // sweeping the committed version is a real thing to want, as long as the
    // results are read as being about that version.
    if (!force && /run it anyway/i.test(error.message)) {
      if (confirm(`${error.message}

Run it anyway?`)) return dispatchCloudRun(true);
      $('status-text').textContent = 'Cloud run cancelled';
      return;
    }
    showError(error.message);
  }
}

$('depth').onchange = scheduleEstimate;
for (const id of ['window-start', 'window-end', 'adults', 'currency', 'trip-name', 'enabled',
  'probe-both-orders']) {
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
    stopButtons((button) => {
      button.hidden = !body.running;
      button.disabled = Boolean(body.stopping);
    });
    renderResume(latest, body.running);
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
    } else if (body.running && waitingUntil(latest) !== null) {
      /* The site is refusing and the runner is waiting it out - up to fifteen
         minutes in which nothing happens and nothing is wrong. Said plainly,
         because the alternative is what this replaces: a green running dot and
         a countdown computed from a constant that knows nothing about the wait,
         which is indistinguishable from a hung run. */
      const minutes = Math.max(1, Math.round((waitingUntil(latest) - Date.now()) / 60000));
      strip.className = 'status-strip is-waiting';
      $('status-text').textContent =
        `The site is refusing this client — waiting about ${minutes} min before trying again` +
        ` (${localStamp(latest.backoff_until, { day: undefined, month: undefined })})` +
        ` · ${latest.completed}/${latest.total} done, ${count(latest.legs_found ?? 0)} flights kept`;
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
          `Last ${what} ${localStamp(latest.stamp)} · ${count(latest.legs_found)} flights`;
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

/* When a paused run intends to try again, in ms, or null if it is not paused.

   A wait that has already elapsed reads as not waiting: the status is written
   when the pause starts and cleared by the next search that answers, so between
   those two the deadline is the only thing that says the pause is still on. */
function waitingUntil(latest) {
  if (!latest || !latest.backoff_until) return null;
  const when = asDate(latest.backoff_until);
  return when !== null && when.getTime() > Date.now() ? when.getTime() : null;
}

/* One line describing a run, shared by the two pickers that list them.

   Ordered so it can be read left to right and abandoned early: the Prague date
   is what you actually scan for, so it leads and is kept short; then what kind
   of run it was and how big. Everything wrong with it collects at the end behind
   a single ⚠ instead of being spliced into the middle of the line, where in a
   picker of otherwise ordinary rows it was easy to slide past — which is the
   whole point of flagging a probe or a run of a different trip at all. */
const sweepLabel = (sweep, kind, warnings = []) => {
  // `|| sweep.stamp` rather than nothing: an unparseable stamp is still the only
  // handle you have on that row, and a picker of dateless rows is unusable.
  const parts = [
    // Defaulted, because one caller passes no kind at all. `join` renders
    // `undefined` as nothing, so the label came out with an empty field
    // between two separators.
    localStamp(sweep.stamp) || sweep.stamp,
    kind || 'sweep',
    `${count(sweep.total)} searches`,
    `${count(sweep.legs_found)} flights`,
  ];
  // Folded in here rather than passed by each caller. It used to be passed,
  // and only one of the three pickers passed it - so the Results picker, where
  // a run is chosen to be believed, was the one place a run of another trip
  // looked ordinary. A flag every label carries cannot be the one a picker
  // forgets.
  const flagged = [
    ...(sweep.differs || []).map((what) => `different ${what}`),
    ...warnings,
  ].filter(Boolean);
  return parts.join(' · ') + (flagged.length ? ` · ⚠ ${flagged.join(' · ')}` : '');
};

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
    option.textContent = sweepLabel(sweep, kind, [
      sweep.state === 'stopped' ? `stopped at ${sweep.completed ?? '?'}` : '',
      dark ? `${dark} dead route(s)` : '',
    ]);
    select.appendChild(option);
  }
  if (!state.stamp && state.sweeps.length) state.stamp = state.sweeps[0].stamp;
  if (state.stamp) select.value = state.stamp;
}

$('sweep-select').onchange = (event) => {
  state.stamp = event.target.value;
  renderResults();
  renderByDate();
  // Follow into the charts only when they can draw it. A probe picked here is
  // a deliberate choice - the panel says so in words - and dragging the charts
  // onto it would either blank them or, worse, have `renderNarrow` pull this
  // selection straight back and undo the click.
  const chosen = state.sweeps.find((s) => s.stamp === state.stamp);
  if (chosen && chosen.has_legs && chosen.mode !== 'explore') renderNarrow();
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
    option.textContent = sweepLabel(sweep, kind, [
      sweep.state === 'throttled' ? 'site refused' : '',
      sweep.state === 'stopped' ? 'stopped early' : '',
    ]);
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
    $('explore-coverage').hidden = true;
    return;
  }
  try {
    renderExploreReport(
      await api(`/api/sweeps/${state.scenario.id}/${state.exploreStamp}/explore`),
    );
  } catch (error) {
    $('explore-report').innerHTML = '';
    $('explore-mismatch').hidden = true;
    $('explore-coverage').hidden = true;
    $('explore-empty').hidden = false;
    $('explore-empty').textContent = `Could not read that run — ${error.message}`;
  }
}

/* Which way round to fly, when a probe was asked to sample both.

   The figure is the cheapest leg seen on each hop, added up. That is a lower
   bound and not a trip — three dates a leg almost never chain, which is why the
   Results tab refuses to draw probe legs as itineraries at all — so it says so
   underneath rather than letting a total that looks bookable sit unqualified.
   It is still the right comparison: both orders were sampled by the same run on
   the same dates, so whatever it leaves out, it leaves out of both.

   Nothing here reorders anything. The button edits the stops on the Search tab
   and leaves it unsaved, because a probe is a reason to look, not a decision. */

function renderOrderVerdict(body) {
  const host = $('explore-orders');
  const orders = body.orders ?? [];
  host.hidden = orders.length < 2;
  if (host.hidden) return;

  const currency = body.currency ?? 'CZK';
  const rows = orders.map((order) => {
    const figure = order.total == null
      ? '<span class="muted">not fully priced</span>'
      : `<strong>${escapeHtml(money(order.total, currency))}</strong>`;
    const verdict = order.total == null
      ? `<span class="muted">${Number(order.unpriced)} hop(s) the site never answered about</span>`
      : order.is_best
        ? '<span class="badge badge--good">cheapest sampled</span>'
        : `<span class="trend trend--up">${Number(order.vs_best_pct).toFixed(0)}% dearer</span>`;
    return `<tr><td>${escapeHtml(order.label)}</td><td class="num">${figure}</td>` +
      `<td>${verdict}</td></tr>`;
  }).join('');

  host.innerHTML =
    '<h3 class="muted small" style="margin-top:0">Which way round</h3>' +
    `<table class="data"><tbody>${rows}</tbody></table>` +
    '<p class="panel__hint small">Cheapest leg seen on each hop, added up — a floor to ' +
    'compare the two orders by, not a trip you could book. Both were sampled on the same ' +
    'dates by the same run.</p>';

  // Only when the reverse actually won, and only as an edit you then save.
  const winner = orders.find((order) => order.is_best);
  if (winner && winner !== orders[0]) {
    const swap = document.createElement('button');
    swap.type = 'button';
    swap.className = 'small primary';
    swap.textContent = `Reorder the trip — ${winner.label}`;
    swap.onclick = () => {
      route.stops.reverse();
      renderStops();
      scheduleEstimate();
      showTab('search');
    };
    host.appendChild(swap);
  }
}

/* How much of the probe's plan was answered, said above its verdicts.

   The table below ranks airports against each other and calls some of them not
   worth pricing. Those are conclusions, and a probe the site refused after 31 of
   123 searches presents them in exactly the words a complete one uses. The
   difference is whether the cheap airport was ever asked about.

   Silent at 100%, like the Results banner: one that is always there is one
   nobody reads. Silent at `null` too - probes from before the figure was
   recorded do not know, and "do not know" must not be drawn as "all answered". */
function renderExploreCoverage(body) {
  const host = $('explore-coverage');
  if (body.coverage == null || body.coverage >= 1) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const pct = Math.round(body.coverage * 100);
  const counted = body.answered != null && body.planned
    ? ` (${count(body.answered)} of ${count(body.planned)} searches)`
    : '';
  host.innerHTML =
    `<strong>${pct}% of this probe's plan was answered${counted}.</strong> ` +
    'The rankings below are drawn from what came back, so an airport can look poor here ' +
    'simply because the site never answered about it. ' +
    (body.state === 'throttled'
      ? 'The site stopped answering partway through — resuming this run fills the gaps without re-asking what it already got.'
      : 'Another run fills the gaps.');
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

  renderExploreCoverage(body);
  renderMismatch(body, probe);
  renderOrderVerdict(body);

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

/* ------------------------------------------------------- results: filters */

/* A filter belongs to the trip it was set on. Carrying "from FRA" onto a trip
   that never flies from Frankfurt shows an empty table with a control the reader
   did not set, which reads as a trip with no results. */
const clearFilters = () => {
  state.filters = { from: '', to: '', bags: false, allWindow: false };
};

const filterQuery = () => {
  const params = new URLSearchParams();
  if (state.filters.from) params.set('from_airport', state.filters.from);
  if (state.filters.to) params.set('to_airport', state.filters.to);
  if (state.filters.bags) params.set('bags', 'true');
  // Only ever sent when asked for. `narrow` is the server's default, so a page
  // that never sends this behaves exactly as it did before the toggle existed.
  if (state.filters.allWindow) params.set('window', 'all');
  const query = params.toString();
  return query ? `?${query}` : '';
};

/* Fill the two pickers and say what the narrowing is doing.

   The options come from the trip's own airports rather than from the rows on
   screen. Pruning can leave an airport out of an unfiltered traversal while it
   still has trips of its own, so building the list from the results would hide
   exactly the choice worth making. An option with nothing behind it says so
   when you pick it. */
function renderFilters(body) {
  for (const [id, codes, chosen, blank] of [
    ['filter-from', body.start_airports ?? [], state.filters.from, 'any airport'],
    ['filter-to', body.end_airports ?? [], state.filters.to, 'any airport'],
  ]) {
    const select = $(id);
    select.innerHTML = '';
    for (const [value, text] of [['', blank], ...codes.map((c) => [c, c])]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      select.appendChild(option);
    }
    // A code saved from a previous sweep may not exist in this one; keep it
    // selectable rather than silently snapping the filter back to "any".
    if (chosen && !codes.includes(chosen)) {
      const orphan = document.createElement('option');
      orphan.value = chosen;
      orphan.textContent = `${chosen} (not in this sweep)`;
      select.appendChild(orphan);
    }
    select.value = chosen;
  }

  $('filter-bags').checked = state.filters.bags;
  $('filter-window').checked = state.filters.allWindow;
  // Hidden when there is no narrowing to ignore: a tick that cannot change
  // anything is a control the reader has to rule out before trusting the table.
  const narrowing = body.window ?? {};
  $('filter-window-wrap').hidden = !(
    narrowing.applied || state.filters.allWindow
  ) && !(narrowing.focus || narrowing.return_focus || narrowing.total_days);
  $('filter-reset').hidden = !body.narrowed && !state.filters.allWindow;

  // Never "34 of 812". Pruning makes the unfiltered traversal a different set
  // rather than a larger one, so the two counts do not form a fraction.
  const trips = `${count(body.matched)} trip${body.matched === 1 ? '' : 's'}`;
  $('filter-count').textContent = body.narrowed ? `${trips} match` : trips;

  const note = $('filter-note');
  const notes = [];
  if (state.filters.allWindow) {
    const said = [];
    if (narrowing.focus) said.push(`leaving ${narrowing.focus[0]} to ${narrowing.focus[1]}`);
    if (narrowing.return_focus) {
      said.push(`home ${narrowing.return_focus[0]} to ${narrowing.return_focus[1]}`);
    }
    if (narrowing.total_days) {
      said.push(`${narrowing.total_days[0]}–${narrowing.total_days[1]} nights away`);
    }
    notes.push(
      'Showing every trip this sweep can build, including ones you said you did not want' +
      (said.length ? ` (${escapeHtml(said.join(', '))})` : '') +
      '. Nothing is being searched differently — this is the same legs read another way.',
    );
  }
  if (state.filters.bags) {
    // Said every time the tick is on, because the distinction decides whether
    // an absent trip is bad news or merely unmeasured: nothing in a real sweep
    // is ever marked "no bag", only "confirmed" or "never said".
    notes.push(
      'Only trips where <strong>every</strong> leg confirms a checked bag. About half ' +
      'this site’s fares are low-cost ones that reveal baggage only at checkout, and ' +
      'those are hidden here as unconfirmed rather than as excluding a bag.',
    );
  }
  if (body.narrowed && body.matched && body.cheapest_unfiltered != null) {
    const best = body.best_same_airport ?? body.best_open_jaw;
    const premium = best ? (best.total_with_bags ?? best.total_price) - body.cheapest_unfiltered : 0;
    if (premium > 0) {
      notes.push(
        `Narrowing this way costs <strong>${escapeHtml(money(premium, body.currency))}</strong> ` +
        'against the cheapest trip in the sweep.',
      );
    }
  }
  note.hidden = notes.length === 0;
  note.innerHTML = notes.join(' ');
}

for (const [id, apply] of [
  ['filter-from', (el) => { state.filters.from = el.value; }],
  ['filter-to', (el) => { state.filters.to = el.value; }],
  ['filter-bags', (el) => { state.filters.bags = el.checked; }],
  ['filter-window', (el) => { state.filters.allWindow = el.checked; }],
]) {
  $(id).onchange = (event) => {
    apply(event.target);
    renderResults();
    // The chart above the table reads the same population or the two disagree
    // about what a departure date costs.
    if (id === 'filter-window') renderByDate();
  };
}

$('filter-reset').onclick = () => {
  clearFilters();
  renderResults();
};

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
    $('results-filters').hidden = true;
    $('filter-note').hidden = true;
    $('results-empty').hidden = false;
    $('results-empty').textContent =
      'That run was a probe — it prices a few dates per leg to compare airports, not to '
      + 'build trips. Its verdicts are in the Explore tab.';
    return;
  }
  $('results-scroll').hidden = false;
  $('results-filters').hidden = false;

  // Every other call site catches; these two did not, so a 500 left a blank
  // table and an explanation only in the browser console.
  let body;
  try {
    body = await api(
      `/api/sweeps/${state.scenario.id}/${state.stamp}/results${filterQuery()}`,
    );
  } catch (error) {
    $('results-empty').hidden = false;
    $('results-empty').textContent = `Could not load results — ${error.message}`;
    return;
  }
  renderFilters(body);
  $('results-empty').hidden = body.itineraries.length > 0;
  // Three different nothings, and telling them apart is the whole difference
  // between "narrow your filter" and "the scraper is broken".
  $('results-empty').textContent = body.narrowed
    ? 'No trip in this sweep matches the filter above.'
    : body.legs_found
      ? 'This sweep found flights, but none of them chain into a complete trip.'
      : 'Run a sweep to see itineraries.';

  renderVerification(body.verification);
  $('completeness').hidden = true;

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
    // A total that includes a journey nobody booked has to say so beside the
    // number. The ⇢ in the route carries the same fact and is easy to skim past.
    const ground = itinerary && itinerary.has_overland
      ? `<div class="stat__sub trend trend--up">${escapeHtml(overlandNote(itinerary))}</div>`
      : '';
    card.innerHTML =
      `<div class="stat__label">${label}</div>` +
      (itinerary
        ? `<div class="stat__value">${money(withBags(itinerary), itinerary.currency)}</div>
           <div class="stat__sub">${itinerary.route}</div>${bagNote}${ground}${measured}${saving}`
        : '<div class="stat__value muted">—</div><div class="stat__sub">none found</div>');
    $('headline').appendChild(card);
  }

  renderCompleteness(body);

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
      // "open jaw" is about where the whole trip starts and ends; "overland"
      // is a gap in the middle that you close yourself. A trip can be either,
      // both or neither, so they are two badges rather than three states.
      `<td>${itinerary.same_airport ? '<span class="badge badge--good">same airport</span>' : '<span class="badge badge--muted">open jaw</span>'}` +
        `${itinerary.has_overland ? ' <span class="badge badge--warning">overland</span>' : ''}</td>` +
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
      // Here rather than only on the Watch tab, because this is where you
      // decide: you are reading the legs of a trip that looked good and the
      // question "is *that* flight moving" arrives with the row in front of
      // you, not later from a form asking you to retype it.
      const follow = document.createElement('button');
      follow.type = 'button';
      follow.className = 'small';
      follow.textContent = 'Follow';
      follow.title = 'Re-price this exact flight every four hours (one search)';
      follow.onclick = () => followLeg({
        origin: leg.origin,
        destination: leg.destination,
        depart_date: leg.depart_date,
        price: leg.price_amount,
      });
      line.append(' ', follow);
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
/* How much of the plan this price was found by looking at.

   Said above the itineraries rather than tucked into a tooltip, because these
   are the numbers you would book on. A sweep with holes in its date grid reports
   its cheapest total in exactly the same words as a complete one, and the
   difference is whether a cheaper trip was ever looked at. Silent at 100%: a
   banner that is always there is a banner nobody reads. */
function renderCompleteness(body) {
  const host = $('completeness');
  if (body.coverage == null || body.coverage >= 1) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  host.className = 'notice notice--warning';
  const pct = Math.round(body.coverage * 100);
  host.innerHTML =
    `<strong>${pct}% of the planned searches were answered.</strong> ` +
    'The dates that went unasked could have been the cheap ones, so treat the totals below ' +
    'as the best of what was priced rather than the best there is. Another run fills the gaps.' +
    (body.focus
      ? ` This sweep was focused on ${escapeHtml(body.focus[0])} to ${escapeHtml(body.focus[1])}, ` +
        'so it never priced the rest of the window at all.'
      : '');
}

const sweepQuality = (row) => {
  const parts = [`${row.depth ?? '?'} · ${row.searches ?? '?'} searches`];
  if (row.legs_per_search != null) parts.push(`${row.legs_per_search} legs/search`);
  if (row.routes_planned) parts.push(`${row.routes_covered}/${row.routes_planned} routes`);
  // Only when short of complete. On a full sweep it is noise; on a partial one
  // it is the reason the best total may not be the best there was - the dates
  // that went unasked could have been the cheap ones.
  if (row.coverage != null && row.coverage < 1) {
    parts.push(`only ${Math.round(row.coverage * 100)}% of searches answered`);
  }
  if (row.focus) parts.push(`focused ${row.focus[0]} to ${row.focus[1]}`);
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

/* The two charts are drawn separately because they now live in different
   steps, and a chart drawn into a hidden section measures its container at zero
   width. Splitting them also stopped each one paying for the other's fetch:
   opening the narrowing no longer asks for the probe. */

async function renderPrices() {
  // Kept for the theme toggle, which has to redraw whatever is on screen —
  // charts read their colours from the design tokens at draw time.
  await Promise.all([renderByDate(), renderHistory()]);
}

async function renderByDate() {
  if (!state.stamp) return;
  const byDateResult = (await Promise.allSettled([
    api(
      `/api/sweeps/${state.scenario.id}/${state.stamp}/by-date` +
      (state.filters.allWindow ? '?window=all' : ''),
    ),
  ]))[0];

  // The chart used to hardcode " CZK" regardless of what the legs were priced
  // in. The scenario says what the currency is.
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const byDate = byDateResult.status === 'fulfilled' ? byDateResult.value : [];

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
}

async function renderHistory() {
  if (!state.stamp) return;
  // Independent endpoints, fetched together. `allSettled` so one failing panel
  // does not blank the other.
  const [historyResult, probeResult] = await Promise.allSettled([
    api(`/api/history/${state.scenario.id}`),
    api('/api/probe'),
  ]);
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const history = historyResult.status === 'fulfilled' ? historyResult.value : [];
  const width = Math.max(420, $('chart-history').clientWidth - 4);

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


/* ------------------------------------------------------------------ watch

   A sweep prices a window once a day; a watch follows a few candidate trips on
   their exact days, every four hours. Pinning the leg dates is what makes that
   affordable - 21 searches a candidate rather than 75 - and pelikan.cz answers
   about 120 from one runner before it stops answering at all, which is why the
   cost of a pick is shown next to the pick rather than buried.

   The tab reads what Prices produced, so it sits after it. */

async function renderWatch() {
  const [watchResult, candidatesResult] = await Promise.allSettled([
    api(`/api/watch/${state.scenario.id}`),
    state.stamp
      ? api(`/api/sweeps/${state.scenario.id}/${state.stamp}/candidates`)
      : Promise.resolve({ candidates: [], coverage: null }),
  ]);

  if (watchResult.status !== 'fulfilled') {
    showWatchError(`Could not read what is being watched — ${watchResult.reason.message}`);
    return;
  }
  renderWatched(watchResult.value);
  renderLegWatches(watchResult.value);
  renderWatchCandidates(
    candidatesResult.status === 'fulfilled' ? candidatesResult.value : { candidates: [] },
    watchResult.value,
  );
}

function showWatchError(message) {
  const host = $('watch-error');
  host.hidden = !message;
  host.textContent = message || '';
}

function renderWatched(body) {
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const candidates = body.candidates ?? [];

  $('watch-empty').hidden = candidates.length > 0;
  // Either kind of watch gives the run something to do, and they go in one
  // check: a followed leg is priced by the same run that prices a pinned trip.
  const anything = candidates.length > 0 || (body.legs ?? []).length > 0;
  $('watch-run-btn').disabled = !anything || body.running;

  const cost = $('watch-cost');
  cost.className = `badge badge--${candidates.length ? 'good' : 'muted'}`;
  cost.textContent = candidates.length
    ? `${body.searches} searches (~${body.minutes} min) a check`
    : 'nothing watched';

  $('watch-run-note').textContent = body.running
    ? 'Checking now…'
    : anything
      ? 'Otherwise checked automatically every four hours in the cloud.'
      : '';
  showWatchError(body.error ? `The last check failed — ${body.error}` : '');

  // The chart. Series are in the same order as the table, so a colour in one
  // is the colour in the other.
  const host = $('watch-chart');
  host.innerHTML = '';
  host.appendChild(multiLineChart(
    candidates
      .filter((c) => (c.series ?? []).length)
      .map((c) => ({
        name: c.depart_date,
        points: c.series.map((point) => ({
          t: point.ts, value: point.total, muted: !point.comparable,
        })),
      })),
    {
      width: Math.max(420, host.clientWidth - 4),
      valueSuffix: suffix,
      ariaLabel: 'Watched days over time',
      emptyText: candidates.length
        ? 'No checks yet — the first one runs within four hours, or press Check now.'
        : 'Nothing is being watched yet.',
    },
  ));

  const body_ = $('watch-table').querySelector('tbody');
  body_.innerHTML = '';
  for (const candidate of candidates) {
    // Two baselines, and they answer different questions: "when picked" is what
    // you decided on, "change" is against the first *measurement*. Showing only
    // the latter makes a day you picked at 30,000 and which has sat at 28,500
    // ever since look perfectly flat.
    const picked = candidate.added_price;
    const now = candidate.latest;
    const move = picked != null && now != null ? now - picked : candidate.net_change;
    const trend = move > 0 ? 'trend--up' : move < 0 ? 'trend--down' : '';
    const row = document.createElement('tr');
    row.innerHTML =
      `<td>${escapeHtml(candidate.depart_date)}</td>` +
      `<td>${escapeHtml(candidate.route ?? candidate.depart_dates.join(' · '))}` +
      `${candidate.has_overland ? ' <span class="badge badge--warning">overland</span>' : ''}</td>` +
      `<td class="num">${picked == null ? '—' : money(picked, candidate.currency)}</td>` +
      `<td class="num">${now == null
        ? '<span class="muted">not checked yet</span>'
        : money(now, candidate.currency)}</td>` +
      `<td class="num ${trend}">${now == null || picked == null
        ? '—'
        : `${move > 0 ? '+' : ''}${money(move, '')}`}</td>`;

    const cell = document.createElement('td');
    const drop = document.createElement('button');
    drop.className = 'small watch-drop';
    drop.textContent = 'Stop watching';
    drop.onclick = () => stopWatching(candidate.depart_date);
    cell.appendChild(drop);
    row.appendChild(cell);
    body_.appendChild(row);
  }
}

function renderWatchCandidates(body, watched) {
  const host = $('watch-candidates');
  host.innerHTML = '';

  const already = new Set((watched.candidates ?? []).map((c) => c.depart_date));
  const candidates = body.candidates ?? [];

  // A day that looks cheap because the days around it went unpriced is not a
  // day worth watching, so a partial sweep says so before anything is picked.
  const warning = $('watch-coverage');
  const coverage = body.coverage;
  warning.hidden = !(coverage != null && coverage < 1);
  if (!warning.hidden) {
    warning.innerHTML =
      `<strong>This sweep answered ${Math.round(coverage * 100)}% of its searches.</strong> ` +
      'The days it never priced could have been the cheap ones, so pick from this list ' +
      'knowing it is the best of what was looked at rather than the best there is.';
  }

  if (!candidates.length) {
    host.innerHTML =
      '<div class="empty">No sweep to pick from yet. Run one, and its cheapest days ' +
      'appear here.</div>';
    return;
  }

  const table = document.createElement('table');
  table.className = 'data';
  table.innerHTML =
    '<thead><tr><th>Departs</th><th>Trip</th><th class="num">Total incl. bags</th><th></th>' +
    '</tr></thead><tbody></tbody>';
  const rows = table.querySelector('tbody');

  for (const candidate of candidates) {
    const row = document.createElement('tr');
    row.innerHTML =
      `<td>${escapeHtml(candidate.depart_date)}</td>` +
      `<td>${escapeHtml(candidate.route)}` +
      `${candidate.has_overland ? ' <span class="badge badge--warning">overland</span>' : ''}</td>` +
      `<td class="num">${money(candidate.total_with_bags, candidate.currency)}</td>`;

    const cell = document.createElement('td');
    const add = document.createElement('button');
    add.className = 'small watch-add';
    const watching = already.has(candidate.depart_date);
    add.textContent = watching ? 'Watching' : 'Watch this day';
    add.disabled = watching;
    add.onclick = () => startWatching(candidate);
    cell.appendChild(add);
    row.appendChild(cell);
    rows.appendChild(row);
  }
  host.appendChild(table);
}

async function startWatching(candidate) {
  try {
    // The price it was picked at travels with the pick, so the very first check
    // can say which way it has gone instead of only setting a baseline.
    await api(`/api/watch/${state.scenario.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        depart_dates: candidate.depart_dates,
        added_price: candidate.total_with_bags,
        currency: candidate.currency,
      }),
    });
    showWatchError('');
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`);
    await renderWatch();
  } catch (error) {
    // The refusals here are the interesting ones - a candidate that cannot
    // chain, or a plan past what the site answers - and both arrive written to
    // be read, so they are shown as they are rather than summarised.
    showWatchError(error.message);
  }
}

async function stopWatching(departDate) {
  try {
    await api(`/api/watch/${state.scenario.id}/${departDate}`, { method: 'DELETE' });
    showWatchError('');
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`);
    await renderWatch();
  } catch (error) {
    showWatchError(error.message);
  }
}

$('watch-run-btn').onclick = async () => {
  try {
    await api(`/api/watch/${state.scenario.id}/run`, { method: 'POST' });
    showWatchError('');
    $('watch-run-note').textContent = 'Checking now…';
    await renderWatch();
  } catch (error) {
    showWatchError(error.message);
  }
};

$('watch-sweep-select').onchange = (event) => {
  state.stamp = event.target.value;
  renderWatch();
};

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
  // Not from `refreshEstimate`: that runs on a 300ms debounce as the form is
  // typed into, and this one reads git per trip.
  await renderNightSweep();
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
  clearFilters();
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
      clearFilters();
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

/* ------------------------------------------------------------- narrowing --

   Two halves of one step, and they answer different questions.

   Above: the constraints. A focus already said when you leave; this adds when
   you fly home and how long you are away, and the planner intersects all three
   so the searches go where the decision already is. On the Japan trip that is
   198 searches down to 44.

   Below: every leg priced on its own. Every other chart in this app is about
   the total, which is the right shape for choosing a departure date and the
   wrong one for choosing a trip — a total cannot show that the flight out is
   flat all January while the one home has a single cheap Thursday. The markers
   are draggable and nothing stops them landing somewhere the stay ranges
   forbid, because a range typed a month ago is not evidence and a price is.
*/

const narrow = {
  stamp: null,
  data: null,
  domain: [],
  cursor: [],       // one date index per leg, into `domain`
  expanded: new Set(),
};

const NIGHTS = (from, to) => Math.round((asDate(to) - asDate(from)) / 86400000);

function narrowSaved() {
  const s = state.scenario;
  return Boolean(s && (s.return_focus_start || s.total_days));
}

// The window is the outer boundary of everything a sweep may price; the
// narrowing only ever chooses inside it. A return window reaching past it is
// therefore refused - dates outside the window are never searched, so the
// intersection would be empty and the sweep would chain nothing.
//
// `widen the window first` is true and says nothing about where. The window is
// a field of the trip, and since the regrouping the trip lives a step away
// behind the gear, so the box that refuses you is not the box that fixes it.
// What the typed dates need is arithmetic, so work it out and offer it here.
// ISO strings, so `<` and `>` are date order.
function windowFor(fields) {
  const s = state.scenario || {};
  if (!s.window_start) return null;
  const start = [fields.focus_start, fields.return_focus_start]
    .filter(Boolean)
    .reduce((a, b) => (b < a ? b : a), s.window_start);
  const end = [fields.focus_end, fields.return_focus_end]
    .filter(Boolean)
    .reduce((a, b) => (b > a ? b : a), s.window_end);
  return start === s.window_start && end === s.window_end ? null : { start, end };
}

function renderWidenOffer() {
  const button = $('narrow-widen');
  const needed = windowFor(narrowFromFields());
  button.hidden = !needed;
  if (!needed) return;
  const s = state.scenario;
  const moved = [];
  if (needed.start !== s.window_start) moved.push(`back to ${needed.start}`);
  if (needed.end !== s.window_end) moved.push(`out to ${needed.end}`);
  // Named rather than "Widen it", because widening is what the next sweep
  // spends its searches on: the same trip priced a fortnight further out is a
  // materially bigger run, and the estimate under this row says how much.
  button.textContent = `Widen the trip ${moved.join(' and ')}`;
}

/* ------------------------------------------------------- the constraints */

function renderNarrowFields() {
  const s = state.scenario || {};
  $('narrow-out-start').value = s.focus_start || '';
  $('narrow-out-end').value = s.focus_end || '';
  $('narrow-back-start').value = s.return_focus_start || '';
  $('narrow-back-end').value = s.return_focus_end || '';
  $('narrow-nights-min').value = s.total_days ? s.total_days[0] : '';
  $('narrow-nights-max').value = s.total_days ? s.total_days[1] : '';

  const badge = $('narrow-state');
  const parts = [];
  if (s.focus_start) parts.push(`out ${s.focus_start.slice(5)}–${s.focus_end.slice(5)}`);
  if (s.return_focus_start) {
    parts.push(`home ${s.return_focus_start.slice(5)}–${s.return_focus_end.slice(5)}`);
  }
  if (s.total_days) parts.push(`${s.total_days[0]}–${s.total_days[1]} nights`);
  badge.className = `badge badge--${parts.length ? 'good' : 'muted'}`;
  badge.textContent = parts.length ? parts.join(' · ') : 'the whole window';

  renderNarrowStays();
  refreshStayDerived();
  renderWidenOffer();
}

/* ------------------------------------------------------------- the stays --

   Not part of the narrowing, and deliberately below it under their own
   heading. The narrowing chooses inside the trip and clears cleanly; a stay
   range is the trip, and tightening one cannot be undone by reading the same
   legs another way — `combine._stay_ok` runs whatever `?window=` says, so the
   itineraries it excludes leave the table as well as the plan. They are here
   because the band above cannot exceed them and the panel could not say so
   while they lived a step away behind the gear. */

function narrowStayStops() {
  // The stops that bound how long you are away: the same slice
  // `_stay_breakdown` names on the server, so the boxes and the sentence about
  // them agree. Nothing has to happen after a one-way chain's final leg
  // departs, so its last stop bounds nothing and a box for it would be a
  // control with no effect.
  const s = state.scenario || {};
  const stops = s.stops || [];
  return stops.slice(0, Math.max(0, stops.length - (s.one_way ? 1 : 0)));
}

function renderNarrowStays() {
  const host = $('narrow-stays');
  host.innerHTML = '';
  narrowStayStops().forEach((stop, index) => {
    const label = document.createElement('label');
    // A stop's label is typed by whoever made the trip, so it is a text node
    // rather than markup, like every other name this page prints.
    label.textContent = stop.label || `Stop ${index + 1}`;
    const row = document.createElement('span');
    row.className = 'row';
    const fields = [0, 1].map((slot) => {
      const field = document.createElement('input');
      field.type = 'number';
      field.min = '1';
      field.max = '365';
      field.step = '1';
      field.dataset.stay = String(index);
      field.dataset.slot = String(slot);
      field.value = stop.stay_days[slot];
      field.onchange = () => { refreshStayDerived(); refreshNarrowCost(); };
      return field;
    });
    const to = document.createElement('span');
    to.className = 'small muted';
    to.textContent = 'to';
    const nights = document.createElement('span');
    nights.className = 'small muted';
    nights.textContent = 'nights';
    row.append(fields[0], to, fields[1], nights);
    label.append(row);
    host.append(label);
  });
}

function staysFromFields() {
  // The saved stops carrying whatever is in the boxes. Copied rather than
  // mutated: `state.scenario` is what a failed save falls back to, and editing
  // it in place would make a refusal look like it had taken.
  const stops = ((state.scenario || {}).stops || []).map((stop) => ({
    ...stop,
    stay_days: [...stop.stay_days],
  }));
  for (const field of $('narrow-stays').querySelectorAll('input[data-stay]')) {
    const stop = stops[Number(field.dataset.stay)];
    if (stop && field.value !== '') {
      stop.stay_days[Number(field.dataset.slot)] = Number(field.value);
    }
  }
  return stops;
}

function refreshStayDerived() {
  const stops = staysFromFields();
  const counted = stops.slice(0, narrowStayStops().length);

  // What the stays make reachable at all, next to the box you type the band
  // into — so an impossible number is obvious before it is saved rather than
  // after the save is refused. Read off the boxes above rather than off the
  // saved trip, or it would keep naming the range you had just changed.
  const hint = $('narrow-nights-hint');
  hint.textContent = counted.length
    ? `the stays allow ${counted.reduce((n, stop) => n + stop.stay_days[0], 0)}–` +
      `${counted.reduce((n, stop) => n + stop.stay_days[1], 0)}`
    : '';

  // Widening costs searches, and the estimate under this row already says how
  // many. Tightening costs future prices, and nothing else on the page would
  // mention it: this scenario's own notes record a 9-11 rule here throwing away
  // a real 12-day Japan stay.
  //
  // Future, and only future. `_sweep_scenario` reads a run's shape - airports,
  // stops, stays, window - off the snapshot that run wrote, so nothing already
  // collected is filtered by a range typed afterwards. An earlier version of
  // this warning said otherwise, which was worth saying and false.
  const saved = (state.scenario || {}).stops || [];
  const tightened = counted
    .map((stop, index) => [stop, saved[index]])
    .filter(([now, was]) => was && (
      now.stay_days[0] > was.stay_days[0] || now.stay_days[1] < was.stay_days[1]
    ))
    .map(([now, was]) => `${now.label || 'a stop'} ${was.stay_days[0]}–${was.stay_days[1]} → ` +
      `${now.stay_days[0]}–${now.stay_days[1]}`);

  const alert = $('narrow-stays-alert');
  alert.hidden = tightened.length === 0;
  alert.textContent = tightened.length
    ? `Tighter than what is saved: ${tightened.join('; ')}. Nothing below moves — every sweep ` +
      'on disk keeps the stays it ran under. From the next sweep on, those nights stop being ' +
      'priced at all.'
    : '';
}

function narrowFromFields() {
  const value = (id) => $(id).value || null;
  const min = $('narrow-nights-min').value;
  const max = $('narrow-nights-max').value;
  return {
    focus_start: value('narrow-out-start'),
    focus_end: value('narrow-out-end'),
    return_focus_start: value('narrow-back-start'),
    return_focus_end: value('narrow-back-end'),
    total_days: min !== '' && max !== '' ? [Number(min), Number(max)] : null,
    // Priced and saved with the rest, so the estimate under the boxes is of the
    // trip in them and one Save writes the whole panel. A stay change written
    // separately would leave a nights band briefly claiming a span its stays no
    // longer reach, which `validate` refuses.
    stops: staysFromFields(),
  };
}

async function refreshNarrowCost() {
  // Priced against the trip on screen rather than the one on disk, so the
  // figure moves as the dates are typed. Deep on both sides: comparing a
  // narrowed quick plan against a broad deep one would flatter the narrowing
  // by the depth rather than by the narrowing.
  const note = $('narrow-cost');
  const trip = { ...state.scenario, ...narrowFromFields() };
  try {
    const [whole, narrowed] = await Promise.all([
      api(`/api/scenarios/${state.scenario.id}/estimate?depth=deep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // The same trip with the narrowing off, not the saved one. The
        // sentence claims the two numbers differ by the narrowing, and a
        // baseline built from the saved stays would not move when a stay does -
        // so widening a stay would report as costing nothing at all.
        body: JSON.stringify({
          ...trip,
          focus_start: null, focus_end: null,
          return_focus_start: null, return_focus_end: null, total_days: null,
        }),
      }),
      api(`/api/scenarios/${state.scenario.id}/estimate?depth=deep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(trip),
      }),
    ]);
    note.textContent =
      `A deep sweep of this is ${count(narrowed.searches)} searches (~${Math.round(narrowed.minutes)} min), ` +
      `against ${count(whole.searches)} searches (~${Math.round(whole.minutes)} min) for the whole window.`;
  } catch (error) {
    // Said as a cost, because that is this line's job and the reason it cannot
    // be given happens to be the reason the save would fail. Without the
    // prefix the refusal renders in the muted voice of an estimate - a fact
    // about the sweep rather than about what you just typed - and then repeats
    // itself word for word in red beside Save.
    note.textContent = `No estimate — ${error.message}.`;
  }
}

async function saveNarrowing(fields) {
  const message = $('narrow-message');
  message.className = 'small';
  try {
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...state.scenario, ...fields }),
    });
    renderNarrowFields();
    // This panel now writes fields the trip's own form holds a draft of - the
    // window, when widening, and the stays. `route` is that draft and it is
    // filled once on load, so without this it keeps the values from before the
    // save and the next Save over in the setup step quietly puts them back.
    fillForm(state.scenario);
    message.textContent = narrowSaved()
      ? 'Saved. Tonight’s sweep prices this.'
      : 'Cleared. Back to the whole window.';
    // Everything in this step reads the narrowing, and all of it is on screen
    // at once now. Refreshing only the charts left the table under them listing
    // trips the narrowing had just excluded - two panels, one screen, two
    // different answers to what this sweep contains.
    await Promise.all([
      refreshNarrowCost(),
      loadLegCharts(),
      renderByDate(),
      renderResults(),
    ]);
  } catch (error) {
    // The server refuses a narrowing nothing can satisfy and says which stays
    // make it impossible. That sentence is the useful one, so it is shown
    // verbatim rather than rewritten into something vaguer.
    message.className = 'small badge badge--error';
    message.textContent = error.message;
  }
}

/* --------------------------------------------------------- the leg charts */

async function renderNarrow() {
  renderNarrowFields();
  const picker = $('narrow-sweep');
  // Probes and runs whose legs never reached disk cannot answer this panel:
  // a probe prices three dates a leg to compare airports, which draws a chart
  // that looks like a price curve and is not one.
  const sweeps = state.sweeps.filter((s) => s.has_legs && s.mode !== 'explore');
  picker.innerHTML = sweeps
    .map((s) => `<option value="${escapeHtml(s.stamp)}">${escapeHtml(sweepLabel(s))}</option>`)
    .join('');
  if (!sweeps.length) {
    $('leg-charts').className = 'empty';
    $('leg-charts').textContent = 'No sweep with flights on disk yet. Run one from Search.';
    $('cursor-readout').innerHTML = '';
    return;
  }
  // One sweep for the whole step. This picker and the table's used to be
  // separate, so the charts could be showing 22 August while the itineraries
  // under them were 23 August's probe - two panels on one screen describing two
  // different runs, with nothing to say so. Follow the step's selection when it
  // can be drawn, and pull the step to this one when it cannot.
  if (state.stamp && sweeps.some((s) => s.stamp === state.stamp)) {
    narrow.stamp = state.stamp;
  } else {
    // The step opened on a run these charts cannot draw - most often the
    // newest, which is a two-hourly probe. Move the whole step onto the newest
    // run that can be drawn rather than leaving the charts and the table below
    // them on different days.
    if (!narrow.stamp || !sweeps.some((s) => s.stamp === narrow.stamp)) {
      narrow.stamp = sweeps[0].stamp;
    }
    state.stamp = narrow.stamp;
    const table = $('sweep-select');
    if ([...table.options].some((o) => o.value === state.stamp)) table.value = state.stamp;
  }
  picker.value = narrow.stamp;
  await Promise.all([refreshNarrowCost(), loadLegCharts()]);
}

async function loadLegCharts() {
  if (!narrow.stamp) return;
  const host = $('leg-charts');
  try {
    narrow.data = await api(
      `/api/sweeps/${state.scenario.id}/${narrow.stamp}/by-leg`,
    );
  } catch (error) {
    host.className = 'empty';
    host.textContent = error.message;
    return;
  }

  // One axis for every leg: the union of every date any leg was asked about,
  // in order. Each chart is then handed the same domain, which is what makes a
  // vertical slice down the stack one trip rather than three unrelated days.
  const dates = new Set();
  for (const leg of narrow.data.legs) for (const p of leg.points) dates.add(p.depart_date);
  const domain = [...dates].sort();
  // A trip picked by hand survives leaving the step and coming back. It is
  // several deliberate drags, and re-snapping it away on the way past would
  // make the panel unusable next to any other one. Only a genuinely different
  // axis - another sweep, or the same trip re-swept over other dates - is
  // grounds for throwing the cursor away, because indices into the old domain
  // would then point at the wrong days.
  const same =
    narrow.domain.length === domain.length
    && narrow.domain.every((label, i) => label === domain[i])
    && narrow.cursor.length === narrow.data.legs.length;
  narrow.domain = domain;
  if (!same) narrow.expanded.clear();
  renderNarrowFields();

  if (!same) await snapCursor({ quiet: true });
  drawLegCharts();
}

function drawLegCharts() {
  const host = $('leg-charts');
  host.className = '';
  host.innerHTML = '';
  if (!narrow.data || !narrow.domain.length) {
    host.className = 'empty';
    host.textContent = 'This sweep found no flights.';
    return;
  }

  // Every chart the same width, or the columns stop lining up and the whole
  // point of stacking them is lost. Filling the panel rather than a fixed 520:
  // three narrow charts floating in a wide panel waste exactly the horizontal
  // room a date axis needs most.
  const width = Math.max(
    host.clientWidth - 4 || 520,
    narrow.domain.length * 26 + 80,
  );
  const suffix = ` ${narrow.data.currency}`;

  narrow.data.legs.forEach((leg, index) => {
    const block = document.createElement('div');
    block.className = 'leg-chart';

    const header = document.createElement('div');
    header.className = 'leg-chart__header';
    const toggle = document.createElement('button');
    toggle.className = 'small';
    toggle.textContent = narrow.expanded.has(index)
      ? `${leg.label} — hide routes`
      : `${leg.label}${leg.routes.length > 1 ? ` — ${count(leg.routes.length)} routes` : ''}`;
    toggle.disabled = leg.routes.length < 2;
    toggle.onclick = () => {
      if (narrow.expanded.has(index)) narrow.expanded.delete(index);
      else narrow.expanded.add(index);
      drawLegCharts();
    };
    header.appendChild(toggle);

    const picked = document.createElement('span');
    picked.className = 'small muted';
    const at = narrow.domain[narrow.cursor[index]];
    const point = at && leg.points.find((p) => p.depart_date === at);
    picked.textContent = point && point.price !== null
      ? `${at} · ${point.origin}→${point.destination} · ${money(point.price, narrow.data.currency)}`
      : at ? `${at} · nothing found` : '';
    header.appendChild(picked);
    block.appendChild(header);

    const series = leg.points.map((p) => ({
      label: p.depart_date,
      value: p.price,
      searched: p.searched,
      note: p.price === null ? null
        : `${p.origin}→${p.destination} · ${p.airline || 'unknown airline'} · ` +
          `${p.stops === 0 ? 'direct' : `${count(p.stops)} stops`}`,
    }));

    block.appendChild(lineChart(series, {
      domain: narrow.domain,
      width,
      height: 190,
      valueSuffix: suffix,
      ariaLabel: `${leg.label} by departure date`,
      marker: { index: narrow.cursor[index] },
      onMarkerMove: (i) => { narrow.cursor[index] = i; renderCursor(); },
      onMarkerRelease: () => drawLegCharts(),
      emptyText: 'Nothing priced on this leg.',
    }));

    if (narrow.expanded.has(index)) {
      // Six colours exist and were validated in both themes. A seventh route
      // would reuse one and two lines would be indistinguishable, so the extra
      // ones are named rather than drawn in a colour that lies.
      const shown = leg.routes.slice(0, 6);
      shown.forEach((route, r) => {
        const row = document.createElement('div');
        row.className = 'leg-chart__route';
        const name = document.createElement('span');
        name.className = 'small';
        name.textContent = route.route;
        row.appendChild(name);
        row.appendChild(lineChart(
          route.points.map((p) => ({
            label: p.depart_date, value: p.price, searched: p.searched,
          })),
          {
            domain: narrow.domain, width, height: 120, valueSuffix: suffix,
            color: `var(--color-chart${r + 1})`,
            ariaLabel: `${route.route} by departure date`,
            emptyText: `${route.route} — nothing sold on any date searched.`,
          },
        ));
        block.appendChild(row);
      });
      if (leg.routes.length > shown.length) {
        const rest = document.createElement('p');
        rest.className = 'panel__hint small';
        rest.textContent =
          `${count(leg.routes.length - shown.length)} other routes not drawn: six is as many ` +
          'lines as this palette can tell apart.';
        block.appendChild(rest);
      }
    }

    host.appendChild(block);
  });

  renderCursor();
}

/* ----------------------------------------------------------- the readout */

function cursorTrip() {
  /* What the markers currently say, priced, split and checked.

     Checked rather than enforced. Every rule it can break is reported by name
     and none of them stops anything, which is the whole reason the markers
     move independently. */
  const legs = narrow.data.legs.map((leg, i) => {
    const at = narrow.domain[narrow.cursor[i]];
    const point = leg.points.find((p) => p.depart_date === at) || null;
    return { label: leg.label, date: at, point };
  });

  const priced = legs.every((l) => l.point && l.point.price !== null);
  const total = priced
    ? legs.reduce((sum, l) => sum + l.point.with_bags, 0)
    : null;

  const stays = [];
  for (let i = 1; i < legs.length; i += 1) {
    stays.push(NIGHTS(legs[i - 1].date, legs[i].date));
  }
  const away = legs.length > 1 ? NIGHTS(legs[0].date, legs[legs.length - 1].date) : 0;

  // Two different kinds of wrong, and conflating them would make the panel
  // useless. A stay outside its range is a rule *you* set and may want to
  // break — the whole reason the markers move freely. A leg that departs before
  // the one before it has arrived is not a preference at all; no sweep, no
  // watch and no airline can price it, and offering to follow it would only
  // produce a refusal from the server one click later.
  const impossible = [];
  for (let i = 1; i < legs.length; i += 1) {
    if (legs[i].date && legs[i - 1].date && legs[i].date <= legs[i - 1].date) {
      impossible.push(`${legs[i].date} is not after ${legs[i - 1].date}`);
    }
  }

  const breaks = [];
  const labels = narrow.data.stop_labels || [];
  narrow.data.stay_days.forEach((range, i) => {
    if (i >= stays.length) return;
    if (stays[i] < range[0] || stays[i] > range[1]) {
      breaks.push(`${stays[i]} nights at ${labels[i] || `stop ${i + 1}`}, not ${range[0]}–${range[1]}`);
    }
  });
  const band = narrow.data.window && narrow.data.window.total_days;
  if (band && (away < band[0] || away > band[1])) {
    breaks.push(`${away} nights away, not ${band[0]}–${band[1]}`);
  }
  // All three parts of the narrowing, in the order they are read on the panel
  // above. Checking two of them and not the departure window is how "fits every
  // rule you set" came to sit under a trip leaving two days outside it.
  const out = narrow.data.window && narrow.data.window.focus;
  const first = legs[0].date;
  if (out && first && (first < out[0] || first > out[1])) {
    breaks.unshift(`leaving ${first}, not ${out[0]}–${out[1]}`);
  }
  const home = narrow.data.window && narrow.data.window.return_focus;
  const last = legs[legs.length - 1].date;
  if (home && (last < home[0] || last > home[1])) {
    breaks.push(`flying home ${last}, not ${home[0]}–${home[1]}`);
  }

  return { legs, priced, total, stays, away, breaks, impossible };
}

function renderCursor() {
  const host = $('cursor-readout');
  if (!narrow.data || !narrow.domain.length || !narrow.cursor.length) {
    host.innerHTML = '';
    return;
  }
  const trip = cursorTrip();
  const currency = narrow.data.currency;

  const split = trip.stays.length
    ? trip.stays.join(' + ') + ` = ${count(trip.away)} nights away`
    : `${count(trip.away)} nights away`;

  const badges = trip.impossible.length
    ? trip.impossible
        .map((b) => `<span class="badge badge--error">cannot exist: ${escapeHtml(b)}</span>`)
        .join(' ')
    : trip.breaks.length
      ? trip.breaks.map((b) => `<span class="badge badge--warning">${escapeHtml(b)}</span>`).join(' ')
      : '<span class="badge badge--good">fits every rule you set</span>';

  // The only thing on this panel that is ever disabled, and only for the one
  // case nothing downstream could price anyway.
  const follow = $('cursor-watch');
  follow.disabled = trip.impossible.length > 0;
  follow.title = trip.impossible.length
    ? 'These legs are out of order — no sweep or watch could price this.'
    : '';

  host.innerHTML = `
    <div class="cursor-readout__total">
      ${trip.priced ? escapeHtml(money(trip.total, currency)) : '—'}
      <span class="small muted">incl. bags</span>
    </div>
    <div class="cursor-readout__split">${escapeHtml(split)}</div>
    <div class="cursor-readout__badges">${badges}</div>
    <div class="cursor-readout__legs">${trip.legs.map((l) => `
      <span>${escapeHtml(l.date || '—')}
        <span class="muted">${l.point && l.point.price !== null
          ? escapeHtml(`${l.point.origin}→${l.point.destination} ${money(l.point.price, currency)}`)
          : 'nothing found'}</span>
      </span>`).join('')}</div>
  `;
  if (!trip.priced) {
    const warn = document.createElement('p');
    warn.className = 'small muted';
    warn.textContent =
      'One of these dates had no flight in this sweep, so there is no total to show. ' +
      'The trip may still exist — it was simply never priced on that day.';
    host.appendChild(warn);
  }
}

/* Snap every marker to the cheapest trip that satisfies the narrowing.

   Taken from `/results?window=narrow` rather than worked out here, so the
   button lands on exactly the itinerary the table calls cheapest. Two pieces
   of code answering "which is cheapest" is two answers, and the one on the
   chart would be the one nobody could check. */
async function snapCursor({ quiet = false } = {}) {
  const message = $('cursor-message');

  const place = (itinerary) => {
    narrow.cursor = itinerary.legs.map((leg) => {
      const at = narrow.domain.indexOf(leg.depart_date);
      return at >= 0 ? at : 0;
    });
  };

  /* Last resort: each leg's own cheapest day, forced into order.

     Cheapest-per-leg on its own is not a trip. The legs are priced
     independently, so the cheapest day for the flight to Manila is regularly
     before the cheapest day for the flight to Tokyo - and the panel would then
     open on "cannot exist", with Follow disabled, before the reader had touched
     a thing. Marching the dates forward gives a cursor that is merely
     uninteresting rather than impossible. */
  const spread = () => {
    let floor = -1;
    narrow.cursor = narrow.data.legs.map((leg) => {
      const priced = leg.points.filter((p) => p.price !== null);
      let at = 0;
      if (priced.length) {
        const best = priced.reduce((a, b) => (b.with_bags < a.with_bags ? b : a));
        at = Math.max(0, narrow.domain.indexOf(best.depart_date));
      }
      if (at <= floor) at = Math.min(floor + 1, narrow.domain.length - 1);
      floor = at;
      return at;
    });
  };

  const ask = async (window) => {
    const body = await api(
      `/api/sweeps/${state.scenario.id}/${narrow.stamp}/results?window=${window}&limit=1`,
    );
    return [body.itineraries && body.itineraries[0], body.currency];
  };

  try {
    const [best, currency] = await ask('narrow');
    if (best) {
      place(best);
      if (!quiet) {
        message.className = 'small';
        message.textContent = `Snapped to ${money(best.total_with_bags, currency)} incl. bags.`;
      }
    } else {
      // Nothing here fits. Show the cheapest trip this sweep *can* build rather
      // than no trip at all: that it exists and sits outside the narrowing is
      // the useful thing to see, and it is a real trip to start dragging from.
      const [nearest, nearestCurrency] = await ask('all');
      if (nearest) place(nearest);
      else spread();
      if (!quiet) {
        message.className = 'small badge badge--warning';
        message.textContent = nearest
          ? `No trip in this sweep fits your narrowing. This is its cheapest, at ` +
            `${money(nearest.total_with_bags, nearestCurrency)} — the badges say what it breaks.`
          : 'This sweep built no complete trip at all.';
      }
    }
  } catch (error) {
    spread();
    if (!quiet) {
      message.className = 'small badge badge--error';
      message.textContent = error.message;
    }
  }
  if (!quiet) drawLegCharts();
}

/* Follow the picked trip: a Watch, pinned to these exact leg dates.

   Legal whether or not it breaks the stay ranges. A watch prices the dates it
   is given; the ranges only ever governed which chains a sweep would build. */
async function watchCursor() {
  const message = $('cursor-message');
  const trip = cursorTrip();
  try {
    await api(`/api/watch/${state.scenario.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        depart_dates: trip.legs.map((l) => l.date),
        added_price: trip.priced ? trip.total : null,
        currency: narrow.data.currency,
      }),
    });
    message.className = 'small badge badge--good';
    message.textContent = trip.breaks.length
      ? 'Following it. It breaks a rule you set, and it will be priced every four hours all the same.'
      : 'Following it. Priced every four hours from now.';
  } catch (error) {
    message.className = 'small badge badge--error';
    message.textContent = error.message;
  }
}

$('notify-save-btn').onclick = saveTrip;
$('night-save-btn').onclick = saveTrip;

$('narrow-sweep').onchange = async (event) => {
  narrow.stamp = event.target.value;
  // The table and the by-date chart in this step move with it, or the step is
  // back to describing two runs at once.
  state.stamp = narrow.stamp;
  const picker = $('sweep-select');
  if ([...picker.options].some((o) => o.value === state.stamp)) picker.value = state.stamp;
  await Promise.all([loadLegCharts(), renderByDate(), renderResults()]);
};
$('narrow-save').onclick = () => saveNarrowing(narrowFromFields());
$('narrow-clear').onclick = () => saveNarrowing({
  return_focus_start: null, return_focus_end: null, total_days: null,
});
$('narrow-widen').onclick = () => {
  // One save, not two. Widening and then narrowing as separate writes leaves a
  // trip that is briefly wider with no narrowing on it, and a nightly sweep
  // firing between the two would price the whole of it.
  const fields = narrowFromFields();
  const needed = windowFor(fields);
  if (!needed) return;
  saveNarrowing({ ...fields, window_start: needed.start, window_end: needed.end });
};
$('cursor-snap').onclick = () => snapCursor();
$('cursor-watch').onclick = () => watchCursor();
for (const id of [
  'narrow-out-start', 'narrow-out-end', 'narrow-back-start', 'narrow-back-end',
  'narrow-nights-min', 'narrow-nights-max',
]) {
  $(id).onchange = () => { renderWidenOffer(); refreshNarrowCost(); };
}

init();

/* ---------------------------------------------------------- watched legs --

   A pinned trip asks "is this trip moving" and costs a search per airport pair
   per leg. This asks "is this ticket moving" and costs one. They share a budget
   because they share a run, so the cost badge here reports the whole planned
   check rather than this panel's share of it — two panels each looking
   affordable while the run they add up to is refused is the failure worth
   designing out. */

function renderLegWatches(body) {
  const legs = body.legs ?? [];
  $('leg-watch-empty').hidden = legs.length > 0;

  const cost = $('leg-watch-cost');
  cost.className = `badge badge--${legs.length ? 'good' : 'muted'}`;
  cost.textContent = legs.length
    ? `${count(body.searches)} searches (~${body.minutes} min) a check, all told`
    : 'nothing followed';

  const host = $('leg-watch-chart');
  host.innerHTML = '';
  host.appendChild(multiLineChart(
    legs.filter((leg) => (leg.series ?? []).length).map((leg) => ({
      name: `${leg.route} ${leg.depart_date}`,
      points: leg.series.map((point) => ({
        t: point.ts, value: point.total, muted: !point.comparable,
      })),
    })),
    {
      width: Math.max(420, host.clientWidth - 4),
      valueSuffix: ` ${state.scenario.currency ?? 'CZK'}`,
      ariaLabel: 'Followed flights over time',
      emptyText: legs.length
        ? 'No checks yet — the first runs within four hours, or press Check now below.'
        : 'Nothing is being followed yet.',
    },
  ));

  const rows = $('leg-watch-table').querySelector('tbody');
  rows.innerHTML = '';
  for (const leg of legs) {
    // Two baselines answering different questions, as on the trip table: what
    // you decided on, and what it has done since it was first measured.
    const picked = leg.added_price;
    const now = leg.latest;
    const move = picked != null && now != null ? now - picked : leg.net_change;
    const trend = move > 0 ? 'trend--up' : move < 0 ? 'trend--down' : '';

    const row = document.createElement('tr');
    row.innerHTML =
      `<td>${escapeHtml(leg.route)}` +
        // Not a refusal — following a route the sweep never prices is allowed
        // and sometimes the point — but this run is then the only thing keeping
        // that price alive, which is worth knowing before relying on it.
        `${leg.off_trip ? ' <span class="badge badge--muted" title="This trip’s sweep never prices this route">off-trip</span>' : ''}` +
        `${leg.airline ? ` <span class="muted small">${escapeHtml(leg.airline)}</span>` : ''}</td>` +
      `<td>${escapeHtml(leg.depart_date)}` +
        // The site answers 22 January with the 23rd. Saying so beats presenting
        // a price for a day you cannot buy as the day you picked.
        `${leg.exact === false && leg.found_date
          ? ` <span class="badge badge--warning" title="The site answered with a nearby day">priced ${escapeHtml(leg.found_date)}</span>`
          : ''}</td>` +
      `<td class="num">${picked == null ? '—' : money(picked, leg.currency)}</td>` +
      `<td class="num">${now == null
        ? '<span class="muted">not checked yet</span>'
        : money(now, leg.currency)}</td>` +
      `<td class="num ${trend}">${now == null || picked == null
        ? '—'
        : `${move > 0 ? '+' : ''}${money(move, '')}`}</td>`;

    const cell = document.createElement('td');
    const drop = document.createElement('button');
    drop.className = 'small watch-drop';
    drop.textContent = 'Stop following';
    drop.onclick = () => unfollowLeg(leg.key);
    cell.appendChild(drop);
    row.appendChild(cell);
    rows.appendChild(row);
  }
}

function showLegWatchError(message) {
  const host = $('leg-watch-error');
  host.hidden = !message;
  host.textContent = message || '';
}

async function followLeg({ origin, destination, depart_date: departDate, price }) {
  try {
    await api(`/api/watch/${state.scenario.id}/legs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        origin, destination, depart_date: departDate,
        // Travels with the pick, so the first check can already say which way
        // it has gone rather than only setting a baseline.
        added_price: price ?? null,
        currency: state.scenario.currency ?? 'CZK',
      }),
    });
    showLegWatchError('');
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`);
    await renderWatch();
    return true;
  } catch (error) {
    // The refusals are the interesting ones — a budget the whole check would
    // exceed, a mistyped year — and each arrives written to be read.
    showLegWatchError(error.message);
    showTab('watch');
    return false;
  }
}

async function unfollowLeg(key) {
  try {
    await api(`/api/watch/${state.scenario.id}/legs/${encodeURIComponent(key)}`,
      { method: 'DELETE' });
    showLegWatchError('');
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`);
    await renderWatch();
  } catch (error) {
    showLegWatchError(error.message);
  }
}

$('leg-watch-add-btn').onclick = async () => {
  const origin = $('leg-watch-from').value.trim().toUpperCase();
  const destination = $('leg-watch-to').value.trim().toUpperCase();
  const departDate = $('leg-watch-date').value;
  if (!origin || !destination || !departDate) {
    showLegWatchError('Give a from, a to and a date.');
    return;
  }
  if (await followLeg({ origin, destination, depart_date: departDate })) {
    for (const id of ['leg-watch-from', 'leg-watch-to']) $(id).value = '';
  }
};
