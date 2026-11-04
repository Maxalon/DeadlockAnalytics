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
  DL.tile = (it, opts = {}) => `<button type="button" class="tile slot-${esc(it.slot)}${opts.small ? " small" : ""}${opts.dim ? " dim" : ""}" data-item="${Number(it.id)}" ${opts.attrs || ""} title="${esc(it.name)}">
      <span class="tile-art"><span class="tile-tier">${Number(it.tier) === 5 ? "★" : "T" + Number(it.tier)}</span><img src="${esc(it.image)}" alt="" loading="lazy">${it.active ? '<span class="tile-active">Active</span>' : ""}${opts.note ? '<span class="tile-note"></span>' : ""}</span>
      <span class="tile-name">${esc(it.name)}</span>${opts.cost === false ? "" : `<span class="tile-cost souls">${num(it.cost)}</span>`}
    </button>`;

  const line = (p, big) => big
    ? `<div class="tip-big">${p.icon ? `<img src="${esc(p.icon)}" alt="">` : ""}<b>${esc(p.value)}</b><span>${esc(p.label)}</span></div>`
    : `<div class="tip-line${p.conditional ? " cond" : ""}">${p.icon ? `<img src="${esc(p.icon)}" alt="">` : ""}<b>${esc(p.value)}</b> ${esc(p.label)}${p.conditional ? " <small>(conditional)</small>" : ""}</div>`;

  function sections(list) {
    return list.map((s) => {
      if (s.type === "innate") {
        const rows = [...s.big, ...s.lines].map((p) => line(p, false)).join("");
        return rows || s.text ? `<div class="tip-innate">${s.text ? `<p class="tip-text">${html(s.text)}</p>` : ""}${rows}</div>` : "";
      }
      const head = s.type === "ability" ? "" : `<div class="tip-sec-head"><span>${s.type === "active" ? "Active" : "Passive"}</span><span class="cd">${s.cooldown ? `⟳ ${esc(s.cooldown)}` : ""}${s.duration ? ` ⧗ ${esc(s.duration)}` : ""}</span></div>`;
      return `<div class="tip-sec ${esc(s.type)}">${head}${s.text ? `<p class="tip-text">${html(s.text)}</p>` : ""}${s.big.length ? `<div class="tip-bigs">${s.big.map((p) => line(p, true)).join("")}</div>` : ""}${s.lines.length ? `<div class="tip-lines">${s.lines.map((p) => line(p, false)).join("")}</div>` : ""}</div>`;
    }).join("");
  }

  DL.itemTip = (it, extra = "") => {
    const from = it.components.map((id) => DL.items.get(id)).filter(Boolean);
    const into = it.upgrades_to.map((id) => DL.items.get(id)).filter(Boolean);
    const foot = (title, list) => list.length ? `<h4>${title}</h4><div class="tile-row">${list.map((x) => DL.tile(x, { small: true })).join("")}</div>` : "";
    return `<div class="tip slot-${esc(it.slot)}">
      <div class="tip-head"><img src="${esc(it.image)}" alt=""><div class="tip-title"><h3>${esc(it.name)}</h3><span class="tip-sub">${esc(SLOT_NAMES[it.slot] || it.slot)} · Tier ${Number(it.tier)}${it.active ? " · Active" : ""}</span></div><span class="souls">${num(it.cost)}</span></div>
      ${sections(it.tooltip)}
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
      ${sections(a.tooltip)}
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
   * opts.onPick(item, tileEl) is called on a tap (default: open the tooltip);
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
