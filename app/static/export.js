// Build editor: the build as text, to paste or share into a chat with an AI. It holds what is
// set up in the editor and nothing the game data already says: the hero, name and description,
// the categories (core or optional, with their descriptions), the items in them by name with
// their imbue target, sell priority and note, and the ability order. No tooltips, stats or
// ability descriptions.
(() => {
  const DL = window.DL;
  const $ = (id) => document.getElementById(id);
  const UNLOCK = 2;
  const oneLine = (s) => String(s || "").replace(/\s+/g, " ").trim();

  function item(m, n) {
    const it = DL.items.get(m.id);
    const out = [`${n}. ${it.name}`];
    if (it.imbue) out.push(`   - Imbue target: ${DL.editor.imbueTarget(m)?.name || "none"}`);
    if (m.prio) out.push(`   - Sell priority: ${m.prio}`);
    if (m.note) out.push(`   - Note: ${oneLine(m.note)}`);
    return out;
  }

  /** What is skilled when. The abilities are named once, then referred to by their number. */
  function abilityOrder(h, order) {
    const number = new Map(h.abilities.map((a, i) => [a.id, i + 1]));
    const out = ["## Ability order", ""];
    out.push(`Abilities: ${h.abilities.map((a, i) => `${i + 1} = ${a.name}`).join(", ")}.`);
    out.push("Each step is an ability's number, then what is bought: its unlock, or its first, second or third upgrade (T1, T2, T3 for 1, 2 and 5 ability points).", "");
    const unlocked = new Set();
    const tiers = new Map();
    order.forEach((s, i) => {
      if (s.type === UNLOCK) unlocked.add(s.id);
      else tiers.set(s.id, (tiers.get(s.id) || 0) + 1);
      out.push(`${i + 1}. ${number.get(s.id)}: ${s.type === UNLOCK ? "unlock" : `T${tiers.get(s.id)}`}`);
    });
    // A full order is every ability's unlock and three upgrades. Short of that, say so and
    // name what is still open, so a reader doesn't take a half-set order for a finished one.
    const open = h.abilities.map((a, i) => {
      const left = [...(unlocked.has(a.id) ? [] : ["unlock"]), ...["T1", "T2", "T3"].slice(tiers.get(a.id) || 0)];
      return left.length ? `${i + 1}: ${left.join(", ")}` : "";
    }).filter(Boolean);
    const total = h.abilities.length * 4;
    if (!order.length) out.push(`No steps set yet (a full order has ${total}: every ability's unlock and three upgrades).`);
    else if (open.length) out.push("", `Not finished: ${order.length} of ${total} steps set. Still open: ${open.join("; ")}.`);
    return out;
  }

  /** The build in the editor as Markdown. */
  function buildText() {
    const { state, hero } = DL.editor;
    const h = hero();
    const name = oneLine($("ed-name").value) || (h ? `${h.name} build` : "Build");
    const description = $("ed-desc").value.trim();
    const out = [`# ${name}`, ""];
    out.push(`A Deadlock build for ${h ? h.name : "a hero that isn't picked yet"}, game version ${DL.data.version}.${state.gameBuildId ? ` In-game build ID ${state.gameBuildId}.` : ""}`, "");
    if (description) out.push(description, "");
    out.push("## Items", "", "By category, in buy order. Core categories are added to the game's quick-buy; optional ones are not.", "");
    let n = 0;
    for (const c of state.cats) {
      out.push(`### ${oneLine(c.name) || "Items"} (${c.optional ? "optional" : "core"})`, "");
      if (c.description.trim()) out.push(c.description.trim(), "");
      if (!c.mods.length) out.push("No items.");
      for (const m of c.mods) out.push(...item(m, ++n));
      out.push("");
    }
    if (h) out.push(...abilityOrder(h, state.order));
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
    if (!DL.data || !DL.editor) {
      $("ed-msg").innerHTML = '<span class="neg">The game data isn’t loaded yet; try again in a moment.</span>';
      return;
    }
    const { name, text } = buildText();
    const file = `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "build"}.md`;
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
    DL.sheet(`<div class="sheet-form export">
        <h2>Export build</h2>
        <p class="muted small">The build as text, as it is set up now: the categories, their items with imbue targets, sell priorities and notes, and the ability order. Paste it into a chat with an AI.</p>
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
