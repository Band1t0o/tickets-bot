import { lineChart } from '/chart.js';

const $ = (id) => document.getElementById(id);
const state = { scenario: null, viability: null, sweeps: [], stamp: null };

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
const route = { origins: [], stops: [], returnTo: null, oneWay: false };

const airportCache = new Map();
const cacheAirports = (list) => {
  for (const airport of list) airportCache.set(airport.iata, airport);
  return list;
};

function airportLabel(code) {
  const airport = airportCache.get(code);
  return airport ? `${airport.city || airport.name}` : '';
}

function viabilityOf(code) {
  const stats = state.viability?.airports?.[code];
  if (!stats) return null;
  if (stats.verdict === 'no_inventory' || stats.verdict === 'no_return') return stats;
  return null;
}

/* ------------------------------------------------------------- typeahead -- */

function typeahead(onPick) {
  const wrap = document.createElement('span');
  wrap.className = 'typeahead';

  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = '+ airport or city';
  input.autocomplete = 'off';

  const menu = document.createElement('div');
  menu.className = 'typeahead__menu';
  menu.hidden = true;

  let items = [];
  let active = -1;
  let timer;

  const close = () => { menu.hidden = true; active = -1; };

  const highlight = () => {
    for (const [index, button] of [...menu.children].entries()) {
      button.classList.toggle('is-active', index === active);
    }
  };

  const pick = (airport) => {
    if (!airport) return;
    cacheAirports([airport]);
    onPick(airport.iata);
    input.value = '';
    close();
  };

  const render = (results) => {
    items = results;
    menu.innerHTML = '';
    for (const airport of results) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'typeahead__item';
      // Catalogue text is third-party data; never interpolate it raw.
      button.innerHTML =
        `<strong>${escapeHtml(airport.iata)}</strong>` +
        `<span class="muted">${escapeHtml(airport.city || airport.name)}` +
        `${airport.country ? ', ' + escapeHtml(airport.country) : ''}</span>`;
      button.onmousedown = (event) => { event.preventDefault(); pick(airport); };
      menu.appendChild(button);
    }
    menu.hidden = results.length === 0;
    active = results.length ? 0 : -1;
    highlight();
  };

  input.oninput = () => {
    clearTimeout(timer);
    const query = input.value.trim();
    if (query.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      try {
        render(cacheAirports(await api(`/api/airports/search?q=${encodeURIComponent(query)}`)));
      } catch { close(); }
    }, 160);
  };

  input.onkeydown = (event) => {
    if (menu.hidden) return;
    if (event.key === 'ArrowDown') { event.preventDefault(); active = Math.min(active + 1, items.length - 1); highlight(); }
    else if (event.key === 'ArrowUp') { event.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
    else if (event.key === 'Enter') { event.preventDefault(); pick(items[active]); }
    else if (event.key === 'Escape') close();
  };

  input.onblur = () => setTimeout(close, 120);

  wrap.append(input, menu);
  return wrap;
}

/* ----------------------------------------------------------------- chips -- */

function renderChips(host, codes, onChange) {
  host.innerHTML = '';
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
    remove.onclick = () => onChange(codes.filter((c) => c !== code));
    chip.appendChild(remove);
    host.appendChild(chip);
  }
  host.appendChild(typeahead((code) => {
    if (!codes.includes(code)) onChange([...codes, code]);
  }));
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
    });

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

$('one-way').onchange = () => {
  route.oneWay = $('one-way').checked;
  // Nowhere to return to when there is no leg home.
  $('open-jaw').disabled = route.oneWay;
  renderReturn();
  scheduleEstimate();
};

$('open-jaw').onchange = () => {
  route.returnTo = $('open-jaw').checked ? [...(route.returnTo ?? route.origins)] : null;
  renderReturn();
  scheduleEstimate();
};

function renderReturn() {
  const on = !route.oneWay && route.returnTo !== null;
  $('return-block').hidden = !on;
  if (on) {
    renderChips($('return-to'), route.returnTo, (codes) => {
      route.returnTo = codes;
      renderReturn();
      scheduleEstimate();
    });
  }
}

function renderOrigins() {
  renderChips($('origins'), route.origins, (codes) => {
    route.origins = codes;
    renderOrigins();
    scheduleEstimate();
  });
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
  route.returnTo = scenario.return_to ? [...scenario.return_to] : null;
  route.oneWay = Boolean(scenario.one_way);

  $('window-start').value = scenario.window_start;
  $('window-end').value = scenario.window_end;
  $('adults').value = scenario.adults;
  $('currency').value = scenario.currency ?? 'CZK';
  $('depth').value = scenario.depth;
  $('one-way').checked = route.oneWay;
  $('open-jaw').checked = route.returnTo !== null;
  $('open-jaw').disabled = route.oneWay;

  renderRoute();
}

function formToScenario() {
  return {
    ...state.scenario,
    origins: route.origins,
    stops: route.stops.map((stop) => ({
      label: stop.label,
      airports: stop.airports,
      stay_days: stop.stay_days,
    })),
    return_to: route.oneWay ? null : route.returnTo,
    one_way: route.oneWay,
    window_start: $('window-start').value,
    window_end: $('window-end').value,
    adults: Number($('adults').value),
    currency: ($('currency').value || 'CZK').toUpperCase(),
    depth: $('depth').value,
  };
}

/* --------------------------------------------------------------- estimate */

let estimateTimer;
const scheduleEstimate = () => {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(refreshEstimate, 300);
};

async function refreshEstimate() {
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
  try {
    const saved = await api(`/api/scenarios/${state.scenario.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formToScenario()),
    });
    state.scenario = saved;
    fillForm(saved);
    await refreshEstimate();
    $('save-btn').textContent = 'Saved';
    setTimeout(() => ($('save-btn').textContent = 'Save scenario'), 1500);
  } catch (error) {
    showError(error.message);
  }
};

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
for (const id of ['window-start', 'window-end', 'adults', 'currency']) {
  $(id).onchange = scheduleEstimate;
}

/* ----------------------------------------------------------------- status */

async function pollStatus() {
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
    card.innerHTML =
      `<div class="stat__label">${label}</div>` +
      (itinerary
        ? `<div class="stat__value">${money(withBags(itinerary), itinerary.currency)}</div>
           <div class="stat__sub">${itinerary.route}</div>${bagNote}${saving}`
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
          : ' · <span class="badge badge--warning">bag extra</span>');
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
  $('chart-history').innerHTML = '';
  // One point is not a trend. Say so rather than drawing a lone dot that
  // implies a flat line.
  $('chart-history').appendChild(lineChart(
    history.length >= 2 ? history.map((row) => ({ label: row.swept_at.slice(0, 10), value: row.best_total })) : [],
    { width, valueSuffix: suffix, ariaLabel: 'Best total over time',
      emptyText: history.length === 1
        ? 'Only one sweep so far — a trend needs at least two.'
        : 'Needs a few sweeps before a trend means anything.' },
  ));

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

async function init() {
  try {
    const [scenarios, viability] = await Promise.all([
      api('/api/scenarios'),
      api('/api/viability').catch(() => ({ airports: {} })),
    ]);
    state.viability = viability;

    if (!scenarios.length) {
      $('status-text').textContent = 'No scenarios yet — add one under scenarios/';
      return;
    }

    const select = $('scenario-select');
    for (const scenario of scenarios) {
      const option = document.createElement('option');
      option.value = scenario.id;
      option.textContent = scenario.name;
      select.appendChild(option);
    }
    select.onchange = async () => {
      state.stamp = null;
      await loadScenario(select.value);
      await refreshEstimate();
      await pollStatus();
    };

    await loadScenario(scenarios[0].id);
    await refreshEstimate();
    await pollStatus();
  } catch (error) {
    $('status-strip').className = 'status-strip is-error';
    $('status-text').textContent = `Could not start — ${error.message}`;
  }
}

init();
