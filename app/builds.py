"""Hero builds in the game's own format (`CMsgHeroBuild.details`), shared by the build browser,
the build editor and publishing through the bot.

    details = {
      "mod_categories": [{"name", "description", "optional", "width", "height",
                          "mods": [{"ability_id", "annotation", "sell_priority", ...}]}],
      "ability_order": {"currency_changes": [{"ability_id", "currency_type", "delta"}]},
    }

In the ability order, currency_type 2 is an ability unlock and 1 is ability points: delta -1,
-2, -5 buy an ability's first, second and third upgrade.
"""

UNLOCK, POINTS = 2, 1
TIER_COST = (1, 2, 5)  # ability points for T1, T2, T3
MAX_CATEGORIES = 20
MAX_MODS = 60
MAX_NAME = 64
MAX_DESCRIPTION = 2000
MAX_NOTE = 200
DEFAULT_CATEGORIES = ("Early game", "Mid game", "Late game", "Situational")


def details_of(build: dict) -> dict:
    """The details of a build as deadlock-api returns it ({"hero_build": {...}}) or as stored."""
    hb = build.get("hero_build", build)
    return hb.get("details") or {}


def categories(details: dict) -> list[dict]:
    return [
        {
            "name": c.get("name") or "",
            "description": c.get("description") or "",
            "optional": bool(c.get("optional")),
            "mods": [
                {"id": m.get("ability_id"), "note": m.get("annotation") or "", "sell_priority": m.get("sell_priority")}
                for m in c.get("mods") or []
            ],
        }
        for c in details.get("mod_categories") or []
    ]


def ability_steps(details: dict, abilities: list[dict]) -> list[dict]:
    """The ability order as steps: {ability, unlock: bool, tier: 1-3}, skipping unknown abilities."""
    by_id = {a["id"]: a for a in abilities}
    tiers: dict[int, int] = {}
    steps = []
    for ch in (details.get("ability_order") or {}).get("currency_changes") or []:
        a = by_id.get(ch.get("ability_id"))
        if not a:
            continue
        if ch.get("currency_type") == UNLOCK:
            steps.append({"ability": a, "unlock": True, "tier": 0})
        elif ch.get("currency_type") == POINTS:
            tiers[a["id"]] = tiers.get(a["id"], 0) + 1
            steps.append({"ability": a, "unlock": False, "tier": tiers[a["id"]]})
    return steps


def valid_ability_order(changes: list[dict], ability_ids: set[int]) -> list[dict]:
    """The longest valid prefix of an ability order: unlock before upgrading, upgrades in order
    (1, 2, 5 points), at most 4 unlocks and 3 upgrades per ability."""
    unlocked: set[int] = set()
    tiers: dict[int, int] = {}
    out = []
    for ch in changes or []:
        aid, kind, delta = ch.get("ability_id"), ch.get("currency_type"), ch.get("delta")
        if aid not in ability_ids:
            break
        if kind == UNLOCK and delta == -1 and aid not in unlocked and len(unlocked) < 4:
            unlocked.add(aid)
        elif kind == POINTS and aid in unlocked and tiers.get(aid, 0) < 3 and delta == -TIER_COST[tiers.get(aid, 0)]:
            tiers[aid] = tiers.get(aid, 0) + 1
        else:
            break
        out.append({"ability_id": aid, "currency_type": kind, "delta": delta})
    return out


def _int(value, lo: int, hi: int) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def clean_details(details: dict, item_ids: set[int], ability_ids: set[int]) -> dict:
    """A build's details from the editor, reduced to what the game accepts: known items only,
    length limits, a valid ability order."""
    cats = []
    for c in (details.get("mod_categories") or [])[:MAX_CATEGORIES]:
        mods = []
        for m in (c.get("mods") or [])[:MAX_MODS]:
            aid = _int(m.get("ability_id"), 1, 2**32 - 1)
            if aid not in item_ids:
                continue
            mod = {"ability_id": aid}
            note = str(m.get("annotation") or "").strip()[:MAX_NOTE]
            if note:
                mod["annotation"] = note
            prio = _int(m.get("sell_priority"), 1, 100)
            if prio:
                mod["sell_priority"] = prio
            mods.append(mod)
        cats.append(
            {
                "name": str(c.get("name") or "").strip()[:MAX_NAME] or "Items",
                "description": str(c.get("description") or "").strip()[:MAX_DESCRIPTION],
                "optional": bool(c.get("optional")),
                "mods": mods,
            }
        )
    changes = valid_ability_order((details.get("ability_order") or {}).get("currency_changes") or [], ability_ids)
    return {"mod_categories": cats, "ability_order": {"currency_changes": changes}}


def from_item_list(items: list[int]) -> dict:
    """Saves from before the editor had categories: one category with the items in buy order."""
    return {
        "mod_categories": [
            {"name": "Items", "description": "", "optional": False, "mods": [{"ability_id": i} for i in items]}
        ],
        "ability_order": {"currency_changes": []},
    }


def empty() -> dict:
    return {
        "mod_categories": [
            {"name": n, "description": "", "optional": n == "Situational", "mods": []} for n in DEFAULT_CATEGORIES
        ],
        "ability_order": {"currency_changes": []},
    }


def all_item_ids(details: dict) -> list[int]:
    seen, out = set(), []
    for c in details.get("mod_categories") or []:
        for m in c.get("mods") or []:
            aid = m.get("ability_id")
            if aid not in seen:
                seen.add(aid)
                out.append(aid)
    return out
