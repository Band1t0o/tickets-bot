/*
 * Dependency-free inline-SVG line chart.
 *
 * Finance-planner uses recharts for this; hand-rolling it keeps a Node
 * toolchain out of a Python repo. Both charts here are single-series
 * change-over-time, so there is no legend: the panel title names the series.
 *
 * Colours come from the theme tokens (--color-chart1 for the series,
 * --color-chartGrid / --color-chartAxis for the frame), so the chart re-themes
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

  svg.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / box.width;
    const px = (event.clientX - box.left) * scale;
    // Nearest point, so the hit target is far bigger than the 4px marker.
    let nearest = 0;
    let bestDist = Infinity;
    points.forEach((_, i) => {
      const d = Math.abs(x(i) - px);
      if (d < bestDist) { bestDist = d; nearest = i; }
    });
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
