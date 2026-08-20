/*
 * Dependency-free inline-SVG line chart.
 *
 * Finance-planner uses recharts for this; hand-rolling it keeps a Node
 * toolchain out of a Python repo.
 *
 * Two exports, because they answer different questions and share almost
 * nothing but the helpers:
 *
 * - `lineChart` is one series against an index of evenly spaced labels. The
 *   price-by-date and best-over-time charts are both that shape, and it has
 *   no legend because the panel title names the series.
 * - `multiLineChart` is several series against *time*. The watched days are
 *   that shape: each is added at a different moment and has its own number of
 *   observations, so an index axis would draw two candidates recorded hours
 *   apart as though they had been measured together.
 *
 * They are deliberately not one function with a flag. Every part of the
 * single-series one - the cheapest-point label, the tooltip, the picking band -
 * assumes one value per x, and threading a series dimension through all of it
 * would put the two working charts at risk to save a helper.
 *
 * Colours come from the theme tokens (--color-chart1..6 for series,
 * --color-chartGrid / --color-chartAxis for the frame), so the charts re-theme
 * with the page instead of hard-coding hexes. Those hues were validated for
 * lightness band, chroma floor and 3:1 contrast against the surface in both
 * light and dark.
 */

const PAD = { top: 18, right: 18, bottom: 34, left: 62 };

function niceTicks(min, max, count = 5) {
  if (min === max) return [min];
  const span = max - min;
  const step = Math.pow(10, Math.floor(Math.log10(span / count)));
  const err = (span / count) / step;
  const mult = err >= 7.5 ? 10 : err >= 3 ? 5 : err >= 1.5 ? 2 : 1;
  const niceStep = mult * step;
  const start = Math.ceil(min / niceStep) * niceStep;
  const ticks = [];
  for (let v = start; v <= max + 1e-9; v += niceStep) ticks.push(Math.round(v));
  return ticks;
}

function svgEl(name, attrs = {}) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/**
 * points: [{ label: '2027-01-12', value: 27000, note?, muted? }]
 * opts:   { height, valueSuffix, xLabel }
 *
 * `muted` marks a point whose value is not trustworthy enough to compare with
 * the others — a sweep that was starved or never covered the whole trip. It is
 * drawn hollow, joined by a dashed segment, and excluded from the cheapest
 * label. Hiding such points entirely would be worse: the gap in the record is
 * itself worth seeing.
 */
export function lineChart(points, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';

  if (!points || points.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = opts.emptyText || 'No data yet.';
    wrap.appendChild(empty);
    return wrap;
  }

  const height = opts.height || 260;
  // Fill the panel when there are few points, and grow past it (the container
  // scrolls) when there are many, so labels never collide.
  const width = Math.max(opts.width || 420, points.length * 26 + PAD.left + PAD.right);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  const values = points.map((p) => p.value);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  // A price chart that starts at zero wastes most of its height on empty space
  // and flattens the variation you are actually looking for. Pad the observed
  // range instead, and label the axis so the truncation is explicit.
  const span = rawMax - rawMin || Math.max(1, rawMax * 0.1);
  const yMin = Math.max(0, rawMin - span * 0.15);
  const yMax = rawMax + span * 0.15;

  const x = (i) => PAD.left + (points.length === 1 ? plotW / 2 : (i / (points.length - 1)) * plotW);
  const y = (v) => PAD.top + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: 'img',
    'aria-label': opts.ariaLabel || 'Price chart',
  });

  // The band of dates being watched closely, drawn behind everything so it
  // reads as ground rather than as another series. Half-open ranges are drawn
  // too: while you are picking, one end is chosen and the other is not, and a
  // band that only appeared once both were set would leave the first click
  // looking like it did nothing.
  if (opts.band && opts.band.length) {
    const marks = opts.band
      .map((label) => points.findIndex((p) => p.label === label))
      .filter((i) => i >= 0);
    if (marks.length) {
      const from = x(Math.min(...marks));
      const to = x(Math.max(...marks));
      svg.appendChild(svgEl('rect', {
        x: Math.min(from, to) - 6, y: PAD.top,
        width: Math.abs(to - from) + 12, height: plotH,
        fill: 'var(--color-chart1)', opacity: 0.12, rx: 4,
      }));
    }
  }

  // Recessive grid and axis, per the design tokens.
  for (const tick of niceTicks(yMin, yMax)) {
    svg.appendChild(svgEl('line', {
      x1: PAD.left, x2: width - PAD.right, y1: y(tick), y2: y(tick),
      stroke: 'var(--color-chartGrid)', 'stroke-width': 1,
    }));
    const label = svgEl('text', {
      x: PAD.left - 8, y: y(tick) + 4,
      'text-anchor': 'end', 'font-size': 11, fill: 'var(--color-chartAxis)',
    });
    label.textContent = tick.toLocaleString();
    svg.appendChild(label);
  }

  // X labels: thinned so they never collide.
  const everyNth = Math.ceil(points.length / 8);
  points.forEach((p, i) => {
    if (i % everyNth !== 0 && i !== points.length - 1) return;
    const label = svgEl('text', {
      x: x(i), y: height - 12, 'text-anchor': 'middle',
      'font-size': 11, fill: 'var(--color-chartAxis)',
    });
    label.textContent = (p.label || '').slice(5); // MM-DD is enough
    svg.appendChild(label);
  });

  // Series line, drawn a segment at a time so a segment touching an untrusted
  // point can be dashed. One continuous solid line would assert a trend across
  // measurements that cannot support one.
  for (let i = 1; i < points.length; i += 1) {
    const uncertain = points[i - 1].muted || points[i].muted;
    svg.appendChild(svgEl('path', {
      d: `M${x(i - 1)},${y(points[i - 1].value)} L${x(i)},${y(points[i].value)}`,
      fill: 'none', stroke: 'var(--color-chart1)',
      'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
      ...(uncertain ? { 'stroke-dasharray': '4 4', opacity: 0.45 } : {}),
    }));
  }

  // Markers, with a surface ring so overlapping points stay separable. Hollow
  // where the value is not comparable.
  points.forEach((p, i) => {
    svg.appendChild(svgEl('circle', {
      cx: x(i), cy: y(p.value), r: 4,
      fill: p.muted ? 'var(--color-panelBackground)' : 'var(--color-chart1)',
      stroke: p.muted ? 'var(--color-chart1)' : 'var(--color-panelBackground)',
      'stroke-width': 2,
      ...(p.muted ? { opacity: 0.6 } : {}),
    }));
  });

  // Direct-label only the cheapest point: a number on every point is noise.
  // Untrusted points are never eligible — the whole reason they are dimmed is
  // that their number cannot be believed, and calling one "the best" is the
  // present bug in miniature. With nothing trustworthy to label, label nothing;
  // the caption under the chart says why.
  const trusted = points.map((p, i) => [p, i]).filter(([p]) => !p.muted);
  if (trusted.length) {
    const [bestPoint, cheapestIndex] = trusted.reduce(
      (a, b) => (b[0].value < a[0].value ? b : a),
    );
    svg.appendChild(svgEl('circle', {
      cx: x(cheapestIndex), cy: y(bestPoint.value), r: 6,
      fill: 'none', stroke: 'var(--color-chart1)', 'stroke-width': 2,
    }));
    const best = svgEl('text', {
      x: x(cheapestIndex),
      y: y(bestPoint.value) - 14,
      'text-anchor': cheapestIndex === 0 ? 'start' : cheapestIndex === points.length - 1 ? 'end' : 'middle',
      'font-size': 12, 'font-weight': 600, fill: 'var(--color-pageText)',
    });
    best.textContent = `${bestPoint.value.toLocaleString()}${opts.valueSuffix || ''}`;
    svg.appendChild(best);
  }

  wrap.appendChild(svg);

  // Hover layer: crosshair plus tooltip, the default for a line chart.
  const tooltip = document.createElement('div');
  tooltip.className = 'chart__tooltip';
  tooltip.hidden = true;
  wrap.appendChild(tooltip);

  const crosshair = svgEl('line', {
    y1: PAD.top, y2: PAD.top + plotH,
    stroke: 'var(--color-chartAxis)', 'stroke-width': 1, 'stroke-dasharray': '3 3',
    visibility: 'hidden',
  });
  svg.appendChild(crosshair);

  // Nearest point to a mouse event, so the hit target is far bigger than the
  // 4px marker. Shared by hover and click: two copies of this would let the
  // tooltip name one date while a click picked its neighbour.
  const nearestTo = (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const px = (event.clientX - box.left) * scale;
    let nearest = 0;
    let bestDist = Infinity;
    points.forEach((_, i) => {
      const d = Math.abs(x(i) - px);
      if (d < bestDist) { bestDist = d; nearest = i; }
    });
    return { nearest, scale };
  };

  if (opts.onPick) {
    svg.style.cursor = 'pointer';
    svg.addEventListener('click', (event) => {
      const { nearest } = nearestTo(event);
      opts.onPick(points[nearest].label, nearest, event);
    });
  }

  svg.addEventListener('mousemove', (event) => {
    const { nearest, scale } = nearestTo(event);
    const point = points[nearest];
    crosshair.setAttribute('x1', x(nearest));
    crosshair.setAttribute('x2', x(nearest));
    crosshair.setAttribute('visibility', 'visible');
    tooltip.hidden = false;
    tooltip.innerHTML = `<strong>${point.label}</strong><br>${point.value.toLocaleString()}${opts.valueSuffix || ''}` +
      (point.note ? `<br><span class="muted">${point.note}</span>` : '');
    tooltip.style.left = `${(x(nearest) / scale) + 12}px`;
    tooltip.style.top = `${(y(point.value) / scale) - 8}px`;
  });

  svg.addEventListener('mouseleave', () => {
    crosshair.setAttribute('visibility', 'hidden');
    tooltip.hidden = true;
  });

  return wrap;
}


/* Series colours, in the order they are handed out. Six tokens exist and were
   validated in both themes; a seventh series would wrap and two lines would
   share a colour, which is why the API refuses a seventh watched day long
   before it gets here. */
const SERIES_COLORS = [
  'var(--color-chart1)', 'var(--color-chart2)', 'var(--color-chart3)',
  'var(--color-chart4)', 'var(--color-chart5)', 'var(--color-chart6)',
];

/**
 * series: [{ name, points: [{ t: ISO string, value, muted? }] }]
 * opts:   { height, width, valueSuffix, ariaLabel, emptyText }
 *
 * Shared y-axis across every series, because the whole reason these are on one
 * chart is to be compared: per-series scaling would draw two candidates 200
 * crowns apart as though they were identical, which is the opposite of what
 * the panel is for.
 *
 * `muted` marks a point from a run the site refused part of. Drawn hollow and
 * joined by a dashed segment, exactly as in `lineChart` - the gap in the record
 * is worth seeing, but the number in it is not a measurement.
 */
export function multiLineChart(series, opts = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';

  const drawable = (series || []).filter((s) => s.points && s.points.length);
  if (!drawable.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = opts.emptyText || 'No observations yet.';
    wrap.appendChild(empty);
    return wrap;
  }

  const height = opts.height || 260;
  const width = Math.max(opts.width || 420, 420);
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;

  const all = drawable.flatMap((s) => s.points);
  const times = all.map((p) => new Date(p.t).getTime());
  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const values = all.map((p) => p.value);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);

  // Same reasoning as the single-series chart: starting at zero spends the
  // height on empty space and flattens the variation being looked for.
  const span = rawMax - rawMin || Math.max(1, rawMax * 0.1);
  const yMin = Math.max(0, rawMin - span * 0.15);
  const yMax = rawMax + span * 0.15;

  // A single observation, or several taken in the same minute, would divide by
  // zero and put every point at the left edge; centre them instead.
  const x = (t) => (tMax === tMin
    ? PAD.left + plotW / 2
    : PAD.left + ((new Date(t).getTime() - tMin) / (tMax - tMin)) * plotW);
  const y = (v) => PAD.top + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: 'img',
    'aria-label': opts.ariaLabel || 'Watched days over time',
  });

  for (const tick of niceTicks(yMin, yMax)) {
    svg.appendChild(svgEl('line', {
      x1: PAD.left, x2: width - PAD.right, y1: y(tick), y2: y(tick),
      stroke: 'var(--color-chartGrid)', 'stroke-width': 1,
    }));
    const label = svgEl('text', {
      x: PAD.left - 8, y: y(tick) + 4,
      'text-anchor': 'end', 'font-size': 11, fill: 'var(--color-chartAxis)',
    });
    label.textContent = tick.toLocaleString();
    svg.appendChild(label);
  }

  // Time axis: first and last observation only. Anything denser collides, and
  // the exact moment of a mid-series point is what the tooltip is for.
  [[tMin, 'start'], [tMax, 'end']].forEach(([stamp, anchor], index) => {
    if (index === 1 && tMax === tMin) return;
    const label = svgEl('text', {
      x: index === 0 ? PAD.left : width - PAD.right,
      y: height - 12, 'text-anchor': anchor,
      'font-size': 11, fill: 'var(--color-chartAxis)',
    });
    label.textContent = new Date(stamp).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
    svg.appendChild(label);
  });

  drawable.forEach((entry, index) => {
    const color = entry.color || SERIES_COLORS[index % SERIES_COLORS.length];
    const points = [...entry.points].sort(
      (a, b) => new Date(a.t).getTime() - new Date(b.t).getTime(),
    );

    for (let i = 1; i < points.length; i += 1) {
      const uncertain = points[i - 1].muted || points[i].muted;
      svg.appendChild(svgEl('path', {
        d: `M${x(points[i - 1].t)},${y(points[i - 1].value)} L${x(points[i].t)},${y(points[i].value)}`,
        fill: 'none', stroke: color,
        'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
        ...(uncertain ? { 'stroke-dasharray': '4 4', opacity: 0.45 } : {}),
      }));
    }

    points.forEach((p) => {
      svg.appendChild(svgEl('circle', {
        cx: x(p.t), cy: y(p.value), r: 4,
        fill: p.muted ? 'var(--color-panelBackground)' : color,
        stroke: p.muted ? color : 'var(--color-panelBackground)',
        'stroke-width': 2,
        ...(p.muted ? { opacity: 0.6 } : {}),
      }));
    });

    // The latest trustworthy value, labelled at the end of its own line. With
    // several series there is no single "cheapest point" worth calling out, and
    // labelling every point would be unreadable - but a line you cannot put a
    // number to is a line you have to hover to use.
    const trusted = points.filter((p) => !p.muted);
    if (trusted.length) {
      const last = trusted[trusted.length - 1];
      const text = svgEl('text', {
        x: Math.min(x(last.t) + 8, width - PAD.right),
        y: y(last.value) + 4,
        'text-anchor': x(last.t) > width - PAD.right - 40 ? 'end' : 'start',
        'font-size': 11, 'font-weight': 600, fill: color,
      });
      text.textContent = `${last.value.toLocaleString()}${opts.valueSuffix || ''}`;
      svg.appendChild(text);
    }
  });

  wrap.appendChild(svg);

  // Legend. `lineChart` needs none because its panel title names its one
  // series; here the whole point is telling several apart.
  const legend = document.createElement('div');
  legend.className = 'chart-legend';
  drawable.forEach((entry, index) => {
    const item = document.createElement('span');
    item.className = 'chart-legend__item';
    const swatch = document.createElement('span');
    swatch.className = 'chart-legend__swatch';
    swatch.style.background = entry.color || SERIES_COLORS[index % SERIES_COLORS.length];
    const name = document.createElement('span');
    name.textContent = entry.name;
    item.append(swatch, name);
    legend.appendChild(item);
  });
  wrap.appendChild(legend);

  const tooltip = document.createElement('div');
  tooltip.className = 'chart__tooltip';
  tooltip.hidden = true;
  wrap.appendChild(tooltip);

  // Nearest point across every series, so hovering between two lines names the
  // one actually closest rather than whichever was drawn last.
  svg.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const px = (event.clientX - box.left) * scale;
    const py = (event.clientY - box.top) * scale;

    let best = null;
    let bestDist = Infinity;
    drawable.forEach((entry, index) => {
      entry.points.forEach((p) => {
        const d = Math.hypot(x(p.t) - px, y(p.value) - py);
        if (d < bestDist) { bestDist = d; best = { entry, point: p, index }; }
      });
    });
    if (!best) return;

    tooltip.hidden = false;
    tooltip.innerHTML =
      `<strong>${best.entry.name}</strong><br>` +
      `${best.point.value.toLocaleString()}${opts.valueSuffix || ''}<br>` +
      `<span class="muted">${new Date(best.point.t).toLocaleString()}</span>` +
      (best.point.muted ? '<br><span class="muted">from a run that was refused part way</span>' : '');
    tooltip.style.left = `${(x(best.point.t) / scale) + 12}px`;
    tooltip.style.top = `${(y(best.point.value) / scale) - 8}px`;
  });

  svg.addEventListener('mouseleave', () => { tooltip.hidden = true; });

  return wrap;
}
