"""Deadlock's game data (shop items, heroes, abilities) for the item browser and the build planner.

The raw data is deadlock-api's datamined copy of the game files (`/v1/assets/items`,
`/v1/assets/heroes`, `/v1/assets/generic-data`), stored as-is in `game_data` and refreshed
whenever the game's client version changes (jobs.refresh_game_data). This module turns it into
the small shapes the pages and planner.js need.
"""

import html
import json
import re
import sqlite3
import threading

SLOTS = ("weapon", "vitality", "spirit")
# What the souls spent in each shop category buy, per deadlock-api's `cost_bonuses`.
INVESTMENT = {
    "weapon": ("Weapon damage", "%"),
    "vitality": ("Base health", "%"),
    "spirit": ("Spirit power", ""),
}
_ZERO = {"", "0", "0.0", "-1", "-1.0"}
# Valve's tooltip markup: keep these tags (without attributes but `class`), escape the rest.
_TAG = re.compile(r"<(/?)(span|br|b|i|strong|em|p)\b([^>]*)>", re.I)
_CLASS = re.compile(r"""class\s*=\s*["']([\w\s-]*)["']""")
# Inline icons Valve puts into some descriptions (e.g. the spirit damage glyph): dropped.
_SVG = re.compile(r"<svg\b.*?</svg>\s*", re.I | re.S)
# Panorama layout tags around inline icons: dropped, their content kept.
_PANEL = re.compile(r"</?Panel\b[^>]*>", re.I)
# Inline stat icons: kept only when they come from deadlock-api's asset host.
_IMG = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*["'](https://assets-bucket\.deadlock-api\.com/[\w./-]+)["'][^>]*>""", re.I
)


def clean_html(text: str | None) -> str:
    """Valve's description markup, made safe to render: only a few tags survive."""
    if not text:
        return ""
    text = _PANEL.sub("", _SVG.sub("", text))
    # Key bindings the game fills in ("( a b i l i t y1 )") and other unresolved tokens.
    text = re.sub(r"\(\s*a\s*b\s*i\s*l\s*i\s*t\s*y\s*(\d)\s*\)", r"(Ability \1)", text)
    text = re.sub(r"\{s:[a-z_]+\}", "", text)
    out, pos = [], 0
    for m in re.finditer(f"{_TAG.pattern}|{_IMG.pattern}", text, re.I):
        if m.group(4):  # an inline icon
            out.append(html.escape(text[pos : m.start()], quote=False))
            out.append(f'<img class="inline-icon" src="{html.escape(m.group(4))}" alt="">')
            pos = m.end()
            continue
        out.append(html.escape(text[pos : m.start()], quote=False))
        close, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        cls = _CLASS.search(attrs) if not close else None
        out.append(f"<{close}{tag}{f' class="{cls.group(1)}"' if cls else ''}>")
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def stat_label(name: str) -> str:
    """EMaxHealth / max_health -> Max Health."""
    name = re.sub(r"^E(?=[A-Z])", "", name).replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip().title().replace("Tech", "Spirit")


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: float, signed: bool, postfix: str) -> str:
    text = f"{value:+g}" if signed else f"{value:g}"
    return f"{text}{postfix or ''}"


def item_stats(props: dict) -> list[dict]:
    """The item's tooltip numbers: labelled, non-zero properties."""
    out = []
    for key, p in (props or {}).items():
        label = p.get("label")
        value = str(p.get("value", "")).strip()
        if not label or value in _ZERO or _num(value) is None:
            continue
        v = _num(value)
        signed = "{s:sign}" in (p.get("prefix") or "") or bool(p.get("provided_property_type"))
        out.append(
            {
                "key": key,
                "label": label,
                "value": v,
                "postfix": p.get("postfix") or "",
                "text": _fmt(v, signed, p.get("postfix") or ""),
                # Summable into build totals: a stat the item gives the hero, always on.
                "stat": p.get("provided_property_type"),
                "conditional": "ConditionallyApplied" in (p.get("usage_flags") or []),
                "important": bool(p.get("tooltip_is_important") or p.get("tooltip_is_elevated")),
            }
        )
    # Stats the hero gets first, then the ability numbers.
    out.sort(key=lambda s: (not s["stat"] or s["conditional"], not s["important"]))
    return out


def _description(raw: dict) -> dict:
    d = raw.get("description") or {}
    return {
        "desc": clean_html(d.get("desc")),
        "active": clean_html(d.get("active")),
        "passive": clean_html(d.get("passive")),
        "quip": clean_html(d.get("quip")),
        "tiers": [clean_html(d.get(f"t{n}_desc")) for n in (1, 2, 3)],
    }


def prop(props: dict, key: str, icon: str | None = None) -> dict | None:
    """One property as the game shows it: `+12% Fire Rate`, with its icon; None if it is zero or unknown."""
    p = (props or {}).get(key)
    if not p:
        return None
    value = str(p.get("value", "")).strip()
    v = _num(value)
    if v is None or value in _ZERO:
        return None
    signed = "{s:sign}" in (p.get("prefix") or "") or bool(p.get("provided_property_type"))
    return {
        "key": key,
        "label": p.get("label") or p.get("postvalue_label") or stat_label(key),
        "value": _fmt(v, signed, p.get("postfix") or ""),
        "icon": icon or p.get("icon"),
        "css": p.get("css_class") or "",
        "conditional": "ConditionallyApplied" in (p.get("usage_flags") or []),
    }


_HEADER_PROPS = ("AbilityCooldown", "AbilityCharges")


def _header(props: dict) -> dict:
    out = {}
    for key, name in (("AbilityCooldown", "cooldown"), ("AbilityCharges", "charges"), ("AbilityDuration", "duration")):
        p = prop(props, key)
        if p:
            out[name] = p["value"]
    return out


def item_tooltip(raw: dict) -> list[dict]:
    """The item's tooltip, laid out like the game's: an innate block of stat lines, then Passive /
    Active blocks with their text, big stat boxes and the remaining numbers."""
    props = raw.get("properties") or {}
    sections = []
    for sec in raw.get("tooltip_sections") or []:
        kind = sec.get("section_type") or "passive"
        text, big, lines = [], [], []
        for attr in sec.get("section_attributes") or []:
            if attr.get("loc_string"):
                text.append(clean_html(attr["loc_string"]))
            for key in (attr.get("important_properties") or []) + (attr.get("elevated_properties") or []):
                big.append(prop(props, key))
            for entry in attr.get("important_properties_with_icon") or []:
                if isinstance(entry, dict):
                    big.append(prop(props, entry.get("name") or entry.get("important_property"), entry.get("icon")))
            for key in attr.get("properties") or []:
                if kind != "innate" and key in _HEADER_PROPS + ("AbilityDuration",):
                    continue
                lines.append(prop(props, key))
        section = {
            "type": kind,
            "text": "<br>".join(text),
            "big": [b for b in big if b],
            "lines": [x for x in lines if x],
        }
        if kind == "active" or (kind == "passive" and not raw.get("is_active_item")):
            section.update(_header(props))
        if section["text"] or section["big"] or section["lines"]:
            sections.append(section)
    if not sections:  # older items without tooltip data: the stat list
        lines = [prop(props, s["key"]) for s in item_stats(props)]
        sections.append(
            {"type": "innate", "text": _description(raw)["desc"], "big": [], "lines": [x for x in lines if x]}
        )
    return sections


def ability_tooltip(raw: dict) -> list[dict]:
    props = raw.get("properties") or {}
    sections = []
    for sec in (raw.get("tooltip_details") or {}).get("info_sections") or []:
        big = []
        for block in sec.get("properties_block") or []:
            for entry in block.get("properties") or []:
                big.append(prop(props, entry.get("important_property"), entry.get("important_property_icon")))
        lines = [prop(props, key) for key in sec.get("basic_properties") or []]
        text = clean_html(sec.get("loc_string"))
        if text or any(big) or any(lines):
            sections.append(
                {"type": "ability", "text": text, "big": [b for b in big if b], "lines": [x for x in lines if x]}
            )
    if not sections:
        sections.append({"type": "ability", "text": _description(raw)["desc"], "big": [], "lines": []})
    return sections


def shop_items(items_raw: list[dict]) -> list[dict]:
    raw = [i for i in items_raw if i.get("type") == "upgrade" and i.get("shopable") and not i.get("disabled")]
    by_class = {i["class_name"]: i["id"] for i in raw}
    items = []
    for i in raw:
        d = _description(i)
        items.append(
            {
                "id": i["id"],
                "class_name": i["class_name"],
                "name": i["name"],
                "slot": i.get("item_slot_type"),
                "tier": i.get("item_tier") or 0,
                "cost": i.get("cost") or 0,
                "active": bool(i.get("is_active_item")),
                "image": i.get("shop_image_webp") or i.get("shop_image") or i.get("image_webp") or i.get("image"),
                "desc": d["desc"] or d["active"] or d["passive"],
                "stats": item_stats(i.get("properties")),
                "tooltip": item_tooltip(i),
                "components": [by_class[c] for c in i.get("component_items") or [] if c in by_class],
                "upgrades_to": [],
            }
        )
    by_id = {i["id"]: i for i in items}
    for i in items:
        for c in i["components"]:
            by_id[c]["upgrades_to"].append(i["id"])
    items.sort(key=lambda i: (SLOTS.index(i["slot"]) if i["slot"] in SLOTS else 9, i["tier"], i["name"]))
    return items


def _ability(raw: dict | None) -> dict | None:
    if not raw:
        return None
    d = _description(raw)
    stats = item_stats(raw.get("properties"))
    upgrades = []
    for n, up in enumerate(raw.get("upgrades") or []):
        bonuses = []
        for pu in up.get("property_upgrades") or []:
            p = (raw.get("properties") or {}).get(pu.get("name"), {})
            v = _num(pu.get("bonus"))
            if v is not None:
                label = p.get("label") or stat_label(pu.get("name", ""))
                bonuses.append(f"{label} {_fmt(v, True, p.get('postfix') or '')}")
        upgrades.append({"text": d["tiers"][n] if n < 3 else "", "bonuses": bonuses})
    return {
        "id": raw["id"],
        "name": raw["name"],
        "image": raw.get("image_webp") or raw.get("image"),
        "desc": d["desc"],
        "quip": d["quip"],
        "stats": [s for s in stats if not s["stat"]],
        "upgrades": upgrades,
        "header": _header(raw.get("properties") or {}),
        "tooltip": ability_tooltip(raw),
        "ultimate": raw.get("ability_type") == "ultimate",
    }


def _weapon(raw: dict | None) -> list[tuple[str, str]]:
    w = (raw or {}).get("weapon_info") or {}
    rows = [
        ("Bullet damage", w.get("damage_per_shot") or w.get("bullet_damage"), ""),
        ("Bullets per shot", w.get("bullets"), ""),
        ("Fire rate", w.get("bullets_per_second"), "/s"),
        ("Magazine", w.get("clip_size"), ""),
        ("Reload", w.get("reload_duration"), "s"),
        ("DPS", w.get("damage_per_second"), ""),
        ("DPS with reloads", w.get("damage_per_second_with_reload"), ""),
        ("Falloff", w.get("damage_falloff_start_range") and w["damage_falloff_start_range"] / 39.37, "m+"),
    ]
    return [(label, f"{v:.3g}{unit}" if isinstance(v, float) else f"{v}{unit}") for label, v, unit in rows if v]


def _v(value, unit="", digits=3):
    if value in (None, ""):
        return None
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.{digits}g}{unit}"
    return f"{int(value) if isinstance(value, float) else value}{unit}"


def stat_panel(h: dict, weapon_raw: dict | None) -> dict:
    """Base stats grouped like the game's hero panel: weapon, vitality, spirit."""
    st = {k: s.get("value") for k, s in (h.get("starting_stats") or {}).items() if isinstance(s, dict)}
    w = (weapon_raw or {}).get("weapon_info") or {}
    lvl = h.get("standard_level_up_upgrades") or {}
    falloff = w.get("damage_falloff_start_range")
    weapon = [
        ("DPS", _v(w.get("damage_per_second"))),
        ("Bullet damage", _v(w.get("damage_per_shot") or w.get("bullet_damage"))),
        ("Bullets per shot", _v(w.get("bullets")) if (w.get("bullets") or 1) > 1 else None),
        ("Bullets per second", _v(w.get("bullets_per_second"))),
        ("Magazine", _v(w.get("clip_size"))),
        ("Reload time", _v(w.get("reload_duration"), "s")),
        ("Falloff starts", _v(falloff / 39.37 if falloff else None, "m", 2)),
        ("Light melee", _v(st.get("light_melee_damage"))),
        ("Heavy melee", _v(st.get("heavy_melee_damage"))),
    ]
    vitality = [
        ("Health", _v(st.get("max_health"))),
        ("Health regen", _v(st.get("base_health_regen"), "/s")),
        ("Move speed", _v(st.get("max_move_speed"), "m/s")),
        ("Sprint speed", _v(st.get("sprint_speed"), "m/s")),
        ("Stamina", _v(st.get("stamina"))),
        ("Dash distance", _v(st.get("ground_dash_distance_in_meters"), "m")),
    ]
    per_level = [
        ("Bullet damage", _v(lvl.get("MODIFIER_VALUE_BASE_BULLET_DAMAGE_FROM_LEVEL"))),
        ("Health", _v(lvl.get("MODIFIER_VALUE_BASE_HEALTH_FROM_LEVEL"))),
        ("Melee damage", _v(lvl.get("MODIFIER_VALUE_BASE_MELEE_DAMAGE_FROM_LEVEL"))),
        ("Spirit power", _v(lvl.get("MODIFIER_VALUE_TECH_POWER"))),
    ]
    spirit = [("Spirit power", _v(st.get("tech_power") or 0))]
    clean = lambda rows: [(k, v) for k, v in rows if v not in (None, "0") or k == "Spirit power"]  # noqa: E731
    return {
        "weapon": clean(weapon),
        "vitality": clean(vitality),
        "spirit": clean(spirit),
        "per_level": [(k, f"+{v}") for k, v in per_level if v not in (None, "0")],
        "base_health": st.get("max_health"),
    }


def heroes(heroes_raw: list[dict], items_raw: list[dict]) -> list[dict]:
    by_class = {i["class_name"]: i for i in items_raw}
    out = []
    for h in heroes_raw:
        # Every hero players can pick. Not `in_development`: Valve leaves that flag on some
        # released heroes (Violet, Baba) and off others (Nurse Harrow).
        if not h.get("player_selectable") or h.get("disabled"):
            continue
        slots = h.get("items") or {}
        abilities = [_ability(by_class.get(slots.get(f"signature{n}"))) for n in (1, 2, 3, 4)]
        images = h.get("images") or {}
        desc = h.get("description") or {}
        start = [
            (stat_label(s.get("display_stat_name") or k), s.get("value"))
            for k, s in (h.get("starting_stats") or {}).items()
            if isinstance(s, dict) and s.get("value") not in (None, 0)
        ]
        per_level = [
            (stat_label(k.replace("MODIFIER_VALUE_", "").replace("_FROM_LEVEL", "").lower()), v)
            for k, v in (h.get("standard_level_up_upgrades") or {}).items()
            if v and k != "MODIFIER_VALUE_BOON_COUNT"
        ]
        investment = {
            slot: [(b["gold_threshold"], b["bonus"]) for b in (h.get("cost_bonuses") or {}).get(slot, [])]
            for slot in SLOTS
        }
        thresholds = sorted({t for rows in investment.values() for t, _ in rows})
        investment_rows = [(t, {slot: dict(investment[slot]).get(t) for slot in SLOTS}) for t in thresholds]
        weapon_raw = by_class.get(slots.get("weapon_primary"))
        out.append(
            {
                "id": h["id"],
                "name": h["name"],
                "panel": stat_panel(h, weapon_raw),
                "background": images.get("background_image_webp") or images.get("background_image"),
                "type": h.get("hero_type"),
                "tags": h.get("tags") or [],
                "icon": images.get("icon_image_small_webp") or images.get("icon_image_small"),
                "card": images.get("icon_hero_card_webp") or images.get("icon_hero_card"),
                "role": clean_html(desc.get("role")),
                "playstyle": clean_html(desc.get("playstyle")),
                "lore": clean_html(desc.get("lore")),
                "complexity": h.get("complexity"),
                "start": start,
                "per_level": per_level,
                "weapon": _weapon(by_class.get(slots.get("weapon_primary"))),
                "abilities": [a for a in abilities if a],
                "investment": investment,
                "investment_rows": investment_rows,
            }
        )
    out.sort(key=lambda h: h["name"])
    return out


def normalise(items_raw: list[dict], heroes_raw: list[dict], generic: dict | None = None) -> dict:
    items = shop_items(items_raw)
    hs = heroes(heroes_raw, items_raw)
    return {
        "items": items,
        "items_by_id": {i["id"]: i for i in items},
        "heroes": hs,
        "heroes_by_id": {h["id"]: h for h in hs},
        "tier_cost": (generic or {}).get("item_price_per_tier") or [],
    }


def planner_json(data: dict, client_version: int | None) -> dict:
    """Everything the build editor and the tooltips need (served gzipped)."""
    return {
        "version": client_version,
        "investment": INVESTMENT,
        "tier_cost": data["tier_cost"],
        "items": [
            {
                "id": i["id"],
                "name": i["name"],
                "slot": i["slot"],
                "tier": i["tier"],
                "cost": i["cost"],
                "active": i["active"],
                "image": i["image"],
                "components": i["components"],
                "upgrades_to": i["upgrades_to"],
                "tooltip": i["tooltip"],
                "search": " ".join([i["name"]] + [s["label"] for s in i["stats"]]).lower(),
                "stats": [
                    {"stat": s["stat"], "label": s["label"], "value": s["value"], "postfix": s["postfix"]}
                    for s in i["stats"]
                    if s["stat"] and not s["conditional"]
                ],
            }
            for i in data["items"]
        ],
        "heroes": [
            {
                "id": h["id"],
                "name": h["name"],
                "icon": h["icon"],
                "card": h["card"],
                "investment": h["investment"],
                "panel": h["panel"],
                "abilities": h["abilities"],
            }
            for h in data["heroes"]
        ],
    }


class GameData:
    """The normalised game data, loaded from the database once per stored client version."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self._data = None

    def get(self, conn: sqlite3.Connection) -> dict | None:
        rows = {r["kind"]: r for r in conn.execute("SELECT kind, client_version, fetched_at FROM game_data")}
        if "items" not in rows or "heroes" not in rows:
            return None
        key = tuple(sorted((k, r["fetched_at"]) for k, r in rows.items()))
        with self._lock:
            if key != self._key:
                blobs = {r["kind"]: json.loads(r["json"]) for r in conn.execute("SELECT kind, json FROM game_data")}
                data = normalise(blobs["items"], blobs["heroes"], blobs.get("generic"))
                data["client_version"] = rows["items"]["client_version"]
                data["fetched_at"] = rows["items"]["fetched_at"]
                data["planner"] = planner_json(data, data["client_version"])
                self._data, self._key = data, key
            return self._data
