import pytest

from app import jobs, scoring

APOLLO, VINDICTA = 7, 12


def players(match):
    return [jobs.player_row(match["match_id"], p) for p in match["players"]]


def test_reproduces_research_scores(match_payload, research_baselines):
    ps = {p["player_slot"]: p for p in players(match_payload[0])}
    # REPORT.md §5: Apollo 3.049, Vindicta 2.161
    assert scoring.score(ps[APOLLO], research_baselines) == pytest.approx(3.049, abs=0.002)
    assert scoring.score(ps[VINDICTA], research_baselines) == pytest.approx(2.161, abs=0.002)


def test_objective_damage_is_the_biggest_gap(match_payload, research_baselines):
    ps = {p["player_slot"]: p for p in players(match_payload[0])}
    a = {c.stat: c.contribution for c in scoring.components(ps[APOLLO], research_baselines)}
    v = {c.stat: c.contribution for c in scoring.components(ps[VINDICTA], research_baselines)}
    gaps = {s: a[s] - v[s] for s in a}
    assert max(gaps, key=gaps.get) == "objective_damage"
    assert gaps["objective_damage"] == pytest.approx(0.621, abs=0.002)


def test_predicted_ranks_match_valve(match_payload, research_baselines):
    m = match_payload[0]
    ps = players(m)
    scores = {p["player_slot"]: scoring.score(p, research_baselines) for p in ps}
    predicted = scoring.predict_ranks(ps, scores, jobs._team(m["winning_team"]))
    actual = {p["player_slot"]: p["mvp_rank"] for p in ps if p["mvp_rank"]}
    assert predicted == actual  # Apollo 1, Vindicta 2, Celeste 3


def test_missing_hero_baseline_gives_no_score(match_payload):
    p = players(match_payload[0])[0]
    assert scoring.score(p, {}) is None


def test_rank_rule_forces_a_loser_into_top_three():
    ps = [{"player_slot": i, "team": 0 if i < 6 else 1} for i in range(12)]
    scores = {i: 10 - i for i in range(12)}  # winners (team 0) hold the six best scores
    assert scoring.predict_ranks(ps, scores, winning_team=0) == {0: 1, 1: 2, 6: 3}


def test_rank_rule_mvp_must_be_a_winner():
    ps = [{"player_slot": i, "team": 0 if i < 6 else 1} for i in range(12)]
    scores = {i: (100 if i >= 6 else 0) - i for i in range(12)}  # losers outscore everyone
    ranks = scoring.predict_ranks(ps, scores, winning_team=0)
    assert ranks[0] == 1 and ranks[6] == 2 and ranks[7] == 3
