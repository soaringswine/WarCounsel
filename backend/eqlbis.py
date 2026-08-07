"""Local eqlbis item catalog and explainable weighted gear scoring.

The catalog shape and scoring model are adapted from sethryder/eqlbis.  This
module deliberately stays advisory: WarCounsel's slot/class/socket gates still
decide whether a recommendation is legal.  The score only resolves otherwise
honest stat trade-offs and explains the result.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
import re
from typing import Iterable, Optional

from backend.paths import bundle_path


CATALOG_PATH = bundle_path("backend", "eql-bis-items.json")
NAME_ALIASES = {
    "deterioriated ancient faydark longbow":
        "Deteriorated Ancient Faydark Longbow",
}

KEYS = ("AC", "HP", "MANA", "STR", "STA", "AGI", "DEX", "WIS",
        "INT", "CHA", "SV", "DPS")
STAT_KEYS = ("STR", "STA", "AGI", "DEX", "WIS", "INT", "CHA")
CLASS_WEIGHTS = {
    "WAR": {"AC": 3, "HP": 3, "STR": 1, "STA": 2.5, "AGI": .3, "DEX": 1, "SV": 1, "DPS": 2},
    "CLR": {"AC": 1.2, "HP": 1.2, "MANA": 1.5, "STA": .7, "WIS": 3, "SV": .5, "DPS": .2},
    "PAL": {"AC": 2.5, "HP": 2.5, "MANA": .4, "STR": 1, "STA": 2, "AGI": .3, "DEX": .8, "WIS": .8, "SV": 1, "DPS": 1.5},
    "RNG": {"AC": 2, "HP": 2, "MANA": .3, "STR": 2, "STA": 1.5, "AGI": .3, "DEX": 1.2, "WIS": .5, "SV": 1, "DPS": 2.5},
    "SHD": {"AC": 2.5, "HP": 2.5, "MANA": .3, "STR": 1, "STA": 2, "AGI": .3, "DEX": .8, "INT": .5, "SV": 1, "DPS": 1.5},
    "DRU": {"AC": .8, "HP": 1.2, "MANA": 1.5, "STA": .7, "WIS": 3, "SV": .5, "DPS": .2},
    "MNK": {"AC": 2.5, "HP": 2, "STR": 1.5, "STA": 1.5, "AGI": .3, "DEX": .8, "SV": 1, "DPS": 3},
    "BRD": {"AC": 1.5, "HP": 1.5, "STR": .5, "STA": 1.2, "AGI": .3, "DEX": 1.5, "CHA": .8, "SV": 1.2, "DPS": 1.5},
    "ROG": {"AC": 1.2, "HP": 1.5, "STR": 2, "STA": 1, "AGI": .2, "DEX": .8, "SV": 1, "DPS": 3},
    "SHM": {"AC": 1, "HP": 1.8, "MANA": 1.5, "STA": 1, "WIS": 3, "SV": .5, "DPS": .3},
    "NEC": {"AC": .5, "HP": 1.8, "MANA": 1.5, "STA": .7, "INT": 3, "SV": .5, "DPS": .2},
    "WIZ": {"AC": .5, "HP": 1.2, "MANA": 1.5, "STA": .6, "INT": 3, "SV": .5, "DPS": .2},
    "MAG": {"AC": .5, "HP": 1.5, "MANA": 1.5, "STA": .6, "INT": 3, "SV": .5, "DPS": .2},
    "ENC": {"AC": .8, "HP": 1.5, "MANA": 1.5, "STA": .6, "INT": 2.5, "CHA": 2, "SV": .8, "DPS": .2},
    "BST": {"AC": 1.5, "HP": 1.5, "MANA": .4, "STR": 1.5, "STA": 1.5, "AGI": .3, "DEX": 1.2, "WIS": .8, "SV": .8, "DPS": 2.5},
    "BER": {"AC": 1.2, "HP": 1.5, "STR": 2, "STA": 1.5, "AGI": .3, "DEX": 1.5, "SV": .8, "DPS": 3},
}
CLASS_NAMES = {
    "WARRIOR": "WAR", "CLERIC": "CLR", "PALADIN": "PAL",
    "RANGER": "RNG", "SHADOW KNIGHT": "SHD", "SHADOWKNIGHT": "SHD",
    "DRUID": "DRU", "MONK": "MNK", "BARD": "BRD", "ROGUE": "ROG",
    "SHAMAN": "SHM", "NECROMANCER": "NEC", "WIZARD": "WIZ",
    "MAGICIAN": "MAG", "ENCHANTER": "ENC", "BEASTLORD": "BST",
    "BERSERKER": "BER",
}
PRESETS = {
    "balanced": {},
    "melee": {"STR": 1.6, "STA": 1.3, "AGI": 1.4, "DEX": 1.5,
              "DPS": 1.8, "AC": 1.2, "WIS": .4, "INT": .4,
              "MANA": .3, "CHA": .6},
    "caster": {"WIS": 1.7, "INT": 1.7, "MANA": 1.8, "CHA": 1.2,
               "HP": 1.1, "STR": .4, "DEX": .5, "AGI": .7,
               "DPS": .3},
    "tank": {"AC": 1.8, "HP": 1.7, "STA": 1.6, "AGI": 1.2,
             "SV": 1.3, "DPS": .7, "INT": .6, "WIS": .6,
             "MANA": .5},
}


def _base_name(name: str) -> str:
    base = re.sub(r"\s*[+]\d+$", "", name or "").strip()
    return NAME_ALIASES.get(base.lower(), base)


def canonical_inventory_name(name: str) -> str:
    """Normalize export adornments/typos while retaining the owned +N."""
    clean = re.sub(r"\*$", "", name or "").strip()
    rank = item_rank(clean)
    base = _base_name(clean)
    return f"{base} +{rank}" if rank else base


def item_rank(name: str) -> int:
    m = re.search(r"[+](\d+)\s*$", name or "")
    return int(m.group(1)) if m else 0


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict[str, dict], int]:
    try:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, 0
    return {str(row.get("name", "")).lower(): row for row in rows}, len(rows)


def catalog_count() -> int:
    return _catalog()[1]


def get_item(name: str) -> Optional[dict]:
    return _catalog()[0].get(_base_name(name).lower())


def catalog_item_line(name: str) -> Optional[str]:
    """Return a WarCounsel-compatible base-stat line from the local catalog."""
    item = get_item(name)
    if not item:
        return None
    slots = item.get("slots") or []
    if not slots and not item.get("dmg"):
        return None
    out = []
    if slots:
        out.append("Slot: " + " ".join(str(x).upper() for x in slots))
    classes = item.get("classes") or []
    out.append("Class: " + (" ".join(classes) if classes else "ALL"))
    for label, key in (("AC", "ac"), ("HP", "hp"), ("MANA", "mana"),
                       ("END", "end")):
        value = item.get(key) or 0
        if value:
            out.append(f"{label}: {value:+g}")
    for key, value in (item.get("stats") or {}).items():
        if key == "SV" or not value:
            continue
        out.append(f"{key}: {value:+g}")
    for key, value in (item.get("resists") or {}).items():
        if value:
            out.append(f"SV {key.upper()}: {value:+g}")
    if item.get("dmg"):
        out.append(f"DMG: {item['dmg']}")
    if item.get("dly"):
        out.append(f"Atk Delay: {item['dly']}")
    if item.get("skill"):
        out.append(f"Skill: {item['skill']}")
    if item.get("haste"):
        out.append(f"Haste: {item['haste']}%")
    if item.get("hpRegen"):
        out.append(f"HP Regen: {item['hpRegen']}")
    if item.get("effect"):
        out.append("Effect: " + str(item["effect"]))
    if item.get("focus"):
        out.append("Focus Effect: " + str(item["focus"]))
    zones = item.get("zones") or []
    if zones:
        out.append("catalog zones: " + ", ".join(zones[:3]))
    return "; ".join(out) + " | local eqlbis catalog"


def catalog_acquisition(name: str) -> Optional[dict]:
    item = get_item(name)
    if not item:
        return None
    sections = []
    zones = item.get("zones") or []
    vendors = item.get("vendors") or []
    if zones:
        sections.append({"label": "Drops From", "lines": [
            {"kind": "note", "text": str(zone)} for zone in zones]})
    if vendors:
        sections.append({"label": "Sold by", "lines": [
            {"kind": "note", "text": str(vendor)} for vendor in vendors]})
    return {"item": _base_name(name), "sections": sections,
            "available": bool(sections), "source": "local-eqlbis"}


def blend_weights(classes: Iterable[str], preset: str = "balanced",
                  overrides: Optional[dict] = None) -> dict[str, float]:
    normalized = [CLASS_NAMES.get(str(c).strip().upper(),
                                  str(c).strip().upper()) for c in classes]
    trio = [c for c in normalized if c in CLASS_WEIGHTS]
    mult = PRESETS.get(preset, PRESETS["balanced"])
    overrides = overrides or {}
    out = {}
    for key in KEYS:
        if key in overrides:
            out[key] = float(overrides[key])
            continue
        base = (sum(CLASS_WEIGHTS[c].get(key, 0) for c in trio) / len(trio)
                if trio else 1.0)
        out[key] = round(base * mult.get(key, 1), 2)
    return out


def tier_stat(value: float, tier: int) -> float:
    return (value + max(math.floor(value * .1 * tier), tier)
            if value > 0 else value)


def tier_dmg(value: float, tier: int) -> float:
    return value + math.floor(value * .1 * tier)


def _ranged(skill: str) -> bool:
    return skill == "Archery" or skill.startswith("Throwing")


def _weapon_active(slot: Optional[str], skill: str) -> bool:
    return (not slot or slot in ("Primary", "Secondary") or
            (_ranged(skill) and slot in ("Range", "Ammo")))


def _damage_bonus(skill: str, delay: float) -> int:
    return (14 if delay >= 28 else 9) if skill.startswith("2H") else 8


def _worn_regen(effect: str) -> tuple[float, float]:
    name = effect.split(" (")[0]
    if name == "Fungal Regrowth":
        return 15, 0
    if name == "Regeneration":
        return 9, 0
    m = re.fullmatch(r"Flowing Thought ([IVX]+)", name)
    roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
             "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
    return (0, roman.get(m.group(1), 1)) if m else (0, 0)


def score_parts(item: dict, tier: int, weights: dict,
                slot: Optional[str] = None) -> list[dict]:
    parts = []

    def add(key: str, label: str, value: float) -> None:
        if value:
            parts.append({"key": key, "label": label,
                          "value": round(float(value), 2)})

    skill = str(item.get("skill") or "")
    dmg, delay = float(item.get("dmg") or 0), float(item.get("dly") or 0)
    if dmg and delay and _weapon_active(slot, skill):
        d = tier_dmg(dmg, tier)
        value = ((2 * d + _damage_bonus(skill, delay)) / delay * 10 *
                 (.5 if _ranged(skill) else 1) * weights["DPS"])
        add("DPS", "white damage", value)
    for key, raw, scale in (("AC", item.get("ac"), 1),
                            ("HP", item.get("hp"), .2),
                            ("Mana", item.get("mana"), .2)):
        value = float(raw or 0)
        weight_key = "MANA" if key == "Mana" else key
        add(weight_key, key, tier_stat(value, tier) * scale * weights[weight_key])
    for key in STAT_KEYS:
        value = float((item.get("stats") or {}).get(key, 0) or 0)
        add(key, key, (tier_stat(value, tier) if value > 0 else value) *
            weights.get(key, 0))
    sv = float((item.get("stats") or {}).get("SV", 0) or 0)
    add("SV", "resists", tier_stat(sv, tier) * .3 * weights["SV"])
    haste = float(item.get("haste") or 0)
    add("Haste", "worn haste", (haste + tier) * 2.5 * weights["DPS"] if haste else 0)
    effect = str(item.get("effect") or "")
    if "(Combat" in effect and (not dmg or _weapon_active(slot, skill)):
        add("Proc", "combat proc", 2 * weights["DPS"])
    if item.get("focus"):
        add("Focus", "focus effect", 10 * weights["MANA"])
    for key, label, factor, weight in (
            ("hpRegen", "HP regen", 6, "HP"),
            ("manaRegen", "mana regen", 6, "MANA"),
            ("endRegen", "endurance regen", 1.5, "DPS")):
        value = float(item.get(key) or 0)
        add(label, label, tier_stat(value, tier) * factor * weights[weight])
    if "(Worn" in effect:
        hp, mana = _worn_regen(effect)
        add("HP regen", "worn HP regen", hp * 6 * weights["HP"])
        add("Mana regen", "worn mana regen", mana * 6 * weights["MANA"])
    end = float(item.get("end") or 0)
    add("END", "endurance", tier_stat(end, tier) * .05 * weights["DPS"])
    add("SV Void", "tier SV Void", tier * .3 * weights["SV"] if tier else 0)
    return parts


def score_item(name: str, classes: Iterable[str], slot: Optional[str] = None,
               preset: str = "balanced") -> Optional[dict]:
    item = get_item(name)
    if not item:
        return None
    weights = blend_weights(classes, preset)
    parts = score_parts(item, item_rank(name), weights, slot)
    return {"item": name, "score": round(sum(p["value"] for p in parts), 2),
            "parts": sorted(parts, key=lambda p: abs(p["value"]), reverse=True),
            "profile": preset, "weights": weights}


def compare_items(candidate: str, current: str, classes: Iterable[str],
                  slot: Optional[str] = None,
                  preset: str = "balanced") -> Optional[dict]:
    a, b = score_item(candidate, classes, slot, preset), score_item(
        current, classes, slot, preset)
    if not a or not b:
        return None
    deltas = {}
    for part in a["parts"]:
        deltas[part["key"]] = deltas.get(part["key"], 0) + part["value"]
    for part in b["parts"]:
        deltas[part["key"]] = deltas.get(part["key"], 0) - part["value"]
    why = [{"key": key, "delta": round(value, 2)}
           for key, value in sorted(deltas.items(),
                                    key=lambda kv: abs(kv[1]), reverse=True)
           if abs(value) >= .05]
    return {"profile": preset, "current_score": b["score"],
            "candidate_score": a["score"],
            "delta": round(a["score"] - b["score"], 2), "why": why}


_VECTOR_WEIGHT_KEYS = {
    "MP": "MANA", "MANA": "MANA", "SV_VOID": "SV",
    "SV_MAGIC": "SV", "SV_FIRE": "SV", "SV_COLD": "SV",
    "SV_POISON": "SV", "SV_DISEASE": "SV",
    "HASTE": "DPS", "HP_REGEN": "HP",
}


def _vector_factor(key: str, weight_key: str) -> float:
    if key == "HASTE":
        return 2.5
    if key == "HP_REGEN":
        return 6
    if weight_key in ("HP", "MANA"):
        return .2
    if weight_key == "SV":
        return .3
    return 1


def vector_score(vector: dict, classes: Iterable[str],
                 preset: str = "balanced") -> float:
    """Score WarCounsel's already cap-adjusted stat vector.

    DMG/DELAY stay excluded: the existing weapon index owns that decision.
    """
    weights = blend_weights(classes, preset)
    total = 0.0
    for key, raw in vector.items():
        if key in ("DMG", "DELAY"):
            continue
        wk = _VECTOR_WEIGHT_KEYS.get(key, key)
        factor = _vector_factor(key, wk)
        total += float(raw or 0) * factor * weights.get(wk, 0)
    return round(total, 2)


def compare_vectors(candidate: dict, current: dict, classes: Iterable[str],
                    preset: str = "balanced") -> dict:
    """Explain a cap-adjusted WarCounsel vector comparison by contribution."""
    weights = blend_weights(classes, preset)
    why = []
    for key in sorted(set(candidate) | set(current)):
        if key in ("DMG", "DELAY"):
            continue
        wk = _VECTOR_WEIGHT_KEYS.get(key, key)
        factor = _vector_factor(key, wk)
        delta = ((float(candidate.get(key, 0) or 0) -
                  float(current.get(key, 0) or 0)) * factor *
                 weights.get(wk, 0))
        if abs(delta) >= .05:
            why.append({"key": key.replace("_", " "),
                        "delta": round(delta, 2)})
    why.sort(key=lambda part: abs(part["delta"]), reverse=True)
    a, b = vector_score(candidate, classes, preset), vector_score(
        current, classes, preset)
    return {"profile": preset, "current_score": b, "candidate_score": a,
            "delta": round(a - b, 2), "why": why,
            "cap_adjusted": True}


def confident_upgrade(comparison: dict) -> bool:
    """Require a material win, not rounding noise in heuristic weights."""
    floor = max(1.0, abs(float(comparison.get("current_score") or 0)) * .05)
    return float(comparison.get("delta") or 0) >= floor
