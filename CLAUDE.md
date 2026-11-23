# DeadlockAnalytics

Standing preferences from the owner. Follow them for every change to the site's pages.

## Phones first

The phone view matters at least as much as the desktop one, if not more (the site is used as an
installed app on Android). Every feature has to be comfortable to use by touch:

- Nothing may depend on hover. Whatever a hover shows needs a tap path that is just as complete.
- Tap targets are at least 40px high and don't sit so close that a thumb hits the wrong one.
- Popovers and sheets open, stay on screen and close by touch; a tap outside closes only the
  topmost one.
- A long press is available for a second action on something a tap already uses (`DL.onHold`
  in `app/static/ui.js`; it cancels the browser's own long-press menu). Say so on the page where
  it applies, since nothing else reveals it, and offer the tap's action inside what it opens.
- Text stays readable at 360px wide, nothing overflows its box, and the page never scrolls
  sideways.
- Before calling UI work done, check it in a phone-sized touch viewport (Playwright with
  `hasTouch` and `isMobile`, 360px and 390px wide) as well as on desktop, in both themes.

## Reliable, modular, maintainable, reusable

The owner asks for these four in every change (said on 2026-11-23, about the bottom sheet). What
that has meant in practice so far:

- UI that more than one page could use is a file of its own with a small interface described at
  its top, and knows nothing about its callers: `app/static/sheet.js` (`Sheet.open`,
  `Sheet.close`). Use what exists (`Sheet`, `DL.onHold`, `DL.tile`, `DL.shop`) before writing
  another, and extend it there rather than around it.
- Nothing is left half done on screen: an action ends in a known state even when it is
  interrupted, called twice or its callback throws.
- Behaviour is pinned by tests that run in CI: `pytest` for the server, `tests/browser`
  (Playwright with real touch input; `npm test` there) for the pages. A change to how a page
  behaves comes with a browser test, and a safeguard with a test that fails without it.
- For web platform patterns (dialogs, drawers, scrolling, gestures) read Chrome's Modern Web
  Guidance first (`developer.chrome.com/docs/modern-web-guidance/guides`); the owner pointed to
  it for the sheet, which follows its "Navigation Drawer" and "Dialog" guides.

## The game pages copy the game

Items, heroes, tooltips and the build editor are meant to be a visual carbon copy of Deadlock's
own UI. Don't invent a look: read how the game does it and match that. The game's panel layouts
and stylesheets are public in `SteamDatabase/GameTracking-Deadlock` under
`game/citadel/pak01_dir/panorama/` (`layout/` and `styles/`), its raw data under
`game/citadel/pak01_dir/scripts/`, and its images are on the asset host the site already uses
(`assets-bucket.deadlock-api.com`). Where touch usability and an exact copy conflict, touch
usability wins; keep the difference small and say so.
