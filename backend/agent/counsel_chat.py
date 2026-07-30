"""Chat WITH the counsel: a grounded Q&A seat beside the Advisor tab.

Deliberately NOT backend/agent/graph.py, which predates the advisor and
rewrites MOCK suggestion data (that is why its tab was removed). This
one answers from exactly what the consult saw — the stored briefing
(spellbook, owned gear, hunting table, class guides, AA sync), the
counsel as displayed after the verification gates, the gear table, and
any check findings — so "why did you slot that?" and "is Leech worth it
at 9?" have real answers instead of model memory.

Same provider seam as everything else (`get_llm()`), so the coding-agent
CLIs work here too. One turn per request, no tools, no streaming: the
value is grounding, not agency.

The transcript is folded into ONE user message rather than sent as a
message list, because the CLI providers flatten a list into a single
prompt anyway and would lose the role labels on the way.
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are the counsel inside WarCounsel, an EverQuest Legends companion "
    "app. You are talking to the player about THEIR character and the advice "
    "the app has produced for them. EQL is a 2026 reimagining of pre-Kunark "
    "EverQuest: classic-EQ memory is a fallback to be flagged as uncertain, "
    "never trusted over the data below.\n"
    "- Ground every answer in the CONTEXT. When the data does not cover "
    "something, say so plainly instead of inventing spells, items, zones, "
    "levels, or numbers.\n"
    "- You may disagree with the counsel shown — say why, citing the data.\n"
    "- The player's own reports about their game beat both the data and your "
    "memory; accept them and reason from there.\n"
    "- Be concise and conversational: a few sentences. Use a short list only "
    "when it genuinely helps. No headers, no preamble, no sign-off."
)

MAX_BRIEFING = 24_000
MAX_COUNSEL = 6_000
MAX_GEAR = 4_000
MAX_TURNS = 8


def _trim(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + "\n…(truncated)"


def _counsel_digest(advice: Optional[dict]) -> str:
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
    return _trim(json.dumps(keep, indent=1, ensure_ascii=False), MAX_COUNSEL)


def _gear_digest(gear: Optional[dict]) -> str:
    """Only rows that say something — a 24-row table of "keep" lines is
    mostly noise in a chat context."""
    if not gear:
        return ""
    lines = []
    for s in gear.get("slots") or []:
        rec, cur = s.get("recommend"), s.get("current")
        if rec and rec != cur:
            lines.append(f"{s.get('slot')}: {cur or 'empty'} -> {rec} "
                         f"({s.get('where') or '?'}) — {s.get('why') or ''}")
    for m in (gear.get("merges") or [])[:5]:
        lines.append(f"MERGE {m.get('item')}: {', '.join(m.get('copies') or [])}"
                     f" -> {m.get('result')}")
    for c in (gear.get("clickies") or [])[:5]:
        lines.append(f"CLICKY {c.get('item')}: {c.get('spell')}")
    for x in (gear.get("exaltations") or [])[:6]:
        lines.append(f"EXALT {x.get('name')} — {x.get('where')} — "
                     f"{x.get('why') or ''}")
    if gear.get("note"):
        lines.insert(0, f"note: {gear['note']}")
    return _trim("\n".join(lines), MAX_GEAR)


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
                 history: Optional[List[dict]] = None) -> str:
    parts = [
        "=== CHARACTER BRIEFING (the exact data the consult was built "
        "from — owned spells, gear, hunting table, class guides) ===",
        _trim(briefing, MAX_BRIEFING),
        "",
        "=== LIVE STATE RIGHT NOW ===",
        live,
        "",
        "=== THE COUNSEL AS SHOWN TO THE PLAYER ===",
        _counsel_digest(advice),
    ]
    g = _gear_digest(gear)
    if g:
        parts += ["", "=== GEAR TABLE (rows proposing a change) ===", g]
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


async def answer(question: str, briefing: str, advice: Optional[dict],
                 gear: Optional[dict], live: str,
                 history: Optional[List[dict]] = None) -> str:
    """One grounded reply. Raises with a user-showable message on failure —
    the route turns that into a 502 so the chat says what went wrong
    instead of inventing an answer."""
    from backend.agent.advisor import _lmstudio_budget, _reply_text
    from backend.llm_runtime import get_llm
    import asyncio

    prompt = build_prompt(question, briefing, advice, gear, live, history)
    llm = get_llm()
    budget = await asyncio.to_thread(_lmstudio_budget, len(prompt))
    if budget:
        try:
            llm = llm.bind(max_tokens=budget)
        except Exception:
            pass
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        msgs = [SystemMessage(content=SYSTEM), HumanMessage(content=prompt)]
    except ImportError:  # lite build: the CLI seam takes plain strings
        class _M:
            def __init__(self, content):
                self.content = content
        msgs = [_M(SYSTEM), _M(prompt)]
    response = await llm.ainvoke(msgs)
    text = (_reply_text(response) or "").strip()
    if not text:
        raise RuntimeError("the model returned an empty reply")
    return text
