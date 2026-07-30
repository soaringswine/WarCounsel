"""Coding-agent CLIs (Claude Code, Codex) as LLM backends.

Both run as ONE-OFF non-interactive processes per request — no server, no
API key in the app; auth is whatever the CLI itself is logged in with
(typically a Claude / ChatGPT subscription). Used two ways:

- as a MAIN advisor provider (`claude_cli` / `codex_cli` in llm_runtime,
  wrapped in a chat-model duck type), and
- as double-check reviewers (backend/agent/doublecheck.py).

Shared invariants, learned the careful way:
- The prompt travels via STDIN — consult briefings are tens of KB, past
  Windows' command-line length limit.
- cwd is the system temp dir: both CLIs auto-discover context files up the
  tree (CLAUDE.md / AGENTS.md), and this repo's would inject thousands of
  tokens of irrelevant coding instructions into a game consult.
- CREATE_NO_WINDOW on Windows: the packaged app is windowed, and without
  it a console flashes up for the whole run.
- Claude: `--tools ""` (pure reasoning, never wanders the filesystem),
  `--strict-mcp-config`, `--no-session-persistence`; NEVER `--bare` — it
  restricts auth to ANTHROPIC_API_KEY and breaks subscription OAuth.
- Codex: `-s read-only --skip-git-repo-check --ephemeral`; it has no
  system-prompt flag, so `system` is prepended to the prompt; the answer
  is read from `-o <file>` (stdout carries the whole transcript).
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from backend.config import settings

logger = logging.getLogger(__name__)

PROVIDERS = ("claude_cli", "codex_cli")

LABELS = {"claude_cli": "Claude Code CLI", "codex_cli": "Codex CLI"}

# Effort values OFFERED in the pickers (claude 2.1.220; codex per a live
# API rejection naming its supported set — "minimal" was dropped after the
# current codex default model 400'd on it mid-play). Codex support varies
# BY MODEL, so validation accepts the superset below rather than blocking
# an .env value an older model might still want.
EFFORTS = {
    "claude_cli": ("low", "medium", "high", "xhigh", "max"),
    "codex_cli": ("none", "low", "medium", "high", "xhigh", "max"),
}
EFFORTS_ACCEPTED = {
    "claude_cli": set(EFFORTS["claude_cli"]),
    "codex_cli": set(EFFORTS["codex_cli"]) | {"minimal"},
}


def _setting_exe(provider: str) -> str:
    return settings.claude_cli if provider == "claude_cli" else settings.codex_cli


def resolve(provider: str) -> Optional[str]:
    """Absolute path to the CLI, or None. shutil.which covers PATH and
    adds .exe on Windows; an explicit path in the setting is honored."""
    raw = _setting_exe(provider)
    exe = shutil.which(raw)
    if exe:
        return exe
    return raw if os.path.isfile(raw) else None


def available() -> dict:
    return {p: resolve(p) is not None for p in PROVIDERS}


def model_effort(provider: str) -> Tuple[str, str]:
    if provider == "claude_cli":
        return settings.claude_cli_model, settings.claude_cli_effort
    return settings.codex_cli_model, settings.codex_cli_effort


def _popen_kw() -> dict:
    kw: dict = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def version(provider: str) -> Optional[str]:
    """`<cli> --version` — the cheap "is it installed and answering" probe.
    Never a paid request for either CLI."""
    exe = resolve(provider)
    if not exe:
        return None
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15,
                           cwd=tempfile.gettempdir(), **_popen_kw())
        out = (p.stdout or "").strip().splitlines()
        return out[0][:80] if p.returncode == 0 and out else None
    except Exception:
        return None


def _run_claude(exe: str, prompt: str, system: Optional[str],
                model: str, effort: str) -> Tuple[str, dict]:
    cmd = [
        exe, "-p",
        "--effort", effort,
        "--output-format", "json",
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if model:  # blank = whatever the CLI's own default model is
        cmd += ["--model", model]
    if system:
        cmd += ["--system-prompt", system]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=settings.cli_timeout_s,
                          cwd=tempfile.gettempdir(), **_popen_kw())
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"claude CLI exited {proc.returncode}: "
                           f"{tail or 'no output'}")
    raw = (proc.stdout or "").strip()
    # --output-format json prints one envelope object; the answer is its
    # "result" string. Treat raw stdout as the answer if the shape ever
    # changes, so a format change degrades instead of breaking.
    text, meta = raw, {}
    try:
        envelope = json.loads(raw)
        if isinstance(envelope, dict) and "result" in envelope:
            if envelope.get("is_error"):
                raise RuntimeError("claude CLI reported an error: "
                                   f"{str(envelope.get('result'))[:400]}")
            text = str(envelope.get("result") or "")
            meta = {"cost_usd": envelope.get("total_cost_usd"),
                    "duration_ms": envelope.get("duration_ms")}
    except json.JSONDecodeError:
        pass
    return text, meta


def _run_codex(exe: str, prompt: str, system: Optional[str],
               model: str, effort: str) -> Tuple[str, dict]:
    if system:
        # codex exec has no system-prompt flag
        prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\n{prompt}"
    fd, outfile = tempfile.mkstemp(prefix="warcounsel-codex-", suffix=".txt")
    os.close(fd)
    try:
        cmd = [
            exe, "exec", "-",              # "-" = read the prompt from stdin
            "-s", "read-only",
            "--skip-git-repo-check",       # cwd is the temp dir, not a repo
            "--ephemeral",                 # no session files on disk
            "--color", "never",
            "-o", outfile,                 # final message, without transcript
        ]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=settings.cli_timeout_s,
                              cwd=tempfile.gettempdir(), **_popen_kw())
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            # codex prints API failures as ERROR: {json}; the human part is
            # the "message" field — a raw last-N-chars tail starts mid-JSON
            # and once reached the UI as the 4 characters 'aram'
            import re as _re
            m = _re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', err)
            detail = (m.group(1).replace('\\"', '"')
                      if m else (err[-400:] or "no output"))
            raise RuntimeError(f"codex CLI exited {proc.returncode}: {detail}")
        try:
            text = open(outfile, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        if not text.strip():
            # -o missing/empty: fall back to stdout, which includes the
            # transcript — the JSON extractor downstream copes with prose
            text = proc.stdout or ""
        if not text.strip():
            raise RuntimeError("codex CLI produced no output "
                               f"({(proc.stderr or '').strip()[-200:] or 'empty stderr'})")
        return text, {}
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass


def run(provider: str, prompt: str, system: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None) -> Tuple[str, dict]:
    """One blocking CLI run -> (answer text, meta). Raises RuntimeError with
    a user-showable message on any failure; callers decide fallback.

    `model`/`effort` override the .env defaults (llm_runtime persists
    per-provider choices made in the UI); "default" is the codex display
    placeholder for "no -m flag — whatever codex itself is configured
    with"."""
    exe = resolve(provider)
    if not exe:
        raise RuntimeError(
            f"{LABELS.get(provider, provider)} not found "
            f"({_setting_exe(provider)!r}) — install it and log in, or set "
            "its path in .env.")
    default_model, default_effort = model_effort(provider)
    model = default_model if model is None else model
    effort = default_effort if effort is None else effort
    if model == "default":
        model = ""
    try:
        if provider == "claude_cli":
            return _run_claude(exe, prompt, system, model, effort)
        return _run_codex(exe, prompt, system, model, effort)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{LABELS.get(provider, provider)} timed out after "
            f"{settings.cli_timeout_s}s ({model or 'default model'}, "
            f"effort {effort}).") from None


async def arun(provider: str, prompt: str, system: Optional[str] = None,
               model: Optional[str] = None,
               effort: Optional[str] = None) -> Tuple[str, dict]:
    return await asyncio.to_thread(run, provider, prompt, system, model,
                                   effort)
