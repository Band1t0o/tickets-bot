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
 * points: [{ label: '2027-01-12', value: 27000 }]
 * opts:   { height, valueSuffix, xLabel }
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

  // 2px series line.
  const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${y(p.value)}`).join(' ');
  svg.appendChild(svgEl('path', {
    d: path, fill: 'none', stroke: 'var(--color-chart1)',
    'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // Markers, with a surface ring so overlapping points stay separable.
  points.forEach((p, i) => {
    svg.appendChild(svgEl('circle', {
      cx: x(i), cy: y(p.value), r: 4,
      fill: 'var(--color-chart1)',
      stroke: 'var(--color-panelBackground)', 'stroke-width': 2,
    }));
  });

  // Direct-label only the cheapest point: a number on every point is noise.
  const cheapestIndex = values.indexOf(rawMin);
  svg.appendChild(svgEl('circle', {
    cx: x(cheapestIndex), cy: y(rawMin), r: 6,
    fill: 'none', stroke: 'var(--color-chart1)', 'stroke-width': 2,
  }));
  const best = svgEl('text', {
    x: x(cheapestIndex),
    y: y(rawMin) - 14,
    'text-anchor': cheapestIndex === 0 ? 'start' : cheapestIndex === points.length - 1 ? 'end' : 'middle',
    'font-size': 12, 'font-weight': 600, fill: 'var(--color-pageText)',
  });
  best.textContent = `${rawMin.toLocaleString()}${opts.valueSuffix || ''}`;
  svg.appendChild(best);

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
