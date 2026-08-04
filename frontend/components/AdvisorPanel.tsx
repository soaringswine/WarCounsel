"use client";

import { Fragment, memo, useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiSend } from "@/lib/api";
import { ItemHover } from "./ItemHover";
import type { Advice, ChatMessage, DoubleCheck, ExportsStatus, GearAdvice, HuntingData, LlmInfo, OwnedAAsInfo, Snapshot, SpellbookInfo } from "@/lib/types";

/** What the counsel optimises for. Mirrors the backend's allowed values. */
const PLAYSTYLES = [
  "solo_dps", "group_dps", "tank", "healer", "support", "pet_focused", "balanced",
];

const CLASSES = [
  "Bard", "Beastlord", "Berserker", "Cleric", "Druid", "Enchanter",
  "Magician", "Monk", "Necromancer", "Paladin", "Ranger", "Rogue",
  "Shadow Knight", "Shaman", "Warrior", "Wizard",
];

const TRIO_LABELS = ["Primary", "Secondary", "Tertiary"] as const;

/** Providers whose model id is a plain text field next to the selector.
 *  The CLI providers get their own model+effort pickers instead. */
const MODEL_EDIT_PROVIDERS = ["openai", "custom"];

const MODEL_FIELD_LABEL: Record<string, string> = {
  openai: "OpenAI model",
  custom: "Custom model",
};

const MODEL_FIELD_HINT: Record<string, string> = {
  openai: "o3",
  custom: "model id",
};

const CLI_PROVIDERS = ["claude_cli", "codex_cli"] as const;
type CliProvider = (typeof CLI_PROVIDERS)[number];

const isCli = (p: string | undefined | null): p is CliProvider =>
  p === "claude_cli" || p === "codex_cli";

/** Datalist SUGGESTIONS only — free text is always allowed, since both
 *  vendors ship new model ids faster than this list can chase them. This
 *  list WILL go stale (the codex half sat two generations behind until
 *  2026-07-31); the model actually available is whatever the installed CLI
 *  offers, so check there before trusting an entry here. Ordered strongest
 *  to fastest within each vendor. */
const CLI_MODEL_SUGGESTIONS: Record<CliProvider, string[]> = {
  claude_cli: ["claude-fable-5", "claude-opus-5", "claude-sonnet-5",
               "claude-haiku-4-5"],
  // gpt-5.6: sol "latest frontier", terra "balanced ... everyday work",
  // luna "fast and affordable" (codex's own descriptions). sol/terra also
  // accept an `ultra` effort that the effort <select> does not offer; luna
  // tops out at max.
  codex_cli: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
};

const CLI_SHORT: Record<CliProvider, string> = {
  claude_cli: "claude",
  codex_cli: "codex",
};

/** FastAPI wraps error reasons as {"detail": "..."} — unwrap for display,
 *  honoring escaped quotes (a naive [^"]+ once cut an API error down to
 *  four characters at the first \" inside it). */
function unwrapApiError(e: unknown): string {
  const msg = e instanceof Error ? e.message : String(e);
  const m = msg.match(/"detail"\s*:\s*"((?:[^"\\]|\\.)*)/);
  return m ? m[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\") : msg;
}

/** Minimal markdown for chat replies: **bold** and line breaks, never
 *  raw HTML — same treatment the retired companion panel used. */
function chatText(text: string) {
  return text.split("\n").map((line, li) => (
    <Fragment key={li}>
      {li > 0 && <br />}
      {line.split(/(\*\*[^*]+\*\*)/g).map((part, pi) =>
        part.startsWith("**") && part.endsWith("**") ? (
          <strong key={pi}>{part.slice(2, -2)}</strong>
        ) : (
          <Fragment key={pi}>{part}</Fragment>
        ),
      )}
    </Fragment>
  ));
}

const AGREEMENT_TEXT: Record<string, string> = {
  agree: "agrees with the 2nd check",
  partial: "partly agrees with the 2nd check",
  disagree: "disagrees with the 2nd check",
};

/** Per-item cross-reference of the two check reviews, deterministic from
 *  their issue lists: who flagged what, and where they split. */
function issueMatrix(second?: DoubleCheck, third?: DoubleCheck) {
  if (!second || !third) return [];
  const rows = new Map<string, { item: string; second?: string; third?: string }>();
  const add = (slot: "second" | "third", dc: DoubleCheck) => {
    for (const iss of dc.issues) {
      const key = (iss.item || iss.problem).toLowerCase().trim();
      const row = rows.get(key) ?? { item: iss.item || `${iss.problem.slice(0, 60)}…` };
      row[slot] = iss.severity;
      rows.set(key, row);
    }
  };
  add("second", second);
  add("third", third);
  return Array.from(rows.values()).map((r) => ({
    ...r,
    stance:
      r.second && r.third
        ? "both checks flag this"
        : r.second
          ? "only the 2nd check raised this"
          : "only the 3rd check raised this",
    split: !(r.second && r.third),
  }));
}

const HG_TICKS = [10, 20, 30, 40, 50, 60];
const HG_MIN = 1;
const HG_MAX = 65;
const hgX = (l: number) => ((Math.min(l, HG_MAX) - HG_MIN) / (HG_MAX - HG_MIN)) * 100;

/** Merge 5-level marks (each mark m = content in [m, m+5)) into bar spans. */
function hgSegments(levels: number[]): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (const m of [...levels].sort((a, b) => a - b)) {
    const last = out[out.length - 1];
    if (last && m <= last[1]) last[1] = m + 5;
    else out.push([m, m + 5]);
  }
  return out;
}

/** eqlwiki page for a zone. Titles are the plain zone name with underscores
 *  ("Lower Guk" -> Lower_Guk), which is what the hunting table already
 *  carries, so no mapping table is needed. */
/** The wiki title for a zone. Advisor picks arrive carrying their level
 *  band ("Upper Guk (5-30)"), which is worth showing but is not part of the
 *  page name -- linking it verbatim 404s on every pick. Strip one trailing
 *  parenthetical; the label itself keeps it. */
function zoneTitle(zone: string) {
  return zone.replace(/\s*\([^()]*\)\s*$/, "").trim();
}

function wikiZoneUrl(zone: string) {
  const t = zoneTitle(zone);
  return `https://eqlwiki.com/index.php/${encodeURIComponent(t.replace(/ /g, "_"))}`;
}

/** Wiki SEARCH rather than a page. For zone names we have not verified,
 *  a search always resolves; a direct page link would dead-end whenever
 *  the name is slightly off. */
function wikiSearchUrl(term: string) {
  return `https://eqlwiki.com/index.php?search=${encodeURIComponent(term)}`;
}

/** A zone name that links out to the wiki.
 *  `verified` distinguishes the two sources we have: hunting picks survive
 *  _gate_locations() against the community table, so their names are real
 *  and get a page link. Gear-farm zones are raw model output with no zone
 *  gate, so they get a search link instead of a link we cannot stand
 *  behind. */
function ZoneLink({ zone, verified = true }: { zone: string; verified?: boolean }) {
  return (
    <a
      className="zone-link"
      href={verified ? wikiZoneUrl(zone) : wikiSearchUrl(zoneTitle(zone))}
      target="_blank"
      rel="noopener noreferrer"
      title={
        verified
          ? `${zoneTitle(zone)} — open on eqlwiki`
          : `Search eqlwiki for ${zoneTitle(zone)}`
      }
    >
      {zone}
    </a>
  );
}

/** Gantt of hunting-zone level bands (community Recommended-Levels table):
 *  the advisor's picks highlighted, best remaining at-level zones as context,
 *  a green line at the character's level. Zone names link out to the wiki.
 *  Note the container is role="group", NOT role="img": an img role makes its
 *  whole subtree presentational, which would hide those links from assistive
 *  tech. */
function HuntChart({ data, picked }: { data: HuntingData; picked: string[] }) {
  const lv = data.level ?? 0;
  const isPicked = (z: string) => picked.some((p) => p.startsWith(z));
  const rows = [
    ...data.zones.filter((z) => isPicked(z.zone)),
    ...data.zones.filter((z) => !isPicked(z.zone)),
  ]
    .slice(0, 8)
    .sort((a, b) => Math.min(...a.levels) - Math.min(...b.levels));
  return (
    <div className="hunt-gantt" role="group" aria-label={`Level bands of ${rows.length} hunting zones around level ${lv}`}>
      <div className="hg-row hg-head" aria-hidden="true">
        <span className="hg-label" />
        <div className="hg-track">
          {HG_TICKS.map((t) => (
            <span key={t} className="hg-tick" style={{ left: `${hgX(t)}%` }}>{t}</span>
          ))}
          <span className="hg-now-label" style={{ left: `${hgX(lv)}%` }}>you · {lv}</span>
        </div>
      </div>
      {rows.map((z) => (
        <div key={z.zone} className={`hg-row${isPicked(z.zone) ? " hg-picked" : ""}`}>
          <a
            className="hg-label"
            href={wikiZoneUrl(z.zone)}
            target="_blank"
            rel="noopener noreferrer"
            title={`${z.zone} (levels ${z.band}) — open on eqlwiki`}
          >
            {z.zone}
          </a>
          <div className="hg-track">
            {HG_TICKS.map((t) => (
              <i key={t} className="hg-grid" style={{ left: `${hgX(t)}%` }} />
            ))}
            {hgSegments(z.levels).map(([a, b]) => (
              <i key={a} className="hg-seg" style={{ left: `${hgX(a)}%`, width: `${hgX(b) - hgX(a)}%` }} />
            ))}
            <i className="hg-now" style={{ left: `${hgX(lv)}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** One check review, rendered identically under the counsel and the gear
 *  table. Carries the model/effort/duration meta, the structured stance
 *  toward the earlier check when present, and the shape-enforced issues. */
function ReviewBlock({ dc, label }: { dc: DoubleCheck; label: string }) {
  return (
    <div className="adv-dc-review">
      <div className="adv-dc-bar">
        <span className="adv-dc-verdict" data-v={dc.verdict}>
          {dc.verdict.replace("_", " ")}
        </span>
        <span className="adv-dc-title">
          {label} — {dc.model}
          {dc.effort ? ` · ${dc.effort} effort` : ""} · {dc.duration_s}s
          {dc.cost_usd ? ` · $${dc.cost_usd.toFixed(2)}` : ""}
        </span>
        {dc.prior_agreement && (
          <span className="adv-dc-agree" data-a={dc.prior_agreement}>
            {AGREEMENT_TEXT[dc.prior_agreement]}
          </span>
        )}
      </div>
      {dc.prior_notes && <p className="adv-dc-endorse">{dc.prior_notes}</p>}
      {dc.summary && <p className="adv-dc-summary">{dc.summary}</p>}
      {dc.issues.length > 0 && (
        <ul className="adv-list adv-dc-issues">
          {dc.issues.map((iss, i) => (
            <li key={i} data-dim={iss.unmatched || undefined}>
              <span className="adv-dc-sev" data-sev={iss.severity}>
                {iss.severity}
              </span>{" "}
              <span className="adv-cls">[{iss.section}]</span>{" "}
              <strong>{iss.item}</strong>
              {iss.unmatched && (
                <span className="adv-cls">
                  {" "}(names something not in the counsel above)
                </span>
              )}
              <br />
              {iss.problem}
              {iss.fix && (
                <>
                  <br />
                  <span className="adv-dc-fix">fix: {iss.fix}</span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {dc.endorsements.length > 0 && (
        <p className="adv-dc-endorse">
          Confirmed right: {dc.endorsements.join(" · ")}
        </p>
      )}
    </div>
  );
}

/** Class-trio counsel: spells to learn, AA spending order, upcoming unlocks,
 *  and picks for the current zone. The backend grounds the counsel in EQL
 *  wiki data (via MCP) and generates it with the configured LLM, caching it
 *  until the character context changes. */

/** Correct an item's stats from what the player can actually read in game.
 *
 * eqlwiki item pages are community-written: some are stubs with no stat
 * block, some carry classic-era numbers for an item EQL rebalanced. Every
 * gate we have -- owned, fits the slot, class-usable -- passes a wrong
 * number happily, so the person holding the item is the only check left.
 * Slot and Class are NOT asked for: the wiki gets those right and they are
 * what gate the item, so a typo there would do real damage. */
function StatFix({ name, onSaved }: { name: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) {
    return (
      <button
        type="button"
        className="adv-statfix-open"
        title={`Correct the stats recorded for ${name}`}
        onClick={() => setOpen(true)}
      >
        stats?
      </button>
    );
  }
  const save = () => {
    const v = draft.trim();
    if (!v || busy) return;
    setBusy(true);
    apiSend("/api/item-stats", { name, stats: v, rank: 0 })
      .then(() => {
        setOpen(false);
        setDraft("");
        onSaved();
      })
      .finally(() => setBusy(false));
  };
  return (
    <span className="adv-statfix">
      <input
        autoFocus
        value={draft}
        placeholder="AC: 6; STR: +5"
        aria-label={`Stats for ${name}`}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") save();
          if (e.key === "Escape") setOpen(false);
        }}
      />
      <button type="button" onClick={save} disabled={busy}>
        {busy ? "…" : "Save"}
      </button>
      <button type="button" onClick={() => setOpen(false)}>
        Cancel
      </button>
    </span>
  );
}

interface MeleeGroup {
  verbs: string[];
  fights: number;
  dps: number;
  avg_hit: number;
  hits_per_min: number;
  level_lo: number | null;
  level_hi: number | null;
}
interface MeleeCompare {
  groups: MeleeGroup[];
  dual_wield_ceiling?: number | null;
  ambidexterity?: boolean;
  ceiling_with_aa?: { as_points: number; as_relative: number } | null;
  level?: number | null;
  overlap: { level_lo: number; level_hi: number; groups: MeleeGroup[] } | null;
  note?: string;
}

export const AdvisorPanel = memo(function AdvisorPanel({
  snap,
  onSnapChange,
}: {
  snap: Snapshot | null;
  onSnapChange: (s: Snapshot) => void;
}) {
  const [advice, setAdvice] = useState<Advice | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dcBusy, setDcBusy] = useState<"second" | "third" | null>(null);
  const [dcError, setDcError] = useState<string | null>(null);
  const [gearDcBusy, setGearDcBusy] = useState<"second" | "third" | null>(null);
  const [gearDcError, setGearDcError] = useState<string | null>(null);
  const [revBusy, setRevBusy] = useState(false);
  const [gearRevBusy, setGearRevBusy] = useState(false);
  const [chatMsgs, setChatMsgs] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const chatEnd = useRef<HTMLDivElement | null>(null);
  const chatInput = useRef<HTMLInputElement | null>(null);
  const [dcDebug, setDcDebug] = useState(false);
  const [cliDraft, setCliDraft] = useState<
    Record<string, { model: string; effort: string }>
  >({});
  const [aaDraft, setAaDraft] = useState("");
  const [slotsDraft, setSlotsDraft] = useState("");
  const [petSlotsDraft, setPetSlotsDraft] = useState("");
  const [petClassDraft, setPetClassDraft] = useState("");
  const [book, setBook] = useState<SpellbookInfo | null>(null);
  const [ownedAAs, setOwnedAAs] = useState<OwnedAAsInfo | null>(null);
  const [exports, setExports] = useState<ExportsStatus | null>(null);
  const [checking, setChecking] = useState(false);
  const [scanResult, setScanResult] = useState<{ text: string; ok: boolean } | null>(null);
  const [hunting, setHunting] = useState<HuntingData | null>(null);
  const [pickSel, setPickSel] = useState<Record<string, boolean>>({});
  const [llm, setLlm] = useState<LlmInfo | null>(null);
  const [llmModelDraft, setLlmModelDraft] = useState("");
  const scanTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flashScanResult = (text: string, ok: boolean, sticky = false) => {
    setScanResult({ text, ok });
    if (scanTimer.current) clearTimeout(scanTimer.current);
    if (!sticky) scanTimer.current = setTimeout(() => setScanResult(null), 8000);
  };
  useEffect(() => () => {
    if (scanTimer.current) clearTimeout(scanTimer.current);
  }, []);

  const trio = (snap?.class_str ?? "").split("/").map((s) => s.trim());

  useEffect(() => {
    apiGet<HuntingData>("/api/hunting")
      .then(setHunting)
      .catch(() => setHunting(null));
  }, [snap?.level]);

  useEffect(() => {
    apiGet<LlmInfo>("/api/llm")
      .then((info) => {
        setLlm(info);
        setLlmModelDraft(
          MODEL_EDIT_PROVIDERS.includes(info.active.provider)
            ? info.active.model
            : "",
        );
      })
      .catch(() => setLlm(null));
  }, []);

  const switchLlm = async (provider: string, model?: string) => {
    try {
      const r = await apiSend<LlmInfo>("/api/llm", { provider, model }, "POST");
      setLlm((prev) => ({ ...(prev ?? r), ...r }));
      if (MODEL_EDIT_PROVIDERS.includes(provider)) setLlmModelDraft(r.active.model);
    } catch {
      /* backend offline */
    }
  };

  // seed the CLI model/effort drafts once per provider — never clobber a
  // draft mid-typing when the llm info object refreshes
  useEffect(() => {
    if (!llm?.cli) return;
    const cli = llm.cli;
    setCliDraft((prev) => {
      const next = { ...prev };
      for (const [p, c] of Object.entries(cli)) {
        if (!next[p]) {
          next[p] = { model: c.model === "default" ? "" : c.model, effort: c.effort };
        }
      }
      return next;
    });
  }, [llm?.cli]);

  // chain-detail (debug) toggle survives reloads, like the layout prefs
  useEffect(() => {
    setDcDebug(localStorage.getItem("adv-dc-debug") === "1");
  }, []);
  const toggleDcDebug = () =>
    setDcDebug((v) => {
      localStorage.setItem("adv-dc-debug", v ? "0" : "1");
      return !v;
    });

  // Persist a CLI provider's model/effort WITHOUT switching the primary —
  // a check slot's picker must never steal the Counsel selector.
  const saveCliPrefs = async (p: CliProvider, patch: { model?: string; effort?: string }) => {
    try {
      const r = await apiSend<{ provider: string; model: string; effort: string }>(
        "/api/llm/cli", { provider: p, ...patch });
      setCliDraft((d) => ({
        ...d,
        [p]: { model: r.model === "default" ? "" : r.model, effort: r.effort },
      }));
      setLlm((prev) => {
        if (!prev) return prev;
        const label = `${p === "claude_cli" ? "Claude Code CLI" : "Codex CLI"} — ${r.model}`;
        return {
          ...prev,
          cli: prev.cli
            ? { ...prev.cli, [p]: { ...prev.cli[p], model: r.model, effort: r.effort } }
            : prev.cli,
          options: prev.options?.map((o) =>
            o.provider === p ? { ...o, model: r.model, label } : o),
          active: prev.active.provider === p
            ? { ...prev.active, model: r.model }
            : prev.active,
        };
      });
    } catch {
      /* backend offline */
    }
  };

  useEffect(() => {
    setAaDraft(snap?.aa_available == null ? "" : String(snap.aa_available));
  }, [snap?.aa_available]);
  useEffect(() => {
    setSlotsDraft(snap?.spell_slots == null ? "" : String(snap.spell_slots));
  }, [snap?.spell_slots]);
  useEffect(() => {
    setPetSlotsDraft(snap?.pet_slots == null ? "" : String(snap.pet_slots));
  }, [snap?.pet_slots]);
  useEffect(() => {
    setPetClassDraft(snap?.pet_classes ?? "");
  }, [snap?.pet_classes]);

  const patch = async (body: Record<string, unknown>) => {
    try {
      onSnapChange(await apiSend<Snapshot>("/api/character", body, "PATCH"));
    } catch {
      /* backend offline */
    }
  };

  /** Model (datalist: suggestions + free text) and effort pickers for one
   *  CLI provider. Per-PROVIDER prefs: the same claude_cli settings apply
   *  wherever it is used (primary or either check slot), and multiple
   *  pickers for one provider stay in sync through saveCliPrefs. */
  const cliPicker = (p: CliProvider) => {
    const info = llm?.cli?.[p];
    if (!info) return null;
    const d = cliDraft[p] ?? {
      model: info.model === "default" ? "" : info.model,
      effort: info.effort,
    };
    return (
      <span className="adv-cli-prefs">
        <input
          list={`cli-models-${p}`}
          value={d.model}
          placeholder={p === "codex_cli" ? "codex default" : "model"}
          spellCheck={false}
          onChange={(e) =>
            setCliDraft((x) => ({ ...x, [p]: { ...d, model: e.target.value } }))
          }
          onBlur={() => saveCliPrefs(p, { model: d.model.trim() })}
          onKeyDown={(e) => e.key === "Enter" && saveCliPrefs(p, { model: d.model.trim() })}
          aria-label={`${CLI_SHORT[p]} CLI model`}
          title="Model — pick a suggestion or type any id this CLI accepts; blank = the CLI's own default"
        />
        <select
          value={d.effort}
          onChange={(e) => saveCliPrefs(p, { effort: e.target.value })}
          aria-label={`${CLI_SHORT[p]} CLI reasoning effort`}
          title="Reasoning effort — how long the model thinks before answering"
        >
          {info.efforts.map((ef) => (
            <option key={ef} value={ef}>{ef}</option>
          ))}
        </select>
      </span>
    );
  };

  const setTrioAt = (i: number, cls: string) => {
    const next = [trio[0] ?? "", trio[1] ?? "", trio[2] ?? ""];
    next[i] = cls;
    patch({ class_str: next.filter(Boolean).join("/") });
  };

  const numberPatch = (draft: string, field: "aa_available" | "spell_slots" | "pet_slots") => {
    if (draft === "") return;
    const n = Number(draft);
    if (Number.isFinite(n) && n >= 0) patch({ [field]: Math.floor(n) });
  };

  const [rescanning, setRescanning] = useState(false);
  const [gear, setGear] = useState<GearAdvice | null>(null);
  const [melee, setMelee] = useState<MeleeCompare | null>(null);
  const [trioOpen, setTrioOpen] = useState(false);
  const [gearLoading, setGearLoading] = useState(false);
  // Slots with nothing in them, so an unverifiable owned item can be shown
  // next to the gap it MIGHT fill. The app cannot match them up itself --
  // no wiki page means no Slot line -- so it says that instead of guessing.
  const emptySlots = (gear?.slots ?? [])
    .filter((s) => !s.current)
    .map((s) => s.slot);

  useEffect(() => {
    // restore the last gear counsel if the backend still has it (no LLM run)
    apiGet<GearAdvice & { cached?: boolean }>("/api/gear?cached=1")
      .then((r) => {
        if (r && (r as { source?: string }).source) setGear(r);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const writeSpellSet = async (source: "loadout" | "prebuffs") => {
    const names =
      source === "loadout" && advice
        ? [...advice.must_have, ...advice.should_have, ...advice.nice_to_have]
            .filter((s) => pickSel[s.name])
            .map((s) => s.name)
        : undefined;
    try {
      const r = await apiSend<{ name: string; count: number; memspellset: string; skipped: string[]; note: string }>(
        "/api/spellsets/generate",
        { source, names },
      );
      flashScanResult(
        `set "${r.name}" written (${r.count} spells) — in game: ${r.memspellset}` +
          (r.skipped.length ? ` · no id for: ${r.skipped.join(", ")}` : "") +
          ` · ${r.note}`,
        true,
        true,
      );
    } catch (e) {
      flashScanResult(`spell-set write failed: ${e instanceof Error ? e.message : "backend error"}`, false, true);
    }
  };

  useEffect(() => {
    if (!advice) return;
    const sel: Record<string, boolean> = {};
    advice.must_have.forEach((s) => { sel[s.name] = true; });
    advice.should_have.forEach((s) => { sel[s.name] = true; });
    advice.nice_to_have.forEach((s) => { sel[s.name] = sel[s.name] ?? false; });
    setPickSel(sel);
  }, [advice]);

  const loadMelee = async () => {
    try {
      setMelee(await apiGet<MeleeCompare>("/api/melee-compare"));
    } catch {
      setMelee(null);
    }
  };

  const consultGear = async (refresh: boolean) => {
    setGearLoading(true);
    setGearDcError(null); // fresh table — an old check error is moot
    try {
      setGear(await apiGet<GearAdvice>(`/api/gear${refresh ? "?refresh=1" : ""}`));
    } catch {
      /* backend offline */
    }
    setGearLoading(false);
  };
  const rescanAAs = async () => {
    setRescanning(true);
    try {
      const res = await apiSend<{ found: boolean; distinct?: number; synced?: string }>(
        "/api/aas/rescan", {});
      const aas = await apiGet<OwnedAAsInfo>("/api/aas");
      setOwnedAAs(aas);
      if (res.found) {
        flashScanResult(
          `log scan done — ${res.distinct} AAs (listed ${res.synced ? new Date(res.synced).toLocaleTimeString() : "?"})`,
          true);
      } else {
        flashScanResult("log scan done — no /alternateadv output found", false);
      }
    } catch {
      flashScanResult("log scan failed — is the backend running?", false);
    }
    setRescanning(false);
  };

  const consult = useCallback(async (refresh: boolean) => {
    setLoading(true);
    setError(null);
    setDcError(null); // fresh counsel — an old double-check error is moot
    setScanResult(null); // sticky spell-set notes live until the next consult
    try {
      setAdvice(await apiGet<Advice>(`/api/advisor${refresh ? "?refresh=1" : ""}`));
    } catch {
      setError("The advisor is unreachable — is the backend running?");
    } finally {
      setLoading(false);
    }
  }, []);

  // Run one check slot: its provider (any — a coding-agent CLI or an
  // API/local model) reviews the displayed counsel against the advisor's
  // exact briefing. The 3rd check also sees the 2nd's review. Button-press
  // only, like the consults; results ride the advice cache.
  const doubleCheck = async (slot: "second" | "third") => {
    setDcBusy(slot);
    setDcError(null);
    try {
      const r = await apiSend<DoubleCheck>("/api/advisor/doublecheck", { slot });
      setAdvice((a) =>
        a ? { ...a, doublechecks: { ...a.doublechecks, [slot]: r } } : a,
      );
    } catch (e) {
      setDcError(unwrapApiError(e));
    } finally {
      setDcBusy(null);
    }
  };

  // chat history survives restarts (same per-character table the old
  // companion tab used); load it once so a reload keeps the thread
  useEffect(() => {
    apiGet<{ messages: ChatMessage[] }>("/api/chat/history?limit=20")
      .then((r) => setChatMsgs(r.messages ?? []))
      .catch(() => undefined);
  }, []);

  const askCounsel = async (e: React.FormEvent) => {
    e.preventDefault();
    const q = chatDraft.trim();
    if (!q || chatBusy) return;
    setChatBusy(true);
    setChatError(null);
    setChatDraft("");
    setChatMsgs((m) => [...m, { role: "user", content: q }]);
    try {
      const r = await apiSend<{ reply: string; sources?: string[] }>(
        "/api/advisor/chat", { message: q });
      setChatMsgs((m) => [
        ...m,
        { role: "assistant", content: r.reply, sources: r.sources },
      ]);
    } catch (err) {
      setChatError(unwrapApiError(err));
    } finally {
      setChatBusy(false);
    }
  };

  // One chat seat answers for BOTH advisors — it holds the counsel and the
  // gear table plus both of their briefings — so the Equipment section
  // routes here instead of growing a second box that would split the
  // thread and send two half-informed prompts.
  const askAboutGear = () => {
    chatInput.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    chatInput.current?.focus();
  };

  // The thread is per-character and persists across reloads and relaunches
  // on purpose — that is what lets "why that pick?" span sessions — so
  // ending one has to be an explicit act. Clears the server's copy too;
  // wiping only the panel would resurrect the thread on the next load AND
  // keep feeding it to the model as history.
  const clearChat = async () => {
    try {
      await apiSend<{ deleted: number }>("/api/chat/history", {}, "DELETE");
      setChatMsgs([]);
      setChatError(null);
    } catch (err) {
      setChatError(unwrapApiError(err));
    }
  };

  // pin to the newest message, but only while the thread is in play
  useEffect(() => {
    if (chatMsgs.length) chatEnd.current?.scrollIntoView({ block: "nearest" });
  }, [chatMsgs.length, chatBusy]);

  // Close the loop: feed the stored check findings back through the
  // counsel model; the revision re-passes every deterministic gate
  // server-side before it replaces the display. Failures keep the
  // original counsel untouched.
  const reviseCounsel = async () => {
    setRevBusy(true);
    setDcError(null);
    try {
      const r = await apiSend<Advice>("/api/advisor/revise", {});
      setAdvice(r);
    } catch (e) {
      setDcError(unwrapApiError(e));
    } finally {
      setRevBusy(false);
    }
  };

  const reviseGear = async () => {
    setGearRevBusy(true);
    setGearDcError(null);
    try {
      const r = await apiSend<GearAdvice>("/api/gear/revise", {});
      setGear(r);
    } catch (e) {
      setGearDcError(unwrapApiError(e));
    } finally {
      setGearRevBusy(false);
    }
  };

  // Same check slots, gear-shaped rubric: the reviewer sees the whole
  // slot table at once, which is exactly the joint-assignment view the
  // per-row consult lacks about its own output.
  const doubleCheckGear = async (slot: "second" | "third") => {
    setGearDcBusy(slot);
    setGearDcError(null);
    try {
      const r = await apiSend<DoubleCheck>("/api/gear/doublecheck", { slot });
      setGear((g) =>
        g ? { ...g, doublechecks: { ...g.doublechecks, [slot]: r } } : g,
      );
    } catch (e) {
      setGearDcError(unwrapApiError(e));
    } finally {
      setGearDcBusy(null);
    }
  };

  // Assign a provider to a check slot; "none" disables it. Existing
  // reviews stay — each records the provider that produced it.
  const setCheckSlot = async (slot: "second" | "third", provider: string) => {
    try {
      const r = await apiSend<{ second: string; third: string }>(
        "/api/llm/checks", { [slot]: provider });
      setLlm((prev) => (prev ? { ...prev, checks: r } : prev));
    } catch {
      /* backend offline */
    }
  };

  useEffect(() => {
    // restore the last counsel if the backend still has it — never trigger
    // an LLM run without the Consult button
    apiGet<Advice & { cached?: boolean }>("/api/advisor?cached=1")
      .then((r) => {
        if (r && (r as { source?: string }).source) setAdvice(r);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // owned-state sync chips (refresh alongside every consult)
  useEffect(() => {
    apiGet<SpellbookInfo>("/api/spellbook").then(setBook).catch(() => {});
    apiGet<OwnedAAsInfo>("/api/aas").then(setOwnedAAs).catch(() => {});
    apiGet<ExportsStatus>("/api/exports").then(setExports).catch(() => {});
  }, [advice]);

  // "check exports": fresh directory scan after the in-game /outputfile
  // macro; re-consult when anything actually changed.
  const checkExports = async () => {
    setChecking(true);
    try {
      const fresh = await apiSend<ExportsStatus>("/api/exports/refresh", {});
      const changed = Object.keys(fresh).filter(
        (k) => fresh[k]?.updated !== exports?.[k]?.updated,
      );
      const found = Object.keys(fresh).filter((k) => fresh[k]?.found);
      setExports(fresh);
      apiGet<SpellbookInfo>("/api/spellbook").then(setBook).catch(() => {});
      if (found.length === 0) {
        flashScanResult("scan done — no exports found; run the /outputfile macro first", false);
      } else if (changed.length > 0) {
        flashScanResult(`scan done — updated: ${changed.join(", ")} — press Consult to refresh counsel`, true);
      } else {
        flashScanResult(`scan done — ${found.length} exports present, nothing new`, true);
      }
    } catch {
      flashScanResult("scan failed — is the backend running?", false);
    }
    setChecking(false);
  };

  // fresh owned-state landing while the tab is open (/alternateadv list in
  // the log, a new /outputfile spellbook) no longer auto-consults — the
  // sync chips show freshness and the user consults when ready.

  const aaBlock = (items: Advice["aa_now"], emptyText: string) =>
    items.length === 0 ? (
      <p className="adv-empty">{emptyText}</p>
    ) : (
      <ul className="adv-list">
        {items.map((a) => (
          <li key={a.name}>
            <strong>{a.name}</strong>
            {a.cost != null && <span className="adv-cost"> · {a.cost} pts</span>}
            <br />
            {a.reason}
          </li>
        ))}
      </ul>
    );

  return (
    <section className="panel advisor-panel">
      <div className="panel-title">
        Advisor
        {advice && (
          <span className="atlas-zone">
            {advice.grounding === "wiki" ? "wiki-grounded" : "from memory"}
          </span>
        )}
      </div>

      <div className="adv-controls">
        {/* The trio comes from /who and is backed up by class inference, so
            the pickers are no longer how anyone tells the app what they
            play -- they are for asking "what would this look like as a
            different trio", which is planning, not setup. Folded away so
            live data leads, but kept: overwriting the detected trio by
            hand is the only way to ask that question. */}
        <details className="adv-trio" open={trioOpen}>
          <summary
            onClick={(e) => {
              e.preventDefault();
              setTrioOpen((v) => !v);
            }}
            title="Try the counsel against a trio you are not currently playing"
          >
            {snap?.class_str || "no trio detected"}
            <span className="adv-cls"> — plan a different trio</span>
          </summary>
          <div className="adv-trio-fields">
            {TRIO_LABELS.map((label, i) => (
              <div className="adv-field" key={label}>
                <label htmlFor={`adv-cls-${i}`}>{label}</label>
                <select
                  id={`adv-cls-${i}`}
                  value={trio[i] ?? ""}
                  onChange={(e) => setTrioAt(i, e.target.value)}
                >
                  <option value="">—</option>
                  {CLASSES.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </details>
        <div className="adv-field">
          <label htmlFor="adv-aa">AA points</label>
          <input
            id="adv-aa"
            type="number"
            min={0}
            placeholder="?"
            value={aaDraft}
            onChange={(e) => setAaDraft(e.target.value)}
            onBlur={() => numberPatch(aaDraft, "aa_available")}
            onKeyDown={(e) => e.key === "Enter" && numberPatch(aaDraft, "aa_available")}
          />
        </div>
        <div className="adv-field">
          <label htmlFor="adv-slots">Spell slots</label>
          <input
            id="adv-slots"
            type="number"
            min={0}
            placeholder="?"
            value={slotsDraft}
            onChange={(e) => setSlotsDraft(e.target.value)}
            onBlur={() => numberPatch(slotsDraft, "spell_slots")}
            onKeyDown={(e) => e.key === "Enter" && numberPatch(slotsDraft, "spell_slots")}
          />
        </div>
        <div className="adv-field">
          <label htmlFor="adv-llm">Counsel model</label>
          <select
            id="adv-llm"
            value={llm?.active.provider ?? "lmstudio"}
            onChange={(e) => switchLlm(e.target.value)}
            title="Local = LM Studio on this machine; OpenAI = frontier model via your API key in .env"
          >
            {(llm?.options ?? [{ provider: "lmstudio", model: "", label: "Local" }]).map((o) => (
              <option key={o.provider} value={o.provider}>{o.label}</option>
            ))}
          </select>
        </div>
        {llm && MODEL_EDIT_PROVIDERS.includes(llm.active.provider) && (
          <div className="adv-field">
            <label htmlFor="adv-llm-model">
              {MODEL_FIELD_LABEL[llm.active.provider]}
            </label>
            <input
              id="adv-llm-model"
              type="text"
              value={llmModelDraft}
              placeholder={MODEL_FIELD_HINT[llm.active.provider]}
              onChange={(e) => setLlmModelDraft(e.target.value)}
              onBlur={() =>
                llmModelDraft.trim() && switchLlm(llm.active.provider, llmModelDraft.trim())
              }
              onKeyDown={(e) =>
                e.key === "Enter" && llmModelDraft.trim() && switchLlm(llm.active.provider, llmModelDraft.trim())
              }
            />
          </div>
        )}
        {llm && isCli(llm.active.provider) && (
          <div className="adv-field">
            <label>
              {CLI_SHORT[llm.active.provider]} CLI model · effort
            </label>
            {cliPicker(llm.active.provider)}
          </div>
        )}
        {llm?.active.provider === "openai" && !llm.openai_key_set && (
          <span className="adv-llm-warn" role="alert">
            No OPENAI_API_KEY in .env — consults will fall back to local data. Paste the key, restart the backend.
          </span>
        )}
        {/* Focus steers the whole consult, so it sits beside the button that
            asks for one. It used to live at the bottom of the session
            summary, which is the last place you would look for a control
            over advice. */}
        <label className="adv-focus" title="What the counsel should optimise for">
          Focus
          <select
            value={snap?.playstyle ?? "balanced"}
            onChange={(e) => patch({ playstyle: e.target.value })}
          >
            {PLAYSTYLES.map((p) => (
              <option key={p} value={p}>
                {p.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <button className="adv-consult" onClick={() => consult(true)} disabled={loading}>
          {loading ? "Consulting…" : "Consult"}
        </button>
        {CLI_PROVIDERS.map((p) => (
          <datalist key={p} id={`cli-models-${p}`}>
            {CLI_MODEL_SUGGESTIONS[p].map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        ))}
      </div>

      <div className="adv-sync">
        <span data-ok={!!book?.available}>
          {book?.available
            ? `spellbook: ${book.castable?.length ?? 0} spells · ${book.pre_launch ? "from BETA — re-export" : `${book.age_hours}h old`}`
            : "spellbook: none — type /outputfile spellbook in-game"}
        </span>
        <span data-ok={!!ownedAAs?.available}>
          {ownedAAs?.available
            ? `AAs: ${ownedAAs.aas.length} synced`
            : "AAs: unsynced — type /alternateadv list in-game"}
        </span>
        {exports && ["missingspells", "inventory", "achievements"].map((k) => (
          <span key={k} data-ok={!!exports[k]?.found}>
            {exports[k]?.found
              ? `${k === "missingspells" ? "missing" : k.slice(0, 4)}: ${exports[k]!.pre_launch ? "beta" : `${exports[k]!.age_hours}h`}`
              : `${k === "missingspells" ? "missing" : k.slice(0, 4)}: —`}
          </span>
        ))}
        <button
          type="button"
          className="adv-rescan"
          onClick={checkExports}
          disabled={checking}
          title="Scan the game folder for fresh /outputfile exports (run your macro first)"
        >
          {checking ? "checking…" : "check exports"}
        </button>
        <button
          type="button"
          className="adv-rescan"
          onClick={rescanAAs}
          disabled={rescanning}
          title="Deep-scan the whole log for the most recent /alternateadv list output"
        >
          {rescanning ? "scanning…" : "rescan log"}
        </button>
      </div>
      {scanResult && (
        <div className="adv-scan-result" data-ok={scanResult.ok} role="status">
          {scanResult.text}
        </div>
      )}

      <div className="advisor-scroll">
        {error && <p className="adv-empty">{error}</p>}
        {!advice && !error && (
          <p className="adv-empty">
            {loading
              ? "Consulting the archives… (wiki + local model, this can take a moment)"
              : "No counsel yet — press Consult."}
          </p>
        )}

        <div className="adv-chat">
          <div className="adv-chat-head">
            <span className="adv-sub">Ask the counsel</span>
            <span className="adv-chat-hint">
              {advice || gear
                ? "spells, AAs and equipment — it holds both consults, their briefings and the check findings, and it can read the wiki"
                : "spells, AAs and equipment — grounded in your owned spells, gear and class guides, and it can read the wiki"}
            </span>
            {chatMsgs.length > 0 && (
              <button
                type="button"
                className="adv-chat-clear"
                onClick={clearChat}
                disabled={chatBusy}
                title="Delete this character's saved thread. It persists across reloads and relaunches, and rides into every new answer as history, until you clear it."
              >
                clear thread
              </button>
            )}
          </div>
          {chatMsgs.length > 0 && (
            <div className="adv-chat-log">
              {chatMsgs.map((m, i) => (
                <div key={i} className="adv-chat-msg" data-role={m.role}>
                  <span className="adv-chat-who">
                    {m.role === "user" ? "you" : "counsel"}
                  </span>
                  <div>{chatText(m.content)}</div>
                  {(m.sources?.length ?? 0) > 0 && (
                    <div className="adv-chat-sources">
                      {m.sources!.map((s) => (
                        <span key={s} className="adv-chat-source">{s}</span>
                      ))}
                    </div>
                  )}
                </div>
              ))}
              <div ref={chatEnd} />
            </div>
          )}
          <form className="adv-chat-form" onSubmit={askCounsel}>
            <input
              ref={chatInput}
              type="text"
              value={chatDraft}
              onChange={(e) => setChatDraft(e.target.value)}
              placeholder={
                advice
                  ? "why that pick? where do I buy Shieldskin? is Leech worth it at 9?"
                  : "ask about your spells, gear, where to hunt, where to buy…"
              }
              aria-label="Ask the counsel"
              disabled={chatBusy}
            />
            <button type="submit" disabled={chatBusy || !chatDraft.trim()}>
              {chatBusy ? "thinking…" : "ask"}
            </button>
          </form>
          {chatError && <p className="adv-dc-error" role="alert">{chatError}</p>}
        </div>
        {advice && (
          <>
            <div className="adv-counsel-section">
            {loading && (
              <div className="adv-gear-loading" role="status" aria-live="polite">
                <span className="adv-gear-spin" aria-hidden />
                Consulting — weighing the loadout against the wiki…
              </div>
            )}
            {advice.stale && (
              <div className="adv-stale">
                Saved counsel from {advice.generated?.replace("T", " ") ?? "earlier"} — your
                level, zone, or exports have changed since. Consult to refresh.
              </div>
            )}
            {advice.note && <div className="adv-note">{advice.note}</div>}

            {advice.revision && (
              <div className="adv-note">
                <strong>Revised counsel</strong> — {advice.revision.model} applied
                the {advice.revision.reviews?.third ? "2nd + 3rd checks'" : "2nd check's"}{" "}
                findings and re-passed every verification gate
                {advice.revision.notes ? <>: {advice.revision.notes}</> : "."}
                {advice.revision.declined.length > 0 && (
                  <ul className="adv-list" style={{ marginTop: 6 }}>
                    {advice.revision.declined.map((d) => (
                      <li key={d.item} data-dim>
                        <span className="adv-cls">declined:</span>{" "}
                        <strong>{d.item}</strong> — {d.reason}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            <div className="adv-dc">
              <div className="adv-dc-bar">
                <span className="adv-dc-title">
                  Second opinions — another model re-derives this counsel from
                  the exact data the advisor saw. The 3rd check also weighs the
                  2nd&apos;s findings.
                </span>
                {(["second", "third"] as const).map((slot) => {
                  const prov = llm?.checks?.[slot] ?? (slot === "second" ? "claude_cli" : "none");
                  return (
                    <span key={slot} className="adv-dc-slot">
                      <label htmlFor={`adv-dc-${slot}`}>
                        {slot === "second" ? "2nd" : "3rd"}
                      </label>
                      <select
                        id={`adv-dc-${slot}`}
                        value={prov}
                        onChange={(e) => setCheckSlot(slot, e.target.value)}
                        title="Any model can sit in any check slot — coding-agent CLIs (subscription auth) or the API/local providers"
                      >
                        <option value="none">none</option>
                        {(llm?.options ?? [])
                          .filter((o) => o.provider !== "none")
                          .map((o) => (
                            <option key={o.provider} value={o.provider}>{o.label}</option>
                          ))}
                      </select>
                      {isCli(prov) && prov !== llm?.active.provider && cliPicker(prov)}
                      <button
                        type="button"
                        className="adv-rescan adv-gear-btn adv-dc-btn"
                        onClick={() => doubleCheck(slot)}
                        disabled={dcBusy !== null || loading || prov === "none"}
                        title="Reviews the picks against the exact briefing the advisor saw. CLI models need their tool installed and logged in; strong models at high effort can take minutes."
                      >
                        {dcBusy === slot
                          ? "checking…"
                          : advice.doublechecks?.[slot] ? "re-run" : "run"}
                      </button>
                    </span>
                  );
                })}
                <label className="adv-dc-debug-toggle" title="Show the full chain: which model produced the counsel, what each check said, and where they disagree">
                  <input type="checkbox" checked={dcDebug} onChange={toggleDcDebug} />
                  chain detail
                </label>
                {(advice.doublechecks?.second || advice.doublechecks?.third) && (
                  <button
                    type="button"
                    className="adv-rescan adv-gear-btn adv-dc-btn"
                    onClick={reviseCounsel}
                    disabled={revBusy || dcBusy !== null || loading}
                    title="Feed these findings back through your counsel model for a revised counsel — the revision re-passes every verification gate before it replaces this one, and the checks reset so you can review the revision fresh"
                  >
                    {revBusy ? "revising…" : "revise counsel with findings"}
                  </button>
                )}
              </div>
              {revBusy && (
                <div className="adv-gear-loading" role="status" aria-live="polite">
                  <span className="adv-gear-spin" aria-hidden />
                  Revising — the counsel model is applying the check findings,
                  then every gate re-runs… (can take minutes)
                </div>
              )}
              {dcBusy && (
                <div className="adv-gear-loading" role="status" aria-live="polite">
                  <span className="adv-gear-spin" aria-hidden />
                  Running the {dcBusy === "second" ? "2nd" : "3rd"} check —
                  re-deriving the counsel from the briefing… (strong models at
                  high effort can take a few minutes)
                </div>
              )}
              {dcError && <p className="adv-dc-error" role="alert">{dcError}</p>}
              {(["second", "third"] as const).map((slot) => {
                const dc = advice.doublechecks?.[slot];
                if (!dc || dcBusy === slot) return null;
                return (
                  <ReviewBlock key={slot} dc={dc}
                               label={slot === "second" ? "2nd check" : "3rd check"} />
                );
              })}
              {dcDebug && (
                <div className="adv-dc-trail">
                  <div className="adv-sub">Chain detail — who said what</div>
                  <table className="adv-table">
                    <tbody>
                      <tr>
                        <td className="adv-dc-stage">primary</td>
                        <td>
                          {advice.source === "llm"
                            ? `${advice.llm?.provider ?? "?"} · ${advice.llm?.model ?? "?"}`
                            : "deterministic (builtin advisor" +
                              (advice.llm && advice.llm.provider !== "none"
                                ? ` — ${advice.llm.provider} was configured but unavailable)`
                                : ")")}
                        </td>
                        <td className="adv-why">
                          {advice.loadout.length} loadout picks ·{" "}
                          {advice.aa_now.length + advice.aa_save.length} AA recs ·{" "}
                          {advice.locations.length} zones ·{" "}
                          {advice.generated?.replace("T", " ")}
                        </td>
                      </tr>
                      {(["second", "third"] as const).map((slot) => {
                        const dc = advice.doublechecks?.[slot];
                        if (!dc) return null;
                        const majors = dc.issues.filter((i) => i.severity === "major").length;
                        return (
                          <tr key={slot}>
                            <td className="adv-dc-stage">
                              {slot === "second" ? "2nd check" : "3rd check"}
                            </td>
                            <td>
                              {dc.provider} · {dc.model}
                              {dc.effort ? ` (${dc.effort})` : ""}
                            </td>
                            <td className="adv-why">
                              <span className="adv-dc-verdict" data-v={dc.verdict}>
                                {dc.verdict.replace("_", " ")}
                              </span>{" "}
                              {dc.issues.length} issue{dc.issues.length === 1 ? "" : "s"}
                              {majors > 0 && ` (${majors} major)`}
                              {slot === "third" && dc.prior_agreement && (
                                <>
                                  {" · "}
                                  <span className="adv-dc-agree" data-a={dc.prior_agreement}>
                                    {AGREEMENT_TEXT[dc.prior_agreement]}
                                  </span>
                                  {dc.prior_notes && (
                                    <span className="adv-cls"> — {dc.prior_notes}</span>
                                  )}
                                </>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {issueMatrix(advice.doublechecks?.second, advice.doublechecks?.third)
                    .length > 0 && (
                    <>
                      <div className="adv-sub" style={{ marginTop: 8 }}>
                        Where the checks overlap and split
                      </div>
                      <table className="adv-table">
                        <thead>
                          <tr>
                            <th>item</th>
                            <th>2nd</th>
                            <th>3rd</th>
                            <th>reading</th>
                          </tr>
                        </thead>
                        <tbody>
                          {issueMatrix(
                            advice.doublechecks?.second,
                            advice.doublechecks?.third,
                          ).map((r) => (
                            <tr key={r.item}>
                              <td><strong>{r.item}</strong></td>
                              <td>
                                {r.second ? (
                                  <span className="adv-dc-sev" data-sev={r.second}>
                                    {r.second}
                                  </span>
                                ) : "—"}
                              </td>
                              <td>
                                {r.third ? (
                                  <span className="adv-dc-sev" data-sev={r.third}>
                                    {r.third}
                                  </span>
                                ) : "—"}
                              </td>
                              <td className="adv-why" data-dim={r.split || undefined}>
                                {r.stance}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <p className="adv-dc-endorse">
                        A dash means that check ran and did not raise the item —
                        an implicit vote of confidence, not missing data. Items
                        both checks flag deserve the most attention.
                      </p>
                    </>
                  )}
                </div>
              )}
            </div>

            {advice.loadout.length + advice.nice_to_have.length > 0 && (
              <div className="adv-section">
                <h3>
                  Memorize now
                  {snap?.spell_slots != null &&
                    ` — ${advice.loadout.length}/${snap.spell_slots} slots filled`}
                  <button
                    type="button"
                    className="adv-rescan adv-gear-btn"
                    onClick={() => writeSpellSet("loadout")}
                    title={
                      // Deliberately NOT recomputing the name here. The
                      // backend derives it from the trio and reports it
                      // back; a second copy of that rule in the UI is the
                      // provider/model bug waiting to happen again.
                      "Write these picks as an in-game spell set named after your trio (e.g. pal/dru/mnk) — then /memspellset <that name> loads the whole bar"
                    }
                  >
                    write in-game spell set
                  </button>
                  <span className="adv-pick-count">
                    {/* Was hardcoded "/14". The gem count is Mnemonic
                        Retention's rank + the base 8, so it MOVES -- and the
                        counter two lines up already reads spell_slots. Two
                        numbers on one heading disagreeing is how a stale
                        setting hides: the hardcode happened to match the real
                        gem count while spell_slots still said 13, so the
                        loadout came back one short with nothing to show why. */}
                    {Object.values(pickSel).filter(Boolean).length}/{snap?.spell_slots ?? 8} picked · gems auto-ordered: DD, DoT, AoE, heals@8, utility, pets
                    {(advice.sa_songs?.length ?? 0) > 0 ? " · SA songs last" : ""}
                  </span>
                </h3>
                {(advice.sa_songs?.length ?? 0) > 0 && (
                  <p className="adv-purchase-note">
                    Symphonic Aura pulses eligible songs from the LAST gem
                    upward, one per owned rank — the written set sinks{" "}
                    {advice.sa_songs!.join(", ")} to the final gems
                    ({advice.sa_songs![0]} bottom-most), so combat songs get
                    the pulses instead of whatever landed there by category.
                  </p>
                )}
                {([
                  ["Must have", advice.must_have, 0],
                  ["Should have", advice.should_have, advice.must_have.length],
                  ["Nice to have — extra alternatives, pick and choose",
                   advice.nice_to_have, -1],
                ] as [string, typeof advice.loadout, number][]).map(
                  ([label, list, offset]) =>
                    list.length > 0 && (
                      <div key={label}>
                        <div className="adv-sub" style={{ marginTop: 8 }}>{label}</div>
                        <table className="adv-table">
                          <tbody>
                            {list.map((s, i) => (
                              <tr key={`${s.cls}-${s.name}`}>
                                <td className="adv-pick">
                                  <input
                                    type="checkbox"
                                    checked={pickSel[s.name] ?? false}
                                    disabled={
                                      !(pickSel[s.name] ?? false) &&
                                      Object.values(pickSel).filter(Boolean).length >= 14
                                    }
                                    onChange={(e) =>
                                      setPickSel((p) => {
                                        const picked = Object.values(p).filter(Boolean).length;
                                        if (e.target.checked && picked >= 14) return p;
                                        return { ...p, [s.name]: e.target.checked };
                                      })
                                    }
                                    title="Include in the written spell set (max 14)"
                                  />
                                </td>
                                <td className="adv-pri">
                                  {offset >= 0 ? offset + i + 1 : `·`}
                                </td>
                                <td>
                                  <strong>{s.name}</strong>
                                  {s.level != null && (
                                    <span className="adv-cls"> (L{s.level})</span>
                                  )}
                                </td>
                                <td className="adv-cls">{s.cls}</td>
                                <td className="adv-why">{s.reason}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ),
                )}
              </div>
            )}

            {advice.prebuffs.length > 0 && (
              <div className="adv-section">
                <h3>
                  Pre-buffs — cast, then swap the slot
                  <button
                    type="button"
                    className="adv-rescan adv-gear-btn"
                    onClick={() => writeSpellSet("prebuffs")}
                    title={
                      "Write the pre-buffs as a set named after your trio with a -buffs suffix (e.g. pal/dru/mnk-buffs), permanent buffs first — memorise it, buff up, then load your combat set"
                    }
                  >
                    write pre-buff set
                  </button>
                </h3>
                <ul className="adv-list">
                  {advice.prebuffs.map((s) => (
                    <li key={`${s.cls}-${s.name}`}>
                      <strong>{s.name}</strong>
                      {s.level != null && <span className="adv-cls"> (L{s.level})</span>}{" "}
                      <span className="adv-cls">({s.cls})</span>
                      {/* A permanent buff is cast once ever; a 27-minute one
                          has to be redone before the next pull. The flat list
                          gave no way to tell those apart. */}
                      {s.permanent ? (
                        <span className="adv-dur" data-kind="perm"> until death</span>
                      ) : s.duration_min ? (
                        <span className="adv-dur"> {s.duration_min} min</span>
                      ) : null}
                      {/* EQ buffs share effect slots and silently overwrite each
                          other — Courage and Center are the same slot, and you
                          only find out by casting both and watching one drop.
                          The stacking data already gates the loadout; this says
                          it out loud. */}
                      {/* A superseded buff is dropped by the backend rather
                          than dimmed — a row telling you to skip it is still
                          a row to read. Only the survivor is listed, and it
                          says what it replaced. */}
                      {(s.overwrites?.length ?? 0) > 0 && (
                        <span className="adv-stack" data-kind="win">
                          {" "}replaces {s.overwrites!.join(", ")}
                        </span>
                      )}
                      <br />
                      {s.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {advice.replace.length > 0 && (
              <div className="adv-section">
                <h3>Upgrade warnings</h3>
                <ul className="adv-list adv-replace">
                  {advice.replace.map((r) => (
                    <li key={r.using}>
                      <strong>{r.using}</strong> → <strong>{r.upgrade}</strong>
                      {/* The upgrade is the best you can CAST today, not
                          merely something better than what you named — and
                          `next` is the rung above it, so the row says where
                          you are and where you are going. */}
                      {r.next && (
                        <span className="adv-cls">
                          {" "}· next {r.next}
                          {r.next_level ? ` at L${r.next_level}` : ""}
                        </span>
                      )}
                      <br />
                      {r.why}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {/* The vendor shopping list is gone. Spell vendors in EQL are
    everywhere and stock broadly, and the in-game find tool already
    answers "where do I buy this" better than a cached wiki lookup
    could -- it was three wiki round-trips per consult to restate
    something the game tells you faster. */}

            {(advice.aa_now.length > 0 || advice.aa_save.length > 0) && (
              <div className="adv-section">
                <h3>
                  AA counsel
                  {snap?.aa_available != null && ` — ${snap.aa_available} unspent`}
                </h3>
                <div className="adv-cols">
                  <div>
                    <div className="adv-sub">Unlock now</div>
                    {aaBlock(advice.aa_now, "Nothing affordable stands out.")}
                  </div>
                  <div>
                    <div className="adv-sub">Save for</div>
                    {aaBlock(advice.aa_save, "No savings goal right now.")}
                  </div>
                </div>
              </div>
            )}

            {advice.horizon.length > 0 && (
              <div className="adv-section">
                <h3>Next five levels</h3>
                <ul className="adv-list">
                  {advice.horizon.map((h) => (
                    <li key={`${h.cls}-${h.name}`}>
                      <span className="adv-lvl">L{h.level ?? "?"}</span>
                      <strong>{h.name}</strong> <span className="adv-cls">({h.cls})</span>
                      <br />
                      {h.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {advice.locations.length > 0 && (
              <div className="adv-section">
                <h3>Where to hunt</h3>
                <ul className="adv-list">
                  {advice.locations.map((l) => (
                    <li key={l.zone}>
                      <strong><ZoneLink zone={l.zone} /></strong>
                      <br />
                      {l.why}
                      {l.notable && (
                        <>
                          <br />
                          <em className="adv-notable">Notable: {l.notable}</em>
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {hunting && hunting.level != null && hunting.zones.length > 0 && (
              <div className="adv-section">
                <h3>Leveling chart</h3>
                <HuntChart
                  data={hunting}
                  picked={(advice?.locations ?? []).map((l) => l.zone)}
                />
              </div>
            )}
            </div>


            {advice.class_notes.length > 0 && (
              <div className="adv-section">
                <h3>Class notes</h3>
                <ul className="adv-list">
                  {advice.class_notes.map((n) => (
                    <li key={n.topic}>
                      <strong>{n.topic}</strong>
                      <br />
                      {n.advice}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="adv-foot">
              <span>
                {advice.source === "llm"
                  ? advice.grounding === "wiki"
                    ? "Grounded in the EQL wiki — verify costs in-game."
                    : "From model memory (wiki unreachable) — treat names as approximate."
                  : "Built-in notes only — the LLM is offline."}
              </span>
              <span>{new Date(advice.generated).toLocaleTimeString()}</span>
            </div>
          </>
        )}

        {/* Equipment lives OUTSIDE the advice guard. It was nested
            inside it, so the consult-gear button could not be reached
            until an advisor consult had run -- two independent
            consults, one of them held hostage by the other. */}
            <div className="adv-section adv-gear-section">
              <h3 className="adv-gear-head">
                <span>Equipment</span>
                <span className="adv-pet-inline" title="Pet equipment slot count and equip class — used only by the gear consult">
                  <label title="Auto-computed from your pet classes (4 base + 3 per pet-capable class in your combo). Type a number here only to override.">
                    pet slots
                    <input
                      type="number"
                      min={0}
                      placeholder="auto"
                      value={petSlotsDraft}
                      onChange={(e) => setPetSlotsDraft(e.target.value)}
                      onBlur={() => numberPatch(petSlotsDraft, "pet_slots")}
                      onKeyDown={(e) => e.key === "Enter" && numberPatch(petSlotsDraft, "pet_slots")}
                    />
                  </label>
                  <label title="Every pet is base Warrior — set only its SECOND class by pet type: Mage Earth=Ranger, Water=Rogue, Fire=Wizard; Enchanter=Paladin; Beastlord=Berserker; Necro/SK Undead=Shadow Knight. The pet can also wear your character classes' gear.">
                    pet 2nd class
                    <select
                      value={petClassDraft}
                      onChange={(e) => {
                        setPetClassDraft(e.target.value);
                        patch({ pet_classes: e.target.value || null });
                      }}
                    >
                      <option value="">Warrior only</option>
                      {CLASSES.filter((c) => c !== "Warrior").map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                    </select>
                  </label>
                </span>
                <button
                  type="button"
                  className="adv-rescan adv-gear-btn"
                  onClick={() => consultGear(true)}
                  disabled={gearLoading}
                  title="Best owned item per slot + farming targets (first run mines item stats from the wiki — slow)"
                >
                  {gearLoading ? "consulting…" : gear ? "re-consult gear" : "consult gear"}
                </button>
                <button
                  type="button"
                  className="adv-rescan adv-gear-btn"
                  onClick={askAboutGear}
                  title="Ask the counsel about this table — the same chat seat holds your gear briefing, every slot row and the check findings, so it answers equipment questions directly"
                >
                  ask about gear
                </button>
              </h3>
              {gearLoading && (
                <div className="adv-gear-loading" role="status" aria-live="polite">
                  <span className="adv-gear-spin" aria-hidden />
                  Consulting — mining item stats from the wiki…
                </div>
              )}
              {gear?.stale && (
                <div className="adv-stale">
                  Saved gear counsel from {gear.generated?.replace("T", " ") ?? "earlier"} —
                  context changed since. Re-consult to refresh.
                </div>
              )}
              {gear?.note && <div className="adv-note">{gear.note}</div>}
              {gear?.revision && (
                <div className="adv-note">
                  <strong>Revised gear table</strong> — {gear.revision.model} applied
                  the check findings and re-passed every gear gate
                  {gear.revision.notes ? <>: {gear.revision.notes}</> : "."}
                  {gear.revision.declined.length > 0 && (
                    <ul className="adv-list" style={{ marginTop: 6 }}>
                      {gear.revision.declined.map((d) => (
                        <li key={d.item} data-dim>
                          <span className="adv-cls">declined:</span>{" "}
                          <strong>{d.item}</strong> — {d.reason}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {gear && (
                <div className="adv-dc">
                  <div className="adv-dc-bar">
                    <span className="adv-dc-title">
                      Second opinions on the gear table — reviewed as a whole
                      (uses the check models configured in the counsel section)
                    </span>
                    {(["second", "third"] as const).map((slot) => (
                      <button
                        key={slot}
                        type="button"
                        className="adv-rescan adv-gear-btn adv-dc-btn"
                        onClick={() => doubleCheckGear(slot)}
                        disabled={
                          gearDcBusy !== null || gearLoading ||
                          (llm?.checks?.[slot] ?? (slot === "second" ? "claude_cli" : "none")) === "none"
                        }
                        title="Reviews the whole gear table jointly against the exact briefing the gear advisor saw — including whether any two recommendations should trade slots"
                      >
                        {gearDcBusy === slot
                          ? "checking…"
                          : `${gear.doublechecks?.[slot] ? "re-run" : "run"} ${slot === "second" ? "2nd" : "3rd"} check`}
                      </button>
                    ))}
                    {(gear.doublechecks?.second || gear.doublechecks?.third) && (
                      <button
                        type="button"
                        className="adv-rescan adv-gear-btn adv-dc-btn"
                        onClick={reviseGear}
                        disabled={gearRevBusy || gearDcBusy !== null || gearLoading}
                        title="Feed these findings back through your counsel model for a revised gear table — it re-passes every gear gate before replacing this one, and the checks reset"
                      >
                        {gearRevBusy ? "revising…" : "revise gear with findings"}
                      </button>
                    )}
                  </div>
                  {gearRevBusy && (
                    <div className="adv-gear-loading" role="status" aria-live="polite">
                      <span className="adv-gear-spin" aria-hidden />
                      Revising the gear table — applying the findings, then
                      every gate re-runs… (can take minutes)
                    </div>
                  )}
                  {gearDcBusy && (
                    <div className="adv-gear-loading" role="status" aria-live="polite">
                      <span className="adv-gear-spin" aria-hidden />
                      Running the gear {gearDcBusy === "second" ? "2nd" : "3rd"} check —
                      re-deriving the table from the briefing… (can take minutes)
                    </div>
                  )}
                  {gearDcError && (
                    <p className="adv-dc-error" role="alert">{gearDcError}</p>
                  )}
                  {(["second", "third"] as const).map((slot) => {
                    const dc = gear.doublechecks?.[slot];
                    if (!dc || gearDcBusy === slot) return null;
                    return (
                      <ReviewBlock key={slot} dc={dc}
                                   label={slot === "second" ? "gear 2nd check" : "gear 3rd check"} />
                    );
                  })}
                </div>
              )}
              {gear && gear.slots.length > 0 && (
                <table className="adv-table">
                  <thead>
                    <tr>
                      <th scope="col">Slot</th>
                      <th scope="col">Now</th>
                      <th scope="col">Use</th>
                      <th scope="col">Why</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gear.slots.map((s) => (
                      <tr
                        key={s.slot + (s.recommend ?? "")}
                        data-dim={
                          !s.why ||
                          s.why.startsWith("keep —") ||
                          s.why.startsWith("empty —") ||
                          (s.recommend != null && s.recommend === s.current)
                            ? "1"
                            : undefined
                        }
                      >
                        <td className="adv-cls">{s.slot}</td>
                        <td>
                          {s.current ? (
                            <>
                              <ItemHover name={s.current} />
                              <StatFix name={s.current} onSaved={() => consultGear(true)} />
                            </>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td>
                          <strong>{s.recommend ? <ItemHover name={s.recommend} /> : "—"}</strong>
                          {s.where && (
                            <span className="adv-cls"> ({s.where})</span>
                          )}
                        </td>
                        <td className="adv-why">{s.why}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="adv-sub" style={{ marginTop: 12 }}>
                Melee loadouts — measured
                <button type="button" className="link-btn" onClick={loadMelee}>
                  {melee ? "refresh" : "compare"}
                </button>
              </div>
              {melee && (
                <>
                  <p className="adv-note">
                    From your own fights, not a formula: the two-handed damage bonus
                    is not published for this game. WEAPON SWINGS ONLY — kick, bash,
                    smite and the monk strike/punch line are class skills on their
                    own timers and are excluded, so this is the weapon&apos;s
                    contribution and not your whole melee output. Hands are inferred
                    NOT inferred: two weapons of the same type both log one verb,
                    and an off-hand adds far less than double because it only
                    swings when a skill check passes.
                    {melee.overlap
                      ? ` Levels ${melee.overlap.level_lo}–${melee.overlap.level_hi} only, so gear and level are not doing the work.`
                      : " Level ranges differ between rows — compare with that in mind."}
                    {melee.dual_wield_ceiling
                      ? ` At level ${melee.level} an off-hand lands at most ${Math.round(
                          melee.dual_wield_ceiling * 100,
                        )}% of the time, so a second weapon adds up to that — not double.`
                      : ""}
                    {melee.ceiling_with_aa
                      ? ` Ambidexterity raises that to ${Math.round(
                          melee.ceiling_with_aa.as_relative * 100,
                        )}–${Math.round(melee.ceiling_with_aa.as_points * 100)}% — the AA text does not say whether its +32% is points or relative.`
                      : ""}
                    {" These are HITS, not swings: a missed off-hand swing is not counted, so an off-hand is worth at least what this shows."}
                  </p>
                  <table className="enc-table">
                    <thead>
                      <tr>
                        <th scope="col">Weapons seen</th>
                        <th scope="col">Fights</th>
                        <th scope="col">DPS</th>
                        <th scope="col">Avg</th>
                        <th
                          scope="col"
                          title={
                            melee.dual_wield_ceiling
                              ? `At level ${melee.level}, a maxed dual-wield skill lands the off-hand at most ${Math.round(melee.dual_wield_ceiling * 100)}% of the time — so a second weapon adds up to that much, not double.`
                              : undefined
                          }
                        >
                          Hits/min
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {(melee.overlap?.groups ?? melee.groups)
                        .filter((g) => g.fights >= 3)
                        .slice(0, 6)
                        .map((g) => (
                          <tr key={g.verbs.join("+")}>
                            {/* No hand-count label. Two attempts at inferring
                                it from the log were both wrong, and two
                                slashing weapons are genuinely indistinguishable
                                from one here — the swing rate is the evidence,
                                and you know what you equipped. */}
                            <td className="enc-name">{g.verbs.join(" + ")}</td>
                            <td>{g.fights}</td>
                            <td>
                              <strong>{g.dps}</strong>
                            </td>
                            <td>{g.avg_hit}</td>
                            <td>{g.hits_per_min}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </>
              )}

              {gear && (gear.merges?.length ?? 0) > 0 && (
                <>
                  <div
                    className="adv-sub"
                    style={{ marginTop: 10 }}
                    title="Two copies of the same base item can be merged in-game to advance its +N. Equal ranks merge to exactly one rank up; unequal ranks add partial progress (wiki upgrade-progression model)."
                  >
                    Merge opportunities — duplicate items you own
                  </div>
                  <ul className="adv-list">
                    {(gear.merges ?? []).map((m) => (
                      <li key={m.item}>
                        <strong><ItemHover name={m.item} /></strong>
                        <span className="adv-cls">
                          {" "}— {m.copies.join(" + ")} → merges to {m.result}
                        </span>
                        {m.compare && (
                          <>
                            <br />
                            <span className="adv-merge-warn">{m.compare}</span>
                          </>
                        )}
                        {m.filter_action && (
                          <>
                            <br />
                            <span className="adv-cls">
                              loot filter: this item is set to auto-{m.filter_action}
                            </span>
                          </>
                        )}
                        {m.hosts_exalt && (
                          <>
                            <br />
                            <span className="adv-cls">
                              a copy hosts an exaltation stone — check it before merging
                            </span>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {gear && (gear.clickies?.length ?? 0) > 0 && (
                <>
                  <div
                    className="adv-sub"
                    style={{ marginTop: 10 }}
                    title="Items you own with an effect you activate yourself. Weapon procs are excluded — those fire on their own."
                  >
                    Clickies you own
                  </div>
                  <ul className="adv-list">
                    {(gear.clickies ?? []).map((k) => (
                      <li key={k.item}>
                        <strong><ItemHover name={k.item} /></strong>
                        <span className="adv-cls"> — {k.spell}</span>
                        {k.note && <span className="adv-cls"> ({k.note})</span>}
                        <br />
                        <span className="adv-cls">
                          {k.where === "worn"
                            ? `worn${k.slot ? ` · ${k.slot}` : ""}`
                            : `in your ${k.where} — not equipped`}
                        </span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {gear && (gear.unknown?.length ?? 0) > 0 && (
                <>
                  <div
                    className="adv-sub"
                    style={{ marginTop: 10 }}
                    title="Owned items with no eqlwiki page. Without a Slot line the app cannot place them, so they are never recommended — including into an empty slot."
                  >
                    Owned, but not on the wiki
                  </div>
                  <ul className="adv-list">
                    {(gear.unknown ?? []).map((n) => (
                      <li key={n}>
                        <strong>{n}</strong>
                        <br />
                        <span className="adv-cls">
                          no wiki page, so its STATS are unknown and it is never
                          used in a comparison. Wear it once and the app learns
                          its slot from your export.
                        </span>
                      </li>
                    ))}
                  </ul>
                  {emptySlots.length > 0 && (
                    <div className="adv-cls" style={{ marginTop: 4 }}>
                      You have {emptySlots.length} empty slot
                      {emptySlots.length === 1 ? "" : "s"} ({emptySlots.join(", ")})
                      {" "}— if one of the above fits, the app cannot tell.
                    </div>
                  )}
                </>
              )}
              {(() => {
                const petInv = snap?.pet_inventory ?? {};
                const petGear = gear?.pet_gear ?? [];
                if (!Object.keys(petInv).length && !petGear.length) return null;
                const held = Object.values(petInv);
                const heldSet = new Set(held.map((v) => v.toLowerCase()));
                const recs = petGear.filter((p) => !heldSet.has(p.item.toLowerCase()));
                return (
                  <>
                    <div className="adv-sub" style={{ marginTop: 10 }}>
                      Pet gear (Warrior{snap?.pet_classes ? `/${snap.pet_classes}` : ""} +
                      your classes) — up to {snap?.pet_slots ?? held.length} items;
                      persists through death &amp; re-summon
                    </div>
                    {held.length > 0 && (
                      <p className="adv-purchase">
                        <span className="adv-cls">
                          {snap?.pet_inventory_stale ? "Was holding (BETA): " : "Now holding: "}
                        </span>
                        {held.join(" · ")}
                      </p>
                    )}
                    {/* Pet gear survives death and re-summon, so this list is
                        deliberately kept between sessions — but it does not
                        survive a wipe, and a beta list describes a pet that no
                        longer exists while still gating hand-over advice. */}
                    {snap?.pet_inventory_stale && (
                      <p className="set-note" data-ok="0">
                        That list was read before launch — that pet is gone. Run{" "}
                        <code>/pet inventory check</code> to replace it.
                      </p>
                    )}
                    {recs.length > 0 ? (
                      <table className="adv-table">
                        <thead>
                          <tr>
                            <th scope="col">Hand to pet</th>
                            <th scope="col">Why</th>
                          </tr>
                        </thead>
                        <tbody>
                          {recs.map((p) => (
                            <tr key={p.item}>
                              <td>
                                <strong><ItemHover name={p.item} /></strong>
                                {p.where && <span className="adv-cls"> ({p.where})</span>}
                              </td>
                              <td className="adv-why">{p.why}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : (
                      <p className="adv-purchase-note">
                        Nothing better in your bags/bank for the pet right now.
                      </p>
                    )}
                  </>
                );
              })()}
              {gear && gear.exaltations.length > 0 && (
                <>
                  <div className="adv-sub" style={{ marginTop: 10 }}>
                    Exaltations you own (can-socket-into is computed from the
                    class/slot rules; the actual move is done in-game)
                  </div>
                  <ul className="adv-list">
                    {gear.exaltations.map((x) => (
                      <li key={x.name}>
                        <strong><ItemHover name={x.name} /></strong>
                        {x.where && <span className="adv-cls"> — {x.where}</span>}
                        <br />
                        {x.why}
                        {x.move_to && (
                          <>
                            <br />
                            <span className="adv-cls">can socket into: {x.move_to}</span>
                          </>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {gear && gear.farm.length > 0 && (
                <>
                  <div className="adv-sub" style={{ marginTop: 10 }}>Where to farm upgrades</div>
                  <ul className="adv-list">
                    {gear.farm.map((f) => (
                      <li key={f.item}>
                        <strong><ItemHover name={f.item} /></strong>
                        {f.slot && <span className="adv-cls"> ({f.slot})</span>}
                        {f.zone && (
                          <>
                            {" — "}
                            <ZoneLink zone={f.zone} verified={false} />
                            {f.source ? ` · ${f.source}` : ""}
                          </>
                        )}
                        <br />
                        {f.why}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
      </div>
    </section>
  );
});