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
from backend.llm_runtime import get_llm, model_for

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
_SECTIONS = {"loadout", "prebuffs", "replace", "aa_now", "aa_save",
             "horizon", "locations", "class_notes", "missing"}


def _public_advice(advice: dict) -> dict:
    return {k: v for k, v in advice.items() if k not in _PRIVATE_KEYS}


def build_review_prompt(briefing: str, advice: dict,
                        prior: Optional[dict] = None) -> str:
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
    parts.append(REVIEW_TASK)
    return "\n".join(parts)


def _clean_issues(items: Any, advice_blob: str) -> List[dict]:
    out: List[dict] = []
    for it in items or []:
        if not (isinstance(it, dict) and it.get("problem")):
            continue
        section = str(it.get("section") or "").strip().lower()
        sev = str(it.get("severity") or "").strip().lower()
        issue = {
            "section": section if section in _SECTIONS else "other",
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


async def _ask(provider: str, prompt: str) -> tuple:
    """(reply text, meta) from the given provider — CLI subprocess or the
    runtime's chat model. Raises with a user-showable message."""
    if provider in cli_llm.PROVIDERS:
        model = model_for(provider)
        return await cli_llm.arun(provider, prompt, system=SYSTEM_PROMPT,
                                  model=model)
    # API/local providers reuse the same seam the advisor consults through
    from langchain_core.messages import HumanMessage, SystemMessage
    from backend.agent.advisor import _reply_text
    llm = get_llm(provider)
    response = await llm.ainvoke([SystemMessage(content=SYSTEM_PROMPT),
                                  HumanMessage(content=prompt)])
    return _reply_text(response), {}


async def run_doublecheck(briefing: str, advice: dict, provider: str,
                          slot: str = "second",
                          prior: Optional[dict] = None) -> dict:
    """Review the displayed counsel with the given provider. Returns the
    review dict, or {"error": ...} — never raises, never fakes a review."""
    if provider in ("none", "", None):
        return _error("This check slot is set to none — pick a model for it "
                      "next to the check button.")
    prompt = build_review_prompt(briefing, advice, prior)
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
    issues = _clean_issues(data.get("issues"), advice_blob)
    if not issues and verdict != "sound":
        # a verdict with zero surviving issues is an empty accusation
        verdict = "sound"
    elapsed = (datetime.now() - started).total_seconds()
    duration_ms = meta.get("duration_ms")
    effort = (cli_llm.model_effort(provider)[1]
              if provider in cli_llm.PROVIDERS else None)
    return {
        "slot": slot,
        "provider": provider,
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
