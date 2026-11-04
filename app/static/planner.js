// Build planner: pick a hero, add shop items in buy order, see souls per category, the
// investment bonuses they reach and the summed always-on item stats. Game data comes from
// /gamedata.json (cached by the service worker, so it also works offline).
(() => {
  const $ = (id) => document.getElementById(id);
  // Only integers get into the markup below, whatever the page's JSON holds.
  const initial = JSON.parse($("planner-state").textContent) || {};
  const state = {
    hero: Number.isInteger(initial.hero) ? initial.hero : null,
    items: (Array.isArray(initial.items) ? initial.items : []).map(Number).filter(Number.isInteger),
  };
  const filter = { slot: "", tier: "", q: "" };
  let data = null;
  let items = new Map();
  let heroes = new Map();

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmt = (n) => (Math.round(n * 100) / 100).toLocaleString();
  const signed = (n) => (n > 0 ? "+" : "") + fmt(n);

  /** Items whose stats count: a component is replaced by the item it upgrades into. */
  function consumed() {
    const inBuild = new Set(state.items);
    const out = new Set();
    for (const id of state.items) for (const c of items.get(id).components) if (inBuild.has(c)) out.add(c);
    return out;
  }

  function summary() {
    const gone = consumed();
    const spent = { weapon: 0, vitality: 0, spirit: 0 };
    let total = 0;
    for (const id of state.items) {
      const it = items.get(id);
      if (gone.has(id)) continue;
      spent[it.slot] = (spent[it.slot] || 0) + it.cost;
      total += it.cost;
    }
    return { gone, spent, total };
  }

  function renderSummary(s) {
    const hero = heroes.get(state.hero);
    const cells = ["weapon", "vitality", "spirit"].map((slot) => {
      const [label, unit] = data.investment[slot];
      let bonus = "";
      if (hero) {
        const steps = hero.investment[slot] || [];
        const reached = steps.filter(([t]) => t <= s.spent[slot]).pop();
        const next = steps.find(([t]) => t > s.spent[slot]);
        bonus = `<span class="pl-bonus">${reached ? `+${fmt(reached[1])}${unit}` : "—"} ${esc(label.toLowerCase())}</span>`
          + (next ? `<span class="muted small">+${fmt(next[1])}${unit} at ${next[0].toLocaleString()} (${(next[0] - s.spent[slot]).toLocaleString()} to go)</span>` : "");
      }
      return `<div class="pl-cat slot-${slot}"><span class="muted small">${slot[0].toUpperCase() + slot.slice(1)}</span><b>${s.spent[slot].toLocaleString()}</b>${bonus}</div>`;
    });
    const n = state.items.length - s.gone.size;
    $("pl-summary").innerHTML = `<div class="pl-total"><b>${s.total.toLocaleString()}</b> souls · ${n} item${n === 1 ? "" : "s"}</div>${cells.join("")}`
      + (hero ? "" : '<p class="muted small">Pick a hero to see investment bonuses.</p>');
  }

  function renderBuild() {
    const s = summary();
    renderSummary(s);
    let running = 0;
    $("pl-items").innerHTML = state.items.map((id, i) => {
      const it = items.get(id);
      const owned = it.components.filter((c) => state.items.indexOf(c) > -1 && state.items.indexOf(c) < i);
      const pay = it.cost - owned.reduce((a, c) => a + items.get(c).cost, 0);
      running += pay;
      const gone = s.gone.has(id);
      return `<li class="slot-${esc(it.slot)}${gone ? " gone" : ""}">
        <img class="item-icon slot-${esc(it.slot)}" src="${esc(it.image)}" alt="" width="32" height="32" loading="lazy">
        <a href="/items/${Number(it.id)}">${esc(it.name)}</a>
        <span class="muted small">${gone ? "upgraded" : `${pay.toLocaleString()} · Σ ${running.toLocaleString()}`}</span>
        <span class="pl-actions"><button type="button" class="link" data-up="${i}" title="Buy earlier" ${i ? "" : "disabled"}>↑</button><button type="button" class="link" data-rm="${i}" title="Remove">✕</button></span>
      </li>`;
    }).join("");
    $("pl-empty").hidden = state.items.length > 0;

    const totals = new Map();
    for (const id of state.items) {
      if (s.gone.has(id)) continue;
      for (const st of items.get(id).stats) {
        const key = `${st.stat}|${st.postfix}`;
        const t = totals.get(key) || { label: st.label, postfix: st.postfix, value: 0 };
        t.value += st.value;
        totals.set(key, t);
      }
    }
    $("pl-stats").innerHTML = [...totals.values()].sort((a, b) => a.label.localeCompare(b.label))
      .map((t) => `<li class="given"><b>${signed(t.value)}${esc(t.postfix)}</b> ${esc(t.label)}</li>`).join("")
      || '<li class="muted">No items yet.</li>';

    $("pl-f-hero").value = state.hero || "";
    $("pl-f-items").value = state.items.join(",");
    $("pl-save-btn").disabled = !state.hero;
    renderGrid();
  }

  function renderGrid() {
    const q = filter.q.toLowerCase();
    const inBuild = new Set(state.items);
    const list = data.items.filter((it) => (!filter.slot || it.slot === filter.slot)
      && (!filter.tier || it.tier === Number(filter.tier))
      && (!q || it.name.toLowerCase().includes(q) || it.stats.some((s) => s.label.toLowerCase().includes(q))));
    $("pl-grid").innerHTML = list.map((it) => `<button type="button" class="pl-shop-item slot-${it.slot}${inBuild.has(it.id) ? " owned" : ""}" data-add="${it.id}" title="${esc(it.name)}">
        <img class="item-icon slot-${it.slot}" src="${esc(it.image)}" alt="" width="40" height="40" loading="lazy">
        <span class="pl-name">${esc(it.name)}</span><span class="muted small">${it.cost.toLocaleString()}</span></button>`).join("")
      || '<p class="muted">No items match.</p>';
  }

  function seg(id, key) {
    $(id).addEventListener("click", (e) => {
      const a = e.target.closest("a");
      if (!a) return;
      for (const x of $(id).querySelectorAll("a")) x.classList.toggle("on", x === a);
      filter[key] = a.dataset[key];
      renderGrid();
    });
  }

  function shareUrl() {
    return `${location.origin}/planner?hero=${state.hero || ""}&items=${state.items.join(",")}`;
  }

  async function load() {
    try {
      const resp = await fetch("/gamedata.json");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      data = await resp.json();
    } catch (err) {
      $("pl-grid").innerHTML = `<p class="neg">Game data isn't available yet (${esc(err.message)}). Reload in a moment.</p>`;
      return;
    }
    items = new Map(data.items.map((it) => [it.id, it]));
    heroes = new Map(data.heroes.map((h) => [h.id, h]));
    state.items = state.items.filter((id) => items.has(id));
    $("pl-hero").insertAdjacentHTML("beforeend", data.heroes.map((h) => `<option value="${h.id}">${esc(h.name)}</option>`).join(""));
    $("pl-hero").value = state.hero || "";
    $("pl-hero").addEventListener("change", (e) => { state.hero = Number(e.target.value) || null; renderBuild(); });
    $("pl-grid").addEventListener("click", (e) => {
      const b = e.target.closest("[data-add]");
      if (!b) return;
      const id = Number(b.dataset.add);
      if (!state.items.includes(id)) state.items.push(id);
      renderBuild();
    });
    $("pl-items").addEventListener("click", (e) => {
      const up = e.target.closest("[data-up]");
      const rm = e.target.closest("[data-rm]");
      if (up) {
        const i = Number(up.dataset.up);
        [state.items[i - 1], state.items[i]] = [state.items[i], state.items[i - 1]];
      } else if (rm) {
        state.items.splice(Number(rm.dataset.rm), 1);
      } else return;
      renderBuild();
    });
    $("pl-q").addEventListener("input", (e) => { filter.q = e.target.value; renderGrid(); });
    seg("pl-slot", "slot");
    seg("pl-tier", "tier");
    $("pl-share").addEventListener("click", async () => {
      const url = shareUrl();
      try {
        if (navigator.share) await navigator.share({ url });
        else await navigator.clipboard.writeText(url);
        $("pl-share").textContent = "Link copied";
      } catch {
        prompt("Link to this build:", url);
      }
    });
    renderBuild();
  }

  load();
})();
