"""Chat WITH the counsel: a grounded Q&A seat beside the Advisor tab.

Deliberately NOT backend/agent/graph.py, which predates the advisor and
rewrites MOCK suggestion data (that is why its tab was removed). This
one answers from exactly what the consult saw — the stored briefing
(spellbook, owned gear, hunting table, class guides, AA sync), the
counsel as displayed after the verification gates, the gear table, and
any check findings — so "why did you slot that?" and "is Leech worth it
at 9?" have real answers instead of model memory.

Same provider seam as everything else (`get_llm()`), so the coding-agent
CLIs work here too. No streaming: the value is grounding, not agency.

It is no longer one turn, though. The briefing is a SNAPSHOT of what the
consult gathered, so anything outside it — a vendor's zone, a mob's
drops, a zone page — used to come back as "nothing in your briefing lists
that", which reads as ignorance of the game rather than the edge of one
cached prompt. `chat_tools` gives the model a handful of live EQL
lookups (MCP server, plain-HTTP wiki fallback) over a one-line-per-call
text protocol every provider can drive, and this module runs up to
MAX_ROUNDS of them before the answer. A model that never emits one gets
exactly the old single-turn behaviour.

The transcript is folded into ONE user message rather than sent as a
message list, because the CLI providers flatten a list into a single
prompt anyway and would lose the role labels on the way.
"""
import logging
from typing import Dict, List, Optional, Tuple

from backend.agent import chat_tools

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are the counsel inside WarCounsel, an EverQuest Legends companion "
    "app. You are talking to the player about THEIR character and the advice "
    "the app has produced for them. You speak for BOTH of its advisors — the "
    "counsel (spells, AAs, where to hunt) and the equipment advisor (the "
    "slot table, merges, clickies, exaltation stones, pet gear) — and both "
    "of their briefings are below. A question about gear is yours to answer, "
    "not something to hand off. EQL is a 2026 reimagining of pre-Kunark "
    "EverQuest: classic-EQ memory is a fallback to be flagged as uncertain, "
    "never trusted over the data below.\n"
    "- Ground every answer in the CONTEXT, or in a lookup you ran. When "
    "neither covers something, say so plainly instead of inventing spells, "
    "items, zones, levels, or numbers.\n"
    "- You may disagree with the counsel shown — say why, citing the data.\n"
    "- The player's own reports about their game beat both the data and your "
    "memory; accept them and reason from there.\n"
    "- Be concise and conversational: a few sentences. Use a short list only "
    "when it genuinely helps. No headers, no preamble, no sign-off."
)

# The lookup half of the system prompt. Separate so a provider that never
# uses it still reads a coherent brief, and so the tool list has exactly
# one source of truth (chat_tools.TOOLS).
LOOKUP_RULES = (
    "\n\nLIVE LOOKUPS — you can read the EQL wiki and the eqlbuilds "
    "dataset during this answer. The briefing above is a snapshot of what "
    "the last consult gathered; it does NOT contain the whole wiki, so "
    "vendor locations, drop tables, zone pages, quests and spells outside "
    "the counsel are things you look up rather than things you lack.\n"
    "To look something up, reply with ONLY lookup lines and nothing else:\n"
    "{tools}\n"
    "Example — the player is in Kelethin and asks where to buy Shieldskin:\n"
    "  LOOKUP vendors: Shieldskin\n"
    "  LOOKUP wiki_page: Kelethin\n"
    "Rules:\n"
    f"- At most {chat_tools.MAX_PER_ROUND} lookups per turn, and at most "
    f"{chat_tools.MAX_ROUNDS} lookup turns — then you MUST answer.\n"
    "- A lookup turn is not seen by the player. Do not greet them, explain "
    "that you are looking something up, or answer partially in it.\n"
    "- Look things up when the briefing genuinely does not cover the "
    "question. Do not look up what is already in front of you.\n"
    "- Arguments are exact names. If a page title misses, run wiki_search "
    "on ONE distinctive word from it and read the real title — eqlwiki "
    "searches titles only, so a phrase finds nothing.\n"
    "- Wiki text is inherited from classic EverQuest and can be wrong for "
    "EQL. Prefer the briefing and the player's own reports where they "
    "conflict, and say when a claim rests on wiki text alone."
)

MAX_BRIEFING = 24_000
MAX_GEAR_BRIEFING = 10_000
MAX_COUNSEL = 6_000
MAX_GEAR = 7_000
MAX_TURNS = 8


def _trim(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + "\n…(truncated)"


_DEFAULT_CAPS = {"briefing": MAX_BRIEFING, "gear_briefing": MAX_GEAR_BRIEFING,
                 "counsel": MAX_COUNSEL, "gear": MAX_GEAR}


def _caps() -> Dict[str, int]:
    """Per-section char caps for this provider.

    The prompt now carries BOTH consults — two briefings and two digests.
    On a cloud or CLI provider that is fine: large windows, and
    `context_limit()` deliberately does not scale them up because their
    tokens are billed. On a LOCAL model it is the difference between a
    full window and a silent overflow, so every section shrinks
    proportionally to fit what is actually loaded. Same policy as
    `game_data.guide_budget()`, applied to the half of the prompt that had
    never consulted it — the fixed caps predate the gear half and already
    overflowed an 8k window on their own.
    """
    caps = dict(_DEFAULT_CAPS)
    try:
        from backend import llm_runtime
        if llm_runtime.active()["provider"] not in ("lmstudio", "local"):
            return caps
        limit = int(llm_runtime.context_limit()["limit"])
    except Exception:
        return caps
    # ~3 chars/token (the ratio _lmstudio_budget already assumes); 60% of
    # the window for these sections, since live state, check findings, the
    # transcript, lookup results and the reply share the rest.
    room = max(4_000, int(limit * 3 * 0.6))
    total = sum(caps.values())
    if total <= room:
        return caps
    scale = room / total
    # Floor every section: a briefing cut to nothing is worse than a short
    # one, because an empty section reads as "the app has no such data".
    return {k: max(600, int(v * scale)) for k, v in caps.items()}


def _counsel_digest(advice: Optional[dict], cap: int = MAX_COUNSEL) -> str:
    """The counsel as the player sees it, compact enough to sit beside the
    briefing: picks with reasons, plus the sections a question usually
    lands on."""
    if not advice:
        return "No counsel generated yet (the player has not pressed Consult)."
    import json

    keep = {}
    for k in ("note", "sa_songs", "must_have", "should_have", "nice_to_have",
              "prebuffs", "replace", "aa_now", "aa_save", "horizon",
              "locations", "class_notes", "purchase"):
        v = advice.get(k)
        if v:
            keep[k] = v
    keep["_meta"] = {"source": advice.get("source"),
                     "model": (advice.get("llm") or {}).get("model"),
                     "generated": advice.get("generated"),
                     "revised": bool(advice.get("revision"))}
    return _trim(json.dumps(keep, indent=1, ensure_ascii=False), cap)


def _gear_digest(gear: Optional[dict], cap: int = MAX_GEAR) -> str:
    """The gear table as the player sees it — the WHOLE 24-slot roster.

    This used to keep only rows proposing a change, on the theory that a
    table of "keep" lines is noise. It is not: a kept row is the answer to
    "what am I wearing on my head", "is my off-hand worth replacing" and
    "why did it leave that alone", and dropping it left the chat unable to
    see two thirds of a consult the player was looking straight at. Kept
    rows are marked so the model never reads one as a suggestion.
    """
    if not gear:
        return ""
    lines = []
    if gear.get("note"):
        lines.append(f"note: {gear['note']}")
    meta = gear.get("llm") or {}
    lines.append(f"[table from {gear.get('source')}"
                 + (f" · {meta.get('model')}" if meta.get("model") else "")
                 + (" · REVISED from check findings" if gear.get("revision") else "")
                 + f" · {gear.get('generated') or '?'}]")
    lines.append("SLOTS (every slot, worn -> recommendation):")
    for s in gear.get("slots") or []:
        rec, cur = s.get("recommend"), s.get("current")
        slot, why = s.get("slot"), (s.get("why") or "").strip()
        if rec and rec != cur:
            lines.append(f"  {slot}: {cur or 'empty'} -> {rec} "
                         f"({s.get('where') or '?'}) — {why}")
        elif rec:
            lines.append(f"  {slot}: {cur} — KEEP (the rec IS what is worn)"
                         + (f" — {why}" if why else ""))
        else:
            lines.append(f"  {slot}: {cur or 'empty'} — no change proposed"
                         + (f" — {why}" if why else ""))
    for label, key, fmt in (
        ("MERGES", "merges",
         lambda m: (f"  {m.get('item')}: {', '.join(m.get('copies') or [])} -> "
                    f"{m.get('result')}"
                    + (" (hosts an exaltation stone)" if m.get("hosts_exalt") else "")
                    + (" (both worn — merging empties a slot)" if m.get("worn_pair") else "")
                    + (f" — {m.get('compare')}" if m.get("compare") else ""))),
        ("CLICKIES (owned items with an activatable effect)", "clickies",
         lambda c: f"  {c.get('item')} ({c.get('where') or c.get('slot')}): "
                   f"{c.get('spell')} — {c.get('note') or ''}"),
        ("EXALTATION STONES", "exaltations",
         lambda x: f"  {x.get('name')} — in {x.get('where') or '?'} — "
                   f"{x.get('why') or ''}"
                   + (f" — can move to: {x['move_to']}" if x.get("move_to") else "")),
        ("PET GEAR", "pet_gear",
         lambda p: f"  {p.get('item')} — {p.get('why') or ''}"),
        ("FARM TARGETS (not owned)", "farm",
         lambda f: f"  {f.get('item')} ({f.get('slot') or '?'}) — "
                   f"{f.get('zone') or '?'} / {f.get('source') or '?'} — "
                   f"{f.get('why') or ''}"),
    ):
        rows = gear.get(key) or []
        if rows:
            lines.append(f"{label}:")
            lines += [fmt(r) for r in rows[:10]]
    unknown = gear.get("unknown") or []
    if unknown:
        lines.append("STATS UNKNOWN (owned, no wiki page — never compared, "
                     "never replaced): " + ", ".join(unknown[:20]))
    return _trim("\n".join(lines), cap)


def _findings_digest(advice: Optional[dict], gear: Optional[dict]) -> str:
    out = []
    for label, blob in (("counsel", advice), ("gear", gear)):
        for slot, r in ((blob or {}).get("doublechecks") or {}).items():
            issues = "; ".join(
                f"[{i.get('severity')}] {i.get('item')}: {i.get('problem')}"
                for i in (r.get("issues") or [])[:6])
            out.append(f"{label} {slot} check ({r.get('model')}) — "
                       f"{r.get('verdict')}: {r.get('summary') or ''} "
                       f"{issues}".strip())
    return _trim("\n".join(out), 3_000)


def build_prompt(question: str, briefing: str, advice: Optional[dict],
                 gear: Optional[dict], live: str,
                 history: Optional[List[dict]] = None,
                 gear_briefing: str = "") -> str:
    caps = _caps()
    parts = [
        "=== COUNSEL BRIEFING (the exact data the SPELL/AA consult was "
        "built from — owned spells, hunting table, class guides) ===",
        _trim(briefing, caps["briefing"]),
        "",
        "=== LIVE STATE RIGHT NOW ===",
        live,
        "",
        "=== THE COUNSEL AS SHOWN TO THE PLAYER ===",
        _counsel_digest(advice, caps["counsel"]),
    ]
    if gear_briefing:
        # The gear consult mines its OWN briefing — every owned item with
        # wiki stats scaled to its +N, exaltation sockets and destinations,
        # the pet pool. None of it is in the counsel briefing, so without
        # this the chat could see the gear table's verdicts but not one
        # number behind them, and "why not the other sword?" had no answer.
        parts += ["", "=== GEAR BRIEFING (the exact data the EQUIPMENT "
                  "consult was built from — owned items with stats already "
                  "scaled to their +N, exaltations, pet pool) ===",
                  _trim(gear_briefing, caps["gear_briefing"])]
    g = _gear_digest(gear, caps["gear"])
    if g:
        parts += ["", "=== THE GEAR TABLE AS SHOWN TO THE PLAYER ===", g]
    else:
        # Say it, rather than letting a missing section read as "no gear".
        # The counsel briefing lists worn item NAMES either way, so a gear
        # question is still answerable — just without stats.
        parts += ["", "=== THE GEAR TABLE AS SHOWN TO THE PLAYER ===",
                  "No equipment consult has been run yet — the player "
                  "presses 'consult gear' in the Equipment section. Their "
                  "WORN ITEM NAMES are in the counsel briefing above; "
                  "per-item stats are not, so look one up rather than "
                  "recalling its numbers."]
    f = _findings_digest(advice, gear)
    if f:
        parts += ["", "=== SECOND-OPINION FINDINGS ON THAT ADVICE ===", f]
    if history:
        turns = "\n".join(
            f"{'Player' if h.get('role') == 'user' else 'You'}: "
            f"{str(h.get('content') or '')[:1500]}"
            for h in history[-MAX_TURNS:])
        parts += ["", "=== CONVERSATION SO FAR ===", turns]
    parts += ["", "=== THE PLAYER ASKS ===", question.strip(),
              "", "Answer them directly, grounded in the data above."]
    return "\n".join(parts)


def _messages(system: str, prompt: str):
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        return [SystemMessage(content=system), HumanMessage(content=prompt)]
    except ImportError:  # lite build: the CLI seam takes plain strings
        class _M:
            def __init__(self, content):
                self.content = content
        return [_M(system), _M(prompt)]


async def _ask(system: str, prompt: str) -> str:
    from backend.agent.advisor import _lmstudio_budget, _reply_text
    from backend.llm_runtime import get_llm
    import asyncio

    llm = get_llm()
    budget = await asyncio.to_thread(_lmstudio_budget, len(system) + len(prompt))
    if budget:
        try:
            llm = llm.bind(max_tokens=budget)
        except Exception:
            pass
    response = await llm.ainvoke(_messages(system, prompt))
    return (_reply_text(response) or "").strip()


async def answer(question: str, briefing: str, advice: Optional[dict],
                 gear: Optional[dict], live: str,
                 history: Optional[List[dict]] = None,
                 lookups: bool = True,
                 gear_briefing: str = "") -> Tuple[str, List[str]]:
    """One grounded reply plus the sources any lookups consulted.

    Raises with a user-showable message on failure — the route turns that
    into a 502 so the chat says what went wrong instead of inventing an
    answer. Lookup failures are NOT that: they fold into the transcript as
    "data unavailable" and the model answers around them, because a
    missing wiki page is an ordinary fact about a question, not a fault.
    """
    prompt = build_prompt(question, briefing, advice, gear, live, history,
                          gear_briefing)
    system = SYSTEM + (LOOKUP_RULES.format(tools=chat_tools.tool_help())
                       if lookups else "")
    sources: List[str] = []
    text = ""
    for round_no in range(chat_tools.MAX_ROUNDS if lookups else 0):
        text = await _ask(system, prompt)
        calls = chat_tools.parse_lookups(text)
        if not calls:
            break
        transcript, found = await chat_tools.run_lookups(calls)
        sources += [s for s in found if s not in sources]
        last = round_no == chat_tools.MAX_ROUNDS - 1
        prompt += (
            "\n\n=== LOOKUP RESULTS (live EQL wiki / eqlbuilds data — "
            "these outrank your memory) ===\n" + transcript
            + ("\n\nNo lookups remain. Answer the player now, from the "
               "briefing and these results."
               if last else
               "\n\nAnswer the player now, or run one more lookup turn if "
               "something essential is still missing."))
        text = ""  # a lookup turn is protocol, never an answer
    if not text:
        text = await _ask(system, prompt)
    stripped = chat_tools.strip_lookups(text) if lookups else text.strip()
    if not stripped and text.strip():
        # It spent its last turn on protocol. Ask once more WITHOUT the
        # lookup rules — with them gone there is no line for it to emit
        # but prose, and the results it asked for are already in the
        # prompt. Weak local models do this; the tab must not break.
        logger.info("Counsel chat: final turn was lookups only, re-asking")
        stripped = chat_tools.strip_lookups(await _ask(SYSTEM, prompt))
    if not stripped:
        raise RuntimeError("the model returned an empty reply")
    return stripped, sources
