// Shared UI for the item, hero, build and editor pages: game data, item tiles, Deadlock-style
// tooltips (a popover on desktop, a bottom sheet on phones) and the shop.
// Any element with data-item="<id>" or data-ability="<heroId>:<abilityId>" opens its tooltip.
(() => {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Tooltip text is already sanitised on the server (gamedata.clean_html): rendered as markup.
  const html = (s) => s || "";
  const num = (n) => Number(n).toLocaleString();
  const SLOT_NAMES = { weapon: "Weapon", vitality: "Vitality", spirit: "Spirit" };
  const narrow = () => window.matchMedia("(max-width: 700px), (pointer: coarse)").matches;

  let dataPromise = null;
  const DL = {
    data: null, items: new Map(), heroes: new Map(), abilities: new Map(), esc, num, SLOT_NAMES,
    // The build whose stats tooltips apply to their numbers: { spirit, duration, range, radius }
    // (the last three in percent) and, per stat, `sources`: [name, amount, unit] rows for the
    // breakdown. Set by the build editor; null shows base values.
    build: null,
    load() {
      dataPromise ||= fetch("/gamedata.json").then((r) => {
        if (!r.ok) throw new Error(`game data: HTTP ${r.status}`);
        return r.json();
      }).then((d) => {
        DL.data = d;
        for (const it of d.items) DL.items.set(it.id, it);
        for (const h of d.heroes) {
          DL.heroes.set(h.id, h);
          for (const a of h.abilities) DL.abilities.set(a.id, { ...a, hero: h.id });
        }
        return d;
      });
      return dataPromise;
    },
  };

  /** An item as the shop shows it. opts: small, dim, note (annotation), attrs (extra attributes). */
  DL.tile = (it, opts = {}) => `<button type="button" class="tile slot-${esc(it.slot)}${opts.small ? " small" : ""}${opts.dim ? " dim" : ""}${it.active ? " active" : ""}" data-item="${Number(it.id)}" ${opts.attrs || ""} title="${esc(it.name)}">
      <span class="tile-art"><span class="tile-tier">${Number(it.tier) === 5 ? "★" : "T" + Number(it.tier)}</span><img src="${esc(it.image)}" alt="" loading="lazy">${it.imbue ? '<span class="tile-imbue">Imbue</span>' : ""}${opts.note ? '<span class="tile-note"></span>' : ""}</span>
      <span class="tile-name">${esc(it.name)}</span>${opts.cost === false ? "" : `<span class="tile-cost souls">${num(it.cost)}</span>`}
    </button>`;

  // Values and scaling as the game draws them (its citadel_ability_property_panel and
  // citadel_stat_scaling_label panels): the stat's arrow glyph with `x0.61` under it in the
  // top right corner; with a build, the current value next to the struck-through base value.
  const ARROWS = "https://assets-bucket.deadlock-api.com/assets-api-res/images/shop/keystat_";
  const SCALE_STATS = { spirit: ["spirit", "Spirit"], weapon: ["courage", "Weapon"], melee: ["melee", "Melee"], boon: ["boon", "Boons"] };
  const BUILD_MODS = { duration: "Ability Duration", range: "Ability Range", radius: "Radius" };
  /** A number as the game's tooltips print it: at most `dp` decimals, half up, no trailing zeros. */
  const round = (v, dp) => {
    const f = 10 ** dp;
    return String(Math.sign(v) * Math.round(Math.abs(v) * f + 1e-9) / f);
  };
  // What the game puts before a number: "+" by its sign, or a literal "+" / "-" (a slow is
  // written 40 and shown -40%).
  const lead = (v, c) => (v > 0 ? (c.sign === "sign" ? "+" : c.sign) : "");
  const fmt = (v, c, dp = c.dp) => `${lead(v, c)}${round(v, dp)}${c.postfix}`;
  /** A property's number with the build's stats applied (DL.build, set by the build editor):
   *  spirit scaling first, then the percentage bonuses to durations, ranges and radii.
   *  Returns the steps too, for the breakdown. */
  function calc(p) {
    const c = p.calc;
    const b = DL.build;
    const steps = [];
    let v = c.base;
    if (p.scale) {
      const applies = p.scale.stat === "spirit";
      const add = applies ? p.scale.per * (b ? b.spirit : 0) : null;
      steps.push({ kind: p.scale.stat, name: SCALE_STATS[p.scale.stat][1], times: p.scale.text, add, sources: applies && b ? b.sources.spirit : [] });
      v += add || 0;
    }
    for (const [k, strength] of Object.entries(c.mods)) {
      const pct = strength * ((b && b[k]) || 0);
      const add = (v * pct) / 100;
      steps.push({ kind: "mod", name: BUILD_MODS[k], pct, add, sources: b ? b.sources[k] : [] });
      v += add;
    }
    return { value: v, steps, changed: Math.abs(v - c.base) > 1e-9 };
  }
  /** `12.5s` with the unit set smaller, as the game does. */
  const number = (text) => {
    const m = /^([+-]?[\d.,]+)(.*)$/.exec(text);
    return m ? `${esc(m[1])}${m[2] ? `<small>${esc(m[2])}</small>` : ""}` : esc(text);
  };
  function shown(p) {
    const now = p.calc ? calc(p) : null;
    return now?.changed ? `<s>${number(p.value)}</s><b>${number(fmt(now.value, p.calc))}</b>` : `<b>${number(p.value)}</b>`;
  }
  function scaling(sc) {
    if (!sc || !SCALE_STATS[sc.stat]) return "";
    const [img, name] = SCALE_STATS[sc.stat];
    return `<span class="sc s-${esc(sc.stat)}" title="${esc(name)} x${esc(sc.text)}"><img src="${ARROWS}${img}_arrow.png" alt="${esc(name)}"><span><i>x</i>${esc(sc.text)}</span></span>`;
  }
  const icon = (p) => (p.icon ? `<img src="${esc(p.icon)}" alt="">` : "");
  // A calculated value opens its breakdown on a click (data-formula, handled below).
  const formula = (p) => (p.calc ? ` data-formula="${esc(JSON.stringify({ label: p.label, value: p.value, calc: p.calc, scale: p.scale }))}" role="button" tabindex="0"` : "");
  const line = (p, big) => big
    ? `<div class="tip-big${p.scale ? ` scaled s-${esc(p.scale.stat)}` : ""}"${formula(p)}><div class="tip-val">${icon(p)}${shown(p)}</div><span>${esc(p.label)}</span>${scaling(p.scale)}</div>`
    : `<div class="tip-line${p.conditional ? " cond" : ""}"${formula(p)}>${icon(p)}<span class="tip-val">${shown(p)}</span> ${esc(p.label)}${p.conditional ? " <small>(conditional)</small>" : ""}${scaling(p.scale)}</div>`;

  /** The game's stat formula panel for one value: base, what each stat adds and where that
   *  stat comes from, and the total, to two decimals. */
  function formulaTip(p) {
    const c = p.calc;
    const whole = (v) => `${lead(v, c)}${round(v, 2)}${esc(c.postfix)}`;
    // What a stat adds: with a minus where the game shows the value as a reduction.
    const added = (v) => `${v > 0 && c.sign === "-" ? "-" : v >= 0 ? "+" : ""}${round(v, 2)}${esc(c.postfix)}`;
    const row = (cls, name, value, extra = "") => `<div class="${cls}"><span class="f-name">${esc(name)}${extra}</span><span class="f-value">${value}</span></div>`;
    const r = calc(p);
    const rows = [row("f-main", "Base", whole(c.base))];
    for (const st of r.steps) {
      const times = st.kind === "mod" ? `<small>+${round(st.pct, 2)}%</small>` : `<small>x (${esc(st.times)})</small>`;
      const zero = !st.add ? " f-zero" : "";
      rows.push(row(`f-main f-${esc(st.kind)}${zero}`, st.name, st.add === null ? "–" : added(st.add), times));
      for (const [name, value, unit] of st.sources || []) rows.push(row("f-sub", name, `${value > 0 ? "+" : ""}${round(value, 2)}${esc(unit || "")}`));
    }
    return `<div class="formula"><h4>${esc(p.label)}</h4>${rows.join("")}${row("f-total", "Total", whole(r.value))}</div>`;
  }

  const hint = (list) => (list.some((s) => [...s.big, ...s.lines].some((p) => p.calc))
    ? '<p class="tip-hint">Tap a value to see how it is calculated.</p>' : "");

  function sections(list) {
    return list.map((s) => {
      if (s.type === "innate") {
        const rows = [...s.big, ...s.lines].map((p) => line(p, false)).join("");
        return rows || s.text ? `<div class="tip-innate">${s.text ? `<p class="tip-text">${html(s.text)}</p>` : ""}${rows}</div>` : "";
      }
      const head = s.type === "ability" ? "" : `<div class="tip-sec-head"><span>${s.type === "active" ? "Active" : "Passive"}</span><span class="cd">${s.cooldown ? `⟳ ${esc(s.cooldown)}` : ""}${s.duration ? ` <span class="tip-val">⧗ ${shown({ value: s.duration, calc: s.duration_calc })}</span>` : ""}</span></div>`;
      return `<div class="tip-sec ${esc(s.type)}">${head}${s.text ? `<p class="tip-text">${html(s.text)}</p>` : ""}${s.big.length ? `<div class="tip-bigs">${s.big.map((p) => line(p, true)).join("")}</div>` : ""}${s.lines.length ? `<div class="tip-lines">${s.lines.map((p) => line(p, false)).join("")}</div>` : ""}</div>`;
    }).join("");
  }

  DL.itemTip = (it, extra = "") => {
    const from = it.components.map((id) => DL.items.get(id)).filter(Boolean);
    const into = it.upgrades_to.map((id) => DL.items.get(id)).filter(Boolean);
    const foot = (title, list) => list.length ? `<h4>${title}</h4><div class="tile-row">${list.map((x) => DL.tile(x, { small: true })).join("")}</div>` : "";
    return `<div class="tip slot-${esc(it.slot)}">
      <div class="tip-head"><img src="${esc(it.image)}" alt=""><div class="tip-title"><h3>${esc(it.name)}</h3><span class="tip-sub">${esc(SLOT_NAMES[it.slot] || it.slot)} · Tier ${Number(it.tier)}${it.active ? " · Active" : ""}</span></div><span class="souls">${num(it.cost)}</span></div>
      ${sections(it.tooltip)}${hint(it.tooltip)}
      ${from.length || into.length ? `<div class="tip-foot">${foot("Builds from", from)}${foot("Upgrades into", into)}</div>` : ""}
      ${extra}
    </div>`;
  };

  DL.abilityTip = (a, extra = "") => {
    const head = a.header || {};
    const tiers = (a.upgrades || []).slice(0, 3).map((u, i) => `<div class="tip-tier"><span class="ap">${[1, 2, 5][i]} AP</span>${u.text ? html(u.text) : esc(u.bonuses.join(", "))}</div>`).join("");
    return `<div class="tip">
      <div class="tip-head"><img src="${esc(a.image)}" alt="" style="background:#222;border-radius:8px;padding:4px"><div class="tip-title"><h3>${esc(a.name)}</h3><span class="tip-sub">${a.ultimate ? "Ultimate" : "Ability"}${a.quip ? " · " + html(a.quip) : ""}</span></div>
        <span class="cd" style="text-align:right;font-size:.85rem">${head.cooldown ? `⟳ ${esc(head.cooldown)}` : ""}${head.charges ? `<br>${esc(head.charges)} charges` : ""}</span></div>
      ${sections(a.tooltip)}${hint(a.tooltip)}
      ${tiers ? `<div class="tip-tiers">${tiers}</div>` : ""}
      ${extra}
    </div>`;
  };

  // ---- popover / sheet ----
  let pop = null;
  let popFor = null;
  function closePop() {
    pop?.remove();
    pop = null;
    popFor = null;
  }
  DL.closeSheet = () => document.querySelector(".sheet-backdrop")?.remove();
  /** A bottom sheet with arbitrary content; `onOpen(sheetEl)` wires its controls. */
  DL.sheet = (content, onOpen) => {
    DL.closeSheet();
    closePop();
    const back = document.createElement("div");
    back.className = "sheet-backdrop";
    back.innerHTML = `<div class="sheet" role="dialog" aria-modal="true"><div class="sheet-grip"></div><button type="button" class="sheet-close" aria-label="Close">✕</button>${content}</div>`;
    back.addEventListener("click", (e) => {
      if (e.target === back || e.target.closest(".sheet-close")) DL.closeSheet();
    });
    document.body.appendChild(back);
    onOpen?.(back.querySelector(".sheet"));
    return back.querySelector(".sheet");
  };
  function tipFor(el) {
    if (el.dataset.item) {
      const it = DL.items.get(Number(el.dataset.item));
      return it && DL.itemTip(it);
    }
    if (el.dataset.ability) {
      const a = DL.abilities.get(Number(el.dataset.ability.split(":").pop()));
      return a && DL.abilityTip(a);
    }
    return null;
  }
  function showPop(el) {
    const content = tipFor(el);
    if (!content) return;
    closePop();
    pop = document.createElement("div");
    pop.className = "tip-pop";
    pop.innerHTML = content;
    document.body.appendChild(pop);
    popFor = el;
    const r = el.getBoundingClientRect();
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    let x = r.right + 10;
    if (x + w > innerWidth - 8) x = Math.max(8, r.left - w - 10);
    let y = Math.min(Math.max(8, r.top), innerHeight - h - 8);
    pop.style.left = `${x}px`;
    pop.style.top = `${Math.max(8, y)}px`;
  }
  /** Open the tooltip for an element in a sheet (phones, or a click on desktop for pages without another click action). */
  DL.openTip = (el, extra = "") => {
    const content = el.dataset.item
      ? DL.itemTip(DL.items.get(Number(el.dataset.item)), extra)
      : DL.abilityTip(DL.abilities.get(Number(el.dataset.ability.split(":").pop())), extra);
    return DL.sheet(content);
  };

  document.addEventListener("mouseover", (e) => {
    if (narrow() || !DL.data) return;
    const el = e.target.closest("[data-item],[data-ability]");
    if (el && el !== popFor && !el.closest(".tip-pop, .sheet")) showPop(el);
    else if (!el && pop) closePop();
  });
  document.addEventListener("scroll", closePop, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closePop();
      DL.closeSheet();
    }
  });
  // ---- long press ----
  const HOLD_MS = 450;
  let swallowClicksUntil = 0;
  // The tap a browser may still deliver when the finger lifts after a long press: dropped, so it
  // doesn't press whatever the press opened under the finger. Registered before every other
  // click handling here.
  document.addEventListener("click", (e) => {
    if (performance.now() < swallowClicksUntil) {
      e.stopPropagation();
      e.preventDefault();
    }
  }, true);
  /**
   * A long press on a touch screen: `handler(el)` for the element matching `selector` under the
   * finger once it has rested for HOLD_MS; moving the finger (a scroll) cancels. The browser's
   * own long-press menu (Chrome's for images and links) and the tap that would follow are
   * suppressed. Chrome reports a long press as `contextmenu`, iOS Safari doesn't, so both that
   * and a timer are used. A mouse is left alone: it has hover and its right click.
   */
  DL.onHold = (root, selector, handler) => {
    let timer = null;
    let start = null;
    let held = false;
    let touch = false;
    const cancel = () => { clearTimeout(timer); timer = null; };
    const fire = (el) => {
      if (held || !el.isConnected) return;
      held = true;
      cancel();
      swallowClicksUntil = performance.now() + 4000;  // until the finger lifts
      navigator.vibrate?.(10);
      handler(el);
    };
    root.addEventListener("pointerdown", (e) => {
      touch = e.pointerType !== "mouse";
      held = false;
      cancel();
      const el = e.target.closest(selector);
      if (!touch || !el || !e.isPrimary) return;
      start = { x: e.clientX, y: e.clientY };
      timer = setTimeout(() => fire(el), HOLD_MS);
    });
    root.addEventListener("pointermove", (e) => {
      if (timer && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) cancel();
    });
    const lift = () => {
      cancel();
      if (held) swallowClicksUntil = performance.now() + 350;
      held = false;
    };
    // On window: by the time the finger lifts, the press may have opened a sheet over `root`.
    for (const type of ["pointerup", "pointercancel"]) window.addEventListener(type, lift, true);
    root.addEventListener("contextmenu", (e) => {
      const el = e.target.closest(selector);
      if (!touch || !el) return;
      e.preventDefault();
      fire(el);
    });
  };

  // ---- breakdown of a calculated value ----
  let formulaPop = null;
  const closeFormula = () => {
    formulaPop?.source.classList.remove("open");
    formulaPop?.remove();
    formulaPop = null;
  };
  function openFormula(el) {
    closeFormula();
    formulaPop = document.createElement("div");
    formulaPop.className = "formula-pop";
    formulaPop.innerHTML = formulaTip(JSON.parse(el.dataset.formula));
    formulaPop.source = el;
    el.classList.add("open");
    document.body.appendChild(formulaPop);
    const r = el.getBoundingClientRect();
    const w = formulaPop.offsetWidth;
    const h = formulaPop.offsetHeight;
    const x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
    const y = r.bottom + 6 + h > innerHeight - 8 ? Math.max(8, r.top - h - 6) : r.bottom + 6;
    formulaPop.style.left = `${x}px`;
    formulaPop.style.top = `${y}px`;
  }
  // In the capture phase: with a breakdown open, a tap anywhere else only closes it. The tap
  // goes no further, so on a phone it doesn't also close the sheet underneath or press a button.
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-formula]");
    if (formulaPop) {
      const again = el === formulaPop.source;
      const inside = e.target.closest(".formula-pop");
      closeFormula();
      if (!el || again || inside) {
        e.stopPropagation();
        e.preventDefault();
        return;
      }
    }
    if (el) openFormula(el);
  }, true);
  document.addEventListener("keydown", (e) => {
    const el = e.target.closest?.("[data-formula]");
    if (e.key === "Escape") closeFormula();
    else if (el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openFormula(el); }
  });
  document.addEventListener("scroll", closeFormula, { passive: true, capture: true });

  // Pages without their own click handling: a tap opens the tooltip.
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-item],[data-ability]");
    // isConnected: a handler may have re-rendered the element away before this runs.
    if (!el || !DL.data || !el.isConnected || el.closest("[data-own-clicks]")) return;
    e.preventDefault();
    DL.openTip(el);
  });

  // ---- shop ----
  /**
   * The in-game shop in `root`: category tabs, a search box, one row per tier.
   * opts.onPick(item, tileEl) is called on a tap (default: open the tooltip); with it, a
   * long press on a touch screen calls opts.onHold(item, tileEl) (default: open the tooltip);
   * opts.dim(item) dims owned items.
   */
  DL.shop = (root, opts = {}) => {
    const state = { slot: opts.slot || "weapon", q: "" };
    root.classList.add("shop");
    root.setAttribute("data-own-clicks", "");
    root.innerHTML = `<div class="shop-tabs">${Object.entries(SLOT_NAMES).map(([k, v]) => `<button type="button" class="slot-${k}" data-slot="${k}">${v}</button>`).join("")}</div>
      <div class="shop-search"><input type="search" placeholder="Search all items or stats (e.g. lifesteal)" aria-label="Search items"></div>
      <div class="shop-body"></div>`;
    const body = root.querySelector(".shop-body");
    function render() {
      for (const b of root.querySelectorAll(".shop-tabs button")) b.classList.toggle("on", !state.q && b.dataset.slot === state.slot);
      const q = state.q.trim().toLowerCase();
      const list = DL.data.items.filter((it) => (q ? it.search.includes(q) : it.slot === state.slot));
      const tiers = [...new Set(list.map((it) => it.tier))].sort((a, b) => a - b);
      body.innerHTML = tiers.map((t) => `<div class="shop-tier"><div class="shop-tier-label">T${t}${DL.data.tier_cost[t] ? `<span class="souls">${num(DL.data.tier_cost[t])}</span>` : ""}</div>
          <div class="tile-row">${list.filter((it) => it.tier === t).map((it) => DL.tile(it, { dim: opts.dim?.(it), cost: !!q })).join("")}</div></div>`).join("")
        || '<p class="shop-empty">No items match.</p>';
    }
    root.querySelector(".shop-tabs").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      state.slot = b.dataset.slot;
      state.q = "";
      root.querySelector("input").value = "";
      render();
    });
    root.querySelector("input").addEventListener("input", (e) => { state.q = e.target.value; render(); });
    body.addEventListener("click", (e) => {
      const el = e.target.closest("[data-item]");
      if (!el) return;
      e.stopPropagation();
      const it = DL.items.get(Number(el.dataset.item));
      if (opts.onPick) opts.onPick(it, el);
      else DL.openTip(el);
    });
    // Where a tap does something else, the tooltip is a long press away on a touch screen.
    if (opts.onPick) {
      root.classList.add("holdable");
      DL.onHold(body, "[data-item]", (el) => (opts.onHold ? opts.onHold(DL.items.get(Number(el.dataset.item)), el) : DL.openTip(el)));
    }
    render();
    /** Only re-dim owned items: no re-render, so the images don't load again. */
    const refresh = () => {
      for (const el of body.querySelectorAll("[data-item]")) el.classList.toggle("dim", !!opts.dim?.(DL.items.get(Number(el.dataset.item))));
    };
    return { render, refresh };
  };

  window.DL = DL;
  DL.load().catch(() => {});
})();
