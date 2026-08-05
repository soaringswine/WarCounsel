"""Held-vs-worn legality for exaltation move targets.

This rule has already flip-flopped once, which is why it has a test.

The eqlwiki Exaltations page claims a stone imposes its SOURCE item's
equip-slot restriction on its host. That was briefly enforced as an exact
slot intersection and immediately disproved in play (2026-07-29): a
SECONDARY-only Hand Drum stone was accepted by a PRIMARY-only Rusty Spear,
and by a scimitar worn in Primary. So the check was removed wherever the
Inventory export could confirm an empty socket.

That over-corrected. On 2026-08-04 a live consult recommended moving a
Bard's Hand Drum into worn Bronze Vambraces (Arms) to free a shield for an
Any Slot — Arms really does have an empty focus socket in the export, so
every gate passed — and the game refused the combine: "doing so will
create an unusable item". Socketing yields an item carrying the COMMON
restriction of both, and held + armor is empty.

The surviving rule is therefore COARSER than the wiki's and STRICTER than
nothing: held stones need held hosts. Both halves are pinned below, so
neither correction can be undone without a failing test.
"""
from backend.agent.advisor import _category_compatible, _slot_category


# The 2026-07-29 finding: exact-slot intersection is WRONG. Each of these
# was accepted by the game with a SECONDARY-sourced instrument stone.
def test_held_stone_fits_any_held_host_regardless_of_exact_slot():
    for host, label in [
        ({"PRIMARY"}, "Rusty Spear, primary-only"),
        ({"PRIMARY", "SECONDARY"}, "Rusty Scimitar"),
        ({"SECONDARY"}, "Shiny Brass Shield"),
        ({"RANGE"}, "Dragoon Dirk"),
    ]:
        assert _category_compatible({"SECONDARY"}, host), label


# The 2026-08-04 finding: armor hosts are refused outright.
def test_held_stone_is_refused_by_armor_hosts():
    for host in ({"ARMS"}, {"CHEST"}, {"HEAD"}, {"LEGS"}, {"NECK"}, {"FINGERS"}):
        assert not _category_compatible({"SECONDARY"}, host), host


def test_worn_stone_is_refused_by_held_hosts():
    assert not _category_compatible({"CHEST"}, {"PRIMARY"})
    assert _category_compatible({"CHEST"}, {"CHEST"})
    assert _category_compatible({"WRIST"}, {"NECK"})


# House rule: absence of data is never evidence of incompatibility. An
# unknown side must not silently prune a legal destination.
def test_unknown_or_any_slot_never_blocks():
    assert _category_compatible(set(), {"CHEST"})
    assert _category_compatible({"SECONDARY"}, set())
    assert _category_compatible({"SECONDARY"}, {"ANY"})
    assert _slot_category(set()) is None
    assert _slot_category({"ANY"}) is None
