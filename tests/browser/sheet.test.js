// The bottom sheet (app/static/sheet.js) in a real browser: a phone-sized touch screen with real
// touch input, and a desktop with a mouse. Run from this directory: npm test
const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const PHONE = { viewport: { width: 390, height: 780 }, hasTouch: true, isMobile: true };
const DESKTOP = { viewport: { width: 1280, height: 800 } };
const LONG = `<h2 style="margin:0 12px">Long</h2>${Array.from({ length: 60 }, (_, i) => `<p style="margin:12px">row ${i}</p>`).join("")}`;
const SHORT = '<h2 style="margin:0 12px">Short</h2><p style="margin:12px">One line.</p><button type="button" id="act" style="margin:12px;min-height:44px">Act</button>';

let server;
let browser;
let base;

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

before(async () => {
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  server = spawn(process.env.PYTHON || "python3", ["-m", "tests.browser.serve", String(port)], { cwd: ROOT, stdio: ["ignore", "inherit", "inherit"] });
  for (let i = 0; ; i++) {
    if (server.exitCode !== null) throw new Error("the test server didn't start");
    if ((await fetch(`${base}/gamedata.json`).catch(() => null))?.ok) break;
    if (i > 150) throw new Error("the test server didn't answer");
    await new Promise((r) => setTimeout(r, 200));
  }
  browser = await chromium.launch();
});
after(async () => {
  await browser?.close();
  server?.kill();
});

/** A page with the game UI loaded, and the helpers the tests share. */
async function open(t, url = "/items", options = PHONE) {
  const context = await browser.newContext(options);
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + url);
  await page.waitForFunction(() => window.DL?.data && window.Sheet);
  const cdp = await context.newCDPSession(page);
  const h = {
    page,
    errors,
    /** A finger from (x, y1) to (x, y2) over `ms`; `lift: false` leaves it down. */
    async swipe(x, y1, y2, { ms = 200, steps = 12, lift = true } = {}) {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x, y: y1 }] });
      for (let i = 1; i <= steps; i++) {
        await page.waitForTimeout(ms / steps);
        await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y: y1 + ((y2 - y1) * i) / steps }] });
      }
      if (lift) await h.lift();
    },
    lift: () => cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] }),
    state: () => page.evaluate(() => {
      const d = document.querySelector(".sheet-dialog");
      const s = d.querySelector(".sheet-scroller");
      const c = d.querySelector(".sheet-content");
      return {
        open: d.open,
        atOpenStop: d.open && s.scrollTop >= s.scrollHeight - s.clientHeight - 1,
        content: Math.round(c.scrollTop),
        contentMax: c.scrollHeight - c.clientHeight,
        top: Math.round(d.querySelector(".sheet").getBoundingClientRect().top),
        label: d.getAttribute("aria-label"),
        pageY: Math.round(scrollY),
      };
    }),
    /** Resolves once the sheet rests fully open; fails if it doesn't get there. */
    opened: () => page.waitForFunction(() => {
      const d = document.querySelector(".sheet-dialog");
      const s = d?.querySelector(".sheet-scroller");
      return d?.open && s.scrollTop > 0 && s.scrollTop >= s.scrollHeight - s.clientHeight - 1;
    }, null, { timeout: 5000 }),
    closed: () => page.waitForFunction(() => document.querySelector(".sheet-dialog")?.open === false, null, { timeout: 5000 }),
    /** After a gesture that must not close the sheet: let it settle, then it rests open. */
    async stillOpen() {
      await page.waitForTimeout(700);
      await h.opened();
    },
    async show(html) {
      await page.evaluate((x) => { window.sheetEl = Sheet.open(x); }, html);
      await h.opened();
      return h.state();
    },
  };
  return h;
}

test("opens as a named modal on the bottom edge", async (t) => {
  const h = await open(t);
  const s = await h.show(SHORT);
  assert.equal(s.label, "Short");
  const info = await h.page.evaluate(() => {
    const d = document.querySelector(".sheet-dialog");
    return { modal: d.matches(":modal"), bottom: Math.round(d.querySelector(".sheet").getBoundingClientRect().bottom) };
  });
  assert.equal(info.modal, true);
  assert.ok(Math.abs(info.bottom - 780) <= 1, "the sheet sits on the bottom edge");
  assert.deepEqual(h.errors, []);
});

test("on a touch screen the grip shows and no close button; a keyboard gets the button", async (t) => {
  const h = await open(t);
  const button = () => h.page.evaluate(() => {
    const b = document.querySelector(".sheet-close");
    const r = b.getBoundingClientRect();
    return {
      shown: getComputedStyle(b).opacity === "1",
      hit: document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.closest(".sheet-close") === b,
      grip: getComputedStyle(document.querySelector(".sheet-grip")).display !== "none",
      focused: document.activeElement === b,
      size: [r.width, r.height],
    };
  });
  const tile = h.page.locator("main [data-item]").first();
  await tile.tap();
  await h.opened();
  let b = await button();
  assert.deepEqual([b.shown, b.hit, b.grip], [false, false, true]);
  await h.page.touchscreen.tap(195, 100);
  await h.closed();
  // Opened from the keyboard: the button is there, focused, and closes the sheet.
  await tile.focus();
  await h.page.keyboard.press("Enter");
  await h.opened();
  b = await button();
  assert.deepEqual([b.shown, b.hit, b.focused], [true, true, true]);
  assert.ok(b.size[0] >= 44 && b.size[1] >= 44, "a 44px target");
  await h.page.keyboard.press("Enter");
  await h.closed();
});

test("a swipe down on the bar closes it, and focus returns to what opened it", async (t) => {
  const h = await open(t);
  await h.page.locator("main [data-item]").first().tap();
  await h.opened();
  const s = await h.state();
  await h.swipe(150, s.top + 22, s.top + 420);
  await h.closed();
  assert.equal(await h.page.evaluate(() => document.activeElement.matches("[data-item]")), true);
});

test("a swipe down on content that is at its top closes it", async (t) => {
  const h = await open(t);
  const s = await h.show(SHORT);
  await h.swipe(150, s.top + 70, s.top + 470);
  await h.closed();
});

test("a short drag settles back open", async (t) => {
  const h = await open(t);
  const s = await h.show(LONG);
  await h.swipe(150, s.top + 22, s.top + 90, { ms: 500, steps: 20 });
  await h.stillOpen();
});

test("long content scrolls inside the sheet; only from its top does a swipe close", async (t) => {
  const h = await open(t);
  let s = await h.show(LONG);
  assert.ok(s.contentMax > 500 && s.top >= 780 * 0.12 - 1, "capped below the top of the screen");
  await h.swipe(150, 600, 200);
  await h.stillOpen();
  s = await h.state();
  assert.ok(s.content > 200, "a swipe up scrolls the content");
  // A hard fling back to the top of the content doesn't carry on into closing the sheet.
  await h.swipe(150, 200, 760, { ms: 90, steps: 6 });
  await h.stillOpen();
  assert.equal((await h.state()).content, 0);
  await h.swipe(150, 200, 760);
  await h.closed();
});

test("the bar drags the sheet wherever its content is scrolled to", async (t) => {
  const h = await open(t);
  const s = await h.show(LONG);
  await h.swipe(150, 600, 300);
  await h.stillOpen();
  assert.ok((await h.state()).content > 100);
  await h.swipe(150, s.top + 22, s.top + 450);
  await h.closed();
});

test("a tap outside and Esc each close it", async (t) => {
  const h = await open(t);
  await h.show(SHORT);
  await h.page.touchscreen.tap(195, 100);
  await h.closed();
  await h.show(SHORT);
  await h.page.keyboard.press("Escape");
  await h.closed();
});

test("the page behind doesn't scroll", async (t) => {
  const h = await open(t);
  await h.page.evaluate(() => {
    document.querySelector("main").style.minHeight = "3000px";
    scrollTo(0, 300);
  });
  const s = await h.show(LONG);
  await h.swipe(150, s.top + 22, s.top - 300);  // up on the bar, the sheet already fully open
  await h.swipe(150, 80, 20);  // on the dimmed part
  await h.swipe(150, 600, 100);  // through the content
  await h.swipe(150, 600, 100);
  await h.stillOpen();
  assert.equal((await h.state()).pageY, 300);
});

test("opening again replaces the content in place", async (t) => {
  const h = await open(t);
  await h.show(LONG);
  await h.swipe(150, 600, 300);
  await h.stillOpen();
  const s = await h.show(SHORT);
  assert.equal(s.label, "Short");
  assert.equal(s.content, 0);
  assert.equal(await h.page.evaluate(() => document.querySelectorAll("dialog").length), 1);
});

test("close() ends closed even when the slide out is undone", async (t) => {
  const h = await open(t);
  await h.show(SHORT);
  // Something stops the slide and puts the sheet back (as a finger landing on it might).
  await h.page.evaluate(() => {
    Sheet.close();
    const s = document.querySelector(".sheet-scroller");
    s.scrollTo({ top: s.scrollHeight, behavior: "instant" });
  });
  await h.closed();
  await h.show(SHORT);  // and it opens again as usual
});

test("on its way out the content no longer reacts", async (t) => {
  const h = await open(t);
  await h.show(SHORT);
  await h.page.evaluate(() => {
    window.hits = 0;
    document.getElementById("act").addEventListener("click", () => { hits++; Sheet.close(); });
  });
  const r = await h.page.locator("#act").boundingBox();
  for (let i = 0; i < 3; i++) await h.page.touchscreen.tap(r.x + r.width / 2, r.y + r.height / 2);
  await h.closed();
  assert.equal(await h.page.evaluate(() => hits), 1);
});

test("close() straight after open(), and open() straight after close()", async (t) => {
  const h = await open(t);
  await h.page.evaluate((x) => { Sheet.open(x); Sheet.close(); }, SHORT);
  await h.closed();
  await h.show(SHORT);
  await h.page.evaluate((x) => { Sheet.close(); window.sheetEl = Sheet.open(x); }, LONG);
  await h.stillOpen();
  assert.equal((await h.state()).label, "Long");
  assert.equal(await h.page.evaluate(() => document.querySelector(".sheet").inert), false);
});

test("an error while wiring the content still leaves a sheet that shows and closes", async (t) => {
  const h = await open(t);
  await h.page.evaluate((x) => {
    try { Sheet.open(x, { onOpen() { throw new Error("boom"); } }); } catch { /* the caller's own error */ }
  }, SHORT);
  await h.opened();
  await h.page.touchscreen.tap(195, 100);
  await h.closed();
});

test("content that changes height keeps the sheet on the bottom edge", async (t) => {
  const h = await open(t);
  await h.show(SHORT);
  await h.page.evaluate(() => sheetEl.insertAdjacentHTML("beforeend", '<p style="height:200px">more</p>'));
  await h.stillOpen();
  await h.page.evaluate(() => sheetEl.lastChild.remove());
  await h.stillOpen();
});

test("reduced motion: no slide", async (t) => {
  const h = await open(t, "/items", { ...PHONE, reducedMotion: "reduce" });
  await h.page.evaluate((x) => Sheet.open(x), SHORT);
  await h.page.waitForTimeout(100);
  assert.equal((await h.state()).atOpenStop, true);
  await h.page.evaluate(() => Sheet.close());
  await h.page.waitForTimeout(150);
  assert.equal((await h.state()).open, false);
});

test("a value's breakdown opens above the sheet and closes on its own", async (t) => {
  const h = await open(t);
  const id = await h.page.evaluate(() => DL.data.items.find((it) => it.tooltip.some((s) => [...s.big, ...s.lines].some((p) => p.calc))).id);
  await h.page.evaluate((x) => DL.openTip(document.querySelector(`main [data-item="${x}"]`)), id);
  await h.opened();
  const value = h.page.locator(".sheet [data-formula]").first();
  const onTop = () => h.page.evaluate(() => {
    const f = document.querySelector(".formula-pop");
    if (!f) return null;
    const r = f.getBoundingClientRect();
    return !!document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)?.closest(".formula-pop") && r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
  });
  await value.tap();
  assert.equal(await onTop(), true, "on screen, above the sheet");
  await h.page.touchscreen.tap(195, 60);  // outside both
  assert.equal(await onTop(), null);
  await h.stillOpen();
  await value.tap();
  await h.page.keyboard.press("Escape");
  assert.equal(await onTop(), null);
  await h.stillOpen();
  await h.page.keyboard.press("Escape");
  await h.closed();
});

test("build editor: a double tap on Remove removes one item", async (t) => {
  const h = await open(t, "/planner");
  const ids = await h.page.evaluate(() => DL.data.items.slice(0, 3).map((it) => it.id));
  await h.page.goto(`${base}/planner?items=${ids.join(",")}`);
  await h.page.waitForFunction(() => document.querySelector("#ed-cats [data-mod]"));
  const mods = h.page.locator("#ed-cats [data-mod]");
  assert.equal(await mods.count(), 3);
  await mods.first().locator("[data-item]").tap();
  await h.opened();
  const remove = h.page.locator(".sheet [data-rm]");
  await remove.scrollIntoViewIfNeeded();
  const r = await remove.boundingBox();
  await h.page.touchscreen.tap(r.x + r.width / 2, r.y + r.height / 2);
  await h.page.touchscreen.tap(r.x + r.width / 2, r.y + r.height / 2);
  await h.closed();
  assert.equal(await mods.count(), 2);
});

test("nothing scrolls sideways at 360px", async (t) => {
  const h = await open(t, "/planner", { ...PHONE, viewport: { width: 360, height: 740 } });
  await h.page.locator("#ed-hero").tap();
  await h.opened();
  const fits = () => h.page.evaluate(() => [document.documentElement, document.querySelector(".sheet-scroller"), document.querySelector(".sheet-content")].every((e) => e.scrollWidth <= e.clientWidth + 1));
  assert.equal(await fits(), true);
  await h.page.keyboard.press("Escape");
  await h.closed();
  await h.page.locator("#ed-export").tap();
  await h.opened();
  assert.equal(await fits(), true);
});

test("with a mouse: a close button instead of the grip, the wheel never moves the sheet, a click outside closes", async (t) => {
  const h = await open(t, "/items", DESKTOP);
  const tile = h.page.locator("main [data-item]").first();
  await tile.click();
  await h.opened();
  const look = await h.page.evaluate(() => {
    const b = document.querySelector(".sheet-close");
    const r = b.getBoundingClientRect();
    return { grip: getComputedStyle(document.querySelector(".sheet-grip")).display, button: getComputedStyle(b).opacity, size: [r.width, r.height] };
  });
  assert.deepEqual(look, { grip: "none", button: "1", size: [44, 44] });
  await h.page.mouse.move(640, 700);
  await h.page.mouse.wheel(0, -600);
  await h.page.mouse.wheel(0, 600);
  await h.stillOpen();
  await h.page.locator(".sheet-close").click();
  await h.closed();
  await tile.click();
  await h.opened();
  await h.page.mouse.click(200, 200);
  await h.closed();
});
