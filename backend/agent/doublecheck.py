"""Second-opinion double-check of the advisor's counsel via the Claude Code
CLI (`claude -p`) — one-off, non-interactive, subscription-authed.

Deliberately NOT a provider in llm_runtime: the point is a second opinion
from a *stronger* model (Opus at high reasoning effort) on what the
configured advisor model produced, triggered per press, never automatic.
The CLI is used instead of the Anthropic API so a Claude Code subscription
covers it with no API key in the app.

The reviewer gets the advisor's EXACT briefing (stashed in the advice as
`_prompt` at consult time) plus the counsel as displayed after the app's
verification gates, and returns strict JSON. House style still applies:
the reply is shape-enforced, capped, and every issue that claims to be
about an advised entry is checked against the displayed counsel — ones
that match nothing are ANNOTATED `unmatched` rather than dropped, because
"the advisor failed to mention X" legitimately names things not advised.

CLI flag notes (verified against claude 2.1.220):
- `--tools ""` disables all tools: this is a pure reasoning call and must
  never wander the filesystem.
- `--strict-mcp-config` with no --mcp-config = no MCP servers.
- `--system-prompt` REPLACES the default agent system prompt.
- NEVER add `--bare`: it restricts auth to ANTHROPIC_API_KEY and would
  break the normal subscription (OAuth) login this feature exists to use.
- cwd is the system temp dir, not the repo and not data/: print mode
  auto-discovers CLAUDE.md up the tree, and this repo's would inject
  thousands of tokens of irrelevant coding instructions into the review.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from typing import Any, List, Optional

from backend.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a second-opinion reviewer inside an EverQuest Legends (EQL) "
    "companion app. A smaller LLM (the 'advisor') produced counsel for a "
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
_PRIVATE_KEYS = ("_prompt", "doublecheck", "stale")

_SEVERITIES = {"major", "minor"}
_SECTIONS = {"loadout", "prebuffs", "replace", "aa_now", "aa_save",
             "horizon", "locations", "class_notes", "missing"}


def _public_advice(advice: dict) -> dict:
    return {k: v for k, v in advice.items() if k not in _PRIVATE_KEYS}


def build_review_prompt(briefing: str, advice: dict) -> str:
    counsel = json.dumps(_public_advice(advice), indent=1, ensure_ascii=False)
    return (
        "=== THE ADVISOR'S FULL BRIEFING (the exact data and rules it saw) ===\n"
        f"{briefing}\n\n"
        "=== COUNSEL SHOWN TO THE PLAYER (after the app's machine-verification "
        "gates — entries the gates dropped are already gone) ===\n"
        f"{counsel}\n\n"
        f"{REVIEW_TASK}"
    )


def _resolve_cli() -> Optional[str]:
    """Absolute path to the claude executable, or None. `shutil.which`
    covers PATH and adds .exe on Windows; an explicit path in the setting
    is honored as-is."""
    exe = shutil.which(settings.claude_cli)
    if exe:
        return exe
    p = settings.claude_cli
    return p if os.path.isfile(p) else None


def _run_cli(exe: str, prompt: str) -> subprocess.CompletedProcess:
    """One blocking `claude -p` run. The prompt travels via stdin — it is
    tens of KB, far past Windows' command-line length limit."""
    cmd = [
        exe, "-p",
        "--model", settings.doublecheck_model,
        "--effort", settings.doublecheck_effort,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    kw: dict = {}
    if os.name == "nt":
        # the packaged app is windowed (no console): without this flag a
        # console window flashes up for the whole run
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        cmd, input=prompt, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=settings.doublecheck_timeout_s,
        cwd=tempfile.gettempdir(), **kw)


def _extract_review(text: str) -> Optional[dict]:
    from backend.agent.advisor import _extract_json
    return _extract_json(text)


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
    logger.warning("Double-check failed: %s", msg)
    return {"error": msg}


async def run_doublecheck(briefing: str, advice: dict) -> dict:
    """Review the displayed counsel with one CLI call. Returns the review
    dict, or {"error": ...} — never raises, never fakes a review."""
    exe = _resolve_cli()
    if not exe:
        return _error(
            f"claude CLI not found ({settings.claude_cli!r}) — install "
            "Claude Code or set CLAUDE_CLI in .env to its full path.")
    prompt = build_review_prompt(briefing, advice)
    started = datetime.now()
    try:
        proc = await asyncio.to_thread(_run_cli, exe, prompt)
    except subprocess.TimeoutExpired:
        return _error(f"claude CLI timed out after "
                      f"{settings.doublecheck_timeout_s}s "
                      f"({settings.doublecheck_model}, "
                      f"effort {settings.doublecheck_effort}).")
    except Exception as e:  # spawn failure — bad path, EPERM, ...
        return _error(f"could not run the claude CLI: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return _error(f"claude CLI exited {proc.returncode}: {tail or 'no output'}")

    # --output-format json prints one envelope object; the answer is its
    # "result" string. Fall back to treating stdout as the answer itself
    # so a format change degrades instead of breaking.
    raw = (proc.stdout or "").strip()
    result_text, cost, duration_ms = raw, None, None
    try:
        envelope = json.loads(raw)
        if isinstance(envelope, dict) and "result" in envelope:
            if envelope.get("is_error"):
                return _error(f"claude CLI reported an error: "
                              f"{str(envelope.get('result'))[:400]}")
            result_text = str(envelope.get("result") or "")
            cost = envelope.get("total_cost_usd")
            duration_ms = envelope.get("duration_ms")
    except json.JSONDecodeError:
        pass

    data = _extract_review(result_text)
    if not data:
        return _error("the reply carried no JSON review "
                      f"({len(result_text)} chars of text seen).")

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
    return {
        "verdict": verdict,
        "summary": (str(data.get("summary")).strip()
                    if data.get("summary") else None),
        "issues": issues,
        "endorsements": [str(e).strip() for e in
                         (data.get("endorsements") or [])[:3]
                         if str(e).strip()],
        "model": settings.doublecheck_model,
        "effort": settings.doublecheck_effort,
        "duration_s": round(duration_ms / 1000 if duration_ms else elapsed),
        "cost_usd": cost,
        "generated": datetime.now().isoformat(timespec="seconds"),
    }
