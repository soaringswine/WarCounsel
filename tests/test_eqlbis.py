"""Deterministic checks for the local eqlbis catalog and scoring core."""
import asyncio

from backend import eqlbis


def test_catalog_loads_and_resolves_game_typo():
    assert eqlbis.catalog_count() >= 6600
    item = eqlbis.get_item("Deterioriated Ancient Faydark Longbow +4")
    assert item and item["name"] == "Deteriorated Ancient Faydark Longbow"


def test_catalog_line_is_compatible_with_warcounsel():
    line = eqlbis.catalog_item_line("A Cracked Femur +2")
    assert line
    assert "Slot: PRIMARY SECONDARY" in line
    assert "Class: WAR RNG SHD" in line
    assert "DMG: 3" in line and "Atk Delay: 25" in line
    assert "SV DISEASE: +2" in line


def test_full_class_names_blend_to_same_profile_as_abbreviations():
    assert eqlbis.blend_weights(["Warrior", "Cleric", "Wizard"]) == \
        eqlbis.blend_weights(["WAR", "CLR", "WIZ"])
    assert eqlbis.blend_weights(["Warrior"])["AC"] == 3


def test_tier_and_slot_gating_match_eqlbis_model():
    assert eqlbis.tier_stat(3, 6) == 9
    assert eqlbis.tier_dmg(15, 4) == 21
    item = {"ac": 0, "hp": 0, "mana": 0, "stats": {}, "dmg": 5,
            "dly": 25, "skill": "Piercing"}
    weights = eqlbis.blend_weights([])
    assert sum(p["value"] for p in eqlbis.score_parts(item, 0, weights,
                                                       "Primary")) == 7.2
    assert eqlbis.score_parts(item, 0, weights, "Range") == []


def test_score_explains_the_largest_tradeoffs():
    current = eqlbis.get_item("Crushbone Belt")
    assert current
    score = eqlbis.score_item("Crushbone Belt +6", ["Warrior"], "Waist")
    assert score and score["score"] > 0
    assert score["parts"] and score["parts"][0]["key"]
    compared = eqlbis.compare_items("Crushbone Belt +6", "Crushbone Belt",
                                    ["Warrior"], "Waist")
    assert compared and compared["delta"] > 0 and compared["why"]


def test_vector_score_uses_warcounsel_cap_adjusted_stats():
    warrior = eqlbis.vector_score({"AC": 10, "HP": 50, "STR": 4},
                                  ["Warrior"])
    assert warrior == 64  # 10*3 + 50*.2*3 + 4*1
    assert eqlbis.vector_score({"DMG": 20, "DELAY": 30}, ["Warrior"]) == 0
    assert eqlbis.vector_score({"HASTE": 10}, ["Warrior"]) == 50


def test_vector_comparison_explains_weighted_tradeoff():
    got = eqlbis.compare_vectors({"AC": 6, "AGI": 0},
                                 {"AC": 0, "AGI": 10}, ["Warrior"])
    assert got["delta"] == 15
    assert got["cap_adjusted"] is True
    assert got["why"] == [{"key": "AC", "delta": 18.0},
                          {"key": "AGI", "delta": -3.0}]
    assert eqlbis.confident_upgrade(got)
    assert not eqlbis.confident_upgrade({"current_score": 10, "delta": .93})
    assert not eqlbis.confident_upgrade({"current_score": 100, "delta": 4.99})
    assert eqlbis.confident_upgrade({"current_score": 100, "delta": 5})


def test_builtin_gear_uses_weights_to_resolve_owned_tradeoff(monkeypatch):
    from backend import game_data
    from backend.agent import advisor

    lines = {
        "Current Breastplate": "Slot: CHEST; Class: WAR; AC: +10",
        "Candidate Breastplate": "Slot: CHEST; Class: WAR; AC: +6; STR: +20",
    }

    async def fake_item_line(name):
        return lines.get(name)

    monkeypatch.setattr(game_data, "item_line", fake_item_line)
    got = asyncio.run(advisor._builtin_gear({
        "class_str": "Warrior",
        "level": 50,
        "worn": {"Chest": "Current Breastplate"},
        "inventory_items": [
            {"name": "Current Breastplate", "where": "worn", "id": 1},
            {"name": "Candidate Breastplate", "where": "bags", "id": 2},
        ],
    }))
    chest = next(row for row in got["slots"] if row["slot"] == "Chest")
    assert chest["recommend"] == "Candidate Breastplate"
    assert chest["weighted"]["delta"] == 8
    assert "trio-weight score" in chest["why"]


def test_builtin_gear_does_not_unseat_unrelocated_exalt_host(monkeypatch):
    from backend import game_data
    from backend.agent import advisor

    lines = {
        "Instrument Shield": "Slot: SECONDARY; Class: BRD; AC: +5",
        "Current Breastplate": "Slot: CHEST; Class: BRD; AC: +30",
        "Candidate Tunic": "Slot: CHEST; Class: BRD; AC: +20; WIS: +5",
    }

    async def fake_item_line(name):
        return lines.get(name)

    monkeypatch.setattr(game_data, "item_line", fake_item_line)
    got = asyncio.run(advisor._builtin_gear({
        "class_str": "Bard",
        "level": 50,
        "worn": {
            "Any Slot 1": "Instrument Shield",
            "Chest": "Current Breastplate",
        },
        "inventory_items": [
            {"name": "Instrument Shield", "where": "worn", "id": 1},
            {"name": "Current Breastplate", "where": "worn", "id": 2},
            {"name": "Candidate Tunic", "where": "stash", "id": 3},
        ],
        "exaltations": [{
            "name": "Hand Drum (Exaltation)",
            "host": "Instrument Shield",
            "where": "worn",
        }],
    }))
    any_one = next(row for row in got["slots"]
                   if row["slot"] == "Any Slot 1")
    assert any_one["recommend"] == "Instrument Shield"
    assert any_one["where"] == "worn"
    assert "Candidate Tunic wins the item-stat comparison" in any_one["why"]
    assert "swap is blocked" in any_one["why"]
    assert "Hand Drum (Exaltation)" in any_one["why"]


def test_inventory_export_distinguishes_extended_storage(monkeypatch, tmp_path):
    from backend import spellbook

    export = tmp_path / "Tester_server-Inventory.txt"
    export.write_text(
        "Location\tName\tID\tCount\n"
        "Chest\tA Bone Necklace\t1\t1\n"
        "General 1-Slot1\tCrushbone Belt\t2\t1\n"
        "SharedBank1\tA Cracked Femur\t3\t1\n"
        "Equipment\tRaw-hide Cloak\t4\t1\n"
        "Hoard 1\tDeterioriated Ancient Faydark Longbow +4*\t5\t1\n"
        "Hoard 1-Slot7\tInvisible socket payload\t6\t1\n"
        "Personal-Depot1\tBone Chips\t7\t4\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(spellbook, "_find_export",
                        lambda _name, _server, _kind: export)
    got = spellbook.load_export("Tester", "server", "Inventory")
    assert got
    by_name = {item["name"]: item for item in got["items"]}
    assert by_name["A Bone Necklace"]["where"] == "worn"
    assert by_name["Crushbone Belt"]["where"] == "bags"
    assert by_name["A Cracked Femur"]["where"] == "bank"
    assert by_name["Raw-hide Cloak"]["where"] == "stash"
    assert by_name["Deteriorated Ancient Faydark Longbow +4"]["where"] == "hoard"
    assert by_name["Bone Chips"]["where"] == "depot"
    assert "Invisible socket payload" not in by_name
