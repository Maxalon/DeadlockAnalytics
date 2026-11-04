// Build editor, modelled on the game's: named categories with descriptions and an "optional"
// flag, items with annotations and sell priority, the ability point order, souls per shop
// category with investment bonuses, and summed stats. State is the game's own build format
// (CMsgHeroBuild.details), so builds copy losslessly from and to the game.
(() => {
  const $ = (id) => document.getElementById(id);
  const DL = window.DL;
  const { esc, num } = DL;
  const init = JSON.parse($("editor-state").textContent) || {};
  const UNLOCK = 2, POINTS = 1, TIER_COST = [1, 2, 5];

  const toInt = (v) => (v !== null && v !== "" && Number.isInteger(Number(v)) ? Number(v) : null);
  const state = {
    id: toInt(init.id),
    hero: toInt(init.hero),
    name: String(init.name || ""),
    description: String(init.description || ""),
    gameBuildId: toInt(init.game_build_id),
    cats: ((init.details || {}).mod_categories || []).map((c) => ({
      name: String(c.name || ""),
      description: String(c.description || ""),
      optional: !!c.optional,
      mods: (c.mods || []).map((m) => ({ id: toInt(m.ability_id), note: String(m.annotation || ""), prio: toInt(m.sell_priority) }))
        .filter((m) => m.id !== null),
    })),
    order: (((init.details || {}).ability_order || {}).currency_changes || [])
      .map((c) => ({ id: toInt(c.ability_id), type: toInt(c.currency_type), delta: toInt(c.delta) }))
      .filter((c) => c.id !== null),
  };
  if (!state.cats.length) state.cats.push({ name: "Items", description: "", optional: false, mods: [] });
  let target = 0;
  let dirty = false;
  let shop = null;

  const hero = () => DL.heroes.get(state.hero);
  const markDirty = () => { dirty = true; $("ed-msg").textContent = ""; };
  window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });

  // ---- phone tabs ----
  document.querySelector(".ed-tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (!b) return;
    for (const x of document.querySelectorAll(".ed-tabs [data-tab]")) x.classList.toggle("on", x === b);
    for (const p of document.querySelectorAll(".ed-pane")) p.classList.toggle("on", p.dataset.pane === b.dataset.tab);
    window.scrollTo({ top: $("editor").offsetTop - 4 });
  });

  // ---- the build ----
  function owned() {
    const all = new Set(state.cats.flatMap((c) => c.mods.map((m) => m.id)));
    // A component is replaced by what it upgrades into: its souls and stats don't count twice.
    const gone = new Set();
    for (const id of all) for (const c of DL.items.get(id)?.components || []) if (all.has(c)) gone.add(c);
    return { all, gone };
  }

  function renderHero() {
    const h = hero();
    $("ed-hero").innerHTML = h ? `<img src="${esc(h.card || h.icon)}" alt=""><span>${esc(h.name)} · change</span>` : "<span>Pick a hero</span>";
  }

  function targetLine() {
    const c = state.cats[target];
    return c ? `Adding to <b>${esc(c.name || "Items")}</b> · tap an item to add it` : "";
  }

  function renderCats() {
    const { gone } = owned();
    $("ed-cats").innerHTML = state.cats.map((c, ci) => `
      <section class="cat ed-cat${ci === target ? " target" : ""}" data-cat="${ci}">
        <div class="cat-head">
          <input type="text" value="${esc(c.name)}" maxlength="64" data-field="name" aria-label="Category name" placeholder="Category name">
          <button type="button" class="target-btn" data-act="target" title="Items from the shop go here">${ci === target ? "Adding here" : "Add here"}</button>
          <button type="button" class="ic${c.optional ? " on" : ""}" data-act="optional" title="Optional categories aren't added to quick-buy in game">${c.optional ? "Optional" : "Core"}</button>
          <button type="button" class="ic" data-act="up" title="Move category up" ${ci ? "" : "disabled"}>↑</button>
          <button type="button" class="ic" data-act="del" title="Delete category">✕</button>
        </div>
        <textarea rows="1" maxlength="2000" data-field="description" placeholder="Category description (optional)">${esc(c.description)}</textarea>
        <div class="tile-row">
          ${c.mods.map((m, mi) => {
            const it = DL.items.get(m.id);
            return it ? `<div data-mod="${mi}">${DL.tile(it, { note: !!m.note, dim: gone.has(m.id) })}${m.note ? `<div class="cat-note">${esc(m.note)}</div>` : ""}</div>` : "";
          }).join("") || `<span class="drop-hint">${ci === target ? "Tap items in the shop to add them here." : "Empty. Tap “Add here”, then items in the shop."}</span>`}
        </div>
      </section>`).join("");
    const n = state.cats.reduce((a, c) => a + c.mods.length, 0);
    $("ed-count").textContent = n ? `(${n})` : "";
    $("ed-target").innerHTML = targetLine();
  }

  $("ed-cats").addEventListener("input", (e) => {
    const el = e.target.closest("[data-field]");
    if (!el) return;
    const ci = Number(el.closest("[data-cat]").dataset.cat);
    state.cats[ci][el.dataset.field] = el.value;
    if (el.dataset.field === "name" && ci === target) $("ed-target").innerHTML = targetLine();
    markDirty();
  });
  $("ed-cats").addEventListener("click", (e) => {
    const catEl = e.target.closest("[data-cat]");
    if (!catEl) return;
    const ci = Number(catEl.dataset.cat);
    const act = e.target.closest("[data-act]")?.dataset.act;
    const modEl = e.target.closest("[data-mod]");
    if (act === "target") target = ci;
    else if (act === "optional") { state.cats[ci].optional = !state.cats[ci].optional; markDirty(); }
    else if (act === "up" && ci > 0) {
      [state.cats[ci - 1], state.cats[ci]] = [state.cats[ci], state.cats[ci - 1]];
      if (target === ci) target = ci - 1; else if (target === ci - 1) target = ci;
      markDirty();
    } else if (act === "del") {
      if (state.cats[ci].mods.length && !confirm(`Delete “${state.cats[ci].name || "Items"}” and its ${state.cats[ci].mods.length} items?`)) return;
      state.cats.splice(ci, 1);
      if (!state.cats.length) state.cats.push({ name: "Items", description: "", optional: false, mods: [] });
      target = Math.min(target, state.cats.length - 1);
      markDirty();
    } else if (modEl) return editMod(ci, Number(modEl.dataset.mod));
    else if (!e.target.closest("input, textarea")) target = ci;
    else return;
    renderAll();
  });
  $("ed-add-cat").addEventListener("click", () => {
    state.cats.push({ name: "New category", description: "", optional: false, mods: [] });
    target = state.cats.length - 1;
    markDirty();
    renderAll();
    $("ed-cats").lastElementChild.querySelector("input").select();
  });

  /** An item in the build: its tooltip plus annotation, sell priority, category, order, remove. */
  function editMod(ci, mi) {
    const m = state.cats[ci].mods[mi];
    const it = DL.items.get(m.id);
    const form = `<div class="sheet-form">
        <label for="ed-note">Annotation (shown in game)</label><input type="text" id="ed-note" maxlength="200" value="${esc(m.note)}" placeholder="e.g. buy when behind, rush vs. healers">
        <label for="ed-prio">Sell priority (1–100, empty for none)</label><input type="number" id="ed-prio" min="1" max="100" value="${m.prio ?? ""}">
        <label for="ed-move">Category</label><select id="ed-move">${state.cats.map((c, i) => `<option value="${i}"${i === ci ? " selected" : ""}>${esc(c.name || "Items")}</option>`).join("")}</select>
        <div class="row"><button type="button" class="btn small ghost" data-mv="-1">← Earlier</button><button type="button" class="btn small ghost" data-mv="1">Later →</button><button type="button" class="btn small" data-done>Done</button><button type="button" class="link" data-rm>Remove</button></div>
      </div>`;
    DL.sheet(DL.itemTip(it) + form, (sheet) => {
      const apply = () => {
        m.note = sheet.querySelector("#ed-note").value.trim();
        const p = toInt(sheet.querySelector("#ed-prio").value);
        m.prio = p && p >= 1 && p <= 100 ? p : null;
        const to = Number(sheet.querySelector("#ed-move").value);
        if (to !== ci) {
          state.cats[ci].mods.splice(state.cats[ci].mods.indexOf(m), 1);
          state.cats[to].mods.push(m);
          ci = to;
        }
        markDirty();
      };
      const finish = () => { apply(); DL.closeSheet(); renderAll(); };
      sheet.addEventListener("click", (e) => {
        if (e.target.closest("[data-done]")) finish();
        else if (e.target.closest("[data-rm]")) {
          state.cats[ci].mods.splice(state.cats[ci].mods.indexOf(m), 1);
          markDirty();
          DL.closeSheet();
          renderAll();
        } else if (e.target.closest("[data-mv]")) {
          apply();
          const list = state.cats[ci].mods;
          const i = list.indexOf(m);
          const j = i + Number(e.target.closest("[data-mv]").dataset.mv);
          if (j >= 0 && j < list.length) [list[i], list[j]] = [list[j], list[i]];
          renderAll();
        }
      });
      sheet.querySelector("#ed-note").addEventListener("keydown", (e) => { if (e.key === "Enter") finish(); });
    });
  }

  let toastTimer;
  function toast(text) {
    $("ed-target").innerHTML = `<b class="souls">✓</b> ${esc(text)}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("ed-target").innerHTML = targetLine(); }, 1800);
  }

  function addItem(it) {
    if (!state.cats[target]) target = 0;
    const where = state.cats.findIndex((c) => c.mods.some((m) => m.id === it.id));
    if (where >= 0) return toast(`${it.name} is already in “${state.cats[where].name || "Items"}”`);
    state.cats[target].mods.push({ id: it.id, note: "", prio: null });
    markDirty();
    renderAll();
    toast(`${it.name} → ${state.cats[target].name || "Items"}`);
  }

  // ---- ability order ----
  function abilityState(order) {
    const unlocked = new Set();
    const tiers = new Map();
    for (const s of order) {
      if (s.type === UNLOCK) unlocked.add(s.id);
      else tiers.set(s.id, (tiers.get(s.id) || 0) + 1);
    }
    return { unlocked, tiers };
  }
  /** Keep the steps that are still valid (e.g. after removing an unlock). */
  function validOrder(order) {
    const ids = new Set((hero()?.abilities || []).map((a) => a.id));
    const out = [];
    for (const s of order) {
      if (!ids.has(s.id)) continue;
      const { unlocked, tiers } = abilityState(out);
      const t = tiers.get(s.id) || 0;
      if (s.type === UNLOCK && !unlocked.has(s.id) && unlocked.size < 4) out.push({ id: s.id, type: UNLOCK, delta: -1 });
      else if (s.type === POINTS && unlocked.has(s.id) && t < 3) out.push({ id: s.id, type: POINTS, delta: -TIER_COST[t] });
    }
    return out;
  }
  function renderAbilities() {
    const h = hero();
    if (!h) {
      $("ed-abilities").innerHTML = '<p class="muted">Pick a hero first.</p>';
      $("ed-order").innerHTML = "";
      return;
    }
    const { unlocked, tiers } = abilityState(state.order);
    const points = state.order.filter((s) => s.type === POINTS).reduce((a, s) => a - s.delta, 0);
    $("ed-abilities").innerHTML = h.abilities.map((a) => {
      const t = tiers.get(a.id) || 0;
      const done = unlocked.has(a.id) ? t >= 3 : unlocked.size >= 4;
      const next = !unlocked.has(a.id) ? "Unlock" : t < 3 ? `T${t + 1} · ${TIER_COST[t]} AP` : "Maxed";
      return `<div style="display:grid;gap:4px"><button type="button" class="ed-ab" data-ab="${a.id}" ${done ? "disabled" : ""}>
          <img src="${esc(a.image)}" alt="" data-ability="${h.id}:${a.id}"><span>${esc(a.name)}</span>
          <span class="pips">${[0, 1, 2].map((i) => `<i class="${i < t ? "on" : ""}"></i>`).join("")}</span><span class="unl">${next}</span></button>
          <a href="#" class="small" data-info="${a.id}" style="text-align:center">Details</a></div>`;
    }).join("");
    const byId = new Map(h.abilities.map((a) => [a.id, a]));
    const seen = new Map();
    $("ed-order").innerHTML = state.order.map((s, i) => {
      const a = byId.get(s.id);
      let label = "Unlock";
      if (s.type === POINTS) {
        seen.set(s.id, (seen.get(s.id) || 0) + 1);
        label = `<b>T${seen.get(s.id)}</b>`;
      }
      return `<div class="ab-step${s.type === UNLOCK ? " unlock" : ""}" data-ability="${h.id}:${s.id}"><span class="n">${i + 1}</span><img src="${esc(a?.image)}" alt="">${label}<button type="button" data-rm-step="${i}" aria-label="Remove step">✕</button></div>`;
    }).join("") + (state.order.length ? `<span class="muted small" style="align-self:center">${points} AP</span>` : '<span class="muted small">No steps yet.</span>');
  }
  $("ed-abilities").addEventListener("click", (e) => {
    const info = e.target.closest("[data-info]");
    if (info) {
      e.preventDefault();
      DL.sheet(DL.abilityTip(DL.abilities.get(Number(info.dataset.info))));
      return;
    }
    const b = e.target.closest("[data-ab]");
    if (!b || b.disabled) return;
    const id = Number(b.dataset.ab);
    const { unlocked, tiers } = abilityState(state.order);
    if (!unlocked.has(id)) state.order.push({ id, type: UNLOCK, delta: -1 });
    else if ((tiers.get(id) || 0) < 3) state.order.push({ id, type: POINTS, delta: -TIER_COST[tiers.get(id) || 0] });
    markDirty();
    renderAbilities();
  });
  $("ed-order").addEventListener("click", (e) => {
    const b = e.target.closest("[data-rm-step]");
    if (b) {
      state.order.splice(Number(b.dataset.rmStep), 1);
      state.order = validOrder(state.order);
      markDirty();
      renderAbilities();
      return;
    }
    const step = e.target.closest("[data-ability]");
    if (step) DL.openTip(step);
  });

  // ---- souls, investment, stats ----
  function renderSummary() {
    const { all, gone } = owned();
    const spent = { weapon: 0, vitality: 0, spirit: 0 };
    const totals = new Map();
    for (const id of all) {
      if (gone.has(id)) continue;
      const it = DL.items.get(id);
      spent[it.slot] += it.cost;
      for (const st of it.stats) {
        const key = `${st.stat}|${st.postfix}`;
        const t = totals.get(key) || { label: st.label, postfix: st.postfix, value: 0, stat: st.stat };
        t.value += st.value;
        totals.set(key, t);
      }
    }
    const h = hero();
    const reached = { weapon: 0, vitality: 0, spirit: 0 };
    $("ed-summary").innerHTML = `<p style="margin:0 0 8px"><span class="souls">${num(spent.weapon + spent.vitality + spent.spirit)}</span> souls · ${all.size - gone.size} items in slots</p>
      <div class="ed-summary">${["weapon", "vitality", "spirit"].map((slot) => {
        const [label, unit] = DL.data.investment[slot];
        const steps = h ? h.investment[slot] || [] : [];
        const r = steps.filter(([t]) => t <= spent[slot]).pop();
        const next = steps.find(([t]) => t > spent[slot]);
        reached[slot] = r ? r[1] : 0;
        return `<div class="ed-sum slot-${slot}"><span class="k">${DL.SLOT_NAMES[slot]}</span><b class="souls">${num(spent[slot])}</b>
          ${h ? `<span class="bonus">${r ? `+${num(r[1])}${unit}` : "—"} ${esc(label.toLowerCase())}</span>${next ? `<span class="next">+${num(next[1])}${unit} at ${num(next[0])}</span>` : ""}` : ""}</div>`;
      }).join("")}</div>${h ? "" : '<p class="muted small">Pick a hero to see investment bonuses.</p>'}`;

    const sum = (stat, postfix) => [...totals.values()].filter((t) => t.stat === stat && (postfix === undefined || t.postfix === postfix)).reduce((a, t) => a + t.value, 0);
    const rows = [];
    if (h) {
      const base = h.panel.base_health || 0;
      rows.push(["Health", num(Math.round(base * (1 + reached.vitality / 100) + sum("MODIFIER_VALUE_HEALTH_MAX")))]);
      rows.push(["Spirit power", num(Math.round(sum("MODIFIER_VALUE_TECH_POWER") + reached.spirit))]);
      rows.push(["Weapon damage", `+${num(Math.round(sum("MODIFIER_VALUE_WEAPON_DAMAGE_INCREASE", "%") + reached.weapon))}%`]);
    }
    // Already summed into the hero rows above.
    const merged = (t) => h && (["MODIFIER_VALUE_HEALTH_MAX", "MODIFIER_VALUE_TECH_POWER"].includes(t.stat)
      || (t.stat === "MODIFIER_VALUE_WEAPON_DAMAGE_INCREASE" && t.postfix === "%"));
    for (const t of [...totals.values()].filter((t) => !merged(t)).sort((a, b) => a.label.localeCompare(b.label))) {
      rows.push([t.label, `${t.value > 0 ? "+" : ""}${num(Math.round(t.value * 100) / 100)}${t.postfix}`]);
    }
    $("ed-stats").innerHTML = rows.map(([k, v]) => `<div><span class="muted">${esc(k)}</span><b>${esc(v)}</b></div>`).join("") || '<span class="muted">No items yet.</span>';
  }

  function renderAll() {
    renderHero();
    renderCats();
    renderAbilities();
    renderSummary();
    shop?.refresh();
  }

  // ---- hero picker ----
  $("ed-hero").addEventListener("click", () => {
    DL.sheet(`<h2 style="margin:0 12px 10px">Pick a hero</h2><div class="hero-pick">${DL.data.heroes.map((h) => `<button type="button" class="hero-tile${h.id === state.hero ? " on" : ""}" data-hero-pick="${h.id}" style="padding:0;cursor:pointer"><img src="${esc(h.card || h.icon)}" alt="" loading="lazy"><span>${esc(h.name)}</span></button>`).join("")}</div>`, (sheet) => {
      sheet.addEventListener("click", (e) => {
        const b = e.target.closest("[data-hero-pick]");
        if (!b) return;
        const id = Number(b.dataset.heroPick);
        if (id !== state.hero && state.order.length && !confirm("Changing the hero clears the ability order. Continue?")) return;
        if (id !== state.hero) state.order = [];
        state.hero = id;
        markDirty();
        DL.closeSheet();
        renderAll();
      });
    });
  });

  // ---- save / publish ----
  function payload(asNew) {
    return {
      id: asNew ? null : state.id,
      as_new: !!asNew,
      hero_id: state.hero,
      name: $("ed-name").value,
      description: $("ed-desc").value,
      details: {
        mod_categories: state.cats.map((c) => ({
          name: c.name,
          description: c.description,
          optional: c.optional,
          mods: c.mods.map((m) => ({ ability_id: m.id, annotation: m.note || undefined, sell_priority: m.prio || undefined })),
        })),
        ability_order: { currency_changes: state.order.map((s) => ({ ability_id: s.id, currency_type: s.type, delta: s.delta })) },
      },
    };
  }
  async function post(url, body) {
    const resp = await fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || data.message || `HTTP ${resp.status}`);
    return data;
  }
  async function save(asNew) {
    if (!state.hero) {
      $("ed-msg").innerHTML = '<span class="neg">Pick a hero first.</span>';
      return null;
    }
    $("ed-msg").textContent = "Saving…";
    try {
      const r = await post("/api/builds", payload(asNew));
      if (asNew) state.gameBuildId = null;
      state.id = r.id;
      dirty = false;
      $("ed-name").value = r.name;
      history.replaceState(null, "", r.url);
      $("ed-msg").innerHTML = '<span class="pos">Saved.</span>';
      renderPublished();
      return r;
    } catch (err) {
      $("ed-msg").innerHTML = `<span class="neg">${esc(err.message)}</span>`;
      return null;
    }
  }
  function renderPublished() {
    if (!state.gameBuildId || !state.hero) {
      $("ed-published").innerHTML = "";
      return;
    }
    $("ed-published").innerHTML = `<div class="ed-published">In game: open ${esc(hero()?.name || "the hero")}'s builds and search build ID <b>${Number(state.gameBuildId)}</b>. <a href="/builds/${Number(state.hero)}/${Number(state.gameBuildId)}">View it here</a>. Publishing again updates it.</div>`;
  }
  $("ed-save").addEventListener("click", () => save(false));
  $("ed-save-new")?.addEventListener("click", () => save(true));
  $("ed-publish")?.addEventListener("click", async () => {
    if (!(await save(false))) return;
    $("ed-msg").textContent = "Publishing through the Steam bot…";
    try {
      const r = await post(`/api/builds/${Number(state.id)}/publish`, {});
      state.gameBuildId = toInt(r.hero_build_id);
      $("ed-msg").innerHTML = '<span class="pos">Published.</span>';
      renderPublished();
    } catch (err) {
      $("ed-msg").innerHTML = `<span class="neg">Not published: ${esc(err.message)}</span>`;
    }
  });
  $("ed-name").addEventListener("input", markDirty);
  $("ed-desc").addEventListener("input", markDirty);

  DL.load().then(() => {
    // Items no longer in the game are dropped, and the ability order is checked again.
    for (const c of state.cats) c.mods = c.mods.filter((m) => DL.items.has(m.id));
    if (!DL.heroes.has(state.hero)) state.hero = null;
    state.order = validOrder(state.order);
    $("ed-name").value = state.name;
    $("ed-desc").value = state.description;
    shop = DL.shop($("ed-shop"), { onPick: addItem, dim: (it) => owned().all.has(it.id) });
    renderAll();
    renderPublished();
  }).catch((err) => {
    $("ed-shop").innerHTML = `<p class="neg">Game data isn't available yet (${esc(err.message)}). Reload in a moment.</p>`;
  });
})();
