// The bottom sheet: window.Sheet. It knows nothing about the pages that use it; any page may
// load this file and call it. Its styles are the "sheet" block in game.css.
//
//   Sheet.open(html, { onOpen, label })  show `html`; an open sheet is replaced in place.
//                                         Returns the element holding the content, which is also
//                                         passed to onOpen(el) to wire its controls.
//   Sheet.close()                         slide out; always ends closed.
//   Sheet.isOpen
//
// It is a modal <dialog> (top layer, the page behind inert, Esc and the phone's back gesture close
// it) whose swipe is native scrolling: a scroller with two snap stops, a screen-high gap above
// the sheet ("closed") and the sheet itself ("open"). A finger on the bar, or on the content
// once that is scrolled to its top, drags the sheet; it settles on one of the stops, and
// reaching "closed" closes the dialog. After Chrome's Modern Web Guidance, "Navigation Drawer",
// turned upright. On a touch screen the grip stands for all that and the close button is out of
// sight (it stays for screen readers and keyboards); with a mouse, which can't swipe, it is the
// other way round.
//
// Something shown on top of the sheet (a popover inside the dialog) keeps Esc and the back
// gesture for itself by preventing the dialog's `cancel` event in the capture phase.
(() => {
  const CLOSE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>';
  // The slide out normally ends by itself (see the observer). Should it not, because it was
  // interrupted or never started, the dialog closes after this long anyway: an open dialog
  // nobody can see would leave the page behind it dead.
  const CLOSE_BACKSTOP_MS = 1000;

  let parts = null;  // built on first use
  let closeTimer = null;

  function build() {
    const dialog = document.createElement("dialog");
    dialog.className = "sheet-dialog";
    dialog.innerHTML = `<div class="sheet-scroller"><div class="sheet">
        <div class="sheet-bar"><span class="sheet-grip"></span><button type="button" class="sheet-close" aria-label="Close" autofocus>${CLOSE_ICON}</button></div>
        <div class="sheet-content"></div>
      </div></div>`;
    document.body.appendChild(dialog);
    const p = {
      dialog,
      scroller: dialog.querySelector(".sheet-scroller"),
      panel: dialog.querySelector(".sheet"),
      closeButton: dialog.querySelector(".sheet-close"),
      content: dialog.querySelector(".sheet-content"),
      shown: false,    // some of the sheet has been on screen since it opened
      closing: false,  // sliding out after close()
    };
    // The close button, or a tap on the dimmed part. composedPath: the tap's own handler may
    // already have replaced what was tapped.
    dialog.addEventListener("click", (e) => {
      if (!e.composedPath().includes(p.panel) || e.target.closest(".sheet-close")) Sheet.close();
    });
    // Esc and the back gesture: slide out instead of vanishing. (A browser may refuse to have a
    // repeated Esc cancelled; then the dialog just closes.)
    dialog.addEventListener("cancel", (e) => {
      if (e.defaultPrevented || !e.cancelable) return;
      e.preventDefault();
      Sheet.close();
    });
    // However the dialog closed, this is where the sheet's state ends.
    dialog.addEventListener("close", () => {
      stopClosing();
      p.shown = false;
      p.content.replaceChildren();
      dialog.style.removeProperty("--sheet-open");
    });
    // Where the sheet is, by any way it got there (a swipe, close(), a snap settling): once it
    // has left the screen the dialog closes.
    const visible = 1 / window.innerHeight;
    new IntersectionObserver((entries) => {
      if (entries.at(-1).intersectionRatio >= visible) p.shown = true;
      else if (p.shown) finish();
    }, { root: dialog, threshold: [visible, 1] }).observe(p.panel);
    // Content that changes height while the sheet is up (an image arriving, a caller adding a
    // row): back onto the bottom edge. The browser re-snaps by itself only once at rest.
    new ResizeObserver(() => {
      if (dialog.open && !p.closing) p.scroller.scrollTo({ top: p.scroller.scrollHeight, behavior: "auto" });
    }).observe(p.panel);
    // The dimming follows the sheet: in CSS where scroll-driven animations exist, else from here.
    if (!CSS.supports("(animation-timeline: scroll()) and (timeline-scope: --sheet)")) {
      p.scroller.addEventListener("scroll", () => {
        dialog.style.setProperty("--sheet-open", Math.min(1, p.scroller.scrollTop / p.panel.offsetHeight));
      }, { passive: true });
    }
    return p;
  }

  function finish() {
    if (parts.dialog.open) parts.dialog.close();  // its "close" event resets the rest
  }
  function stopClosing() {
    clearTimeout(closeTimer);
    closeTimer = null;
    parts.closing = false;
    parts.dialog.classList.remove("closing");
    parts.panel.inert = false;
  }

  const Sheet = {
    get isOpen() {
      return !!parts?.dialog.open;
    },
    open(html, { onOpen, label } = {}) {
      parts ||= build();
      const { dialog, scroller } = parts;
      // Already open and at rest: the new content takes its place without a slide.
      const inPlace = dialog.open && parts.shown && !parts.closing;
      stopClosing();
      // A fresh element each time: the listeners of the content before go with it.
      const el = document.createElement("div");
      el.className = "sheet-content";
      el.innerHTML = html;
      parts.content.replaceWith(el);
      parts.content = el;
      dialog.setAttribute("aria-label", label || el.querySelector("h1, h2, h3")?.textContent.trim() || "Details");
      if (dialog.open) parts.closeButton.focus({ preventScroll: true });
      else dialog.showModal();  // opens on the "closed" stop, the sheet below the screen
      try {
        onOpen?.(el);  // before the slide: it may still change how tall the content is
      } finally {
        // Also when onOpen threw: the dialog is open, so the sheet has to come into view.
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: inPlace ? "instant" : "auto" });  // auto: smooth unless reduced motion (CSS)
      }
      return el;
    },
    close() {
      if (!parts?.dialog.open || parts.closing) return;
      if (parts.scroller.scrollTop <= 0) return finish();  // nothing on screen to slide out
      parts.closing = true;
      // On its way out nothing in it reacts: a second tap must not act again, and a finger
      // must not stop the slide (.closing, in the styles).
      parts.dialog.classList.add("closing");
      parts.panel.inert = true;
      parts.scroller.scrollTo({ top: 0, behavior: "auto" });
      closeTimer = setTimeout(finish, CLOSE_BACKSTOP_MS);
    },
  };
  window.Sheet = Sheet;
})();
