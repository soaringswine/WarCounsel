"""Live EQL lookups for the counsel chat (MCP server + wiki fallbacks).

The chat's briefing is a SNAPSHOT — whatever the consult already gathered
— which is exactly why "where do I buy Shieldskin?" came back as "nothing
in your briefing lists merchant locations". Vendor tables, mob drops and
zone pages are per-page wiki round-trips the consult never makes for
things outside the counsel it was building. This module hands the chat a
small named set of live lookups so it can go and read the page instead of
apologising for the briefing's edges.

**Why a TEXT protocol instead of native tool-calling.** `get_llm()` is
the seam every provider shares, and two of them (`claude_cli`,
`codex_cli`) are one-shot subprocesses with no tool channel at all, while
LM Studio's tool support varies per loaded model. So a lookup is one
line, one string argument, matched by a regex: the weakest provider in
the list can drive it, and a model that ignores the protocol entirely
just answers from the briefing exactly as before.

Every lookup is READ-ONLY and fails soft — a miss returns a "nothing
found" line rather than raising. A broken lookup must degrade the
answer, never the chat. Results are hard-capped: the briefing is already
the big half of the prompt, and this rides alongside it.

Nothing here is a verification gate. The house rule ("the LLM proposes,
structured data disposes") governs the CONSULT, whose picks are machine-
checked before display; a chat reply is prose the player reads with their
own judgement. What this buys is grounding — a cited page beats model
memory of classic EverQuest, which is the failure mode the whole app
exists to avoid.
"""
import asyncio
import logging
import re
from typing import Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2        # lookup turns before the model must answer
MAX_PER_ROUND = 4     # lookups honoured per turn (the rest are dropped)
MAX_RESULT = 2_400    # chars per lookup result
LOOKUP_TIMEOUT = 25.0  # seconds for one round's lookups, all together

# One line per lookup. Deliberately forgiving about how a model dresses
# it up: bullets, blockquotes, backticks and a colon after LOOKUP all
# appear in practice, and a rejected line costs the player an answer.
_LOOKUP_RE = re.compile(
    r"^[\s>*\-+`]*LOOKUP\b\s*:?\s*([a-z_]{3,20})\s*[:=]?\s+(.+?)[\s`\"']*$",
    re.IGNORECASE | re.MULTILINE)


def _clean_arg(arg: str) -> str:
    """Strip the quoting a model wraps an argument in. Parentheticals go
    too: "Shieldskin (the wizard rune)" is a page title that does not
    exist, and the bare name is what every lookup here wants."""
    arg = arg.strip().strip("`\"'").strip()
    arg = re.sub(r"\s*\([^()]*\)\s*$", "", arg).strip()
    return arg[:120]


# --------------------------------------------------------------- tools

async def _wiki_search(arg: str) -> Tuple[str, str]:
    from backend.mcp_client import get_mcp_client
    rows = await get_mcp_client().wiki_search(arg, limit=6)
    if not rows:
        # eqlwiki's search is effectively over TITLES: "Cleric" returns six
        # pages, "Kelethin merchant" returns nothing at all. Measured
        # 2026-07-31, identical through MCP and the plain-HTTP fallback, so
        # it is the wiki, not the client. An empty result is therefore
        # usually a phrase, not a missing subject — say so, because the
        # model's next move should be one word, not a rephrase.
        return (f"No eqlwiki page TITLE matched {arg!r}. This wiki searches "
                f"titles, not article text — retry with ONE distinctive "
                f"word (a spell, zone, item or NPC name), or browse with "
                f"LOOKUP wiki_category."), ""
    out = []
    for r in rows:
        title = r.get("title") or "?"
        # MediaWiki search snippets are HTML with <span class="searchmatch">
        snip = re.sub(r"<[^>]+>", "", str(r.get("snippet") or ""))
        snip = re.sub(r"\s+", " ", snip).strip()
        out.append(f"- {title}" + (f" — {snip[:160]}" if snip else ""))
    return ("Pages on eqlwiki (read one with LOOKUP wiki_page: <title>):\n"
            + "\n".join(out)), f"wiki search: {arg}"


async def _wiki_page(arg: str) -> Tuple[str, str]:
    from backend.mcp_client import get_mcp_client
    page = await get_mcp_client().wiki_page(arg, max_characters=MAX_RESULT)
    if not page or not (page.get("text") or "").strip():
        return (f"No eqlwiki page named {arg!r} (try LOOKUP wiki_search: "
                f"{arg} for the real title)."), ""
    return (f"eqlwiki page {page.get('title') or arg}:\n"
            f"{page['text'][:MAX_RESULT]}"), f"wiki: {page.get('title') or arg}"


async def _wiki_category(arg: str) -> Tuple[str, str]:
    from backend.mcp_client import get_mcp_client
    arg = re.sub(r"^category:\s*", "", arg, flags=re.IGNORECASE)
    pages = await get_mcp_client().wiki_category_pages(arg, limit=40)
    if not pages:
        return (f"No eqlwiki category named {arg!r}. Real ones include "
                f"Merchants, NPCs, Zones, Spells, Equipment, Quests."), ""
    titles = [p.get("title") for p in pages if p.get("title")]
    # Categories come back ALPHABETICALLY and this asks for 40, so a full
    # page of A-names is the norm for a big category. Say that: a truncated
    # list read as complete is how "no merchant sells it" gets invented.
    more = (" — this is the alphabetical START of a larger category, not "
            "the whole of it" if len(titles) >= 40 else "")
    return (f"Category:{arg} — {len(titles)} page(s){more}:\n"
            + "\n".join(f"- {t}" for t in titles),
            f"wiki category: {arg}")


async def _spell(arg: str) -> Tuple[str, str]:
    from backend import game_data
    rec = await game_data.spell_record(arg)
    if not rec:
        return (f"No spell named {arg!r} in the eqlbuilds dataset (exact "
                f"names only — try LOOKUP wiki_search: {arg})."), ""
    lines = [f"{rec.get('name') or arg} — eqlbuilds record"]
    # The dataset's own field names — manaCost, not mana. Getting one wrong
    # drops the line silently, which reads as "the data does not say".
    for label, key in (("mana", "manaCost"), ("cast", "castTime"),
                       ("duration", "duration"), ("skill", "skill"),
                       ("range", "range")):
        v = rec.get(key)
        if v not in (None, ""):  # mana 0 is a FACT (songs), not a blank
            lines.append(f"  {label}: {v}")
    recast = rec.get("recastTimeMs")
    if recast:
        lines.append(f"  recast: {recast / 1000:g}s")
    desc = (rec.get("description") or rec.get("resolvedDescription")
            or "").strip()
    summary = ""
    try:
        summary = game_data.builds_data.effect_summary(rec)
    except Exception:
        pass
    lines.append(f"  effect: {(desc or summary)[:300]}"
                 if (desc or summary) else "  effect: (not described)")
    for e in (rec.get("effects") or [])[:8]:
        # id-10 zero-value charisma spacers are a record-format convention,
        # not an effect — game_data._primary_effect skips them for the same
        # reason. Listing them reads as "this spell does nothing twice".
        if e.get("effectId") == 10 and not e.get("baseValue"):
            continue
        s = e.get("summary") or e.get("name")
        # "Effect 457: 1000 to 28" is a raw undecoded slot; effect_summary
        # already renders the ones whose meaning is established.
        if s and not re.match(r"^Effect \d+\b", str(s)):
            lines.append(f"  - {s}")
    usable = rec.get("usableBy") or []
    if usable:
        lines.append("  learned by: " + ", ".join(
            f"{u.get('name')} L{u.get('level')}" for u in usable[:8]))
    elif rec.get("levels"):
        lines.append("  learned by: " + ", ".join(
            f"{k} L{v}" for k, v in list(rec["levels"].items())[:8]))
    return "\n".join(lines)[:MAX_RESULT], f"eqlbuilds spell: {rec.get('name') or arg}"


async def _vendors(arg: str) -> Tuple[str, str]:
    from backend import game_data
    rows = await game_data.spell_vendors(arg)
    if not rows:
        # The wiki's Where-to-Obtain is a per-spell TABLE for some spells
        # and a link to a "<Class> Spell Vendors" page for others; only
        # the table parses here, so say which door is still open rather
        # than reading as "this spell has no vendor".
        return (f"No per-vendor table on the {arg!r} wiki page. Its "
                f"Where-to-Obtain section may point at a "
                f"'<Class> Spell Vendors' page instead — read that with "
                f"LOOKUP wiki_page."), ""
    out = [f"{arg} — vendors (eqlwiki Where to Obtain):"]
    for r in rows[:12]:
        out.append(f"  {r.get('zone')} — {r.get('vendor')}"
                   + (f" · {r.get('where')}" if r.get("where") else "")
                   + (f" {r.get('loc')}" if r.get("loc") else ""))
    return "\n".join(out)[:MAX_RESULT], f"wiki vendors: {arg}"


async def _item(arg: str) -> Tuple[str, str]:
    from backend import game_data
    line = await game_data.item_line(arg)
    acq = await game_data.item_acquisition(arg)
    if not line and not (acq or {}).get("available"):
        return (f"No eqlwiki item page for {arg!r} (try LOOKUP "
                f"wiki_search: {arg})."), ""
    out = [f"{arg} — eqlwiki item"]
    if line:
        out.append(f"  stats (BASE +0 values): {line[:600]}")
    for sec in (acq or {}).get("sections", []):
        out.append(f"  {sec.get('label')}:")
        for row in (sec.get("lines") or [])[:8]:
            out.append(f"    {row.get('text')}")
    return "\n".join(out)[:MAX_RESULT], f"wiki item: {arg}"


async def _aa(arg: str) -> Tuple[str, str]:
    from backend.mcp_client import get_mcp_client
    mcp = get_mcp_client()
    sc = await mcp.call_tool("eql_builds_ability", {"id": arg})
    ab = (sc or {}).get("ability")
    if not ab:
        sc = await mcp.call_tool("eql_builds_ability_search",
                                 {"query": arg, "limit": 6})
        hits = (sc or {}).get("results") or []
        if not hits:
            return f"No AA matching {arg!r} in the eqlbuilds dataset.", ""
        out = [f"AAs matching {arg!r}:"]
        for h in hits:
            out.append(f"  {h.get('name')} ({h.get('id')}) — "
                       f"{(h.get('description') or '')[:140]}")
        return ("\n".join(out)[:MAX_RESULT],
                f"eqlbuilds AA search: {arg}")
    out = [f"{ab.get('name')} — AA ({ab.get('category')}/{ab.get('group')})"]
    if ab.get("description"):
        out.append(f"  {str(ab['description'])[:400]}")
    if ab.get("maxRank"):
        out.append(f"  max rank: {ab['maxRank']}")
    ranks = ab.get("ranks") or []
    if ranks:
        out.append("  ranks: " + ", ".join(
            f"{r.get('rank')}={r.get('cost')}pts" for r in ranks[:12]))
    classes = ab.get("classes") or ab.get("eligibleClasses") or []
    if classes:
        out.append("  classes: " + ", ".join(
            str(c.get("name") if isinstance(c, dict) else c)
            for c in classes[:12]))
    return "\n".join(out)[:MAX_RESULT], f"eqlbuilds AA: {ab.get('name')}"


# name -> (argument label, one-line help, coroutine). The help text IS
# the prompt documentation, so keep it short and concrete.
TOOLS: Dict[str, Tuple[str, str, Callable]] = {
    "wiki_search": ("<one distinctive word>",
                    "find eqlwiki page TITLES. This wiki does not search "
                    "article text, so search a single name (Kelethin, "
                    "Merchant, Shieldskin) — a phrase returns nothing",
                    _wiki_search),
    "wiki_page": ("<exact page title>",
                  "read an eqlwiki page: zones, mobs, quests, NPCs, "
                  "vendors, factions, spell and item pages. A ZONE page "
                  "lists that city's merchants and what each one sells",
                  _wiki_page),
    "wiki_category": ("<category name>",
                      "list a category's pages — Merchants, NPCs, Zones, "
                      "Spells, Equipment, Quests. The way to browse a wiki "
                      "whose search only sees titles", _wiki_category),
    "spell": ("<exact spell name>",
              "eqlbuilds record: mana, cast time, duration, effects and the "
              "level EACH class learns it at", _spell),
    "vendors": ("<exact spell name>",
                "which zone and NPC sells a spell scroll, with coordinates "
                "when the wiki lists them", _vendors),
    "item": ("<exact item name>",
             "item stats plus where it drops, who sells it, and any quest "
             "or recipe that yields it", _item),
    "aa": ("<AA name>",
           "eqlbuilds alternate-advancement detail: per-rank costs, max "
           "rank, eligible classes", _aa),
}


def tool_help() -> str:
    lines = [f"  LOOKUP {name}: {label}\n      {help_}"
             for name, (label, help_, _) in TOOLS.items()]
    return "\n".join(lines)


# ------------------------------------------------------------- protocol

def parse_lookups(text: str) -> List[Tuple[str, str]]:
    """Extract (tool, argument) pairs, deduped, capped. Unknown tool names
    are dropped silently — a hallucinated tool is just noise, and telling
    the model off costs a round it could spend answering."""
    seen, out = set(), []
    for m in _LOOKUP_RE.finditer(text or ""):
        tool = m.group(1).lower()
        arg = _clean_arg(m.group(2))
        if tool not in TOOLS or not arg:
            continue
        key = (tool, arg.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((tool, arg))
        if len(out) >= MAX_PER_ROUND:
            break
    return out


def strip_lookups(text: str) -> str:
    """Remove lookup lines from a reply before it reaches the player. Runs
    on the FINAL answer too: a model that emits one on its last turn would
    otherwise show the player raw protocol."""
    return re.sub(r"\n{3,}", "\n\n", _LOOKUP_RE.sub("", text or "")).strip()


async def run_lookups(calls: List[Tuple[str, str]]) -> Tuple[str, List[str]]:
    """Run a round concurrently. Returns (transcript, source labels).

    The whole round shares one timeout: these are wiki round-trips behind
    a chat box, and a player waiting on an answer would rather have four
    lookups or a stated timeout than an unbounded wait.
    """
    async def one(tool: str, arg: str) -> Tuple[str, str]:
        try:
            body, source = await TOOLS[tool][2](arg)
        except Exception as e:
            logger.warning("chat lookup %s(%r) failed: %.200s", tool, arg, e)
            return f"lookup failed ({type(e).__name__}) — data unavailable", ""
        return body, source
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(one(t, a) for t, a in calls)), LOOKUP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("chat lookups timed out after %ss", LOOKUP_TIMEOUT)
        return ("The lookups timed out. Answer from the briefing and say "
                "which part you could not verify."), []
    parts, sources = [], []
    for (tool, arg), (body, source) in zip(calls, results):
        parts.append(f"--- LOOKUP {tool}: {arg} ---\n{body}")
        if source:
            sources.append(source)
    return "\n\n".join(parts), sources
