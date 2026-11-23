// Build editor: the build as text, to paste or share into a chat with an AI. Everything the
// editor shows is in it: categories, items with their tooltips, annotations, sell priorities and
// imbue targets, the ability order, souls, investment bonuses and stats. Tooltip numbers are the
// ones on screen (ui.js's calc: the build's spirit power and duration/range/radius bonuses
// applied), each with its base value and how it was reached.
// Numbers are written without thousands separators and with a decimal point, whatever the
// phone's language, so a reader can't take 1.250 for one and a quarter.
(() => {
  const DL = window.DL;
  const $ = (id) => document.getElementById(id);
  const SLOTS = ["weapon", "vitality", "spirit"];
  const TIER_COST = [1, 2, 5];
  const parser = new DOMParser();

  /** Tooltip markup as one line of plain text (parsed in an inert document: its icons are not
   *  loaded). Line breaks become "; " where the line before doesn't end a sentence itself. */
  const plain = (markup) => parser.parseFromString(String(markup || "").replace(/<br\s*\/?>|<\/p>/gi, "\n"), "text/html")
    .body.textContent.split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean)
    .reduce((text, l) => (text ? `${text}${/[.!?:;,]$/.test(text) ? " " : "; "}${l}` : l), "");
  const oneLine = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const signed = (v, unit = "") => `${v > 0 ? "+" : ""}${DL.round(v, 2)}${unit}`;
  const names = (ids) => ids.map((id) => DL.items.get(id)?.name).filter(Boolean).join(", ");

  /** A tooltip value. Where the build changes it: `131.8 (base 22; Spirit x0.6 × 183 = +109.8)`. */
  function value(p) {
    if (!p.calc) return p.value;
    const unit = p.calc.postfix;
    const r = DL.calc(p);
    const how = r.steps.map((st) => {
      if (st.kind === "mod") return st.pct ? `${st.name} +${DL.round(st.pct, 2)}% = ${signed(st.add, unit)}` : `grows with ${st.name}`;
      if (st.add === null) return `scales with ${st.name} x${st.times}, not applied`;
      return st.add ? `${st.name} x${st.times} × ${DL.build.spirit} = ${signed(st.add, unit)}` : `scales with ${st.name} x${st.times}`;
    }).join("; ");
    return r.changed ? `${DL.fmt(r.value, p.calc)} (base ${p.value}; ${how})` : `${p.value} (${how})`;
  }
  const prop = (p) => `${p.label}: ${value(p)}${p.conditional ? " (conditional)" : ""}`;

  /** One block of a tooltip as list lines. */
  function section(s, indent) {
    const props = [...s.big, ...s.lines].map(prop);
    const text = s.text ? plain(s.text) : "";
    if (s.type !== "active" && s.type !== "passive") return [...(text ? [text] : []), ...props].map((l) => `${indent}- ${l}`);
    const head = [
      s.cooldown && `cooldown ${s.cooldown}`,
      s.duration && `duration ${value({ value: s.duration, calc: s.duration_calc })}`,
      s.charges && `${s.charges} charges`,
    ].filter(Boolean).join(", ");
    return [
      `${indent}- ${s.type === "active" ? "Active" : "Passive"}${head ? ` (${head})` : ""}${text ? `: ${text}` : ""}`,
      ...props.map((l) => `${indent}  - ${l}`),
    ];
  }

  function item(m, n, { all, gone }) {
    const it = DL.items.get(m.id);
    const kind = [it.active && "active", it.imbue && "imbue"].filter(Boolean).map((k) => `, ${k}`).join("");
    const out = [`${n}. **${it.name}** (${DL.SLOT_NAMES[it.slot] || it.slot}, tier ${it.tier}, ${it.cost} souls${kind})`];
    const sub = (line) => out.push(`   - ${line}`);
    if (m.note) sub(`Annotation: ${oneLine(m.note)}`);
    if (m.prio) sub(`Sell priority: ${m.prio}`);
    if (it.imbue) sub(`Imbue target: ${DL.editor.imbueTarget(m)?.name || "none chosen"}`);
    if (it.components.length) sub(`Builds from: ${names(it.components)}`);
    if (it.upgrades_to.length) sub(`Upgrades into: ${names(it.upgrades_to)}`);
    if (gone.has(it.id)) sub(`Replaced in this build by ${names(it.upgrades_to.filter((id) => all.has(id)))}: its souls and stats are not in the totals`);
    for (const s of it.tooltip) out.push(...section(s, "   "));
    return out;
  }

  function totals(h) {
    const sum = DL.editor.summary();
    const b = DL.build;
    const sources = (list) => (list.length ? ` (${list.map(([name, v, unit]) => `${name} ${signed(v, unit || "")}`).join(", ")})` : "");
    const out = ["## Totals", ""];
    out.push(`- Souls: ${SLOTS.reduce((a, s) => a + sum.spent[s], 0)} for ${sum.counted} items (${SLOTS.map((s) => `${DL.SLOT_NAMES[s]} ${sum.spent[s]}`).join(", ")})`);
    if (h) {
      for (const slot of SLOTS) {
        const [label, unit] = DL.data.investment[slot];
        const next = (h.investment[slot] || []).find(([t]) => t > sum.spent[slot]);
        out.push(`- ${DL.SLOT_NAMES[slot]} investment bonus: +${sum.reached[slot]}${unit} ${label.toLowerCase()}${next ? `; next step +${next[1]}${unit} at ${next[0]} souls` : "; highest step reached"}`);
      }
    }
    out.push("", "Applied to the tooltip numbers below:", "");
    out.push(`- Spirit power: ${b.spirit}${sources(b.sources.spirit)}`);
    for (const [key, label] of [["duration", "Ability duration"], ["range", "Ability range"], ["radius", "Ability radius"]]) {
      out.push(`- ${label}: ${signed(b[key], "%")}${sources(b.sources[key])}`);
    }
    if (sum.rows.length) {
      out.push("", h ? "Stats (hero base, investment bonuses and always-on item stats; conditional and active effects are with each item):" : "Stats (always-on item stats):", "");
      for (const [label, v, unit, plus] of sum.rows) out.push(`- ${label}: ${plus ? "+" : ""}${v}${unit}`);
    }
    return out;
  }

  function abilityOrder(h, order) {
    const byId = new Map(h.abilities.map((a) => [a.id, a]));
    const tiers = new Map();
    const out = ["## Ability order", ""];
    if (!order.length) return [...out, "No steps set."];
    order.forEach((s, i) => {
      const name = byId.get(s.id)?.name || s.id;
      if (s.type === 2) return out.push(`${i + 1}. Unlock ${name}`);
      tiers.set(s.id, (tiers.get(s.id) || 0) + 1);
      out.push(`${i + 1}. ${name} tier ${tiers.get(s.id)} (${-s.delta} AP)`);
    });
    const points = order.filter((s) => s.type === 1).reduce((a, s) => a - s.delta, 0);
    out.push("", `${points} ability points spent.`);
    return out;
  }

  function abilities(h, order) {
    const out = [`## ${h.name}'s abilities`, "", "Numbers are the base ability with this build's stats; the upgrades are listed, not applied.", ""];
    h.abilities.forEach((a, i) => {
      const steps = order.map((s, n) => ({ ...s, n: n + 1 })).filter((s) => s.id === a.id);
      const unlock = steps.find((s) => s.type === 2);
      const bought = steps.filter((s) => s.type === 1);
      const head = a.header || {};
      const facts = [
        a.ultimate ? "ultimate" : `ability ${i + 1}`,
        head.cooldown && `cooldown ${head.cooldown}`,
        head.charges && `${head.charges} charges`,
        head.duration && `duration ${value({ value: head.duration, calc: head.duration_calc })}`,
        unlock ? `unlocked at step ${unlock.n}` : "not in the ability order",
      ].filter(Boolean).join(", ");
      out.push(`### ${a.name} (${facts})`, "");
      for (const s of a.tooltip) out.push(...section(s, ""));
      (a.upgrades || []).slice(0, 3).forEach((u, t) => {
        const text = [u.text && plain(u.text), u.bonuses.length && `[${u.bonuses.join(", ")}]`].filter(Boolean).join(" ");
        out.push(`- Tier ${t + 1} upgrade (${TIER_COST[t]} AP, ${bought[t] ? `taken at step ${bought[t].n}` : "not taken"}): ${text}`);
      });
      out.push("");
    });
    return out;
  }

  function heroStats(h) {
    const p = h.panel;
    const row = (title, list) => (list.length ? [`- ${title}: ${list.map(([k, v]) => `${k} ${v}`).join(", ")}`] : []);
    return [`## ${h.name}'s base stats`, "", ...row("Weapon", p.weapon), ...row("Vitality", p.vitality), ...row("Spirit", p.spirit), ...row("Per level", p.per_level)];
  }

  /** The build in the editor as Markdown. */
  function buildText() {
    const { state, hero, owned } = DL.editor;
    const h = hero();
    const have = owned();
    const name = oneLine($("ed-name").value) || (h ? `${h.name} build` : "Build");
    const description = $("ed-desc").value.trim();
    const out = [`# ${name}`, ""];
    out.push(`A Deadlock build for ${h ? h.name : "a hero that isn't picked yet"}, exported from the build editor. Game version ${DL.data.version}.${state.gameBuildId ? ` Published in the game as build ID ${state.gameBuildId}.` : ""}`, "");
    if (description) out.push(description, "");
    out.push("How to read it:", "");
    out.push("- Items are listed in the build's categories and in buy order. A core category is added to the game's quick-buy; an optional one is not.");
    out.push("- A value the build changes reads `131.8 (base 22; Spirit x0.6 × 183 = +109.8)`: the value now, the item's or ability's base value, and what was added. `Spirit x0.6` means every point of spirit power adds 0.6.");
    out.push("- Applied: spirit scaling at the build's spirit power, and the build's ability duration, range and radius bonuses. Not applied: ability upgrades, cooldown reduction, hero level and boons, and weapon and melee scaling (marked `not applied`).", "");
    out.push(...totals(h), "");
    out.push("## Items", "");
    let n = 0;
    for (const c of state.cats) {
      out.push(`### ${oneLine(c.name) || "Items"} (${c.optional ? "optional" : "core"})`, "");
      if (c.description.trim()) out.push(c.description.trim(), "");
      if (!c.mods.length) out.push("No items.");
      for (const m of c.mods) out.push(...item(m, ++n, have));
      out.push("");
    }
    if (h) out.push(...abilityOrder(h, state.order), "", ...abilities(h, state.order), ...heroStats(h));
    return { name, text: `${out.join("\n").trim()}\n` };
  }

  // ---- the sheet ----
  async function copy(text, area) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // No clipboard access (an insecure address, or a refused permission): the old way.
      area.focus();
      area.select();
      if (!document.execCommand("copy")) throw new Error("copy failed");
    }
  }

  let url = null;
  $("ed-export").addEventListener("click", () => {
    if (!DL.data || !DL.editor?.summary()) {
      $("ed-msg").innerHTML = '<span class="neg">The game data isn’t loaded yet; try again in a moment.</span>';
      return;
    }
    const { name, text } = buildText();
    const file = `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "build"}.md`;
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
    DL.sheet(`<div class="sheet-form export">
        <h2>Export build</h2>
        <p class="muted small">The whole build as text, as it is now: items with their tooltips and this build's numbers, annotations, imbue targets, the ability order, souls and stats. Paste it into a chat with an AI.</p>
        <div class="row">
          <button type="button" class="btn" data-copy>Copy</button>
          ${navigator.share ? '<button type="button" class="btn ghost" data-share>Share</button>' : ""}
          <a class="btn ghost" href="${url}" download="${DL.esc(file)}">Download</a>
        </div>
        <span class="ed-msg" data-msg role="status"></span>
        <textarea readonly rows="12" aria-label="The build as text" spellcheck="false"></textarea>
      </div>`, (sheet) => {
      const area = sheet.querySelector("textarea");
      const msg = sheet.querySelector("[data-msg]");
      area.value = text;
      msg.textContent = `${text.length.toLocaleString()} characters`;
      sheet.querySelector("[data-copy]").addEventListener("click", () => copy(text, area).then(
        () => { msg.innerHTML = '<span class="pos">Copied.</span>'; },
        () => { msg.innerHTML = '<span class="neg">Couldn’t copy. Select the text below and copy it.</span>'; },
      ));
      sheet.querySelector("[data-share]")?.addEventListener("click", () => navigator.share({ title: name, text }).catch((err) => {
        if (err.name !== "AbortError") msg.innerHTML = '<span class="neg">Couldn’t share. Use Copy instead.</span>';
      }));
    });
  });
})();
