"""Inline SVG charts rendered server-side. They work without JavaScript; static/chart.js only
adds the hover box."""

from datetime import UTC, datetime

from markupsafe import Markup, escape

W, H, PAD = 720, 200, 28


def _match_details(m: dict, hero: str) -> dict[str, str]:
    """What the hover box shows for one match; also the native tooltip and the link's label."""
    rank = {1: "MVP", 2: "Key Player", 3: "Key Player"}.get(m.get("mvp_rank"), "")
    mvp_score = None if m.get("mvp_gap") is None else m["score"] - m["mvp_gap"]
    return {
        "date": datetime.fromtimestamp(m["start_time"], UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "hero": hero,
        "result": ("Win" if m.get("won") else "Loss") + (" · not scored" if m.get("not_scored") else ""),
        "kda": f"{m['kills']} / {m['deaths']} / {m['assists']}",
        "score": f"{m['score']:+.2f}",
        "mvp": "" if mvp_score is None else f"{mvp_score:+.2f}",
        "gap": "" if m.get("mvp_gap") is None else f"{m['mvp_gap']:+.2f}",
        "rank": rank,
    }


def score_trend(matches: list[dict], hero_names: dict[int, str] | None = None) -> Markup | None:
    """Score per match (oldest → newest) against the MVP score of that match.
    Dots: filled = MVP, ringed = Key Player. Every match is a full-height link column: hovering
    it shows the match's details (static/chart.js; the native tooltip without JavaScript),
    clicking it opens the match."""
    hero_names = hero_names or {}
    pts = [m for m in matches if m.get("score") is not None]
    if len(pts) < 2:
        return None
    values = [m["score"] for m in pts] + [m["score"] - m["mvp_gap"] for m in pts if m.get("mvp_gap") is not None]
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    span = (hi - lo) or 1.0
    step = (W - 2 * PAD) / (len(pts) - 1)

    def x(i):
        return PAD + i * step

    def y(v):
        return H - PAD - (v - lo) * (H - 2 * PAD) / span

    you = " ".join(f"{x(i):.1f},{y(m['score']):.1f}" for i, m in enumerate(pts))
    mvp = " ".join(
        f"{x(i):.1f},{y(m['score'] - m['mvp_gap']):.1f}" for i, m in enumerate(pts) if m.get("mvp_gap") is not None
    )
    dots, hits = [], []
    for i, m in enumerate(pts):
        cls, r = "dot", 2.5
        if m.get("mvp_rank") == 1:
            cls, r = "dot mvp", 4.5
        elif m.get("mvp_rank") in (2, 3):
            cls, r = "dot key", 4.5
        cx, cy = f"{x(i):.1f}", f"{y(m['score']):.1f}"
        dots.append(f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{r}"/>')

        d = _match_details(m, hero_names.get(m["hero_id"], f"hero {m['hero_id']}"))
        summary = (
            f"{d['date']} · {d['hero']} · {d['result']} · {d['kda']} · score {d['score']}"
            + (f" · MVP score {d['mvp']}" if d["mvp"] else "")
            + (f" · {d['rank']}" if d["rank"] else "")
        )
        data = " ".join(f'data-{k}="{escape(v)}"' for k, v in d.items())
        # The column reaches halfway to each neighbour, so the pointer never has to hit the dot.
        left = max(x(i) - step / 2, 0)
        width = min(x(i) + step / 2, W) - left
        hits.append(
            f'<a class="hit" href="/matches/{m["match_id"]}" aria-label="{escape(summary)}" '
            f'data-x="{cx}" data-y="{cy}" {data}>'
            f"<title>{escape(summary)}</title>"
            f'<rect x="{left:.1f}" y="0" width="{width:.1f}" height="{H}"/></a>'
        )
    return Markup(
        '<div class="chart-wrap">'
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="group" aria-label="Score per match, oldest to newest">'
        f'<line class="zero" x1="{PAD}" x2="{W - PAD}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>'
        f'<text class="axis" x="{PAD - 6}" y="{y(0) + 4:.1f}" text-anchor="end">0</text>'
        f'<line class="crosshair" x1="0" x2="0" y1="{PAD / 2}" y2="{H - PAD / 2}"/>'
        f'<polyline class="line mvp-line" points="{mvp}"/>'
        f'<polyline class="line you" points="{you}"/>'
        + "".join(dots)
        + '<circle class="focus-ring" r="7" cx="-20" cy="-20"/>'
        + "".join(hits)
        + '</svg><div class="chart-tip" role="status" hidden></div></div>'
    )
