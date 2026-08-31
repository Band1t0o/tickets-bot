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
  // Which run the Final sweeps step reads. Its own selection, like the probe's:
  // that step lists only the narrowed runs and this one only the broad ones, so
  // one shared stamp would have each step keep snapping the other's picker back.
  finalStamp: null,
  // Which run the Follow step picks candidates from. Its own again, and this is
  // the third of them for a reason: every step that lists runs has to own its
  // selection or it inherits whichever one another step last chose. This picker
  // was the original case - it had a label and an `onchange` and was never
  // filled by anything - and binding it to the broad step's stamp only moved the
  // bug, because it could then never land on a narrowed run.
  watchStamp: null,
  // How each ranking table is narrowed. Held here rather than read off the DOM
  // so a re-render cannot lose it, and sent to the server rather than applied
  // here - see the comment on the filter row in index.html. One per table:
  // "ignore my narrowing" means opposite things either side of the split.
  filters: { from: '', to: '', bags: false, allWindow: false },
  finalFilters: { from: '', to: '', bags: false, allWindow: false },
};

/* Must equal `API_CONTRACT` in src/web/app.py; a test fails if it does not.

   Static files are served from disk on every request, but the Python is frozen
   at import time — so a server left running from an older commit hands you this
   very file and then 404s the endpoints it asks for. That is how an afternoon
   was lost: the page rendered an empty trip picker and empty charts, which is
   exactly what a deleted database looks like, when in fact nothing on disk had
   changed and the answer was `make ui` again. */
const EXPECTED_CONTRACT = 15;

/* Preferences that may be followed at once, mirroring `scenario.MAX_PREFERENCES`.
   Only ever used to *say* the number - "2 of 4" - never to refuse anything. The
   refusals are the server's, on the planned search count, because that is the
   figure that sees what a preference's slack and airport pools really cost. */
const MAX_PREFERENCES = 4;

/* Days either side of each pinned date a new preference prices, mirroring
   `scenario.DEFAULT_SLACK_DAYS`. */
const DEFAULT_SLACK_DAYS = 2;

/* Every request the page makes, with the two failures that are not HTTP turned
   into sentences.

   `fetch` rejects with a bare "Failed to fetch" when the server is not there at
   all, and that reached the page verbatim wherever anything caught it - beside a
   cost line, in a badge - and reached nothing at all where nothing did. It is
   the same trap the stale-server blocker above exists for, and it earns the same
   sentence: this app is a local server plus a page, and "it stopped" is by far
   the likeliest reason a request never lands. */
const api = async (path, options) => {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error(
      'The server is not answering — it has probably stopped. Restart it with '
      + '`make ui` and reload. Nothing on disk is affected: your trips and every '
      + 'sweep are files, and they are still there.',
    );
  }
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

/* A run stamp as the ISO instant `multiLineChart` plots against.

   The history chart moved from `lineChart`, which spaces points evenly and
   labels them, to `multiLineChart`, which puts them on a real time axis. Two
   series only line up on a shared axis if both are placed by when they ran -
   the broad sweeps and the narrowed ones do not alternate neatly, and evenly
   spacing each line on its own would draw them as though they had. */
const isoOf = (stamp) => (asDate(stamp) ?? new Date(0)).toISOString();

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

/* ------------------------------------------- explanations on demand ------

   Every panel in this app explains itself, and most of those explanations
   record a failure that cost a day - a probe read against the wrong trip, a
   sweep charted through its own breakage, a stay range that quietly threw away
   a twelve-day Japan. They are worth keeping and they are worth reading once.
   What they should not be is the first paragraph on every panel of a screen you
   already understand, which is what pushed the actual numbers below the fold.

   So: teaching collapses, telling does not, and that is one class each rather
   than a class and an exemption from it. `.explain` is prose about how a
   mechanic works. `.answer` is a statement about what is on screen - which run
   priced this pool, how many routes are not drawn, what a source replied - and
   stays whatever the switch says.

   It used to be `.panel__hint` with a `--live` modifier that opted out, so
   every renderer writing grey prose reached for the modifier and got a
   paragraph the switch could not reach. Turning explanations off then left most
   of the teaching on screen. Notices and warnings are neither and are never
   touched.

   The prose is hidden by a class on the panel, not by `hidden` on the paragraph:
   it stays in the DOM, so find-in-page still reaches it. */

const HINTS_KEY = 'hints';
const HINTS_OPEN_KEY = 'hintsOpen';

// Off by default. The alternative was defensible for a first run and wrong for
// the four hundredth.
const hintsOn = () => localStorage.getItem(HINTS_KEY) === 'on';

const hintsOpen = () => {
  try {
    const raw = JSON.parse(localStorage.getItem(HINTS_OPEN_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch {
    // A hand-edited or half-written value is not worth a broken page.
    return new Set();
  }
};

/* One panel's prose, shown or hidden, remembered by the panel's own name.

   Named by `data-panel` and an index within it, because several panels share a
   section and nothing else about them is stable: a heading is prose someone may
   reword, and DOM order changes whenever a panel moves. */
function panelKey(panel) {
  const section = panel.closest('section[data-panel]');
  if (!section) return null;
  const siblings = [...section.querySelectorAll('.panel')];
  return `${section.dataset.panel}:${siblings.indexOf(panel)}`;
}

function applyHints() {
  const on = hintsOn();
  const open = hintsOpen();

  // The global default, on the document, so it reaches prose that is not inside
  // a panel. It was applied only to panels, and the Follow step's summary is a
  // notice sitting directly in its section - so one paragraph of teaching was
  // beyond the switch entirely, for no reason anyone chose.
  document.body.classList.toggle('hints-off', !on);

  for (const panel of document.querySelectorAll('section[data-panel] .panel')) {
    const button = panel.querySelector('.panel__info');
    if (!button) continue;
    const key = panelKey(panel);
    // A panel in the override set is the other way round from the default,
    // whichever way round that is. Both states are named rather than one, so a
    // panel opened against an "off" default can out-specify it.
    const showing = open.has(key) ? !on : on;
    panel.classList.toggle('is-quiet', !showing);
    panel.classList.toggle('is-loud', showing);
    button.setAttribute('aria-expanded', String(showing));
    button.title = showing ? 'Hide what this panel is for' : 'What is this panel for?';
  }
  const toggle = $('hints-toggle');
  toggle.textContent = `Explanations: ${on ? 'on' : 'off'}`;
  toggle.setAttribute('aria-pressed', String(on));
}

/* Give every panel that has prose to hide a button to hide it with.

   Run once, after the static markup is parsed. Panels whose only hints are live
   ones get no button, because a control that does nothing is worse than none -
   and that is most of the Cloud tab.  */
function mountHintToggles() {
  for (const panel of document.querySelectorAll('section[data-panel] .panel')) {
    const collapsible = panel.querySelector('.explain');
    const header = panel.querySelector('.panel__header');
    if (!collapsible || !header || panel.querySelector('.panel__info')) continue;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'panel__info';
    button.textContent = 'What this is for';
    button.onclick = () => {
      const key = panelKey(panel);
      const open = hintsOpen();
      if (open.has(key)) open.delete(key);
      else open.add(key);
      localStorage.setItem(HINTS_OPEN_KEY, JSON.stringify([...open]));
      applyHints();
    };
    header.appendChild(button);
  }
  applyHints();
}

$('hints-toggle').onclick = () => {
  localStorage.setItem(HINTS_KEY, hintsOn() ? 'off' : 'on');
  // The per-panel overrides were answers to the old default and mean the
  // opposite under the new one. Flipping the switch is the deliberate act, so
  // it wins outright rather than leaving a scatter of panels disagreeing with it.
  localStorage.removeItem(HINTS_OPEN_KEY);
  applyHints();
};

mountHintToggles();

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
  search: 'map', explore: 'map', 'broad-charts': 'map', results: 'map',
  narrow: 'narrow',
  'final-charts': 'narrow', 'final-results': 'narrow',
  watch: 'follow', history: 'follow',
  notify: 'setup', night: 'setup', cloud: 'setup', sources: 'setup',
};

// Where a name that is not a step should take you. `showTab` is called with a
// panel name from a few places - a finished sweep opens Results, a save error
// opens Search - and those care about the panel, not about the grouping.
const stepFor = (name) => STEP_OF[name] ?? name;

/* The step on screen. Read by `loadScenario`, which has to redraw whatever that
   is: every step renderer runs from `showTab` and from nowhere else, so a step
   you were already standing on was never told the trip under it had changed. */
let activeStep = 'map';

/* Which of Map it out's three views is on screen.

   The step tabs say which decision you are on; this row says which of that
   step's answers you are reading. Only Map it out has one, and its three are
   the form, what a probe judged, and what a sweep found.

   The last two used to live on the narrowing step behind a switch between
   "the whole window" and "just what I chose". That switch is gone. It put the
   results of a sweep over the whole window on the tab named for cutting the
   window down, and it made four panels exist twice - once per population -
   which is what "where does this apply to?" was asking about. The broad runs
   now answer beside the buttons that start them; the narrowing step is the
   narrowed runs and nothing else. */
let activeSub = 'options';

const SUB_OF = {
  search: 'options',
  explore: 'probe',
  'broad-charts': 'results', results: 'results',
};

// Which sub-tab a panel name belongs to, or null for a panel on a step that
// has no second row. `showTab` uses it to land a named panel on the view that
// actually contains it - a finished sweep opens Results, and Results is now
// one of three things Map it out can be showing.
const subFor = (name) => SUB_OF[name] ?? null;

function showSub(next) {
  activeSub = next;
  for (const button of $('subtabs').querySelectorAll('button[data-sub]')) {
    button.classList.toggle('is-active', button.dataset.sub === next);
  }
  for (const section of document.querySelectorAll('section[data-sub]')) {
    section.hidden = section.dataset.step !== activeStep || section.dataset.sub !== next;
  }
  // Re-drawn and not merely unhidden. A chart drawn into a hidden section
  // measures its container at zero width, so a view that was hidden when its
  // data arrived has axes and nothing else.
  if (next === 'probe') return Promise.resolve(renderExplore());
  if (next === 'results') return renderSearchResults();
  return Promise.resolve();
}

$('subtabs').onclick = (event) => {
  const button = event.target.closest('button[data-sub]');
  if (button) showSub(button.dataset.sub);
};

/* What a sweep found: every leg priced on its own, then the same runs ranked.
   Both read the broad population, and both are on Map it out because that is
   where the sweep that made them is started. */
async function renderSearchResults() {
  await renderLegStep(NARROW_CHARTS);
  await renderResults(BROAD_VIEW);
  await renderCloudSync();
}

function showTab(name) {
  const step = stepFor(name);
  activeStep = step;
  for (const b of $('tabs').querySelectorAll('button[data-tab]')) {
    b.classList.toggle('is-active', b.dataset.tab === step);
  }
  // Only Map it out has a second row, so the row itself is part of the step.
  $('subtabs').hidden = step !== 'map';
  for (const section of document.querySelectorAll('section[data-panel]')) {
    const wanted = section.dataset.step === step;
    section.hidden = wanted
      ? Boolean(section.dataset.sub) && section.dataset.sub !== activeSub
      : true;
  }
  // A named panel wins over whichever sub-tab was last open: a link into
  // "Search results" must not land on the step still showing the form.
  if (step === 'map') showSub(subFor(name) ?? activeSub);
  if (step === 'narrow') renderNarrowStep();
  if (step === 'follow') { renderWatch(); renderHistory(); }
  if (step === 'setup') {
    renderNightSweep();
    renderCloudRuns();
    renderCloudSync();
    renderSources();
    renderNotifyTarget();
    loadHomeAirports();
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
const route = { origins: [], stops: [], returnTo: [], preferredTiers: [], probeExtra: {} };

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

/* --------------------------------------------------------- ranked airports */

/* Airports in tiers, best first, with two of them on the page.

   One is global - the airports you can get to, easiest first - and one is a
   trip's own override of it. They used to be a flat list and a tier list, on
   two panels, each explaining that it was not the other; and because their
   shapes differed, nothing could inherit anything. Tiering the global one made
   them the same kind of object, which is what lets a trip that says nothing
   simply use it.

   So there is one editor and it is drawn twice. A tier holds airports that are
   equally good - Prague and Vienna are both a morning - and position is the
   whole content of the list. */

const ORDINALS = ['1st choice', '2nd choice', '3rd choice', '4th choice', '5th choice'];

/* Draw a tier list into `host`. `onChange` receives the whole new list.

   `suggest` is what the typeahead offers first, and differs by list: the global
   one suggests the airports your trips already use, and the trip's override
   suggests its own origins. */
function renderTierEditor(host, tiers, onChange, { key, suggest, empty }) {
  host.innerHTML = '';
  // Suggested across the whole list, not per tier. Otherwise every rank offers
  // the airports sitting in the other ranks, which on a three-tier list is
  // three identical rows of one-click chips and most of the panel's height -
  // and each of them offers to *move* an airport, which is not what a row
  // headed Yours: reads as.
  const ranked = new Set(tiers.flat());
  const offer = (suggest ?? []).filter((airport) => !ranked.has(airport.iata));

  tiers.forEach((tier, index) => {
    const block = document.createElement('div');
    block.className = 'tier';

    const heading = document.createElement('div');
    heading.className = 'row';
    heading.innerHTML = `<span class="small muted">${ORDINALS[index] ?? `choice ${index + 1}`}</span>`;
    const drop = document.createElement('button');
    drop.type = 'button';
    drop.className = 'small';
    drop.textContent = 'Remove';
    drop.title = `Remove ${ORDINALS[index] ?? 'this tier'}`;
    drop.onclick = () => onChange(tiers.filter((_, i) => i !== index));
    heading.appendChild(drop);
    block.appendChild(heading);

    const chips = document.createElement('div');
    chips.className = 'chips';
    block.appendChild(chips);
    host.appendChild(block);

    renderChips(chips, tier, (next) => {
      // An airport in two tiers makes "the best tier holding it" ambiguous, and
      // both the scenario and the ranking file reject it on save. Drop it from
      // the others rather than letting a save fail on something the editor
      // could see coming.
      onChange(tiers.map((other, i) =>
        (i === index ? next : other.filter((code) => !next.includes(code)))));
    }, { key: `${key}-${index}`, suggest: offer });
  });

  if (!tiers.length) {
    host.innerHTML = `<p class="empty small">${escapeHtml(empty)}</p>`;
  }
}

function renderPreferred() {
  renderTierEditor($('preferred-tiers'), route.preferredTiers, (next) => {
    route.preferredTiers = next;
    renderPreferred();
  }, {
    key: 'tier',
    suggest: state.frequent.origins,
    empty: 'Nothing set, so this trip uses Your airports above.',
  });
  renderInheritedNote();
}

/* Whether this trip is following the global list, said where the override is.

   An empty override is the normal case and used to read as "no preference —
   only the cheapest is ever reported", which stopped being true the moment a
   trip could inherit. */
function renderInheritedNote() {
  const line = $('preferred-inherited');
  if (route.preferredTiers.length) {
    line.textContent = 'This trip uses the tiers above, not Your airports.';
    return;
  }
  line.textContent = home.tiers.length
    ? `Following Your airports: ${home.tiers.map((tier) => tier.join(' / ')).join(' → ')}.`
    : 'Neither this trip nor Your airports ranks anything, so only the cheapest '
      + 'trip is ever reported.';
}

$('add-tier-btn').onclick = () => {
  route.preferredTiers.push([]);
  renderPreferred();
};
$('preferred-save-btn').onclick = saveTrip;

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
  route.probeExtra = Object.fromEntries(
    Object.entries(scenario.probe_extra ?? {}).map(([key, codes]) => [key, [...codes]]),
  );

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
  // Filled here and nowhere else, because it is a field of this form now. Set
  // only by the narrowing step's own renderer, it read false on every load
  // until that step was visited - so `isDirty` compared a false against the
  // trip's true and every trip on disk opened claiming unsaved changes.
  //
  // Default true for every trip written before the field existed, matching the
  // schema - `plan_sweep --final` used to select on having a narrowing at all,
  // so reading a missing value as false would show every committed trip as
  // opted out of runs it is in fact still getting.
  $('sweep-narrowing').checked = scenario.sweep_narrowing !== false;
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
    // Both scheduled runs are configured in one panel now, so both are fields
    // of the trip form that panel saves. It was a tick fenced inside the date
    // boxes on another step, written only by that panel's own Save - which is
    // why the two runs a trip makes on its own were set up two tabs apart.
    sweep_narrowing: $('sweep-narrowing').checked,
    probe_both_orders: $('probe-both-orders').checked,
    // An empty tier is a ranking with a hole in it and the scenario rejects it,
    // so a row you added and never filled is simply dropped on save.
    preferred_origins: route.preferredTiers.filter((tier) => tier.length),
    // An empty list and a missing key are the same fact, and keeping both would
    // make the trip file differ from itself over a distinction with no meaning.
    probe_extra: Object.fromEntries(
      Object.entries(route.probeExtra).filter(([, codes]) => codes.length),
    ),
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

/* What each button in the run row costs, written on the button itself.

   This was one badge in the panel header, priced at whatever the depth select
   happened to be set to, plus a separate sentence for the probe. The select is
   gone - depth is now which button you press - so each button carries its own
   number and the row can be read without changing a control to find out what
   the other option would cost.

   Two calls, because two sweep depths are on screen at once. They are cheap:
   the estimator plans the run without searching anything. And they answer the
   question the row actually asks, which is whether a deep sweep is worth four
   times a quick one on this trip. */
async function refreshEstimate() {
  const costs = { quick: 'quick-cost', deep: 'deep-cost' };
  renderScheduleSummary();
  // A trip that has never been saved has no file to estimate against, and the
  // endpoint is keyed on the id. Say what is missing instead of 404ing.
  if (state.isNew) {
    for (const id of ['probe-cost', ...Object.values(costs)]) {
      $(id).textContent = 'save the trip first';
    }
    return;
  }
  renderDirty();
  refreshExploreCost();
  const price = async (depth) => {
    const body = await api(
      `/api/scenarios/${state.scenario.id}/estimate?depth=${depth}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formToScenario()) },
    );
    return `${count(body.searches)} searches · ~${body.minutes} min`;
  };
  for (const [depth, id] of Object.entries(costs)) {
    try {
      $(id).textContent = await price(depth);
    } catch (error) {
      $(id).textContent = error.message;
    }
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
  $('probe-cost').textContent = state.exploreCost;
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
      `<span class="night-list__cost">${slot.mode === 'final'
        ? 'only what you narrowed to'
        : 'the whole date window'}</span></div>`).join('')
    : '<span class="muted">The schedule could not be read from the workflow file.</span>';

  const warning = $('cloud-unknown');
  warning.hidden = body.known;
  if (!body.known) {
    warning.textContent = `${body.reason} What is scheduled above is read from the ` +
      'workflow file and is still accurate.';
  }

  const held = body.queued ?? [];
  // On the summary, because the list is behind a click now and a queue you
  // cannot see is a delay nobody asked for. Opened as well when anything is in
  // it: a held run is the answer to "why has nothing dispatched".
  $('cloud-queue-count').textContent = held.length ? `· ${count(held.length)}` : '';
  if (held.length) $('cloud-queue-box').open = true;
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
  // The other half of the same question. A checkout with commits of its own
  // cannot fast-forward and should not try - but that refusal is about history,
  // and the runs behind it are directories this machine does not have. Copying
  // those needs no merge, so the sentence stops appearing with nothing under it.
  const takeable = Boolean(body.known && missing > 0 && !body.can_fast_forward);

  for (const [box, text, button, take] of [
    ['cloud-sync', 'cloud-sync-text', 'cloud-sync-get', 'cloud-sync-take'],
    ['cloud-sync-cloud', 'cloud-sync-cloud-text', 'cloud-sync-cloud-get', 'cloud-sync-cloud-take'],
    ['final-cloud-sync', 'final-cloud-sync-text', 'final-cloud-sync-get', 'final-cloud-sync-take'],
  ]) {
    if (!$(box)) continue;
    $(box).hidden = !show;
    $(text).textContent = message;
    $(button).hidden = !actionable;
    if ($(take)) $(take).hidden = !takeable;
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

/* `path` is either the fast-forward or the copy-the-directories-only route.
   The two differ in what they move and in nothing else the page cares about:
   both answer with the runs that arrived, and both can refuse with a sentence
   git wrote. `gained` is the sync's word for it and `taken` is the take's. */
async function pullCloudResults(button, path = '/api/cloud-sync') {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Getting…';
  try {
    const body = await api(path, { method: 'POST' });
    // The pull can bring a newer src/web/app.py with it, which this running
    // server has already imported. `contractMatches` is the existing handshake
    // for exactly that, and its message - restart the server - is the right one.
    if (!(await contractMatches())) return;
    await renderCloudSync();

    const gained = Object.values(body.gained ?? body.taken ?? {}).reduce((n, v) => n + v.length, 0);
    // Land on the newest run, but only when one actually arrived: moving the
    // picker under someone who is reading a specific sweep is its own bug.
    if (gained) { state.stamp = null; state.finalStamp = null; state.watchStamp = null; }
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

for (const id of ['cloud-sync-get', 'cloud-sync-cloud-get', 'final-cloud-sync-get']) {
  if ($(id)) $(id).onclick = (event) => pullCloudResults(event.target);
}
for (const id of ['cloud-sync-take', 'cloud-sync-cloud-take', 'final-cloud-sync-take']) {
  if ($(id)) $(id).onclick = (event) => pullCloudResults(event.target, '/api/cloud-sync/take');
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
      `${localStamp(slot.next)}${slot.mode === 'final'
        ? ' (only what you narrowed to)'
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
    // "Not on the branch" and "cannot read the branch" were one message, and
    // only the first of them is a thing to act on. A checkout with no remote
    // was being told to push a file it may well already have pushed.
    warning.innerHTML = cloud.on_branch === false
      ? `The night sweep runs the trips committed to <code>${ref}</code>, and this one is ` +
        `not on it. Tonight will not include it whatever the box above says — commit and ` +
        `push <code>scenarios/${escapeHtml(trip.id)}.json</code>.`
      : `This app cannot read <code>${ref}</code>, so it cannot say whether tonight's sweep ` +
        `is about this version of the trip — or about it at all.`;
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
  //
  // An open dialog wins outright. It is modal, so a message routed to a panel
  // behind it is a message on a part of the page that cannot be seen or
  // scrolled to, and the button reads as having done nothing - which is the
  // failure this whole function exists to prevent.
  const dialog = document.querySelector('dialog[open]');
  const acted = document.activeElement?.closest('section[data-panel]:not([hidden])');
  const panel = dialog ?? acted ?? document.querySelector('section[data-panel]:not([hidden])');
  const box = panel?.querySelector('.panel-error') ?? $('save-error');
  box.textContent = message;
  box.className = `panel-error badge badge--${tone}`;
  box.hidden = false;
  box.scrollIntoView({ block: 'nearest' });
  return box;
};

/* A handler that forgot to catch must not be able to fail in silence.

   The trip picker's `onchange` did exactly that: three awaited calls and no
   `catch`, so with the server stopped the rejection went nowhere and switching
   trips looked like a picker that had quietly refused to load one. Every such
   handler could be found and fixed, and the ones below were - but the class of
   bug is "somebody will add another one", so it is caught here as well.

   `preventDefault` only to keep the console clean; the message is the point. */
window.addEventListener('unhandledrejection', (event) => {
  const message = event.reason?.message ?? String(event.reason ?? 'unknown error');
  showError(message);
  event.preventDefault();
});

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
  state.finalStamp = null;
  state.watchStamp = null;
  state.sweeps = [];
  clearFilters();
  state.scenario = blankScenario();
  fillForm(state.scenario);
  $('scenario-select').value = '';
  updateNewTripUi();
  for (const id of ['probe-cost', 'quick-cost', 'deep-cost']) {
    $(id).textContent = 'add airports, then save';
  }
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
  // Nothing to sweep until the trip exists on disk. The narrow sweep's two
  // buttons are in this list because they are runs of the same trip: they
  // arrived on the narrowing panel from a step of their own and were left out,
  // so a brand new trip offered to sweep a file that did not exist yet.
  // `refreshFinalCost` disables them too, for the different reason that nothing
  // has been narrowed - whichever reason applies, they must not be pressable.
  for (const id of [
    'run-quick-btn', 'run-deep-btn', 'explore-btn', 'explore-run-btn', 'final-run-btn',
  ]) {
    $(id).disabled = state.isNew;
  }
  $('delete-trip-btn').textContent = state.isNew ? 'Discard' : 'Delete';
}

/* A run searches the trip on disk, so the edits on screen have to reach disk
   before it starts. Without this the app happily spent two 25-minute probes
   searching the previous day's airports and reported on them as if they were
   the trip — the tool being confidently wrong, which is worse than no tool.

   A trip that will not save does not run: the reason appears on whichever tab
   the button was pressed from. */
/* The depth a run nobody is watching uses. The manual buttons each carry their
   own, so this is read only by the scheduled sweep and by a resume of one. */
const scheduledDepth = () => $('depth').value;

async function startRun(mode, depth = scheduledDepth()) {
  clearError();
  if (isDirty() && !(await saveTrip())) return false;
  try {
    // Which depth picker to read. The Final sweeps step has its own, defaulted
    // to `deep`: a five-day departure window stepped every seven days prices one
    // date, and a narrowed sweep that samples one date is not a measurement.
    await api(
      `/api/scenarios/${state.scenario.id}/run?depth=${depth}&mode=${mode}`,
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

/* Every run in this row goes to the cloud, and the reason is not convenience.
   pelikan.cz answers about 120 searches from one address before it stops
   answering at all, and a deep sweep of a three-leg trip is three hundred. This
   machine is one address; the cloud shards the plan across runners so that no
   one of them is handed more than the cap. A sweep started here would half-fail
   every time, and a sweep that half-fails is worth less than a shallow one that
   works. Running on this machine is still possible from the API, and it is what
   `make run` is for. */
const cloudRun = (mode, depth) => async () => {
  clearError();
  // The cloud reads the trip out of the repo, so an unsaved edit is even
  // further from what would be searched than it is locally.
  if (isDirty() && !(await saveTrip())) return;
  await dispatchCloudRun(false, mode, depth);
};

for (const id of ['run-quick-btn', 'run-deep-btn']) {
  $(id).onclick = cloudRun('sweep', $(id).dataset.depth);
}

/* The probe goes up too, and always could have: `explore` is one of MODES, the
   run-cloud endpoint accepts it, and the workflow forwards the mode verbatim.
   It ran here on the reasoning that a probe is what you press on a trip you
   have just typed in, and the cloud can only search what is committed - which
   is true, and is what the branch check already says, by name, before anything
   is dispatched.

   What that reasoning left out is the cost. A probe of this trip is ~51
   searches against the ~120 pelikan.cz answers from one address in a day, so
   running it here spends nearly half the machine's budget on the cheapest
   question the app asks. The cloud has its own address and its own 120. */
$('explore-btn').onclick = cloudRun('explore');

/* Both stop buttons, driven together so neither can disagree with the other
   about whether a run is going. */
const STOP_BUTTONS = ['stop-btn', 'run-stop-btn', 'final-stop-btn'];

const stopButtons = (fn) => { for (const id of STOP_BUTTONS) { if ($(id)) fn($(id)); } };

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
async function dispatchCloudRun(force, mode = 'sweep', depth = scheduledDepth()) {
  const query =
    `depth=${depth}&mode=${mode}${force ? '&force=true' : ''}`;
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

Run it anyway?`)) return dispatchCloudRun(true, mode, depth);
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
      populateSweepSelects();
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
        // Onto the picker that lists this kind of run. Setting `state.stamp` to
        // a final run would be setting the broad table's selection to something
        // its own picker does not offer, and the next redraw would snap it back.
        if (latest.mode === 'final') state.finalStamp = latest.stamp;
        else state.stamp = latest.stamp;
        state.exploreStamp = latest.stamp;
        populateSweepSelects();
        // A probe answers under Probe results, a narrowed sweep on the
        // narrowing step, a broad sweep under Search results. Show whichever
        // one the run you just watched actually filled in - `showTab` opens the
        // sub-tab for you, because the panel it is asked for knows which of
        // Map it out's three views it lives on.
        showTab({ explore: 'explore', final: 'final-results' }[latest.mode] ?? 'results');
        return;
      }
      populateSweepSelects();
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
/* What kind of run this is, in one word, for the label every picker shares.

   A probe and a stopped run are both "not a sweep of this trip", and reading
   either as one is how a headline price ends up 7,000 Kč wrong. A final sweep
   is a third case and the subtlest: it *is* a real sweep of this trip, priced
   at the same depth, and its only difference is that it looked at a fifth of
   the days. The depth alone - "deep" against "deep" - says nothing about that,
   which is why the word is in front of it rather than instead of it. */
const kindOf = (sweep) => {
  if (sweep.mode === 'explore') return 'probe';
  const depth = sweep.depth ?? '?';
  return sweep.mode === 'final' ? `final · ${depth}` : depth;
};

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
  //
  // Collapsed back to the old wording when everything differs, which on real
  // data is most rows: japan-philippines has fifteen sweeps flagging all three
  // at once, and `⚠ different airports · different stays · different window`
  // three times down a picker is a wall to skim past rather than a warning.
  // Three is everything `_differs_from_live` checks; a fourth check belongs in
  // this count too.
  const differs = sweep.differs || [];
  const flagged = [
    ...(differs.length >= 3 ? ['a different trip'] : differs.map((w) => `different ${w}`)),
    ...warnings,
  ].filter(Boolean);
  return parts.join(' · ') + (flagged.length ? ` · ⚠ ${flagged.join(' · ')}` : '');
};

/* The runs one view's picker may offer.

   A broad sweep and a final sweep answer different questions of the same trip,
   and the step you are on says which you are asking. Mixing them in one picker
   was the alternative and it is worse than it sounds: the two look identical in
   a list - same trip, same depth, same day - and only the search count hints
   that one of them priced a fifth of the window. Selecting the wrong one puts a
   narrowed run's cheapest under a heading that says it is the trip's cheapest.

   A run with no mode recorded predates the split and priced whatever the trip
   said at the time, which is what `sweep` meant then. */
const runsFor = (view) =>
  state.sweeps.filter((sweep) => view.modes.includes(sweep.mode ?? 'sweep'));

function populateSweepSelect(view = BROAD_VIEW) {
  const select = view.$('sweep-select');
  if (!select) return;
  select.innerHTML = '';
  const offered = runsFor(view);
  for (const sweep of offered) {
    const option = document.createElement('option');
    option.value = sweep.stamp;
    // Depth and search count are part of the label because a 20-search smoke
    // test and a 204-search real sweep are otherwise indistinguishable here -
    // and reading the wrong one produced a headline price 7,000 Kč too high.
    const dark = (sweep.routes_with_no_results || []).length;
    option.textContent = sweepLabel(sweep, kindOf(sweep), [
      sweep.state === 'stopped' ? `stopped at ${sweep.completed ?? '?'}` : '',
      dark ? `${dark} dead route(s)` : '',
    ]);
    select.appendChild(option);
  }
  // An empty picker is a blank box beside a heading, which reads as a control
  // that lost its contents rather than as a step with no runs yet. The empty
  // state under the table already says which of those it is.
  select.hidden = !offered.length;
  // Selected against what this picker actually offers. Defaulting to
  // `state.sweeps[0]` would pick a run the list does not contain the moment the
  // newest run is of the other kind, and the picker would show a blank row.
  if (!offered.some((sweep) => sweep.stamp === view.stamp)) {
    view.stamp = offered.length ? offered[0].stamp : null;
  }
  if (view.stamp) select.value = view.stamp;
}

/* Every picker that lists runs, refilled together.

   They must be: a run of either kind arrives in one list and can leave the
   other's selection pointing at a stamp its picker no longer offers, which
   renders as a blank row above a table full of some other run's prices. */
function populateSweepSelects() {
  for (const view of RESULT_VIEWS) populateSweepSelect(view);
}

$('sweep-select').onchange = (event) => {
  state.stamp = event.target.value;
  renderResults();
  // Follow into the charts only when they can draw it. A probe picked here is
  // a deliberate choice - the panel says so in words - and dragging the charts
  // onto it would either blank them or, worse, have `renderNarrow` pull this
  // selection straight back and undo the click.
  const chosen = state.sweeps.find((s) => s.stamp === state.stamp);
  if (chosen && chosen.has_legs && chosen.mode !== 'explore') renderLegStep(NARROW_CHARTS);
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

function renderVerification(report, view) {
  const host = view.$('verification');
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

/* The inverse of `removeFromTrip`, for the airport you kept probing and have
   now decided to search again.

   Leaves it unsaved in the route editor, exactly as removing does: a probe is a
   reason to look, not a decision, and the same button that puts an airport back
   should not commit the trip behind you. */
function putBackInTrip(pool, code) {
  const add = (list) => (list.includes(code) ? list : [...list, code]);
  if (pool.role === 'origins') route.origins = add(route.origins);
  else if (pool.role === 'return_to') route.returnTo = add(route.returnTo);
  else if (pool.role === 'stop') {
    const stop = route.stops[pool.stop_index];
    if (stop) stop.airports = add(stop.airports);
  }
  // It is no longer being dropped, whatever it was doing a moment ago.
  for (let i = pending.length - 1; i >= 0; i -= 1) {
    if (pending[i] === code) pending.splice(i, 1);
  }
  renderRoute();
  scheduleEstimate();
  renderPending();
  renderExplore();
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

// The same run as the button on the Run row, so the same destination. Two
// buttons that say "run a probe" and run it in two different places is a
// question nobody should have to ask of a button.
$('explore-run-btn').onclick = cloudRun('explore');

/* Whether the *edited* trip still contains this airport. The report describes
   a sweep, so it keeps listing airports you have just dropped; without this
   they would sit there still offering a Remove button that does nothing. */
function stillInTrip(pool, code) {
  if (pool.role === 'origins') return route.origins.includes(code);
  if (pool.role === 'return_to') return route.returnTo.includes(code);
  return Boolean(route.stops[pool.stop_index]?.airports.includes(code));
}

/* ------------------------------------------------------- the probe list ---

   Airports the probe keeps asking about after the trip has stopped searching
   them. Typed, never inferred: removing an airport from the route removes it
   from the trip and does nothing else, because a list that grew by itself would
   walk a ~51-search probe up toward the cost of the sweep it exists to avoid.

   Saved on the spot rather than through the trip form, like the narrowing
   panel's own fields: this panel is two tabs from the route editor and its
   edits are decisions in themselves. */

const probeList = (key) => route.probeExtra[key] ?? [];

/* Edit the draft, not the file. The pending bar's Save commits it along with
   whatever airports were dropped in the same pass, so one press of Save means
   one coherent trip - which is the whole reason `removeFromTrip` leaves its
   edit pending in the first place. */
function setProbeList(key, codes) {
  if (codes.length) route.probeExtra[key] = codes;
  else delete route.probeExtra[key];
  renderDirty();
  // The probe costs more when it is asked to watch more, and the number on the
  // button is the whole reason the trade is visible before it is made.
  scheduleEstimate();
  renderExplore();
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
  const watched = probeList(pool.key).includes(row.iata);
  const button = (label, title, onclick) => {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'small';
    element.textContent = label;
    element.title = title;
    element.onclick = onclick;
    return element;
  };
  if (dropped) {
    action.innerHTML = '<span class="muted small">dropped</span>';
  } else if (outside) {
    // The row this whole change is about: an airport the sweep no longer
    // searches. Said, and then both ways out of it - the state first, because
    // two buttons alone leave a reader working out from the verbs what the row
    // is, and that is the question the row exists to answer.
    const state = document.createElement('span');
    state.className = 'muted small';
    state.textContent = watched ? 'not in this trip · still probed' : 'not in this trip';
    action.appendChild(state);
    action.appendChild(button(
      watched ? 'Stop probing' : 'Keep probing',
      watched
        ? 'Stop asking about this airport, and let this verdict go stale'
        : 'Go on pricing this airport on every probe, without sweeping it',
      () => setProbeList(
        pool.key,
        watched
          ? probeList(pool.key).filter((code) => code !== row.iata)
          : [...probeList(pool.key), row.iata],
      ),
    ));
    action.appendChild(button('Put it back', 'Search this airport again', () => {
      putBackInTrip(pool, row.iata);
    }));
  } else if (DROPPABLE.has(row.verdict)) {
    // Two ways out, offered together, because this is the only moment the
    // choice can be made. A row that leaves the trip and is not on the probe
    // list leaves the table with it - so an airport dropped here and reconsidered
    // later could not be found again, and `Keep probing` on an outside row was a
    // button nothing could ever reach.
    action.appendChild(button(
      'Remove from trip',
      'Stop searching it and stop pricing it',
      () => removeFromTrip(pool, row.iata),
    ));
    action.appendChild(button(
      'Keep probing it',
      'Stop searching it, but go on pricing it on every probe so this verdict stays current',
      async () => {
        removeFromTrip(pool, row.iata);
        setProbeList(pool.key, [...probeList(pool.key), row.iata]);
      },
    ));
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
    const kind = sweep.mode === 'explore' ? 'probe' : `${kindOf(sweep)} sweep`;
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
  $('explore-one-run').hidden = !anything;
  $('explore-run-note').textContent = state.exploreCost ?? '';
  if (!anything) {
    $('explore-verdicts').innerHTML = '';
    $('explore-source').textContent = 'nothing measured yet';
    $('explore-report').innerHTML = '';
    $('explore-intro').textContent = '';
    $('explore-mismatch').hidden = true;
    $('explore-coverage').hidden = true;
    return;
  }
  // Two independent reads, fetched together. The merged table is what the tab
  // opens on; the single-run report is folded shut below it and still has to be
  // right when it is opened.
  await Promise.all([renderAirportVerdicts(), renderOneRun()]);
}

async function renderOneRun() {
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

/* Every airport of the trip, judged by the best run that measured it.

   The tab used to open on a picker of runs by date and time, so answering "is
   Vienna worth keeping" began with choosing which of twenty-two runs to ask -
   and whichever you chose, a route it never reached read as an airport with
   nothing to say for itself. The server picks per pool now, so the rows in one
   table are always scored against one baseline; this only has to say which run
   each pool came from, because a verdict is worth exactly as much as the run
   behind it. */
async function renderAirportVerdicts() {
  const host = $('explore-verdicts');
  const badge = $('explore-source');
  let body;
  try {
    body = await api(`/api/scenarios/${state.scenario.id}/airport-verdicts`);
  } catch (error) {
    host.innerHTML = '';
    badge.textContent = 'could not read';
    $('explore-empty').hidden = false;
    $('explore-empty').textContent = `Could not judge these airports — ${error.message}`;
    return;
  }

  // Not a count of runs. The server stops reading as soon as no older run could
  // beat what it has, so `runs_read` is a fact about the search and not about
  // the evidence - it says 1 on a trip with twenty-two sweeps behind it. Each
  // pool names its own run below, which is the number that means something.
  const measured = body.pools.filter((pool) => pool.measured_by).length;
  badge.className = `badge badge--${measured ? 'good' : 'muted'}`;
  badge.textContent = measured
    ? 'judged from your runs on disk'
    : 'nothing measured yet';

  renderMergeLine(body.pools);

  host.innerHTML = '';
  for (const pool of body.pools) {
    const block = document.createElement('div');
    block.className = 'panel__section';

    const heading = document.createElement('h3');
    heading.textContent = pool.role === 'origins' && pool.index === 0
      ? 'Flying from'
      : `${pool.label}`;
    block.appendChild(heading);

    // Which run this pool's numbers are from, on the pool rather than on the
    // panel: they genuinely can differ, and a single line at the top claiming
    // one run for all of them would be the lie this endpoint exists to avoid.
    const from = document.createElement('p');
    from.className = 'answer small';
    if (pool.measured_by) {
      const run = pool.measured_by;
      const kind = run.mode === 'explore' ? 'probe' : `${run.depth ?? '?'} sweep`;
      const holes = run.coverage != null && run.coverage < 1
        ? ` — it answered ${Math.round(run.coverage * 100)}% of its plan, so a thin row here may be the site rather than the airport`
        : '';
      from.innerHTML =
        `From the ${escapeHtml(kind)} of ${escapeHtml(localStamp(run.stamp))}` +
        `${escapeHtml(holes)}.`;
    } else {
      from.textContent = 'No run on disk has priced any of these.';
    }
    block.appendChild(from);

    if (pool.airports.length) {
      const scroll = document.createElement('div');
      scroll.className = 'table-scroll';
      const table = document.createElement('table');
      table.className = 'data';
      table.innerHTML =
        '<thead><tr><th>Airport</th><th>Verdict</th><th class="num">Cheapest in</th>' +
        '<th class="num">Cheapest out</th><th class="num">Together</th>' +
        '<th class="num">vs best</th><th class="num">Searches</th><th></th></tr></thead>';
      const tbody = document.createElement('tbody');
      // The same row builder the single-run report uses, so a verdict reads the
      // same and "Remove from trip" behaves the same wherever it is met.
      for (const row of pool.airports) tbody.appendChild(exploreRow(pool, row, body.currency));
      table.appendChild(tbody);
      scroll.appendChild(table);
      block.appendChild(scroll);
    }

    // Answered rather than left to inference, exactly as the single-run report
    // does it: the absence of a row is not an answer to "what about this one?".
    if (pool.not_searched?.length) {
      const missing = document.createElement('p');
      missing.className = 'answer small';
      missing.innerHTML =
        `<strong>${escapeHtml(pool.not_searched.join(', '))}</strong> ` +
        (pool.measured_by
          // Added to the trip since the run these rows come from, so there is
          // no row to put it in rather than a verdict to report.
          ? 'is not in the run these rows come from — run a probe to price it.'
          : 'has never been priced by any run on disk — run a probe.');
      block.appendChild(missing);
    }

    block.appendChild(probeListEditor(pool));
    host.appendChild(block);
  }

  renderUnusedProbeList(body.probe_extra_unused ?? {});

  // Offered only when there is enough table for it to be worth one. On a
  // two-airport trip the box is another control between the question and the
  // answer.
  const rows = host.querySelectorAll('tbody tr').length;
  $('explore-filter-row').hidden = rows < 6;
  if (rows < 6) $('explore-filter').value = '';
  filterVerdicts();
}

/* The typed list, under the pool it belongs to.

   The row buttons above edit the same thing one airport at a time, which is the
   ergonomic path when you are looking at the verdict that made you decide. This
   is the field itself, for adding an airport the trip has never held - a nearby
   one you want measured before you would consider it. */
function probeListEditor(pool) {
  const row = document.createElement('div');
  row.className = 'row probe-list';

  const label = document.createElement('span');
  label.className = 'small muted';
  label.textContent = 'Also probe';
  label.title = 'Priced on every probe, never swept. Costs a few searches each.';
  row.appendChild(label);

  const chips = document.createElement('div');
  chips.className = 'chips';
  row.appendChild(chips);

  renderChips(chips, probeList(pool.key), (next) => {
    setProbeList(pool.key, next);
    // Suggested by what kind of pool this is, exactly as the route editor does
    // it: offering Brno and Prague under *Japan* is a list of airports nobody
    // would ever add there.
  }, {
    key: `probe-${pool.key}`,
    suggest: pool.role === 'stop' ? state.frequent.destinations : state.frequent.origins,
  });

  return row;
}

/* A probe list naming a pool the trip no longer has.

   Kept on disk on purpose - a stop removed for an afternoon should not cost the
   list - so something has to say it is being kept and not used. Otherwise it is
   a saved setting that has silently stopped applying, which is the shape of bug
   this panel keeps being redesigned around. */
function renderUnusedProbeList(unused) {
  const host = $('explore-orphans');
  const keys = Object.keys(unused);
  host.hidden = !keys.length;
  if (!keys.length) return;
  const said = keys.map((key) => `${unused[key].join(', ')} (${key})`).join('; ');
  host.innerHTML =
    `Kept but not probed: <strong>${escapeHtml(said)}</strong> — the trip has no such ` +
    'stop any more. Add the stop back and they are probed again; nothing here is lost ' +
    'in the meantime.';
}

/* Which way round to fly, when a probe was asked to sample both.

   The figure is the cheapest leg seen on each hop, added up. That is a lower
   bound and not a trip — three dates a leg almost never chain, which is why the
   Results tab refuses to draw probe legs as itineraries at all — so the heading
   says so, rather than letting a total that looks bookable sit unqualified. In
   the heading and not the paragraph under it, because the paragraph collapses
   with the rest of the teaching and this is the one part of it that must not:
   the figure reads as a price you could pay.

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
    '<h3 class="subhead">Which way round — cheapest legs added up, not a trip you could ' +
    'book</h3>' +
    `<table class="data"><tbody>${rows}</tbody></table>` +
    '<p class="explain small">Three dates a leg almost never chain, which is why the ' +
    'ranking refuses to draw probe legs as itineraries at all. It is still the right ' +
    'comparison: both orders were sampled on the same dates by the same run, so whatever ' +
    'it leaves out, it leaves out of both.</p>';

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

  // What *this* run is, and nothing about how to read a verdict: that sentence
  // moved to the panel's own hint, above the merged table, where it is said
  // once for both tables rather than twice.
  const probe = body.mode === 'explore';
  $('explore-intro').innerHTML =
    (probe
      ? 'A probe, not a sweep: every route on three spread-out dates. '
      : 'A full sweep, so these prices come from many more dates than a probe. ') +
    (body.state === 'stopped' ? '<strong>This run was stopped early</strong>, so some routes went unasked.' : '') +
    (body.state === 'throttled' ? '<strong>The site stopped answering during this run</strong>, so treat thin rows as unmeasured.' : '');

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
      missing.className = 'answer small';
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

/* Two readings of two kinds of run, drawn by one set of renderers.

   "Narrow it down" reads the broad sweeps — the whole window, which is what you
   pick a trip out of. "Final sweeps" reads the narrowed ones, which re-price
   that pick every few hours. They ask the same questions of different runs, so
   duplicating six hundred lines of renderer per tab was never the answer; each
   view says which ids it owns, which run it has selected, and which runs its
   picker may offer.

   The ids differ only by the prefix, so a panel added to one view and forgotten
   in the other shows up as a missing element rather than as the two views
   quietly sharing a control. */
function resultsView({ prefix, stampKey, filtersKey, modes }) {
  return {
    prefix,
    modes,
    // The leg charts this view's rows lift onto, assigned once both halves
    // exist. Every view has a set now; while the final step had none, a row of
    // its ranking was a dead end.
    legs: null,
    $: (id) => $(prefix + id),
    get stamp() { return state[stampKey]; },
    set stamp(value) { state[stampKey] = value; },
    get filters() { return state[filtersKey]; },
    set filters(value) { state[filtersKey] = value; },
  };
}

// The broad half of the app, which includes the probe: it samples the whole
// window, it is started from Map it out, and this table has a branch that says
// what a probe is when one is picked. Only the leg charts refuse it, and they
// filter it out themselves - a probe prices three dates a leg, which draws a
// line that looks like a price curve and is not one.
const BROAD_VIEW = resultsView({
  prefix: '', stampKey: 'stamp', filtersKey: 'filters',
  modes: ['sweep', 'explore'],
});
const FINAL_VIEW = resultsView({
  prefix: 'final-', stampKey: 'finalStamp', filtersKey: 'finalFilters',
  modes: ['final'],
});
const RESULT_VIEWS = [BROAD_VIEW, FINAL_VIEW];

/* A filter belongs to the trip it was set on. Carrying "from FRA" onto a trip
   that never flies from Frankfurt shows an empty table with a control the reader
   did not set, which reads as a trip with no results. */
const clearFilters = () => {
  for (const view of RESULT_VIEWS) {
    view.filters = { from: '', to: '', bags: false, allWindow: false };
  }
};

const filterQuery = (view) => {
  const params = new URLSearchParams();
  if (view.filters.from) params.set('from_airport', view.filters.from);
  if (view.filters.to) params.set('to_airport', view.filters.to);
  if (view.filters.bags) params.set('bags', 'true');
  // Only ever sent when asked for. `narrow` is the server's default, so a page
  // that never sends this behaves exactly as it did before the toggle existed.
  if (view.filters.allWindow) params.set('window', 'all');
  const query = params.toString();
  return query ? `?${query}` : '';
};

/* Fill the two pickers and say what the narrowing is doing.

   The options come from the trip's own airports rather than from the rows on
   screen. Pruning can leave an airport out of an unfiltered traversal while it
   still has trips of its own, so building the list from the results would hide
   exactly the choice worth making. An option with nothing behind it says so
   when you pick it. */
function renderFilters(body, view) {
  for (const [id, codes, chosen, blank] of [
    ['filter-from', body.start_airports ?? [], view.filters.from, 'any airport'],
    ['filter-to', body.end_airports ?? [], view.filters.to, 'any airport'],
  ]) {
    const select = view.$(id);
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

  view.$('filter-bags').checked = view.filters.bags;
  view.$('filter-window').checked = view.filters.allWindow;
  // Hidden when there is no narrowing to ignore: a tick that cannot change
  // anything is a control the reader has to rule out before trusting the table.
  const narrowing = body.window ?? {};
  view.$('filter-window-wrap').hidden = !(
    narrowing.applied || view.filters.allWindow
  ) && !(narrowing.focus || narrowing.return_focus || narrowing.total_days);
  view.$('filter-reset').hidden = !body.narrowed && !view.filters.allWindow;

  // Never "34 of 812". Pruning makes the unfiltered traversal a different set
  // rather than a larger one, so the two counts do not form a fraction.
  const trips = `${count(body.matched)} trip${body.matched === 1 ? '' : 's'}`;
  view.$('filter-count').textContent = body.narrowed ? `${trips} match` : trips;

  const note = view.$('filter-note');
  const notes = [];
  if (view.filters.allWindow) {
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
  if (view.filters.bags) {
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

// Bound per view, so the two tables filter independently. They must: "ignore my
// narrowing" means opposite things either side of the split. On a broad sweep it
// reveals the window you did not choose; on a narrowed one there is nothing
// outside the narrowing for it to reveal.
for (const view of RESULT_VIEWS) {
  for (const [id, apply] of [
    ['filter-from', (el) => { view.filters.from = el.value; }],
    ['filter-to', (el) => { view.filters.to = el.value; }],
    ['filter-bags', (el) => { view.filters.bags = el.checked; }],
    ['filter-window', (el) => { view.filters.allWindow = el.checked; }],
  ]) {
    if (!view.$(id)) continue;
    view.$(id).onchange = (event) => {
      apply(event.target);
      renderResults(view);
      // The chart above the table reads the same population or the two disagree
      // about what a departure date costs. Only the broad view has one.
    };
  }
  if (view.$('filter-reset')) {
    view.$('filter-reset').onclick = () => {
      view.filters = { from: '', to: '', bags: false, allWindow: false };
      renderResults(view);
    };
  }
}

async function renderResults(view = BROAD_VIEW) {
  // A view whose panel is not in the page at all - never the case today, but
  // the cheapest guard against a half-added second view drawing into the first.
  if (!view.$('results-table')) return;
  const tbody = view.$('results-table').querySelector('tbody');
  tbody.innerHTML = '';
  view.$('headline').innerHTML = '';

  if (!view.stamp) {
    view.$('results-empty').hidden = false;
    view.$('results-empty').textContent = view === BROAD_VIEW
      ? 'Run a sweep to see itineraries.'
      : 'No narrowed sweep yet. Press “Sweep narrowed” above to price the dates you have chosen.';
    return;
  }

  // A probe prices three dates a leg, so it almost never chains into a whole
  // trip. An empty table here would read as "the probe found nothing" rather
  // than "that is not what it is for", so say where its answer actually is.
  if (state.sweeps.find((sweep) => sweep.stamp === view.stamp)?.mode === 'explore') {
    view.$('verification').innerHTML = '';
    view.$('results-scroll').hidden = true;
    view.$('results-filters').hidden = true;
    view.$('filter-note').hidden = true;
    view.$('results-empty').hidden = false;
    view.$('results-empty').textContent =
      'That run was a probe — it prices a few dates per leg to compare airports, not to '
      + 'build trips. Its verdicts are in the Explore tab.';
    return;
  }
  view.$('results-scroll').hidden = false;
  view.$('results-filters').hidden = false;

  // Every other call site catches; these two did not, so a 500 left a blank
  // table and an explanation only in the browser console.
  let body;
  try {
    body = await api(
      `/api/sweeps/${state.scenario.id}/${view.stamp}/results${filterQuery(view)}`,
    );
  } catch (error) {
    view.$('results-empty').hidden = false;
    view.$('results-empty').textContent = `Could not load results — ${error.message}`;
    return;
  }
  renderFilters(body, view);
  view.$('results-empty').hidden = body.itineraries.length > 0;
  // Three different nothings, and telling them apart is the whole difference
  // between "narrow your filter" and "the scraper is broken".
  view.$('results-empty').textContent = body.narrowed
    ? 'No trip in this sweep matches the filter above.'
    : body.legs_found
      ? 'This sweep found flights, but none of them chain into a complete trip.'
      : 'Run a sweep to see itineraries.';

  renderVerification(body.verification, view);
  view.$('completeness').hidden = true;

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
    view.$('headline').appendChild(card);
  }

  renderCompleteness(body, view);

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

    // Which trip this row is, in the terms the charts above index by. Lets the
    // readout mark its own row without matching on prices or on route strings.
    const dates = itinerary.legs.map((leg) => leg.depart_date);
    row.dataset.dates = dates.join(',');

    // The way back up into the charts. A ranking that can only be read is a
    // dead end: the useful move on seeing a trip you half like is to put it
    // under the markers and drag one leg of it.
    // Onto this step's own charts. Each step draws one population, so a row of
    // the narrowed ranking goes onto the narrowed curves and a broad row onto
    // the broad ones; crossing them would put the markers on another run's
    // prices and read the total off that instead.
    const onCharts = document.createElement('td');
    const charts = view.legs;
    if (charts) {
      const lift = document.createElement('button');
      lift.type = 'button';
      lift.className = 'small';
      lift.textContent = 'Put on the charts';
      lift.title = 'Move the markers above onto this trip’s dates';
      lift.onclick = () => {
        if (!charts.data || !charts.domain.length) return;
        placeCursor(charts, itinerary);
        drawLegCharts(charts);
        charts.$('leg-charts')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      };
      onCharts.appendChild(lift);
    }
    row.appendChild(onCharts);

    const detail = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 8;
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
      // Here rather than only on the Follow step, because this is where you
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
function renderCompleteness(body, view) {
  const host = view.$('completeness');
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
  const narrowing = row.narrowing ?? {};
  if (narrowing.focus) parts.push(`leaving ${narrowing.focus[0]} to ${narrowing.focus[1]}`);
  if (narrowing.return_focus) {
    parts.push(`home ${narrowing.return_focus[0]} to ${narrowing.return_focus[1]}`);
  }
  if (narrowing.total_days) {
    parts.push(`${narrowing.total_days[0]}-${narrowing.total_days[1]} nights`);
  }
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

/* The two charts are drawn separately because they now live in different
   steps, and a chart drawn into a hidden section measures its container at zero
   width. Splitting them also stopped each one paying for the other's fetch:
   opening the narrowing no longer asks for the probe. */

async function renderPrices() {
  // Kept for the theme toggle, which has to redraw whatever is on screen —
  // charts read their colours from the design tokens at draw time.
  await renderHistory();
}


async function renderHistory() {
  if (!state.stamp) return;
  // Said before the wait, not after it. `/api/history` re-reads and re-combines
  // every sweep on disk: measured at 7.0s for a trip with twelve of them, and
  // for all seven of those seconds this panel was a heading over nothing. An
  // empty trend and a trend still being counted look identical, and only one of
  // them means there is nothing to see.
  if (!$('chart-history').childElementCount) {
    $('chart-history').className = 'chart empty';
    $('chart-history').textContent = 'Reading every sweep on disk…';
  }
  // Independent endpoints, fetched together. `allSettled` so one failing panel
  // does not blank the other.
  const [historyResult, probeResult] = await Promise.allSettled([
    api(`/api/history/${state.scenario.id}`),
    api('/api/probe'),
  ]);
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const history = historyResult.status === 'fulfilled' ? historyResult.value : [];
  const width = Math.max(420, $('chart-history').clientWidth - 4);

  $('chart-history').className = 'chart';
  $('chart-history').innerHTML = '';
  // Every sweep is drawn, including the ones too incomplete to trust: the gaps
  // in the record are worth seeing, and a chart that silently dropped them
  // would be its own kind of lie. What comparability buys is the *line* — only
  // trustworthy points are joined solid and eligible for the cheapest label.
  // Drawn through starved sweeps, this chart tracked how well the scraper was
  // working rather than what flights cost.
  const comparable = history.filter((row) => row.comparable);
  // Two lines, never one. A broad sweep's cheapest is the cheapest of the whole
  // window; a final sweep's is the cheapest of the few days you chose out of it,
  // and it is almost always the dearer number. Joined, the pair draws a sawtooth
  // that tracks which slot ran last rather than what anything costs - the same
  // mistake as charting a probe beside a sweep, one level up.
  const line = (name, rows) => ({
    name,
    points: rows.map((row) => ({
      t: isoOf(row.swept_at),
      value: row.best_total,
      muted: !row.comparable,
      note: sweepQuality(row),
    })),
  });
  const broad = history.filter((row) => row.series !== 'final');
  const narrowed = history.filter((row) => row.series === 'final');
  $('chart-history').appendChild(multiLineChart(
    history.length >= 2
      ? [
        line('The whole window', broad),
        line('What you narrowed to', narrowed),
      ].filter((s) => s.points.length)
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
      `<p class="answer">${escapeHtml(probe.recommendation)}</p>`
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
  // Settled before the fetch, so the candidates come from the run the picker is
  // about to show. Filling it afterwards would ask for one run and label it as
  // another on the first render of a page that has never had a selection.
  chooseWatchSweep();
  const [watchResult, candidatesResult] = await Promise.allSettled([
    api(`/api/watch/${state.scenario.id}`),
    state.watchStamp
      ? api(`/api/sweeps/${state.scenario.id}/${state.watchStamp}/candidates`)
      : Promise.resolve({ candidates: [], coverage: null }),
  ]);

  if (watchResult.status !== 'fulfilled') {
    showWatchError(`Could not read what is being watched — ${watchResult.reason.message}`);
    return;
  }
  renderFollowSummary(watchResult.value);
  renderWatched(watchResult.value);
  renderLegWatches(watchResult.value);
  renderPreferenceForm();
  populateWatchSweepSelect();
  renderWatchCandidates(
    candidatesResult.status === 'fulfilled' ? candidatesResult.value : { candidates: [] },
    watchResult.value,
  );
}

/* What is being followed, what one check costs, and what will not move.

   Above both panels because the budget is one budget: a followed flight and a
   pinned trip are priced by the same run, and the cap is on the run. Both
   panels used to carry a badge of the whole figure, so a step costing 27
   searches announced 27 twice and read as costing 54.

   The second half answers the question the tab is actually arrived with, which
   nothing on it used to answer: change the trip, and does any of this move? It
   does not. `watch._admitting` widens the stays to whatever a candidate pinned
   and drops the narrowing entirely, per candidate. */
function renderFollowSummary(body) {
  const trips = (body.preferences ?? []).length;
  const legs = (body.legs ?? []).length;
  const host = $('follow-summary-text');

  if (!trips && !legs) {
    host.textContent =
      'Nothing is being followed yet, so no check is scheduled and nothing is being spent.';
    return;
  }
  const parts = [];
  if (legs) parts.push(`${count(legs)} flight${legs === 1 ? '' : 's'}`);
  if (trips) parts.push(`${count(trips)} preference${trips === 1 ? '' : 's'}`);
  host.textContent =
    `Following ${parts.join(' and ')} — ${count(body.searches)} searches ` +
    `(~${body.minutes} min) every four hours, for both panels together.`;
}

/* Which run the candidates below are picked from.

   The one picker that deliberately offers both families. Every other list on
   the page belongs to a step that has already decided which question it is
   asking; this one is where you choose a trip to follow, and by then the
   freshest reading of the days you care about is usually the narrowed run
   while the broad one is what says whether a better week exists. Both are
   reasonable answers, so both are offered and each row says which it is.

   Defaults to the newest final run when there is one, because that is the
   freshest pricing of the dates a follow is about to pin. Falls back to the
   newest broad run before any final sweep has been taken.

   This picker existed, was labelled, had an `onchange` that moved the whole
   tab - and was never given a single option by anything. It silently showed the
   run `state.stamp` happened to hold, which is whichever one another step last
   selected. */
const watchableSweeps = () => state.sweeps.filter((sweep) => sweep.mode !== 'explore');

/* Settle which run this step reads, keeping a choice already made.

   Defaults to the newest final run, because by the time you are pinning days
   the freshest pricing of *those days* is the narrowed one; the broad run is
   what says whether a better week exists, which is a question you have stopped
   asking by now. Falls back to the newest broad run before any final sweep. */
function chooseWatchSweep() {
  const usable = watchableSweeps();
  if (usable.some((sweep) => sweep.stamp === state.watchStamp)) return;
  const preferred = usable.find((sweep) => sweep.mode === 'final') ?? usable[0];
  state.watchStamp = preferred ? preferred.stamp : null;
}

function populateWatchSweepSelect() {
  const select = $('watch-sweep-select');
  select.innerHTML = watchableSweeps()
    .map((sweep) => `<option value="${escapeHtml(sweep.stamp)}">${escapeHtml(sweepLabel(sweep, kindOf(sweep)))}</option>`)
    .join('');
  if (state.watchStamp) select.value = state.watchStamp;
}

function showWatchError(message) {
  const host = $('watch-error');
  host.hidden = !message;
  host.textContent = message || '';
}

function renderWatched(body) {
  const suffix = ` ${state.scenario.currency ?? 'CZK'}`;
  const prefs = body.preferences ?? [];
  preferenceOrder = prefs.map((p) => p.depart_date);

  $('watch-empty').hidden = prefs.length > 0;
  // Either kind of follow gives the run something to do, and they go in one
  // check: a followed leg is priced by the same run that prices a preference.
  const anything = prefs.length > 0 || (body.legs ?? []).length > 0;
  $('watch-run-btn').disabled = !anything || body.running;

  const cost = $('watch-cost');
  cost.className = `badge badge--${prefs.length ? 'good' : 'muted'}`;
  cost.textContent = prefs.length
    ? `${count(prefs.length)} of ${MAX_PREFERENCES}`
    : 'none yet';

  $('watch-run-note').textContent = body.running
    ? 'Checking now…'
    : anything
      ? 'Otherwise checked automatically every four hours in the cloud.'
      : '';
  showWatchError(body.error ? `The last check failed — ${body.error}` : '');

  // The chart. Series are in the same order as the table, so a colour in one
  // is the colour in the other.
  //
  // One line per preference, and the line is *the trip you pinned* - never the
  // cheapest thing inside its slack. A series that drifted onto a neighbouring
  // trip would draw a fall you could not act on. The neighbour is reported in
  // words under the row instead, with a button that moves the preference to it.
  const host = $('watch-chart');
  host.innerHTML = '';
  host.appendChild(multiLineChart(
    prefs
      .filter((p) => (p.series ?? []).length)
      .map((p, index) => ({
        name: `${index + 1} · ${p.label}`,
        points: p.series.map((point) => ({
          t: point.ts, value: point.total, muted: !point.comparable,
        })),
      })),
    {
      width: Math.max(420, host.clientWidth - 4),
      valueSuffix: suffix,
      ariaLabel: 'Your preferences over time',
      emptyText: prefs.length
        ? 'No checks yet — the first one runs within four hours, or press Check now.'
        : 'No preferences yet.',
    },
  ));

  const rows = $('watch-table').querySelector('tbody');
  rows.innerHTML = '';
  prefs.forEach((pref, index) => {
    rows.appendChild(preferenceRow(pref, index, prefs.length));
    const offer = nearbyOffer(pref);
    if (offer) rows.appendChild(offer);
  });
}


/* One preference, as a table row.

   Rank is the position in the list and nothing hangs off it - it is the order
   you would take them in. Reordering is a move within the list rather than a
   number on the row, which is what stops two preferences ever claiming the
   same rank. */
function preferenceRow(pref, index, total) {
  // Two baselines, and they answer different questions: "when picked" is what
  // you decided on, "change" is against the first *measurement*. Showing only
  // the latter makes a trip you picked at 30,000 and which has sat at 28,500
  // ever since look perfectly flat.
  const picked = pref.added_price;
  const now = pref.latest;
  const move = picked != null && now != null ? now - picked : pref.net_change;
  const trend = move > 0 ? 'trend--up' : move < 0 ? 'trend--down' : '';

  const row = document.createElement('tr');
  row.innerHTML =
    `<td><span class="rank">${index + 1}</span> ${escapeHtml(pref.label)}` +
    `<div class="small muted">${escapeHtml(pref.depart_dates.join(' → '))}</div></td>` +
    `<td>${escapeHtml(pref.route ?? '—')}` +
    `${pref.has_overland ? ' <span class="badge badge--warning">overland</span>' : ''}</td>` +
    `<td class="num">${picked == null ? '—' : money(picked, pref.currency)}</td>` +
    `<td class="num">${now == null
      ? '<span class="muted">not checked yet</span>'
      : money(now, pref.currency)}</td>` +
    `<td class="num ${trend}">${now == null || picked == null
      ? '—'
      : `${move > 0 ? '+' : ''}${money(move, '')}`}</td>`;

  const cell = document.createElement('td');
  cell.className = 'pref-actions';

  // How far either side of each pinned day the run also prices. The one control
  // that decides what this costs, so it sits on the row it costs for rather
  // than in a settings panel away from the consequence.
  const slack = document.createElement('select');
  slack.className = 'small';
  slack.title = 'Days either side of each date that are also priced';
  slack.innerHTML = [0, 1, 2, 3, 4, 5, 6, 7]
    .map((d) => `<option value="${d}">${d ? `±${d} days` : 'exact days'}</option>`)
    .join('');
  slack.value = String(pref.slack_days);
  slack.onchange = () => editPreference(pref.depart_date, { slack_days: Number(slack.value) });
  cell.appendChild(slack);

  for (const [label, to, enabled] of [
    ['↑', index - 1, index > 0],
    ['↓', index + 1, index < total - 1],
  ]) {
    const move_ = document.createElement('button');
    move_.className = 'small';
    move_.textContent = label;
    move_.disabled = !enabled;
    move_.title = 'Reorder';
    move_.onclick = () => editPreference(pref.depart_date, { rank: to });
    cell.appendChild(move_);
  }

  const drop = document.createElement('button');
  drop.className = 'small watch-drop';
  drop.textContent = 'Stop following';
  drop.onclick = () => stopWatching(pref.depart_date);
  cell.appendChild(drop);

  row.appendChild(cell);
  return row;
}


/* "Two days later is 1,890 cheaper", and a button that takes it.

   The entire return on the slack. Without this the extra searches buy a number
   nobody sees, and a preference can only ever report that its own Tuesday has
   not moved.

   Drawn only when the saving clears `MEANINGFUL_DROP_PCT` - the same 1% the
   watch uses to decide a fall is worth a message, and for the same measured
   reason: this site moves fares by a few crowns at a time, and an offer to
   shift your whole trip to save six of them is noise wearing a button. */
const MEANINGFUL_MOVE_PCT = 1.0;

function nearbyOffer(pref) {
  const now = pref.latest;
  const nearby = pref.nearby_total;
  if (now == null || nearby == null || !pref.nearby_dates) return null;
  const saving = now - nearby;
  if (!(now > 0 && (saving / now) * 100 >= MEANINGFUL_MOVE_PCT)) return null;
  // The same days it already flies, priced again. Nothing to offer.
  if (pref.nearby_dates.join(',') === pref.depart_dates.join(',')) return null;

  const shift = NIGHTS(pref.depart_dates[0], pref.nearby_dates[0]);
  const when = shift === 0
    ? 'The same day out'
    : `${Math.abs(shift)} day${Math.abs(shift) === 1 ? '' : 's'} ${shift > 0 ? 'later' : 'earlier'}`;

  const row = document.createElement('tr');
  row.className = 'pref-offer';
  const cell = document.createElement('td');
  cell.colSpan = 6;
  cell.innerHTML =
    `<span class="trend--down">${when} is ${money(saving, pref.currency)} cheaper</span> — ` +
    `${escapeHtml(pref.nearby_dates.join(' → '))}` +
    `${pref.nearby_route ? ` · ${escapeHtml(pref.nearby_route)}` : ''} ` +
    `at ${money(nearby, pref.currency)}. `;

  const take = document.createElement('button');
  take.className = 'small primary';
  take.textContent = 'Move it to those dates';
  // A move, not a new preference. The series belongs to this decision, and
  // starting a fresh one on every shift would leave you unable to see that the
  // trip has fallen four thousand since you began looking at it.
  take.onclick = () =>
    editPreference(pref.depart_date, { depart_dates: pref.nearby_dates });
  cell.appendChild(take);
  row.appendChild(cell);
  return row;
}


async function editPreference(key, changes) {
  try {
    await api(`/api/watch/${state.scenario.id}/${key}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(changes),
    });
    showWatchError('');
  } catch (error) {
    showWatchError(error.message);
  }
  await renderWatch();
}

// Opened for you once, and only when there is nothing to open it for. A folded
// list is right when you have already followed something and wrong when the
// panel above it is empty and this is the only way to fill it.
let nudgedCandidates = false;

function renderWatchCandidates(body, watched) {
  const host = $('watch-candidates');
  host.innerHTML = '';

  const already = new Set((watched.preferences ?? []).map((p) => p.depart_date));
  if (!nudgedCandidates && !already.size && (body.candidates ?? []).length) {
    $('watch-from-sweep').open = true;
    nudgedCandidates = true;
  }
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
    add.textContent = watching ? 'A preference' : 'Save as preference';
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


/* Adding a preference by hand: one date box per leg of the trip.

   Built from `leg_count` rather than written into the markup, because a trip is
   a chain of any length - three fixed boxes would be a two-stop assumption
   sitting in HTML, which is the shape of bug the whole schema was rewritten to
   remove.

   No `routes` are sent. This form knows the dates and cannot know which airport
   pair will win them, and inventing one would follow a flight nobody looked at.
   The preference itself is still priced across every pair, as always. */
function renderPreferenceForm() {
  const host = $('preference-add-dates');
  const legs = legCountOf(state.scenario);
  if (host.children.length === legs) return;

  host.innerHTML = '';
  for (let i = 0; i < legs; i += 1) {
    const field = document.createElement('label');
    field.className = 'field';
    field.textContent = i === 0 ? 'Leaves' : i === legs - 1 ? 'Home' : `Leg ${i + 1}`;
    const box = document.createElement('input');
    box.type = 'date';
    box.dataset.legIndex = String(i);
    field.appendChild(box);
    host.appendChild(field);
  }
}

/* Legs from the trip on screen: one per hop, plus the way home unless it is a
   one-way. The same arithmetic as `Scenario.leg_count`, which the page cannot
   import. */
const legCountOf = (trip) =>
  trip ? (trip.stops || []).length + (trip.one_way ? 0 : 1) : 0;

async function addPreferenceByHand() {
  const boxes = [...$('preference-add-dates').querySelectorAll('input[type="date"]')];
  const dates = boxes.map((box) => box.value);
  if (dates.some((value) => !value)) {
    showWatchError('A preference needs one date per leg — fill in every box.');
    return;
  }
  try {
    await api(`/api/watch/${state.scenario.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        depart_dates: dates,
        label: $('preference-add-label').value.trim(),
        slack_days: DEFAULT_SLACK_DAYS,
        currency: state.scenario.currency,
      }),
    });
    showWatchError('');
    for (const box of boxes) box.value = '';
    $('preference-add-label').value = '';
    state.scenario = await api(`/api/scenarios/${state.scenario.id}`);
    await renderWatch();
  } catch (error) {
    // The refusals here are the interesting ones - a chain that runs backwards,
    // a plan past what the site answers - and both arrive written to be read.
    showWatchError(error.message);
  }
}

$('preference-add-btn').onclick = addPreferenceByHand;

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
  state.watchStamp = event.target.value;
  renderWatch();
};

/* ------------------------------------------------ the departure window ---

   Once a broad sweep has shown which departure dates are cheap, the departure
   window narrows the next one onto them. It bounds the *first* leg only; the
   later legs are derived from it through the stay ranges, so the three parts of
   the narrowing can never contradict each other and a narrowed sweep can still
   complete a trip.

   Picked on the chart, because the decision is made by looking at it. It used to
   be picked on a *panel of its own*, with its own badge, its own Save and its own
   words for the same thing - "watching 2027-01-08 to 2027-01-12" two panels below
   "out 01-08–01-12" - and both wrote `focus_start`/`focus_end`. Two controls over
   one field is two states to reconcile and two chances to disagree on screen, so
   there is one now: the chart fills the boxes, and the panel's Save writes them.

   Which also settles what a click *means*. It no longer saves anything on its
   own, so a misclick is corrected by clicking again or by typing in the box,
   and the third-click-starts-over rule stays as the cheapest way to do that. */

/* ---------------------------------------------------------------- sources */

/* What the merged table is standing on, said once at the top.

   Each group below already names its own run, because they can genuinely differ
   - a pool is judged by the newest run that priced most of *it*, and one
   refused probe does not throw away yesterday's complete sweep. What nothing
   said was how many runs the table was built from, or how old the newest of
   them is, which is the first thing worth knowing about a floor price. */
function renderMergeLine(pools) {
  const line = $('explore-merge');
  const runs = new Map();
  for (const pool of pools) {
    if (pool.measured_by) runs.set(pool.measured_by.stamp, pool.measured_by);
  }
  if (!runs.size) {
    line.textContent = '';
    return;
  }
  const name = (run) => `${localStamp(run.stamp)} · ` +
    (run.mode === 'explore' ? 'probe' : `${run.depth ?? '?'} sweep`);
  // Newest first, which is the order the endpoint reads them in and the order
  // the question is asked in.
  const sorted = [...runs.values()].sort((a, b) => (a.stamp < b.stamp ? 1 : -1));
  // The run list is the answer and stays; why the runs differ is the lesson and
  // collapses with the rest of the teaching. They were one paragraph, which is
  // how a sentence naming the evidence came to be exempt from the switch on the
  // strength of the sentence next to it.
  line.textContent = sorted.length === 1
    ? `All of it from the ${name(sorted[0])}.`
    : `Merged from ${count(sorted.length)} runs — ${sorted.map(name).join(', ')}.`;
  $('explore-merge-why').hidden = sorted.length < 2;
}

/* Show one airport, or a few. A trip with four pools of six airports is a lot
   of table to read when the question is about one of them, and the answer to
   "is Brno worth keeping" was four scrolls away from the question. Filters what
   is drawn rather than what is fetched: the verdicts are one small payload and
   re-asking for them per keystroke would be a request per letter. */
function filterVerdicts() {
  const query = $('explore-filter').value.trim().toUpperCase();
  let shown = 0;
  for (const block of $('explore-verdicts').querySelectorAll('.panel__section')) {
    const rows = [...block.querySelectorAll('tbody tr')];
    let any = false;
    for (const row of rows) {
      const hit = !query || (row.cells[0]?.textContent ?? '').toUpperCase().includes(query);
      row.hidden = !hit;
      if (hit) any = true;
    }
    // A group whose table is empty reads as a group that failed to draw, so the
    // whole group goes. A group with no table at all - nothing has priced any
    // of it - is left alone while nothing is typed, and hidden once something
    // is: it cannot match, and it is not the answer to what was typed.
    block.hidden = rows.length ? !any : Boolean(query);
    if (any) shown += 1;
  }
  $('explore-filter-note').textContent =
    query && !shown ? `No airport here matches “${$('explore-filter').value.trim()}”.` : '';
}

$('explore-filter').oninput = filterVerdicts;

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
    note.className = 'explain small';
    note.textContent = source.note;
    card.appendChild(note);
  }

  const stake = document.createElement('p');
  // Teaching, and it collapses with the rest: what a broken source costs you is
  // worth reading once. The verdict badge beside it is the answer and stays.
  stake.className = 'explain small';
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
    '<p class="explain small">Edit the string that broke, then save — it re-checks itself. ' +
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
    '<h3 class="subhead">Selectors</h3>'
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

/* Which trip the page opens on when nothing else has said.

   It used to be `scenarios[0]`, which is alphabetical by id and so is nobody's
   answer. On this machine that opened "EU - Phil - Japan - EU" - a switched-off
   trip with no sweeps on disk - in front of someone whose actual trip is called
   "Europe → Japan → Philippines → Europe". Two near-identical names, and the one
   that opened drew empty charts, which is what a lost trip looks like.

   Last opened first, because the answer to "which of my trips" is almost always
   "the one I was just looking at". Then the first trip that is switched on, since
   an enabled trip is one being swept and therefore one being decided. The
   alphabetical fallback stays for a fresh checkout where neither applies. */
const LAST_TRIP_KEY = 'tickets-bot:last-trip';

// Both wrapped: a private window, or a browser set to block site data, throws on
// access rather than returning null, and a remembered convenience must never be
// what stops the page loading.
function rememberTrip(id) {
  try { localStorage.setItem(LAST_TRIP_KEY, id); } catch { /* not worth an error */ }
}

function lastTripOpened() {
  try { return localStorage.getItem(LAST_TRIP_KEY); } catch { return null; }
}

function firstToOpen(scenarios) {
  const remembered = lastTripOpened();
  if (scenarios.some((trip) => trip.id === remembered)) return remembered;
  return (scenarios.find((trip) => trip.enabled) ?? scenarios[0]).id;
}

/* Open a saved trip and redraw the step in front of you.

   Redrawing used to be nobody's job. `fillForm` refills the route editor and
   `pollStatus` refills the two run pickers, and between them that covers "Map
   it out" completely - which is why this went unnoticed. Stand on "Narrow it
   down" or "Follow it" and switch, though, and the boxes, the badge, the leg
   charts, the ranking and the whole preference list went on describing the trip
   you had just left, changing only the name in the picker. It reads exactly
   like a picker that refused to load the trip.

   The order is the whole of it, and getting it wrong is a quieter version of
   the same bug: every panel here draws from `state.sweeps`, which `pollStatus`
   is what refreshes. Redrawing before it ran painted the new trip's boxes over
   the old trip's list of runs, found nothing in it this trip could draw, and
   left "no broad sweep with flights on disk yet" above a trip with thirteen. */
async function openScenario(id) {
  state.stamp = null;
  state.finalStamp = null;
  state.watchStamp = null;
  clearFilters();
  await loadScenario(id);
  rememberTrip(id);
  await refreshEstimate();
  await pollStatus();
  // `showTab` is the one function that knows how to draw a step, including
  // which side of the population switch it is on.
  showTab(activeStep);
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

  const id = scenarios.some((s) => s.id === preferred) ? preferred : firstToOpen(scenarios);
  select.value = id;
  await openScenario(id);
}


/* ------------------------------------------------------------ your airports

   The global ranking, in tiers, drawn by the same editor as a trip's own
   override - see `renderTierEditor`. It answers two questions at once now, and
   that is the dedupe: the flattened order is the one-click chips beside
   *Depart from*, and the tiers are what Discord reports by for every trip that
   has not overridden them.

   Reordering is by removing and re-adding rather than by arrow buttons. The
   flat row had arrows because position was its entire content; a tier list has
   a tier per row and moving an airport between two of them is the edit people
   actually make. */

const home = { tiers: [] };

// The flat order, for the badge and for anything that wants "my airports" as a
// sequence. Within a tier the order is the order it was typed in, which is
// arbitrary and says nothing - that is what sharing a tier means.
const homeFlat = () => home.tiers.flat();

function renderHomeAirports() {
  const badge = $('home-airports-state');
  badge.className = `badge badge--${home.tiers.length ? 'good' : 'muted'}`;
  badge.textContent = home.tiers.length
    ? home.tiers.map((tier) => tier.join(' / ')).join(' → ')
    : 'not set — chips come from your trips';

  renderTierEditor($('home-airports'), home.tiers, (next) => {
    home.tiers = next;
    renderHomeAirports();
  }, {
    key: 'home',
    suggest: state.frequent.origins,
    empty: 'Nothing set. Add your first choice, and these become the chips on Map it out.',
  });
  renderInheritedNote();
}

$('home-airports-add-tier').onclick = () => {
  home.tiers.push([]);
  renderHomeAirports();
};

async function loadHomeAirports() {
  try {
    const body = await api('/api/home-airports');
    home.tiers = body.tiers ?? [];
    cacheAirports(body.described ?? []);
  } catch {
    // Not fatal and not worth a blocker. No ranking is the normal state of a
    // fresh checkout, and every caller falls back to what it did before.
    home.tiers = [];
  }
  renderHomeAirports();
}

$('home-airports-save').onclick = async () => {
  const message = $('home-airports-message');
  try {
    const body = await api('/api/home-airports', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      // An empty tier is a row added and never filled, and the server drops it.
      // Sent anyway rather than filtered here, so one place decides what an
      // unfinished row means.
      body: JSON.stringify({ tiers: home.tiers }),
    });
    home.tiers = body.tiers ?? [];
    cacheAirports(body.described ?? []);
    renderHomeAirports();
    message.className = 'small badge badge--good';
    message.textContent = homeFlat().length
      ? 'Saved. These are the chips on Map it out, and the order Discord reports in.'
      : 'Saved. The chips go back to the airports your trips already use.';
    // The chip row reads `state.frequent`, which the ranking has just replaced.
    // Left stale, the trip form would go on offering the old order until a
    // reload - two answers to "which are my airports", on two steps.
    state.frequent = await api('/api/airports/frequent');
    renderRoute();
  } catch (error) {
    message.className = 'small badge badge--error';
    message.textContent = error.message;
  }
};

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
      try {
        await openScenario(id);
      } catch (error) {
        // Named, because the alternative is what this actually did: with the
        // server stopped, `loadScenario` threw first, the rest never ran, and
        // nothing on the page said anything at all.
        showError(error.message);
      }
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

/* One of these per step that draws the leg charts.

   The broad set on "Narrow it down" and the narrowed set on "Final sweeps" are
   the same three curves over two different populations, and they must not share
   a cursor: a drag on one moving the other would be exactly the broad/final
   toggle this deliberately is not. The same shape as `resultsView` above, for
   the same reason - one set of renderers, addressed by an id prefix.

   `stamp` is the step's selected run and lives in `state`, shared with the
   ranking table beside these charts. `chartStamp` is the run actually drawn,
   which can lag by one render while the step is being pulled onto a run its
   charts can draw. */
function legView({ prefix, stampKey, modes, picker, mate, table, results, emptyText }) {
  return {
    prefix, modes, picker, mate, table, results, emptyText,
    $: (id) => $(prefix + id),
    get stamp() { return state[stampKey]; },
    set stamp(value) { state[stampKey] = value; },
    chartStamp: null,
    data: null,
    domain: [],
    cursor: [],       // one date index per leg, into `domain`
    expanded: new Set(),
  };
}

/* Broad runs only. A probe prices three dates a leg to compare airports, which
   draws a line that looks like a price curve and is not one. */
const NARROW_CHARTS = legView({
  prefix: '', stampKey: 'stamp', modes: ['sweep'],
  picker: 'narrow-sweep', mate: 'sweep-select', table: 'results-table',
  results: BROAD_VIEW,
  emptyText: 'No broad sweep with flights on disk yet. Run one from Map it out.',
});

const FINAL_CHARTS = legView({
  prefix: 'final-', stampKey: 'finalStamp', modes: ['final'],
  picker: 'final-sweep-charts', mate: 'final-sweep-select', table: 'final-results-table',
  results: FINAL_VIEW,
  emptyText: 'No narrowed sweep yet. Press “Sweep narrowed” above to price the dates you have chosen.',
});

const LEG_VIEWS = [NARROW_CHARTS, FINAL_CHARTS];

/* Which charts a ranking row's "Put on the charts" lifts onto. Assigned here
   rather than in `resultsView` because the views are declared six hundred lines
   apart, and a step's table and its charts must be two readings of one run. */
BROAD_VIEW.legs = NARROW_CHARTS;
FINAL_VIEW.legs = FINAL_CHARTS;

const NIGHTS = (from, to) => Math.round((asDate(to) - asDate(from)) / 86400000);

/* Whether this trip says anything less than its whole window.

   All three parts, matching `plan_sweep.has_narrowing` on the server. It used
   to check the return window and the nights band only, so a trip narrowed by a
   departure window alone was told "Cleared. Back to the whole window." while
   the scheduler counted it as narrowed and gave it two extra runs a day. The
   same shape of mistake as reading a status that recorded the focus alone, one
   layer up. */
function narrowSaved() {
  const s = state.scenario;
  return Boolean(s && (s.focus_start || s.return_focus_start || s.total_days));
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

/* The two jobs these dates do, in the words of this trip, always visible.

   Explanations collapse by default, so everything the panel said about itself
   was one click away behind "What this is for" - and the questions it answered
   are the ones the step is unusable without: do these boxes filter what I read,
   or do they buy searches, or both, and does the nightly sweep still cover the
   window if I narrow. A `--live` line is never collapsed, and this one is a
   fact about the trip on screen rather than teaching, which is the same rule
   the cost lines already follow. */
function renderNarrowRole() {
  const line = $('narrow-role');
  const trip = state.scenario || {};
  const narrowed = narrowingSentence(trip);
  // The nights band used to be named here, because it filters and had no box
  // left to remove it with. It has a button of its own now - see
  // `renderNightsBand` - and the badge above already states it, so neither
  // survives only as prose the switch can hide.
  if (!narrowed) {
    line.textContent = 'Nothing narrowed yet, so there is no narrowed sweep to run and the '
      + 'charts below stay empty. The whole window is under Map it out → Search results.';
    return;
  }
  const scheduled = trip.sweep_narrowing !== false
    ? 'and they are what the narrowed sweep prices at 13:00 and 20:00'
    : 'and nothing runs on them on its own — scheduled sweeping is off for them';
  line.textContent =
    `These dates filter the charts and the ranking on this step, ${scheduled}. `
    + 'The nightly sweep is untouched either way: it goes on pricing every date in the '
    + 'window, and it is the only thing that can still find a cheaper week outside what '
    + 'you chose — that one answers under Map it out → Search results.';
}

/* The nights-away band, as a control rather than a sentence about one.

   The band is a real filter with no box on this panel: it was set when the
   narrowing lived elsewhere, and it goes on narrowing every sweep until
   something removes it. That something was a paragraph saying "Clear it removes
   that too", which is both indirect - Clear it also drops the two date windows
   you meant to keep - and prose, so the explanations switch could hide the only
   mention of a live constraint. */
function renderNightsBand() {
  const band = (state.scenario || {}).total_days;
  $('narrow-nights-row').hidden = !band;
  if (band) {
    $('narrow-nights-label').textContent = `${band[0]}–${band[1]} nights away`;
  }
}

/* The route at the top of the narrowing step, read-only.

   Drawn from the saved trip and not from the route editor's draft: what these
   dates will be swept against is the file on disk, and a strip that followed
   unsaved edits would name a route the sweep is not going to use. */
function renderTripStrip() {
  const trip = state.scenario || {};
  const host = $('narrow-trip-chain');
  const cell = (text, nights) =>
    `<span class="trip-strip__stop">${escapeHtml(text)}`
    + (nights ? `<span class="trip-strip__nights">${escapeHtml(nights)}</span>` : '')
    + '</span>';
  const arrow = '<span class="trip-strip__arrow">→</span>';
  const parts = [];
  const origins = trip.origins || [];
  if (origins.length) parts.push(cell(origins.join(' / ')));
  for (const stop of trip.stops || []) {
    const nights = stop.stay_days ? `${stop.stay_days[0]}–${stop.stay_days[1]} nights` : '';
    parts.push(cell(stop.label || (stop.airports || []).join(' / '), nights));
  }
  if (!trip.one_way) {
    const back = (trip.return_to && trip.return_to.length) ? trip.return_to : origins;
    if (back.length) parts.push(cell(back.join(' / ')));
  }
  host.innerHTML = parts.length ? parts.join(arrow) : '<span class="muted">No route yet.</span>';
}

/* The narrowing step, whole.

   This was `renderNarrow`, which also drew the broad leg charts. Those moved to
   Map it out with the sweep that produces them, so what is left here is the
   trip, the dates, and the narrowed runs. */
async function renderNarrowStep() {
  renderTripStrip();
  renderNarrowFields();
  await renderFinal();
  // The step the cloud files two runs a day into, so it has to be able to say
  // when one has landed in the repo but not in this checkout.
  await Promise.all([refreshNarrowCost(), renderCloudSync()]);
}

function renderNarrowFields() {
  const s = state.scenario || {};
  $('narrow-out-start').value = s.focus_start || '';
  $('narrow-out-end').value = s.focus_end || '';
  $('narrow-back-start').value = s.return_focus_start || '';
  $('narrow-back-end').value = s.return_focus_end || '';
  renderNarrowRole();
  renderNightsBand();

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

  // What the run on screen was priced under, when the boxes above no longer
  // say the same. Not a warning and not a filter - `_sweep_scenario` reads a
  // run through its own snapshot, so those are simply the stays this table and
  // these charts are able to describe. Without the line, a table of 9-night
  // Japan stays sits under a box reading 10 with nothing to connect them.
  const line = $('narrow-stays-sweep');
  // Whichever charts are actually drawn on the step these boxes are on. That
  // used to be the broad ones and only the broad ones; they moved to Map it
  // out, so the sweep on screen beside these boxes is the narrowed run when
  // there is one, and the broad run this trip was last read under otherwise.
  const shown = FINAL_CHARTS.data ? FINAL_CHARTS : NARROW_CHARTS;
  const ran = (shown.data || {}).stay_days;
  const names = ((shown.data || {}).stop_labels) || [];
  const drifted = ran && counted.some((stop, index) => ran[index]
    && (ran[index][0] !== stop.stay_days[0] || ran[index][1] !== stop.stay_days[1]));
  line.hidden = !drifted;
  line.textContent = drifted
    ? 'The sweep on screen was priced under ' +
      counted.map((stop, index) =>
        `${stop.label || names[index] || `stop ${index + 1}`} ${ran[index][0]}–${ran[index][1]}`,
      ).join(' · ') +
      ' — that is what its charts and table can show you. Changing the boxes above ' +
      'moves neither; it decides what the next sweep prices.'
    : '';

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
  return {
    focus_start: value('narrow-out-start'),
    focus_end: value('narrow-out-end'),
    return_focus_start: value('narrow-back-start'),
    return_focus_end: value('narrow-back-end'),
    // Carried through untouched. The panel stopped offering a control for it,
    // which is not the same as deciding it should be thrown away: a trip
    // narrowed to 22-27 nights last week is still narrowed to that, and
    // silently clearing it on the next Save would widen a sweep nobody asked
    // to widen. `Clear it` still clears it, along with everything else.
    total_days: (state.scenario || {}).total_days ?? null,
    // Priced and saved with the rest, so the estimate under the boxes is of the
    // trip in them and one Save writes the whole panel. A stay change written
    // separately would leave a nights band briefly claiming a span its stays no
    // longer reach, which `validate` refuses.
    stops: staysFromFields(),
    // Whether the 13:00/20:00 slots also sweep the boxes above. In the same
    // write for the same reason: it is a decision about the dates beside it,
    // and a tick saved on its own would schedule a run of a narrowing the trip
    // file does not yet hold.
    sweep_narrowing: $('sweep-narrowing').checked,
  };
}

async function refreshNarrowCost() {
  // Priced against the trip on screen rather than the one on disk, so the
  // figure moves as the dates are typed. Deep on both sides: comparing a
  // narrowed quick plan against a broad deep one would flatter the narrowing
  // by the depth rather than by the narrowing.
  //
  // The two calls differ by `mode`, not by the body they send. They used to
  // differ by the body - the same trip with the narrowing nulled out - which
  // stopped meaning anything the moment a broad sweep stopped reading the
  // narrowing: both sides came back with the identical number, and the line
  // reported that narrowing to five days out of thirty-five saved nothing.
  const note = $('narrow-sweep-line');
  const trip = { ...state.scenario, ...narrowFromFields() };
  const price = (mode) =>
    api(`/api/scenarios/${state.scenario.id}/estimate?depth=deep&mode=${mode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(trip),
    });
  // Nothing typed in yet is not an error, and asking anyway makes it look like
  // one: `plan_final` refuses a trip with no narrowing by design, and the
  // refusal rendered here as "No estimate — There is nothing to narrow to yet",
  // in red, on a panel whose boxes are empty because you have not filled them
  // in. The broad cost is the true and useful answer at that point.
  const narrowedYet = Boolean(
    (trip.focus_start && trip.focus_end)
    || (trip.return_focus_start && trip.return_focus_end)
    || trip.total_days,
  );
  try {
    const whole = await price('sweep');
    const broadly =
      `${count(whole.searches)} searches (~${Math.round(whole.minutes)} min) for the broad sweep, `
      + 'which goes on pricing the whole window either way';
    if (!narrowedYet) {
      note.textContent = `Nothing narrowed yet — ${broadly}.`;
      return;
    }
    const narrowed = await price('final');
    note.textContent =
      `A narrow sweep of this is ${count(narrowed.searches)} searches ` +
      `(~${Math.round(narrowed.minutes)} min), against ${broadly}.`;
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
      ? ($('sweep-narrowing').checked
        ? 'Saved. Swept at 13:00 and 20:00, and it filters what you read here.'
        : 'Saved. It filters what you read here and costs no searches.')
      : 'Cleared. Back to the whole window.';
    // Everything in this step reads the narrowing, and all of it is on screen
    // at once now. Refreshing only the charts left the table under them listing
    // trips the narrowing had just excluded - two panels, one screen, two
    // different answers to what this sweep contains.
    await Promise.all([
      refreshNarrowCost(),
      loadLegCharts(NARROW_CHARTS),
      renderResults(),
      // The narrowing is what a narrow sweep searches, so the panels that read
      // one are describing a different plan the moment this saves. Left out,
      // they went on quoting the old dates and the old cost.
      renderFinal(),
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



/* Populate one step's chart picker and land it on a run it can draw. */
async function renderLegStep(view) {
  const picker = $(view.picker);
  // Probes and runs whose legs never reached disk cannot answer this panel:
  // a probe prices three dates a leg to compare airports, which draws a chart
  // that looks like a price curve and is not one.
  const sweeps = state.sweeps.filter(
    (s) => s.has_legs && view.modes.includes(s.mode ?? 'sweep'),
  );
  // Named by depth, exactly as the table's picker below names the same run.
  // Left to the default it read "sweep" here and "standard" there, so one run
  // appeared twice on one screen under two names.
  picker.innerHTML = sweeps
    .map((s) => `<option value="${escapeHtml(s.stamp)}">${escapeHtml(sweepLabel(s, kindOf(s)))}</option>`)
    .join('');
  picker.hidden = !sweeps.length;
  if (!sweeps.length) {
    const host = view.$('leg-charts');
    host.className = 'empty';
    host.textContent = view.emptyText;
    view.$('cursor-readout').innerHTML = '';
    view.data = null;
    // Three live buttons under an empty panel, one of them the brightest thing
    // on the step. "Save as preference" with no trip on screen is an invitation
    // to press something that can only report an error.
    setCursorActions(view, false);
    return;
  }
  setCursorActions(view, true);
  // One sweep for the whole step. This picker and the table's used to be
  // separate, so the charts could be showing 22 August while the itineraries
  // under them were 23 August's probe - two panels on one screen describing two
  // different runs, with nothing to say so. Follow the step's selection when it
  // can be drawn, and pull the step to this one when it cannot.
  if (view.stamp && sweeps.some((s) => s.stamp === view.stamp)) {
    view.chartStamp = view.stamp;
  } else {
    // The step opened on a run these charts cannot draw - most often the
    // newest, which is a two-hourly probe. Move the whole step onto the newest
    // run that can be drawn rather than leaving the charts and the table below
    // them on different days.
    if (!view.chartStamp || !sweeps.some((s) => s.stamp === view.chartStamp)) {
      view.chartStamp = sweeps[0].stamp;
    }
    view.stamp = view.chartStamp;
    const table = $(view.mate);
    if ([...table.options].some((o) => o.value === view.stamp)) table.value = view.stamp;
  }
  picker.value = view.chartStamp;
  await loadLegCharts(view);
}

async function renderFinalCharts() {
  await renderLegStep(FINAL_CHARTS);
}

async function loadLegCharts(view) {
  if (!view.chartStamp) return;
  const host = view.$('leg-charts');
  try {
    view.data = await api(
      `/api/sweeps/${state.scenario.id}/${view.chartStamp}/by-leg`,
    );
  } catch (error) {
    host.className = 'empty';
    host.textContent = error.message;
    return;
  }

  // One axis for every leg: the union of every date any leg was asked about,
  // in order. Each chart is then handed the same domain, which is what makes a
  // vertical slice down the stack one trip rather than three unrelated days.
  //
  // A narrowed run's legs do not overlap at all - five departures, seven
  // middles, nine returns, measured on the live trip - so its axis is three
  // tight clusters rather than one long overlap. That reads better, not worse:
  // every column on it is a day some leg was actually priced on.
  const dates = new Set();
  for (const leg of view.data.legs) for (const p of leg.points) dates.add(p.depart_date);
  const domain = [...dates].sort();
  // A trip picked by hand survives leaving the step and coming back. It is
  // several deliberate drags, and re-snapping it away on the way past would
  // make the panel unusable next to any other one. Only a genuinely different
  // axis - another sweep, or the same trip re-swept over other dates - is
  // grounds for throwing the cursor away, because indices into the old domain
  // would then point at the wrong days.
  const same =
    view.domain.length === domain.length
    && view.domain.every((label, i) => label === domain[i])
    && view.cursor.length === view.data.legs.length;
  view.domain = domain;
  if (!same) view.expanded.clear();
  // The stay boxes render before this resolves, so the line about what this
  // run was priced under has nothing to compare against on the first pass.
  // They belong to the narrowing step alone - the final step has no boxes to
  // write, and calling this from there would describe the wrong run in them.
  if (view === NARROW_CHARTS) {
    refreshStayDerived();
    renderNarrowFields();
  }

  if (!same) await snapCursor(view, { quiet: true });
  drawLegCharts(view);
}

/* Whether this step's chart buttons can do anything yet. Off until a run with
   flights is drawn - `renderCursor` narrows it further, disabling Follow alone
   for a pick whose legs are out of order. */
function setCursorActions(view, live) {
  for (const id of ['cursor-snap', 'cursor-watch', 'cursor-in-table']) {
    view.$(id).disabled = !live;
  }
}

function drawLegCharts(view) {
  const host = view.$('leg-charts');
  host.className = '';
  host.innerHTML = '';
  if (!view.data || !view.domain.length) {
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
    view.domain.length * 26 + 80,
  );
  const suffix = ` ${view.data.currency}`;

  view.data.legs.forEach((leg, index) => {
    const block = document.createElement('div');
    block.className = 'leg-chart';

    const header = document.createElement('div');
    header.className = 'leg-chart__header';
    const toggle = document.createElement('button');
    toggle.className = 'small';
    toggle.textContent = view.expanded.has(index)
      ? `${leg.label} — hide routes`
      : `${leg.label}${leg.routes.length > 1 ? ` — ${count(leg.routes.length)} routes` : ''}`;
    toggle.disabled = leg.routes.length < 2;
    toggle.onclick = () => {
      if (view.expanded.has(index)) view.expanded.delete(index);
      else view.expanded.add(index);
      drawLegCharts(view);
    };
    header.appendChild(toggle);

    const picked = document.createElement('span');
    picked.className = 'small muted';
    const at = view.domain[view.cursor[index]];
    const point = at && leg.points.find((p) => p.depart_date === at);
    picked.textContent = point && point.price !== null
      ? `${at} · ${point.origin}→${point.destination} · ${money(point.price, view.data.currency)}`
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
      domain: view.domain,
      width,
      height: 190,
      valueSuffix: suffix,
      ariaLabel: `${leg.label} by departure date`,
      marker: { index: view.cursor[index] },
      onMarkerMove: (i) => { view.cursor[index] = i; renderCursor(view); },
      onMarkerRelease: () => drawLegCharts(view),
      emptyText: 'Nothing priced on this leg.',
    }));

    if (view.expanded.has(index)) {
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
            domain: view.domain, width, height: 120, valueSuffix: suffix,
            color: `var(--color-chart${r + 1})`,
            ariaLabel: `${route.route} by departure date`,
            emptyText: `${route.route} — nothing sold on any date searched.`,
          },
        ));
        block.appendChild(row);
      });
      if (leg.routes.length > shown.length) {
        const rest = document.createElement('p');
        rest.className = 'answer small';
        rest.textContent =
          `${count(leg.routes.length - shown.length)} other routes not drawn: six is as many ` +
          'lines as this palette can tell apart.';
        block.appendChild(rest);
      }
    }

    host.appendChild(block);
  });

  renderCursor(view);
}

/* ----------------------------------------------------------- the readout */

function cursorTrip(view) {
  /* What the markers currently say, priced, split and checked.

     Checked rather than enforced. Every rule it can break is reported by name
     and none of them stops anything, which is the whole reason the markers
     move independently.

     Unchanged for a narrowed run, deliberately. Everything a final sweep
     priced already obeys the narrowing, so nothing here can fire on its own -
     but a marker dragged past a stay range still can, and that is exactly the
     case worth naming. */
  const legs = view.data.legs.map((leg, i) => {
    const at = view.domain[view.cursor[i]];
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

  // From the live trip, not from `view.data`. `by-leg` reports the stays the
  // *run* was made under, because that is what it priced - but the other three
  // rules checked here come off the live trip through `_narrowing_of`, and a
  // readout mixing the two says "fits every rule you set" about a stay length
  // the trip stopped allowing weeks ago. This is you choosing a trip to book,
  // so it is measured against what you want now.
  const breaks = [];
  const stops = (state.scenario || {}).stops || [];
  const fallback = view.data.stop_labels || [];
  stops.forEach((stop, i) => {
    if (i >= stays.length) return;
    const [low, high] = stop.stay_days;
    if (stays[i] < low || stays[i] > high) {
      breaks.push(`${stays[i]} nights at ${stop.label || fallback[i] || `stop ${i + 1}`}, ` +
        `not ${low}–${high}`);
    }
  });
  const band = view.data.window && view.data.window.total_days;
  if (band && (away < band[0] || away > band[1])) {
    breaks.push(`${away} nights away, not ${band[0]}–${band[1]}`);
  }
  // All three parts of the narrowing, in the order they are read on the panel
  // above. Checking two of them and not the departure window is how "fits every
  // rule you set" came to sit under a trip leaving two days outside it.
  const out = view.data.window && view.data.window.focus;
  const first = legs[0].date;
  if (out && first && (first < out[0] || first > out[1])) {
    breaks.unshift(`leaving ${first}, not ${out[0]}–${out[1]}`);
  }
  const home = view.data.window && view.data.window.return_focus;
  const last = legs[legs.length - 1].date;
  if (home && (last < home[0] || last > home[1])) {
    breaks.push(`flying home ${last}, not ${home[0]}–${home[1]}`);
  }

  return { legs, priced, total, stays, away, breaks, impossible };
}

function renderCursor(view) {
  const host = view.$('cursor-readout');
  if (!view.data || !view.domain.length || !view.cursor.length) {
    host.innerHTML = '';
    setCursorActions(view, false);
    return;
  }
  setCursorActions(view, true);
  const trip = cursorTrip(view);
  const currency = view.data.currency;

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
  const follow = view.$('cursor-watch');
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
    <div class="cursor-readout__legs"></div>
  `;

  /* One row per leg, and each row is everything you can do about that flight.

     This panel is where a trip is actually chosen - three curves, a marker on
     each, a total that moves as you drag - and it was the one place in the app
     with no way to buy anything and nothing to follow. The link was already in
     the payload it fetches: `by-leg` carries the offer's `url` beside its price
     and nothing read it, so the way to book the trip you had just built was to
     scroll past the ranking, find the row again and open its Legs. */
  const rows = host.querySelector('.cursor-readout__legs');
  trip.legs.forEach((leg) => {
    const row = document.createElement('div');
    row.className = 'cursor-readout__leg';
    const point = leg.point;
    const priced = point && point.price !== null;

    const when = document.createElement('span');
    when.className = 'cursor-readout__when';
    when.textContent = leg.date || '—';
    row.appendChild(when);

    const what = document.createElement('span');
    what.className = 'muted';
    what.textContent = priced
      ? `${point.origin}→${point.destination}` +
        (point.airline ? ` · ${point.airline}` : '') +
        (point.stops === 0 ? ' · direct' : point.stops ? ` · ${count(point.stops)} stops` : '')
      : 'nothing found on this date';
    row.appendChild(what);

    const price = document.createElement('span');
    price.className = 'cursor-readout__price tnum';
    price.textContent = priced ? money(point.price, currency) : '—';
    row.appendChild(price);

    if (priced) {
      // Scraped from a third-party page, so it is built as a node with `.href`
      // and never through innerHTML - a `javascript:` URL cannot be injected
      // into an attribute that is assigned rather than parsed. Same rule the
      // itinerary table's leg links follow.
      if (point.url && /^https?:\/\//i.test(point.url)) {
        const book = document.createElement('a');
        book.href = point.url;
        book.target = '_blank';
        book.rel = 'noopener';
        book.className = 'small';
        book.textContent = 'Book ↗';
        book.title = `Open this flight on the site it was priced on · ${observedAt(point.observed_at)}`;
        row.appendChild(book);
      } else {
        // Said rather than left as a gap, because an absent link between two
        // present ones reads as a page that failed rather than as a sweep that
        // recorded a price without one.
        const none = document.createElement('span');
        none.className = 'small muted';
        none.textContent = 'no link saved';
        row.appendChild(none);
      }

      const follow = document.createElement('button');
      follow.type = 'button';
      follow.className = 'small';
      follow.textContent = 'Follow';
      follow.title = 'Re-price this exact flight every four hours (one search)';
      follow.onclick = () => followLeg({
        origin: point.origin,
        destination: point.destination,
        depart_date: leg.date,
        price: point.price,
      });
      row.appendChild(follow);
    }
    rows.appendChild(row);
  });

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
/* Put an itinerary's leg dates under the markers.

   Top-level rather than a closure inside `snapCursor`, because the ranking
   below now uses it too: a row of the table is a trip, and "put it on the
   charts" is the way back from the ranking into the panel where a trip can be
   adjusted. Before this the table was a dead end - you could read that a trip
   existed and had no way to see it against the curves. */
function placeCursor(view, itinerary) {
  view.cursor = itinerary.legs.map((leg) => {
    const at = view.domain.indexOf(leg.depart_date);
    return at >= 0 ? at : 0;
  });
}

async function snapCursor(view, { quiet = false } = {}) {
  const message = view.$('cursor-message');

  /* Last resort: each leg's own cheapest day, forced into order.

     Cheapest-per-leg on its own is not a trip. The legs are priced
     independently, so the cheapest day for the flight to Manila is regularly
     before the cheapest day for the flight to Tokyo - and the panel would then
     open on "cannot exist", with Follow disabled, before the reader had touched
     a thing. Marching the dates forward gives a cursor that is merely
     uninteresting rather than impossible. */
  const spread = () => {
    let floor = -1;
    view.cursor = view.data.legs.map((leg) => {
      const priced = leg.points.filter((p) => p.price !== null);
      let at = 0;
      if (priced.length) {
        const best = priced.reduce((a, b) => (b.with_bags < a.with_bags ? b : a));
        at = Math.max(0, view.domain.indexOf(best.depart_date));
      }
      if (at <= floor) at = Math.min(floor + 1, view.domain.length - 1);
      floor = at;
      return at;
    });
  };

  const ask = async (window) => {
    const body = await api(
      `/api/sweeps/${state.scenario.id}/${view.chartStamp}/results?window=${window}&limit=1`,
    );
    return [body.itineraries && body.itineraries[0], body.currency];
  };

  try {
    const [best, currency] = await ask('narrow');
    if (best) {
      placeCursor(view, best);
      if (!quiet) {
        message.className = 'small';
        message.textContent = `Snapped to ${money(best.total_with_bags, currency)} incl. bags.`;
      }
    } else {
      // Nothing here fits. Show the cheapest trip this sweep *can* build rather
      // than no trip at all: that it exists and sits outside the narrowing is
      // the useful thing to see, and it is a real trip to start dragging from.
      const [nearest, nearestCurrency] = await ask('all');
      if (nearest) placeCursor(view, nearest);
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
  if (!quiet) drawLegCharts(view);
}

/* Follow the picked trip: a Watch, pinned to these exact leg dates.

   Legal whether or not it breaks the stay ranges. A watch prices the dates it
   is given; the ranges only ever governed which chains a sweep would build. */
async function watchCursor(view) {
  const message = view.$('cursor-message');
  const trip = cursorTrip(view);
  try {
    await api(`/api/watch/${state.scenario.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        depart_dates: trip.legs.map((l) => l.date),
        added_price: trip.priced ? trip.total : null,
        currency: view.data.currency,
        slack_days: DEFAULT_SLACK_DAYS,
        // Named from the shape rather than left to the server, which would have
        // to derive it from dates alone: "13+14, Jan 2027" is what the readout
        // above already calls this trip, and two names for one pick is two
        // things to recognise it by.
        label: `${trip.stays.join('+')} from ${dayOf(trip.legs[0].date)}`,
        // The airports the markers are actually on. A preference pins dates and
        // not airports - that is deliberate, so a check can still find that
        // Frankfurt undercut Vienna overnight - but the *flights* you were
        // looking at when you picked it are worth following one search each,
        // and this is the only place that knows which they were.
        routes: trip.legs
          .filter((l) => l.point)
          .map((l) => ({
            origin: l.point.origin,
            destination: l.point.destination,
            price: l.point.price,
          })),
      }),
    });
    message.className = 'small badge badge--good';
    message.textContent = trip.breaks.length
      ? 'Saved as a preference. It breaks a rule you set, and it will be priced every four '
        + 'hours all the same.'
      : 'Saved as a preference. Priced every four hours from now, with a couple of days '
        + 'either side.';
  } catch (error) {
    message.className = 'small badge badge--error';
    message.textContent = error.message;
  }
}

/* "12 Jan" from an ISO date, for naming a preference after the day it leaves.

   The day and not just the month: two preferences of the same shape a week
   apart are the pair worth telling apart, and a legend naming both of them
   "13+12, Jan" is a comparison you cannot read. Matches `Preference.describe`
   on the server, which names one the same way when the page sends no label. */
const dayOf = (iso) => {
  const when = asDate(iso) ?? new Date(0);
  return `${when.getDate()} ${when.toLocaleString('en', { month: 'short' })}`;
};

/* The three buttons under each set of charts, bound once per step.

   Written as a loop for the same reason the results tables are: two copies of
   this that drifted apart would be two panels answering the same question
   differently, on two tabs, with nothing on screen to say which was right. */
for (const view of LEG_VIEWS) {
  $(view.picker).onchange = async (event) => {
    view.chartStamp = event.target.value;
    // The table in this step moves with it, or the step is back to describing
    // two runs at once.
    view.stamp = view.chartStamp;
    const picker = $(view.mate);
    if ([...picker.options].some((o) => o.value === view.stamp)) picker.value = view.stamp;
    await Promise.all([loadLegCharts(view), renderResults(view.results)]);
  };

  view.$('cursor-snap').onclick = () => snapCursor(view);
  view.$('cursor-watch').onclick = () => watchCursor(view);

  /* Mark the picked trip in the ranking below and scroll to it.

     The two panels describe the same sweep and had nothing joining them, so a
     trip built by dragging could only be checked against the ranking by reading
     dates off one panel and hunting for them in the other. Matched on the leg
     dates, which is what a row is keyed by - not on the price, which two
     different trips can share.

     Each step points at its own table. Pointing at the broad one from the final
     step would send you hunting through a run these prices did not come from. */
  view.$('cursor-in-table').onclick = () => {
    const message = view.$('cursor-message');
    if (!view.data) return;
    const wanted = cursorTrip(view).legs.map((leg) => leg.date).join(',');
    const rows = [...document.querySelectorAll(`#${view.table} tbody tr[data-dates]`)];
    for (const row of rows) row.classList.remove('is-cursor');

    const found = rows.find((row) => row.dataset.dates === wanted);
    if (!found) {
      // Not a failure. The ranking is narrowed and capped, and a hand-picked trip
      // is very often one it never offered - which is the whole reason the
      // markers move freely.
      message.className = 'small';
      message.textContent =
        'This combination is not in the table below — it is either outside your narrowing or '
        + 'was not among the cheapest it lists. The charts priced it all the same.';
      return;
    }
    message.textContent = '';
    found.classList.add('is-cursor');
    found.scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
}

$('notify-save-btn').onclick = saveTrip;

$('narrow-save').onclick = () => saveNarrowing(narrowFromFields());
// Saved with the panel rather than on the click, so one Save writes the boxes
// and the decision about them together. The cost line moves immediately, since
// what the tick is worth is the number beside it.
$('sweep-narrowing').onchange = refreshFinalCost;
/* All three parts, including the departure window.

   It used to leave `focus_start`/`focus_end` alone, because the departure window
   had its own "Watch the whole window" button on a panel of its own. That panel
   is gone, so a Clear that skipped the departure window would leave a narrowing
   nothing on screen could undo. */
$('narrow-clear').onclick = () => saveNarrowing({
  focus_start: null, focus_end: null,
  return_focus_start: null, return_focus_end: null, total_days: null,
});
// Only the band, keeping the date windows. `Clear it` drops all three, which is
// a different thing to want and was the only way to be rid of this one.
$('narrow-nights-clear').onclick = () => saveNarrowing({ total_days: null });
$('narrow-widen').onclick = () => {
  // One save, not two. Widening and then narrowing as separate writes leaves a
  // trip that is briefly wider with no narrowing on it, and a nightly sweep
  // firing between the two would price the whole of it.
  const fields = narrowFromFields();
  const needed = windowFor(fields);
  if (!needed) return;
  saveNarrowing({ ...fields, window_start: needed.start, window_end: needed.end });
};
for (const id of [
  'narrow-out-start', 'narrow-out-end', 'narrow-back-start', 'narrow-back-end',
]) {
  $(id).onchange = () => {
    renderWidenOffer();
    renderNarrowRole();
    refreshNarrowCost();
  };
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

  // This panel's own count. The searches figure is the whole run's and now sits
  // once, above both panels, where it is not read as this panel's share.
  const cost = $('leg-watch-cost');
  cost.className = `badge badge--${legs.length ? 'good' : 'muted'}`;
  cost.textContent = legs.length
    ? `${count(legs.length)} flight${legs.length === 1 ? '' : 's'}`
    : 'none yet';

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
      // Which preference brought this row along, or that you picked it. Not
      // decoration: it is the difference between a row you may drop from here
      // and one that is dropped by dropping its preference.
      `<td class="small">${leg.source
        ? `<span class="badge badge--muted" title="Followed because a preference leaves on ${escapeHtml(leg.source)}">${escapeHtml(preferenceName(leg.source))}</span>`
        : '<span class="muted">yours</span>'}</td>` +
      `<td class="num">${picked == null ? '—' : money(picked, leg.currency)}</td>` +
      `<td class="num">${now == null
        ? '<span class="muted">not checked yet</span>'
        : money(now, leg.currency)}</td>` +
      `<td class="num ${trend}">${now == null || picked == null
        ? '—'
        : `${move > 0 ? '+' : ''}${money(move, '')}`}</td>`;

    // Where to go and buy it, on the row that says it got cheaper. A search
    // link rather than the offer's own: the watch keeps a price and not a URL,
    // and a deep link recorded four hours ago need not still be the cheapest
    // thing on that route. Built server-side, and still assigned to `.href`
    // rather than interpolated, like every other link this page draws.
    const booking = document.createElement('td');
    if (leg.book_url && /^https?:\/\//i.test(leg.book_url)) {
      const book = document.createElement('a');
      book.href = leg.book_url;
      book.target = '_blank';
      book.rel = 'noopener';
      book.className = 'small';
      book.textContent = 'Book ↗';
      book.title = 'Search this route and date on the site it is priced from';
      booking.appendChild(book);
    }
    row.appendChild(booking);

    const cell = document.createElement('td');
    if (leg.source) {
      // Dropped by dropping its preference, not from here. Two ways to unfollow
      // one flight is two lists that can disagree about what is being checked -
      // and the preference would go on pricing those days regardless, so the
      // button would be a promise the run does not keep.
      cell.className = 'small muted';
      cell.textContent = 'with its preference';
      cell.title = 'Stop following the preference above to drop this too';
    } else {
      const drop = document.createElement('button');
      drop.className = 'small watch-drop';
      drop.textContent = 'Stop following';
      drop.onclick = () => unfollowLeg(leg.key);
      cell.appendChild(drop);
    }
    row.appendChild(cell);
    rows.appendChild(row);
  }
}

/* "pref 2" for the preference leaving on `key`, or its date if it has gone.

   Read off the last render rather than passed down, because the two tables are
   drawn from one payload and threading the list through every row builder to
   resolve one badge is more plumbing than the badge is worth. */
let preferenceOrder = [];
const preferenceName = (key) => {
  const at = preferenceOrder.indexOf(key);
  return at === -1 ? key : `pref ${at + 1}`;
};

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

/* ---------------------------------------------------------- final sweeps */

/* The step that prices the decision rather than the window.

   It reads the same narrowing the step before it writes — one field, one
   control, the rule the by-date chart was moved to obey. Nothing here is
   editable: this panel says what a run of it would search and lets you start
   one, and a second set of date boxes would be two controls over one field
   again, three hundred pixels apart and disagreeing.

   Everything below the plan is the ordinary results view, pointed at the
   narrowed runs instead of the broad ones. */

const NARROWING_LINES = [
  ['focus_start', 'focus_end', 'leaving'],
  ['return_focus_start', 'return_focus_end', 'home'],
];

/* What a final sweep of this trip would search, in a sentence, or '' for a trip
   that has not been narrowed to anything. */
function narrowingSentence(trip) {
  if (!trip) return '';
  const said = NARROWING_LINES
    .filter(([from, to]) => trip[from] && trip[to])
    .map(([from, to, word]) => `${word} ${trip[from]} to ${trip[to]}`);
  if (trip.total_days) said.push(`${trip.total_days[0]}–${trip.total_days[1]} nights away`);
  return said.join(', ');
}

async function renderFinal() {
  populateSweepSelect(FINAL_VIEW);
  await renderFinalCharts();
  await renderResults(FINAL_VIEW);
  await refreshFinalCost();
}

/* What a narrow sweep of this trip would cost, at the depth the buttons will use.

   Split out of `renderFinal` because the depth select changes only this line:
   re-running the whole step would refetch `by-leg` and redraw three charts to
   answer a question about a number. Priced at the selected depth rather than a
   fixed one - a `quick` narrow sweep of a five-day window is one date, and
   quoting a deep cost beside a quick button describes a different run.

   Lives beside the checkbox now rather than on a step of its own, and it is the
   number the checkbox is a decision about: ticking it spends this twice a day.
   A trip narrowed to nothing has no such run, and the controls say so rather
   than leaving dead buttons to be clicked. The server refuses it too - these
   are the notice, not the guard. */
async function refreshFinalCost() {
  const trip = state.scenario;
  const badge = $('narrow-sweep-cost');
  const narrowing = narrowingSentence(trip);

  // `state.isNew` as well: a trip with no file cannot be swept whatever it has
  // been narrowed to, and `updateNewTripUi` sets the same buttons for that
  // reason. Whichever refusal applies, they must not be pressable.
  for (const id of ['final-run-btn', 'sweep-narrowing']) {
    $(id).disabled = !narrowing || state.isNew;
  }
  renderScheduleSummary();
  if (!trip || !narrowing) {
    badge.textContent = 'nothing narrowed yet';
    return;
  }
  try {
    const body = await api(
      `/api/scenarios/${trip.id}/estimate?depth=${$('final-depth').value}&mode=final`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(trip) },
    );
    badge.textContent =
      `${count(body.searches)} searches · ~${Math.round(body.minutes)} min`;
  } catch (error) {
    // The reason, not a shrug. This said "no estimate" and hid the sentence in
    // a `title` nobody hovers, which is how a stopped server read as a trip
    // that could not be priced.
    badge.textContent = error.message;
  }
}

/* What runs without being pressed, on the button that opens the settings for
   it. Two buttons carry this - one on Map it out, one on the narrowing step -
   because "and does this happen on its own?" is a question asked in both
   places, and it used to be answerable only by going to a third. */
function renderScheduleSummary() {
  const trip = state.scenario || {};
  const nightly = trip.enabled ? 'nightly' : null;
  const narrowed = trip.sweep_narrowing !== false && narrowingSentence(trip)
    ? 'twice a day, narrowed'
    : null;
  const on = [nightly, narrowed].filter(Boolean);
  const text = on.length ? on.join(' · ') : 'nothing scheduled';
  for (const id of ['schedule-summary', 'narrow-schedule-summary']) $(id).textContent = text;
}

function showFinalError(message) {
  const host = $('final-error');
  host.hidden = !message;
  host.textContent = message || '';
}

$('final-depth').onchange = refreshFinalCost;

$('final-run-btn').onclick = async () => {
  showFinalError('');
  // Saved first, because the cloud sweeps the trip on the branch and not the
  // boxes on screen. A narrowing typed but not saved would be swept as whatever
  // the file still says, which is the run reading as a no-op.
  if (isDirty() && !(await saveTrip())) return;
  await dispatchCloudRun(false, 'final', $('final-depth').value);
};

// The gateway out of both run rows and into what happens unattended.
/* ------------------------------------------------- the schedule editor ---

   Four controls, one dialog, three ways in: the run row on Map it out, the run
   row on Narrow it down, and `Edit the schedule` behind the gear.

   All three used to be `showTab('night')`, which opened the whole gear - nine
   panels, one of them about scheduling. A button naming one subject should open
   that subject. And a dialog rather than an expander under each button because
   the same four controls are wanted from three places: three copies would be
   three sets of the same ids, and `#enabled` existing twice is a checkbox that
   reads whichever the DOM found first and saves the other. */

const scheduleDialog = () => $('schedule-dialog');

/* What the schedule will cost this trip, priced when the dialog opens.

   Both runs, because the dialog is where the two are chosen between and the
   trade is otherwise invisible: the nightly one is the whole window and the
   narrowed one is a fraction of it, twice as often. */
async function refreshScheduleCost() {
  const line = $('schedule-cost');
  const trip = state.scenario || {};
  if (state.isNew) {
    line.textContent = 'Save the trip first — the schedule sweeps the trip on the branch.';
    return;
  }
  const parts = [];
  const price = async (mode, depth) => {
    const body = await api(
      `/api/scenarios/${trip.id}/estimate?depth=${depth}&mode=${mode}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formToScenario()) },
    );
    return `${count(body.searches)} searches · ~${Math.round(body.minutes)} min`;
  };
  if ($('enabled').checked) {
    try {
      parts.push(`02:00 — ${await price('sweep', $('depth').value)}`);
    } catch (error) {
      parts.push(`02:00 — ${error.message}`);
    }
  }
  if ($('sweep-narrowing').checked) {
    try {
      parts.push(`13:00 and 20:00 — ${await price('final', $('final-depth').value)} each`);
    } catch (error) {
      // `plan_final` refuses a trip with nothing narrowed, by name. That is the
      // answer to "why is nothing happening twice a day", so it is shown rather
      // than swallowed.
      parts.push(`13:00 and 20:00 — ${error.message}`);
    }
  }
  line.textContent = parts.length ? parts.join(' · ') : 'Nothing is scheduled for this trip.';
}

function openSchedule() {
  clearScheduleError();
  scheduleDialog().showModal();
  refreshScheduleCost();
}

function clearScheduleError() {
  const box = $('schedule-error');
  box.hidden = true;
  box.textContent = '';
}

for (const id of ['schedule-btn', 'narrow-schedule-btn', 'night-edit-btn']) {
  $(id).onclick = openSchedule;
}

// Re-priced as the controls move, because the number is the reason to choose
// one depth over another and a stale one is worse than none.
for (const id of ['enabled', 'depth', 'sweep-narrowing', 'final-depth']) {
  $(id).addEventListener('change', refreshScheduleCost);
}

// `method="dialog"` closes on any submit, so the two secondary buttons only
// need to say what else happens.
$('schedule-more').onclick = () => showTab('night');

$('night-save-btn').onclick = async (event) => {
  // Held open on failure: a dialog that closes on a refused save loses both the
  // edit and the reason. `showError` routes into the open dialog, so the reason
  // lands where the button was pressed.
  event.preventDefault();
  clearScheduleError();
  if (await saveTrip()) scheduleDialog().close();
};

$('final-sweep-select').onchange = async (event) => {
  state.finalStamp = event.target.value;
  // And the charts above it, or the step is back to describing two runs at once
  // - the thing the two synced pickers on the narrowing step exist to prevent.
  const charts = $(FINAL_CHARTS.picker);
  if ([...charts.options].some((o) => o.value === state.finalStamp)) {
    charts.value = state.finalStamp;
    FINAL_CHARTS.chartStamp = state.finalStamp;
    await loadLegCharts(FINAL_CHARTS);
  }
  await renderResults(FINAL_VIEW);
};
