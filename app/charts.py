"""Inline SVG charts rendered server-side, so the page needs no JavaScript."""

from markupsafe import Markup, escape

W, H, PAD = 720, 200, 28


def score_trend(matches: list[dict]) -> Markup | None:
    """Score per match (oldest → newest) against the MVP score of that match.
    Dots: filled = MVP, ringed = Key Player."""
    pts = [m for m in matches if m.get("score") is not None]
    if len(pts) < 2:
        return None
    values = [m["score"] for m in pts] + [m["score"] - m["mvp_gap"] for m in pts if m.get("mvp_gap") is not None]
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    span = (hi - lo) or 1.0

    def x(i):
        return PAD + i * (W - 2 * PAD) / (len(pts) - 1)

    def y(v):
        return H - PAD - (v - lo) * (H - 2 * PAD) / span

    you = " ".join(f"{x(i):.1f},{y(m['score']):.1f}" for i, m in enumerate(pts))
    mvp = " ".join(
        f"{x(i):.1f},{y(m['score'] - m['mvp_gap']):.1f}" for i, m in enumerate(pts) if m.get("mvp_gap") is not None
    )
    dots = []
    for i, m in enumerate(pts):
        title = escape(f"Match {m['match_id']}: score {m['score']:+.2f}")
        cls, r = "dot", 2.5
        if m.get("mvp_rank") == 1:
            cls, r, title = "dot mvp", 4.5, title + " · MVP"
        elif m.get("mvp_rank") in (2, 3):
            cls, r, title = "dot key", 4.5, title + " · Key Player"
        cx, cy = f"{x(i):.1f}", f"{y(m['score']):.1f}"
        dots.append(f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{r}"><title>{title}</title></circle>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Score per match, oldest to newest">'
        f'<line class="zero" x1="{PAD}" x2="{W - PAD}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>'
        f'<text class="axis" x="{PAD - 6}" y="{y(0) + 4:.1f}" text-anchor="end">0</text>'
        f'<polyline class="line mvp-line" points="{mvp}"/>'
        f'<polyline class="line you" points="{you}"/>' + "".join(dots) + "</svg>"
    )
