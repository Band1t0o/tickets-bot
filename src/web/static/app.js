import { lineChart } from '/chart.js';

const $ = (id) => document.getElementById(id);
const state = { scenario: null, airports: null, sweeps: [], stamp: null };

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

/* --------------------------------------------------------------- airports */

function renderAirports() {
  const selected = {
    europe: new Set(state.scenario.origins),
    japan: new Set(state.scenario.japan_airports),
    philippines: new Set(state.scenario.ph_airports),
  };

  for (const group of ['europe', 'japan', 'philippines']) {
    const host = $(`airports-${group}`);
    host.innerHTML = '';
    for (const airport of state.airports[group]) {
      const label = document.createElement('label');
      label.className = `airport${airport.available ? '' : ' is-unavailable'}`;

      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = airport.iata;
      box.dataset.group = group;
      box.checked = selected[group].has(airport.iata);
      box.disabled = !airport.available;
      box.onchange = scheduleEstimate;

      const body = document.createElement('div');
      body.className = 'airport__body';
      body.innerHTML =
        `<div class="airport__code">${airport.iata} <span class="muted">${airport.city}</span></div>` +
        (airport.note ? `<div class="airport__note">${airport.note}</div>` : '') +
        (airport.available ? '' : '<div class="airport__note"><span class="badge badge--muted">unavailable</span></div>');

      label.append(box, body);
      host.appendChild(label);
    }
  }
}

const chosen = (group) =>
  [...document.querySelectorAll(`input[data-group="${group}"]:checked`)].map((b) => b.value);

/* -------------------------------------------------------------- scenarios */

function fillForm(scenario) {
  $('trip-type').value = scenario.trip_type;
  $('window-start').value = scenario.window_start;
  $('window-end').value = scenario.window_end;
  $('adults').value = scenario.adults;
  $('jp-min').value = scenario.japan_stay_days[0];
  $('jp-max').value = scenario.japan_stay_days[1];
  $('ph-min').value = scenario.ph_stay_days[0];
  $('ph-max').value = scenario.ph_stay_days[1];
  $('depth').value = scenario.depth;
  toggleTripFields();
}

function toggleTripFields() {
  const isMulti = $('trip-type').value === 'multi_city';
  $('ph-heading').hidden = !isMulti;
  $('airports-philippines').hidden = !isMulti;
  $('ph-min').closest('label').hidden = !isMulti;
  $('ph-max').closest('label').hidden = !isMulti;
}

function formToScenario() {
  return {
    ...state.scenario,
    trip_type: $('trip-type').value,
    origins: chosen('europe'),
    japan_airports: chosen('japan'),
    ph_airports: $('trip-type').value === 'multi_city' ? chosen('philippines') : [],
    window_start: $('window-start').value,
    window_end: $('window-end').value,
    adults: Number($('adults').value),
    japan_stay_days: [Number($('jp-min').value), Number($('jp-max').value)],
    ph_stay_days: [Number($('ph-min').value), Number($('ph-max').value)],
    depth: $('depth').value,
  };
}

/* --------------------------------------------------------------- estimate */

let estimateTimer;
const scheduleEstimate = () => {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(refreshEstimate, 250);
};

async function refreshEstimate() {
  try {
    const body = await api(
      `/api/scenarios/${state.scenario.id}/estimate?depth=${$('depth').value}`,
      { method: 'POST' },
    );
    $('estimate').textContent = `${body.searches} searches · ~${body.minutes} min`;
    $('estimate').className = 'badge badge--muted';
  } catch (error) {
    $('estimate').textContent = error.message;
    $('estimate').className = 'badge badge--error';
  }
}

/* -------------------------------------------------------------- save/run */

$('save-btn').onclick = async () => {
  $('save-error').hidden = true;
  try {
    const saved = await api(`/api/scenarios/${state.scenario.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(formToScenario()),
    });
    state.scenario = saved;
    await refreshEstimate();
    $('save-btn').textContent = 'Saved';
    setTimeout(() => ($('save-btn').textContent = 'Save scenario'), 1500);
  } catch (error) {
    $('save-error').textContent = error.message;
    $('save-error').hidden = false;
  }
};

$('run-local-btn').onclick = async () => {
  try {
    await api(`/api/scenarios/${state.scenario.id}/run?depth=${$('depth').value}`, { method: 'POST' });
    pollStatus();
  } catch (error) {
    $('save-error').textContent = error.message;
    $('save-error').hidden = false;
  }
};

$('run-cloud-btn').onclick = async () => {
  try {
    await api(`/api/scenarios/${state.scenario.id}/run-cloud?depth=${$('depth').value}`, { method: 'POST' });
    $('status-text').textContent = 'Dispatched to GitHub Actions';
  } catch (error) {
    $('save-error').textContent = error.message;
    $('save-error').hidden = false;
  }
};

$('trip-type').onchange = () => { toggleTripFields(); scheduleEstimate(); };
$('depth').onchange = scheduleEstimate;
for (const id of ['window-start', 'window-end', 'jp-min', 'jp-max', 'ph-min', 'ph-max', 'adults']) {
  $(id).onchange = scheduleEstimate;
}

/* ----------------------------------------------------------------- status */

async function pollStatus() {
  const strip = $('status-strip');
  try {
    const body = await api(`/api/sweeps/${state.scenario.id}`);
    state.sweeps = body.sweeps;
    const latest = body.sweeps[0];

    if (body.running && latest) {
      const left = latest.total
        ? Math.max(0, Math.round(((latest.total - latest.completed) * 17) / 4 / 60))
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
    option.textContent = `${sweep.stamp.replace('T', ' ').replace('Z', '')} (${sweep.legs_found ?? 0} flights)`;
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
    return;
  }

  const body = await api(`/api/sweeps/${state.scenario.id}/${state.stamp}/results`);
  $('results-empty').hidden = body.itineraries.length > 0;

  const same = body.best_same_airport;
  const jaw = body.best_open_jaw;
  // Exactly one card is the headline. On a tie the same-airport option wins,
  // since it is the one preferred when the price is equal.
  const headline = !same ? jaw : !jaw ? same : (jaw.total_price < same.total_price ? jaw : same);

  for (const [label, itinerary] of [
    ['Cheapest, same airport', same],
    ['Cheapest, open jaw', jaw],
  ]) {
    const card = document.createElement('div');
    card.className = `stat${itinerary && itinerary === headline ? ' is-headline' : ''}`;
    const saving = itinerary && itinerary !== headline && headline
      ? `<div class="stat__sub trend trend--up">${money(itinerary.total_price - headline.total_price, '')}more</div>`
      : '';
    card.innerHTML =
      `<div class="stat__label">${label}</div>` +
      (itinerary
        ? `<div class="stat__value">${money(itinerary.total_price, itinerary.currency)}</div>
           <div class="stat__sub">${itinerary.route}</div>${saving}`
        : '<div class="stat__value muted">—</div><div class="stat__sub">none found</div>');
    $('headline').appendChild(card);
  }

  for (const itinerary of body.itineraries) {
    const row = document.createElement('tr');
    const airlines = [...new Set(itinerary.legs.map((l) => l.airline))].join(', ');
    row.innerHTML =
      `<td>${itinerary.route}</td>` +
      `<td>${itinerary.legs[0].depart_date}</td>` +
      `<td>${itinerary.legs[itinerary.legs.length - 1].depart_date}</td>` +
      `<td>${airlines}</td>` +
      `<td>${itinerary.same_airport ? '<span class="badge badge--good">same airport</span>' : '<span class="badge badge--muted">open jaw</span>'}</td>` +
      `<td class="num">${money(itinerary.total_price, itinerary.currency)}</td>`;

    const detail = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 6;
    cell.innerHTML =
      '<details class="disclosure"><summary class="small muted">Legs</summary>' +
      itinerary.legs.map((l) =>
        `<div class="small">${l.origin}→${l.destination} · ${l.depart_date}` +
        (l.depart_time ? ` ${l.depart_time}→${l.arrive_time}` : '') +
        ` · ${l.airline} · ${l.stops ?? '?'} stop(s) · ${money(l.price_amount, l.price_currency)}` +
        (l.url ? ` · <a href="${l.url}" target="_blank" rel="noopener">open</a>` : '') +
        '</div>').join('') +
      '</details>';
    detail.appendChild(cell);

    tbody.append(row, detail);
  }
}

/* ----------------------------------------------------------------- prices */

async function renderPrices() {
  if (!state.stamp) return;

  const byDate = await api(`/api/sweeps/${state.scenario.id}/${state.stamp}/by-date`);
  const byDateHost = $('chart-by-date');
  const width = Math.max(420, byDateHost.clientWidth - 4);
  byDateHost.innerHTML = '';
  byDateHost.appendChild(lineChart(
    byDate.map((row) => ({ label: row.depart_date, value: row.cheapest_total, note: row.route })),
    { width, valueSuffix: ' CZK', ariaLabel: 'Cheapest total by departure date',
      emptyText: 'No itineraries in this sweep yet.' },
  ));

  const history = await api(`/api/history/${state.scenario.id}`);
  $('chart-history').innerHTML = '';
  // One point is not a trend. Say so rather than drawing a lone dot that
  // implies a flat line.
  $('chart-history').appendChild(lineChart(
    history.length >= 2 ? history.map((row) => ({ label: row.swept_at.slice(0, 10), value: row.best_total })) : [],
    { width, valueSuffix: ' CZK', ariaLabel: 'Best total over time',
      emptyText: history.length === 1
        ? 'Only one sweep so far — a trend needs at least two.'
        : 'Needs a few sweeps before a trend means anything.' },
  ));

  const probe = await api('/api/probe');
  const routes = Object.entries(probe.routes);
  $('probe-body').className = routes.length ? '' : 'empty';
  $('probe-body').innerHTML = routes.length
    ? '<div class="table-scroll"><table class="data"><thead><tr>' +
      '<th>Route</th><th class="num">Observations</th><th class="num">Changed</th>' +
      '<th class="num">Median move</th><th class="num">Biggest drop</th></tr></thead><tbody>' +
      routes.map(([name, r]) =>
        `<tr><td>${name}</td><td class="num">${r.n_observations}</td>` +
        `<td class="num">${Math.round(r.change_rate * 100)}%</td>` +
        `<td class="num">${Math.round(r.median_change).toLocaleString()}</td>` +
        `<td class="num">${Math.round(r.largest_drop).toLocaleString()}</td></tr>`).join('') +
      `</tbody></table></div><p class="panel__hint" style="margin-top:12px">${probe.recommendation}</p>`
    : 'No observations yet.';
}

/* -------------------------------------------------------------------- init */

async function init() {
  state.airports = await api('/api/airports');
  const scenarios = await api('/api/scenarios');

  const select = $('scenario-select');
  for (const scenario of scenarios) {
    const option = document.createElement('option');
    option.value = scenario.id;
    option.textContent = scenario.name;
    select.appendChild(option);
  }
  select.onchange = async () => {
    state.scenario = await api(`/api/scenarios/${select.value}`);
    state.stamp = null;
    fillForm(state.scenario);
    renderAirports();
    await refreshEstimate();
    await pollStatus();
  };

  state.scenario = scenarios[0];
  fillForm(state.scenario);
  renderAirports();
  await refreshEstimate();
  await pollStatus();
}

init();
