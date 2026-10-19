// Hover box for the server-rendered score chart (charts.score_trend). Each match is an <a class="hit">
// column carrying its details as data-* attributes; this only shows them nicer than the native
// tooltip and moves a crosshair. Clicking the column opens the match without any of this.
(function () {
  const VIEW_W = 720;

  function row(tip, key, label, value, strong) {
    const r = document.createElement('div');
    r.className = 'tip-row';
    const k = document.createElement('i');
    k.className = key;
    const v = document.createElement(strong ? 'strong' : 'span');
    v.textContent = value;
    const l = document.createElement('span');
    l.className = 'muted';
    l.textContent = label;
    r.append(k, v, l);
    tip.append(r);
  }

  for (const wrap of document.querySelectorAll('.chart-wrap')) {
    const svg = wrap.querySelector('svg');
    const tip = wrap.querySelector('.chart-tip');
    const cross = svg.querySelector('.crosshair');
    const ring = svg.querySelector('.focus-ring');
    let shown = null;

    function show(hit) {
      if (shown === hit) return;
      shown = hit;
      const d = hit.dataset;
      tip.replaceChildren();
      const head = document.createElement('div');
      head.className = 'tip-head';
      head.textContent = `${d.hero} · ${d.result}`;
      const date = document.createElement('div');
      date.className = 'muted';
      date.textContent = d.date;
      tip.append(head, date);
      row(tip, 'key-you', 'your score', d.score, true);
      if (d.mvp) row(tip, 'key-mvp', `MVP score (${d.gap} to it)`, d.mvp, false);
      row(tip, '', 'K / D / A', d.kda, false);
      if (d.rank) row(tip, d.rank === 'MVP' ? 'dot-mvp' : 'dot-key', 'Valve', d.rank, false);
      const hint = document.createElement('div');
      hint.className = 'muted tip-hint';
      hint.textContent = 'click for the match';
      tip.append(hint);

      cross.setAttribute('x1', d.x);
      cross.setAttribute('x2', d.x);
      ring.setAttribute('cx', d.x);
      ring.setAttribute('cy', d.y);
      svg.classList.add('active');
      tip.hidden = false;
      // Beside the hovered match, flipped to its left in the right half so it never overflows.
      const scale = svg.clientWidth / VIEW_W;
      const px = Number(d.x) * scale;
      const right = px > svg.clientWidth / 2;
      tip.style.left = right ? '' : `${px + 14}px`;
      tip.style.right = right ? `${svg.clientWidth - px + 14}px` : '';
    }

    function hide() {
      shown = null;
      tip.hidden = true;
      svg.classList.remove('active');
    }

    for (const hit of svg.querySelectorAll('.hit')) {
      hit.addEventListener('pointerenter', () => show(hit));
      hit.addEventListener('focus', () => show(hit));
      // The native tooltip would double the custom one once script runs.
      hit.querySelector('title')?.remove();
    }
    svg.addEventListener('pointerleave', hide);
    svg.addEventListener('focusout', (e) => { if (!svg.contains(e.relatedTarget)) hide(); });
  }
})();
