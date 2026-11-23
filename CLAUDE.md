# DeadlockAnalytics

Standing preferences from the owner. Follow them for every change to the site's pages.

## Phones first

The phone view matters at least as much as the desktop one, if not more (the site is used as an
installed app on Android). Every feature has to be comfortable to use by touch:

- Nothing may depend on hover. Whatever a hover shows needs a tap path that is just as complete.
- Tap targets are at least 40px high and don't sit so close that a thumb hits the wrong one.
- Popovers and sheets open, stay on screen and close by touch; a tap outside closes only the
  topmost one.
- Text stays readable at 360px wide, nothing overflows its box, and the page never scrolls
  sideways.
- Before calling UI work done, check it in a phone-sized touch viewport (Playwright with
  `hasTouch` and `isMobile`, 360px and 390px wide) as well as on desktop, in both themes.

## The game pages copy the game

Items, heroes, tooltips and the build editor are meant to be a visual carbon copy of Deadlock's
own UI. Don't invent a look: read how the game does it and match that. The game's panel layouts
and stylesheets are public in `SteamDatabase/GameTracking-Deadlock` under
`game/citadel/pak01_dir/panorama/` (`layout/` and `styles/`), its raw data under
`game/citadel/pak01_dir/scripts/`, and its images are on the asset host the site already uses
(`assets-bucket.deadlock-api.com`). Where touch usability and an exact copy conflict, touch
usability wins; keep the difference small and say so.
