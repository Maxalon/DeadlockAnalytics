"""Reconstruction of Valve's MVP score. See research/mvp-115701682/REPORT.md.

score = sum over stats of  weight * (value - hero_mean) / hero_sd

- Values are raw match totals (no per-minute scaling; Valve's own debug log printed the
  raw integer value, and duration-normalised variants fit the observed ranks worse).
- Means and SDs are per hero, from recent ranked+unranked Normal-mode matches.
- Weights were fitted on 26k matches (95.5% exact-MVP accuracy); Valve's own constants
  are not public, so treat them as estimates.
- Rank 1 goes to the best score on the winning team; ranks 2-3 to the next two scores
  from either team, with at least one player from the losing team always in the top 3.
"""

from dataclasses import dataclass

# Valve tracked-stat name -> (match_players column(s), weight)
STATS: dict[str, tuple[tuple[str, ...], float]] = {
    "kills": (("kills",), 1.0),
    "assists": (("assists",), 1.0),
    "deaths": (("deaths",), -0.5),
    "net_worth": (("net_worth",), 0.2),
    # Valve's stat 54 is the expression player_damage + ally_healing; self-healing is not counted.
    "player_damage_and_ally_healing": (("player_damage", "ally_healing"), 1.0),
    "objective_damage": (("objective_damage",), 0.5),
}

LABELS = {
    "kills": "Kills",
    "assists": "Assists",
    "deaths": "Deaths",
    "net_worth": "Souls",
    "player_damage_and_ally_healing": "Player damage + ally healing",
    "objective_damage": "Objective damage",
}


def stat_value(player, stat: str) -> float:
    columns, _ = STATS[stat]
    return float(sum(player[c] for c in columns))


@dataclass
class Component:
    stat: str
    value: float
    mean: float | None
    sd: float | None
    weight: float

    @property
    def z(self) -> float | None:
        if self.mean is None or not self.sd:
            return None
        return (self.value - self.mean) / self.sd

    @property
    def contribution(self) -> float:
        z = self.z
        return 0.0 if z is None else self.weight * z

    @property
    def label(self) -> str:
        return LABELS[self.stat]


def components(player, baselines: dict[tuple[int, str], tuple[float, float]]) -> list[Component]:
    """baselines maps (hero_id, stat) -> (mean, sd)."""
    out = []
    for stat, (_, weight) in STATS.items():
        mean, sd = baselines.get((player["hero_id"], stat), (None, None))
        out.append(Component(stat, stat_value(player, stat), mean, sd, weight))
    return out


def score(player, baselines) -> float | None:
    comps = components(player, baselines)
    if any(c.z is None for c in comps):
        return None  # hero missing from the baseline snapshot (e.g. a brand-new hero)
    return sum(c.contribution for c in comps)


def predict_ranks(players: list[dict], scores: dict[int, float | None], winning_team: int) -> dict[int, int]:
    """Apply Valve's selection rule to scores keyed by player_slot. Returns slot -> rank (1-3)."""
    scored = [p for p in players if scores.get(p["player_slot"]) is not None]
    by_score = sorted(scored, key=lambda p: scores[p["player_slot"]], reverse=True)
    winners = [p for p in by_score if p["team"] == winning_team]
    losers = [p for p in by_score if p["team"] != winning_team]
    if not winners or not losers:
        return {}
    first = winners[0]
    rest = [p for p in by_score if p is not first]
    second = rest[0]
    if second["team"] != winning_team or rest[1]["team"] != winning_team:
        third = rest[1]
    else:
        third = losers[0]  # top three would all be winners: the best loser takes rank 3
    return {first["player_slot"]: 1, second["player_slot"]: 2, third["player_slot"]: 3}
