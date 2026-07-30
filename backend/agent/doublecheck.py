"""Second/third-opinion checks of the advisor's counsel.

Two CHECK SLOTS ("second" and "third", llm_runtime.checks()) each hold ANY
provider — a coding-agent CLI (claude_cli / codex_cli, one-off subprocess
on its own subscription login) or any API/local provider from the runtime
(LM Studio, Ollama, OpenAI, Anthropic, custom). So LM Studio can be the
primary advisor with Claude CLI as the 2nd check and Codex CLI as the 3rd,
or Codex primary / LM Studio 2nd / Claude 3rd — any mix. Checks run per
button press, never automatically.

The reviewer gets the advisor's EXACT briefing (stashed in the advice as
`_prompt` at consult time) plus the counsel as displayed after the app's
verification gates, and returns strict JSON. The THIRD check also sees the
second's review (when one exists) and is asked to agree or disagree — an
arbiter, not an echo. House style applies: the reply is shape-enforced,
capped, and every issue that claims to be about an advised entry is checked
against the displayed counsel — ones that match nothing are ANNOTATED
`unmatched` rather than dropped, because "the advisor failed to mention X"
legitimately names things not advised.

CLI mechanics (flags, stdin, temp cwd, no --bare) live in backend/cli_llm.py.
"""
import json
import logging
from datetime import datetime
from typing import Any, List, Optional

from backend import cli_llm
from backend.llm_runtime import effort_for, get_llm, model_for

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a second-opinion reviewer inside an EverQuest Legends (EQL) "
    "companion app. Another LLM (the 'advisor') produced counsel for a "
    "player; you double-check it with fresh eyes and deeper reasoning. You "
    "receive the advisor's full briefing — the exact data and rules it was "
    "given — and the counsel shown to the player. Judge ONLY against that "
    "briefing and the EQL mechanics it states; EQL is a 2026 reimagining of "
    "pre-Kunark EverQuest, so classic-EQ memory is a fallback to be flagged "
    "as uncertain, never silently trusted over the briefing. Never invent "
    "stats, spell levels, or zone facts. Reply with ONLY the JSON object "
    "requested — no markdown fences, no prose around it."
)

REVIEW_TASK_GEAR = """=== YOUR TASK ===
Double-check the gear counsel against the briefing. Look for:
- slot recs that violate the briefing: unowned items, wrong equip slot, STATS UNKNOWN items replaced, comparisons that ignore the pre-scaled +N numbers, a Secondary rec alongside a 2H primary
- JOINT-ASSIGNMENT waste — the most valuable check: worn stats apply identically from any legal slot, but weapon swings exist only in Primary/Secondary (and Bash needs a shield in Secondary for WAR/PAL/SHD). Flag any pair of recs that would waste less if swapped between their slots (e.g. a shield recommended into Secondary while a swinging-capable weapon gets parked in an Any Slot), and give the better assignment as the fix
- stranded exaltations: a rec that unseats a stone's host — trust each stone's machine-checked destination line; flag advice that contradicts it
- Any Slot picks justified by weapon DMG/Delay (they contribute nothing there; BACKSTAB is the exception)
- farm targets that are unrealistic for the level or contradict the briefing's drop data
- pet_gear picks that violate the stated pet rules (weapon delay/ratio reasoning, duplicate categories, items better than the player's own)
- anything important the data supports that the advisor failed to say ("missing" — e.g. an obvious merge opportunity or an ignored clicky)

Reply with ONLY this JSON object:
{
  "verdict": "sound" | "minor_issues" | "major_issues",
  "summary": "2-4 plain sentences: overall quality and the single most important correction",
  "issues": [
    {"section": "slots|farm|exaltations|merges|clickies|pet_gear|missing",
     "item": "the advised entry at fault — or, for section 'missing', the thing left unsaid",
     "problem": "what is wrong, citing the briefing data",
     "fix": "the concrete correction (owned items only), or null",
     "severity": "major" | "minor"}
  ],
  "endorsements": ["up to 3 short notes on advice that is notably right and worth trusting"]
}

Rules:
- Every issue must be grounded in the briefing. If the briefing lacks the data to judge something, say so in summary instead of guessing.
- Any item you name in a fix must be OWNED per the briefing's gear list.
- Judge the CONTENT, not the app's structure (the 24-slot table shape is fixed).
- An empty issues list with verdict "sound" is a perfectly good answer — do not manufacture nitpicks.
"""

REVIEW_TASK = """=== YOUR TASK ===
Double-check the counsel against the briefing. Look for:
- loadout picks that violate the briefing's rules: unowned or over-level picks that slipped through, spells superseded by a better owned spell, travel/resurrection misuse, picks that ignore the stated focus/playstyle, wasted slots, and clearly better OWNED spells the advisor passed over
- replace pairs that are wrong, and strong same-line upgrades it missed
- AA advice that wastes points for this trio and focus (autogranted or achievement-granted lines, poor value order, contradicting the briefing's costs)
- horizon/location errors: wrong levels, zones outside the briefing's hunting-grounds list, or plainly better in-list zones ignored
- class_notes that contradict the briefing
- anything important the data supports that the advisor failed to say ("missing")

Reply with ONLY this JSON object:
{
  "verdict": "sound" | "minor_issues" | "major_issues",
  "summary": "2-4 plain sentences: overall quality and the single most important correction",
  "issues": [
    {"section": "loadout|prebuffs|replace|aa_now|aa_save|horizon|locations|class_notes|missing",
     "item": "the advised entry at fault — or, for section 'missing', the thing left unsaid",
     "problem": "what is wrong, citing the briefing data",
     "fix": "the concrete correction, or null",
     "severity": "major" | "minor"}
  ],
  "endorsements": ["up to 3 short notes on advice that is notably right and worth trusting"]
}

Rules:
- Every issue must be grounded in the briefing. If the briefing lacks the data to judge something, say so in summary instead of guessing.
- Any spell you name in a fix must appear in the briefing's "Spellbook USABLE NOW" list; any zone must come from its hunting-grounds list.
- Judge the CONTENT, not the app's structure (tier layout, slot counts, section shapes are fixed).
- An empty issues list with verdict "sound" is a perfectly good answer — do not manufacture nitpicks.
"""

# keys of the advice payload that are app-internal, not counsel — the
# reviewer never sees them (the briefing is passed separately anyway)
_PRIVATE_KEYS = ("_prompt", "doublecheck", "doublechecks", "stale")

_SEVERITIES = {"major", "minor"}
_SECTIONS = {
    "advisor": {"loadout", "prebuffs", "replace", "aa_now", "aa_save",
                "horizon", "locations", "class_notes", "missing"},
    "gear": {"slots", "farm", "exaltations", "merges", "clickies",
             "pet_gear", "missing"},
}
_TASKS = {"advisor": REVIEW_TASK, "gear": REVIEW_TASK_GEAR}


def _public_advice(advice: dict) -> dict:
    return {k: v for k, v in advice.items() if k not in _PRIVATE_KEYS}


def build_review_prompt(briefing: str, advice: dict,
                        prior: Optional[dict] = None,
                        kind: str = "advisor") -> str:
    counsel = json.dumps(_public_advice(advice), indent=1, ensure_ascii=False)
    parts = [
        "=== THE ADVISOR'S FULL BRIEFING (the exact data and rules it saw) ===",
        briefing,
        "",
        "=== COUNSEL SHOWN TO THE PLAYER (after the app's machine-verification "
        "gates — entries the gates dropped are already gone) ===",
        counsel,
        "",
    ]
    if kind == "advisor":
        # The pick lists above are PRIORITY order, not gem order — the app
        # writes the in-game set deterministically. Without the ACTUAL
        # order here, a checker reconstructs gem positions from list
        # positions and flags Symphonic Aura problems the writer already
        # prevents (live false positive: "SA would capture Selo's").
        try:
            from backend.agent.advisor import stack_gem_order
            names = [str(p.get("name")) for p in
                     (advice.get("must_have") or [])
                     + (advice.get("should_have") or []) if p.get("name")]
            if names:
                order = stack_gem_order(
                    names, [str(s) for s in (advice.get("sa_songs") or [])])
                parts += [
                    "=== WRITTEN SPELL-SET GEM ORDER (deterministic — this "
                    "is what 'write in-game spell set' actually produces; "
                    "judge Symphonic Aura behavior against THIS, never "
                    "against the pick lists' display order. A user-selected "
                    "subset re-stacks by the same rules.) ===",
                    "\n".join(f"gem {i}: {n}"
                              for i, n in enumerate(order, 1)),
                    "",
                ]
        except Exception:
            pass  # review still works without the order section
    if prior:
        keep = {k: prior.get(k) for k in
                ("verdict", "summary", "issues", "endorsements")}
        parts += [
            "=== AN EARLIER INDEPENDENT CHECK "
            f"(by {prior.get('model') or prior.get('provider') or 'another model'}) ===",
            json.dumps(keep, indent=1, ensure_ascii=False),
            "",
            "Form your OWN view from the briefing FIRST, then compare: say in "
            "your summary where you agree or disagree with this earlier check, "
            "and do not repeat an issue of it you cannot independently ground "
            "in the briefing.",
            "",
        ]
    parts.append(_TASKS.get(kind) or REVIEW_TASK)
    if prior:
        parts.append(
            '\nBecause an earlier check exists, ALSO include these two keys '
            'in your JSON object: "prior_agreement": "agree" | "partial" | '
            '"disagree" (how your findings relate to the earlier check as a '
            'whole) and "prior_notes": one short sentence naming the key '
            'point you differ on, or null when you fully agree.')
    return "\n".join(parts)


def _clean_issues(items: Any, advice_blob: str,
                  kind: str = "advisor") -> List[dict]:
    sections = _SECTIONS.get(kind) or _SECTIONS["advisor"]
    out: List[dict] = []
    for it in items or []:
        if not (isinstance(it, dict) and it.get("problem")):
            continue
        section = str(it.get("section") or "").strip().lower()
        sev = str(it.get("severity") or "").strip().lower()
        issue = {
            "section": section if section in sections else "other",
            "item": str(it.get("item") or "").strip(),
            "problem": str(it.get("problem")).strip(),
            "fix": (str(it["fix"]).strip()
                    if it.get("fix") not in (None, "", "null") else None),
            "severity": sev if sev in _SEVERITIES else "minor",
        }
        # deterministic cross-check: an issue about an ADVISED entry must
        # name something actually displayed. "missing" issues are exempt —
        # their whole point is naming what was not advised.
        if issue["section"] not in ("missing", "other") and issue["item"]:
            if issue["item"].casefold() not in advice_blob:
                issue["unmatched"] = True
        out.append(issue)
        if len(out) >= 12:
            break
    # majors first, so a capped list never hides the important ones
    out.sort(key=lambda i: i["severity"] != "major")
    return out


def _error(msg: str) -> dict:
    logger.warning("Check failed: %s", msg)
    return {"error": msg}


async def _ask(provider: str, prompt: str,
               system: Optional[str] = SYSTEM_PROMPT) -> tuple:
    """(reply text, meta) from the given provider — CLI subprocess or the
    runtime's chat model. Raises with a user-showable message. `system`
    defaults to the reviewer role; the revision flow passes None because
    a reviser is the ADVISOR again, not a critic."""
    if provider in cli_llm.PROVIDERS:
        return await cli_llm.arun(provider, prompt, system=system,
                                  model=model_for(provider),
                                  effort=effort_for(provider))
    # API/local providers reuse the same seam the advisor consults through
    from langchain_core.messages import HumanMessage, SystemMessage
    from backend.agent.advisor import _reply_text
    llm = get_llm(provider)
    msgs = ([SystemMessage(content=system)] if system else []) + \
           [HumanMessage(content=prompt)]
    response = await llm.ainvoke(msgs)
    return _reply_text(response), {}


def build_revision_prompt(briefing: str, advice: dict, reviews: dict) -> str:
    """The revise-with-findings prompt: the ORIGINAL briefing (schema and
    rules included), the counsel as displayed, and the check findings.
    The reviser answers in the SAME schema — its reply re-enters every
    deterministic gate via generate_advice(reply_json=...)."""
    counsel = json.dumps(_public_advice(advice), indent=1, ensure_ascii=False)
    finds = []
    for slot in ("second", "third"):
        r = reviews.get(slot)
        if not r:
            continue
        keep = {k: r.get(k) for k in
                ("provider", "model", "verdict", "summary", "issues")}
        finds.append(f"--- {slot} check ---\n"
                     + json.dumps(keep, indent=1, ensure_ascii=False))
    return "\n".join([
        "=== YOUR ORIGINAL BRIEFING (data, rules, and the EXACT reply "
        "schema — all still binding) ===",
        briefing,
        "",
        "=== YOUR PREVIOUS COUNSEL (as shown to the player) ===",
        counsel,
        "",
        "=== INDEPENDENT REVIEW FINDINGS ===",
        "\n".join(finds),
        "",
        "=== YOUR TASK ===",
        "Produce a REVISED counsel. Apply each finding you can "
        "independently verify against the briefing; decline the rest. "
        "Keep everything a finding does not touch STABLE — do not "
        "reshuffle unrelated picks. Reply with ONLY a JSON object in "
        "EXACTLY the briefing's schema, plus two extra keys:",
        '  "revision_notes": "2-4 sentences: what you changed and why",',
        '  "declined_findings": [{"item": "...", "reason": "why this '
        'finding was not applied"}]',
    ])


async def run_revision(briefing: str, advice: dict, reviews: dict,
                       provider: str) -> tuple:
    """(parsed revision dict, error) — the caller gates the dict through
    generate_advice(reply_json=...); this only asks and parses."""
    prompt = build_revision_prompt(briefing, advice, reviews)
    try:
        text, _meta = await _ask(provider, prompt, system=None)
    except Exception as e:
        return None, str(e)[:400]
    from backend.agent.advisor import _extract_json
    data = _extract_json(text)
    if not data:
        return None, (f"the revision reply carried no JSON "
                      f"({len(text)} chars of text seen)")
    return data, None


async def run_doublecheck(briefing: str, advice: dict, provider: str,
                          slot: str = "second",
                          prior: Optional[dict] = None,
                          kind: str = "advisor") -> dict:
    """Review the displayed counsel with the given provider. `kind` picks
    the review rubric — "advisor" (spells/AA) or "gear" (the slot table,
    reviewed JOINTLY — the assignment-across-slots miss is precisely what
    a per-row advisor cannot see about its own output). Returns the review
    dict, or {"error": ...} — never raises, never fakes a review."""
    if provider in ("none", "", None):
        return _error("This check slot is set to none — pick a model for it "
                      "next to the check button.")
    prompt = build_review_prompt(briefing, advice, prior, kind)
    started = datetime.now()
    try:
        text, meta = await _ask(provider, prompt)
    except Exception as e:
        return _error(f"{cli_llm.LABELS.get(provider, provider)} check "
                      f"failed: {str(e)[:400]}")

    from backend.agent.advisor import _extract_json
    data = _extract_json(text)
    if not data:
        return _error("the reply carried no JSON review "
                      f"({len(text)} chars of text seen).")

    advice_blob = json.dumps(_public_advice(advice),
                             ensure_ascii=False).casefold()
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("sound", "minor_issues", "major_issues"):
        verdict = "minor_issues"
    issues = _clean_issues(data.get("issues"), advice_blob, kind)
    if not issues and verdict != "sound":
        # a verdict with zero surviving issues is an empty accusation
        verdict = "sound"
    elapsed = (datetime.now() - started).total_seconds()
    duration_ms = meta.get("duration_ms")
    effort = (effort_for(provider)
              if provider in cli_llm.PROVIDERS else None)
    # structured stance toward the earlier check — only meaningful when one
    # was actually shown; a model volunteering it unprompted is discarded
    agreement, agreement_notes = None, None
    if prior:
        pa = str(data.get("prior_agreement") or "").strip().lower()
        agreement = pa if pa in ("agree", "partial", "disagree") else None
        pn = data.get("prior_notes")
        agreement_notes = (str(pn).strip()
                           if pn not in (None, "", "null") else None)
    return {
        "slot": slot,
        "provider": provider,
        "prior_agreement": agreement,
        "prior_notes": agreement_notes,
        "verdict": verdict,
        "summary": (str(data.get("summary")).strip()
                    if data.get("summary") else None),
        "issues": issues,
        "endorsements": [str(e).strip() for e in
                         (data.get("endorsements") or [])[:3]
                         if str(e).strip()],
        "model": model_for(provider),
        "effort": effort,
        "duration_s": round(duration_ms / 1000 if duration_ms else elapsed),
        "cost_usd": meta.get("cost_usd"),
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
