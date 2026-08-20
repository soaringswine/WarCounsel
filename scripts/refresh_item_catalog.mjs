#!/usr/bin/env node
// build-eql-bis-items.mjs
// Generates eql-bis-items.json for EQL BiS by scraping the EverQuest Legends
// Wiki (https://eqlwiki.com) via its MediaWiki API. Adapted from eqlfilter's
// tools/build-eql-items.mjs — same enumeration, but parses the {{Itempage}}
// statsblock into full numeric stats needed for scoring.
//
// Run:  node build-eql-bis-items.mjs > eql-bis-items.json
//       node build-eql-bis-items.mjs --inspect "Crushbone Belt"   (dump wikitext)
//       node build-eql-bis-items.mjs --limit 50                   (quick test)
// Needs: Node 18+. No npm install.
//
// ALTERNATIVE DATA SOURCE: https://eqlegendstools.com/bis-gear/ has a curated,
// audited BiS gear dataset (by FlammHammer, @theFlammHammer on Discord) with
// per-class filtering and mote upgrade scales. It's a client-side app, so its
// item JSON could likely be reused with permission — ask before scraping.
//
// NOTE: statsblock field regexes below follow the classic EQ tooltip format
// (AC: / DMG: / Atk Delay: / STR: +N / SV FIRE: etc). Run --inspect on a few
// items first and tune parseStats() if the wiki template differs.

const API = "https://eqlwiki.com/api.php";
const UA = "EQLBiS-item-builder/1.0 (eqlbis)";

const CLASS_CODES = ["WAR","CLR","PAL","RNG","SHD","DRU","MNK","BRD","ROG","SHM","NEC","WIZ","MAG","ENC","BST","BER"];
const RACE_CODES = ["HUM","BAR","ERU","ELF","HIE","DEF","HEF","DWF","TRL","OGR","HFL","GNM","IKS","VAH","FRG","DRK"];
const WEAPON_CATS = new Set(["1H Slashing","2H Slashing","1H Blunt","2H Blunt","1H Piercing","2H Piercing","Piercing","Hand to Hand","Archery","Throwing","Ammo"]);
// Wiki slot lines mix singular/plural ("Slot: FINGER" on Platinum Ring vs
// "Slot: FINGERS" elsewhere) — normalize every variant to the app's names.
const SLOT_MAP = {
  PRIMARY: "Primary", SECONDARY: "Secondary", RANGE: "Range", AMMO: "Ammo",
  HEAD: "Head", FACE: "Face", EAR: "Ear", EARS: "Ear", NECK: "Neck",
  SHOULDER: "Shoulders", SHOULDERS: "Shoulders", BACK: "Back",
  ARM: "Arms", ARMS: "Arms", WRIST: "Wrist", WRISTS: "Wrist",
  HAND: "Hands", HANDS: "Hands", FINGER: "Fingers", FINGERS: "Fingers",
  CHEST: "Chest", WAIST: "Waist", LEG: "Legs", LEGS: "Legs",
  FOOT: "Feet", FEET: "Feet", CHARM: "Charm", HELD: "Held",
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(params) {
  const url = new URL(API);
  url.search = new URLSearchParams({ format: "json", formatversion: "2", ...params }).toString();
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url, { headers: { "User-Agent": UA } });
    if (res.ok) return res.json();
    await sleep(500 * (attempt + 1));
  }
  throw new Error("API failed: " + url);
}

async function categoryMembers(category) {
  const titles = [];
  let cont;
  do {
    const data = await api({
      action: "query", list: "categorymembers",
      cmtitle: "Category:" + category, cmtype: "page",
      cmlimit: "500", ...(cont ? { cmcontinue: cont } : {}),
    });
    for (const m of data?.query?.categorymembers ?? []) titles.push(m.title);
    cont = data?.continue?.cmcontinue;
    process.stderr.write(`  ${category}: ${titles.length}\r`);
  } while (cont);
  process.stderr.write("\n");
  return titles;
}

// One pass over Category:Zones: the zone-name set (to pick zone categories off
// item pages) plus each zone's minimum monster level, parsed from the zone
// page's "Level of Monsters: | 5-20" (or "60+") table row. An item's level to
// obtain is derived from its cheapest drop zone when the wiki has no REC/REQ.
async function buildZones() {
  const set = new Set(), levels = new Map();
  const titles = await categoryMembers("Zones");
  for (let i = 0; i < titles.length; i++) {
    const title = titles[i];
    const lvl = num(await wikitext(title), /Level of Monsters:[^|]*\|\s*(\d+)/i);
    for (const v of [title, title.replace(/^The\s+/i, ""), ...(!/^The\s+/i.test(title) ? ["The " + title] : [])]) {
      set.add(v);
      if (lvl) levels.set(v, lvl);
    }
    process.stderr.write(`  zone levels: ${i + 1}/${titles.length}\r`);
    await sleep(120);
  }
  process.stderr.write("\n");
  return { zoneSet: set, zoneLevels: levels };
}

async function wikitext(title) {
  const data = await api({
    action: "query", prop: "revisions", rvprop: "content",
    rvslots: "main", titles: title, redirects: "1", // "a dread bone" -> "A Dread Bone"
  });
  return data?.query?.pages?.[0]?.revisions?.[0]?.slots?.main?.content ?? "";
}

// Min level of a mob from its {{Namedmobpage}}: "18" or a range "3-10" (take
// the low end). 0 when the page is missing or has no level.
async function mobLevel(title) {
  const m = (await wikitext(title)).match(/\|\s*level\s*=\s*(\d+)/i);
  return m ? parseInt(m[1], 10) : 0;
}

function param(text, name) {
  const m = text.match(new RegExp("\\|\\s*" + name + "\\s*=([\\s\\S]*?)(?=\\n\\s*\\||\\n\\}\\})"));
  return m ? m[1].trim() : "";
}
// Some pages repeat a param (e.g. two |notes on Shimmering Ruby Stiletto,
// the first empty) — collect every non-empty occurrence.
function paramAll(text, name) {
  return [...text.matchAll(new RegExp("\\|\\s*" + name + "\\s*=([\\s\\S]*?)(?=\\n\\s*\\||\\n\\}\\})", "g"))]
    .map((m) => m[1].trim()).filter(Boolean);
}
// Drop {{templates}} (two passes for one nesting level, e.g. {{a|{{b}}}}).
const stripTpl = (s) => s.replace(/\{\{[^{}]*\}\}/g, "").replace(/\{\{[^{}]*\}\}/g, "");

const num = (s, re) => { const m = s.match(re); return m ? parseInt(m[1], 10) : 0; };
const titleCase = (s) => s.toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
// [[Page|label]] -> label, [[Page]] -> Page, strip html tags, collapse spaces.
const cleanWiki = (s) => s
  .replace(/\[\[([^\]|]+)\|([^\]]*)\]\]/g, "$2")
  .replace(/\[\[([^\]]+)\]\]/g, "$1")
  .replace(/<[^>]+>/g, "")
  .replace(/\s+/g, " ").trim();

// Parse the statsblock into the eql-bis-items.json record shape.
function parseStats(stats) {
  const out = {
    ac: num(stats, /\bAC:\s*\+?(-?\d+)/i),
    hp: num(stats, /\bHP:\s*\+?(-?\d+)/i),
    mana: num(stats, /\bMANA:\s*\+?(-?\d+)/i),
    dmg: num(stats, /\bDMG:\s*(\d+)/i),
    dly: num(stats, /\b(?:Atk\s*)?Delay:\s*(\d+)/i),
    haste: num(stats, /\bHaste:\s*\+?(\d+)%/i),
    hpRegen: num(stats, /\bHP\s+Regen:\s*\+?(\d+)/i),
    manaRegen: num(stats, /\bMana\s+Regen:\s*\+?(\d+)/i),
    endRegen: num(stats, /\bEnd(?:urance)?\s+Regen:\s*\+?(\d+)/i),
    end: num(stats, /\bEND:\s*\+?(-?\d+)/i),
    backstab: num(stats, /\bBACKSTAB(?:\s*DMG)?:\s*(\d+)/i),
    charges: num(stats, /\bCharges:\s*(\d+)/i),
    range: (stats.match(/\bRange:\s*([\d/ ]*\d)/i) || [])[1]?.trim() || "",
    deity: cleanWiki((stats.match(/Deity:\s*(.+?)(?:<br|\n)/i) || [])[1] || ""),
    stats: {},
  };
  for (const k of ["STR","STA","AGI","DEX","WIS","INT","CHA"]) {
    const v = num(stats, new RegExp("\\b" + k + ":\\s*\\+?(-?\\d+)", "i"));
    if (v) out.stats[k] = v;
  }
  // Resists summed into one SV bucket for scoring (weighting treats them
  // equally), plus per-resist values kept for the item-info tooltip.
  let sv = 0;
  out.resists = {};
  for (const k of ["MAGIC","FIRE","COLD","DISEASE","POISON"]) {
    const v = num(stats, new RegExp("SV\\s*" + k + ":\\s*\\+?(-?\\d+)", "i"));
    if (v) { out.resists[k] = v; sv += v; }
  }
  if (sv) out.stats.SV = sv;

  // Tooltip extras: weight, size, race list (ALL -> []), inline Effect line.
  const wtm = stats.match(/\bWT:\s*([\d.]+)/i);
  out.wt = wtm ? parseFloat(wtm[1]) : 0;
  out.size = (stats.match(/\bSize:\s*(TINY|SMALL|MEDIUM|LARGE|GIANT)/i) || [])[1]?.toUpperCase() || "";
  out.races = allExcept(
    (stats.match(/Race(?:s)?:\s*([A-Za-z` ,]+?)\s*(?:<br|\n)/i) || [])[1] || "", RACE_CODES);
  out.effect = cleanWiki((stats.match(/Effect:\s*(.+?)(?:<br|\n)/i) || [])[1] || "");

  // Slots: "Slot: PRIMARY SECONDARY" — normalize known slot words, dedupe.
  const slotLine = (stats.match(/Slot:\s*([A-Z ,]+?)\s*(?:<br|\n)/i) || [])[1] || "";
  out.slots = [...new Set(slotLine.toUpperCase().split(/[\s,]+/).map((w) => SLOT_MAP[w]).filter(Boolean))];

  // Level to obtain: "REC LEVEL: 10" style or prose "Recommended level of 10."
  out.level = num(stats, /\b(?:REC|REQ)?\s*LEVEL(?:\s*TO\s*OBTAIN)?:\s*(\d+)/i)
    || num(stats, /Recommended level of (\d+)/i);

  // Classes: "Class: ALL", "Class: WAR PAL SHD", or "Class: ALL except NEC WIZ".
  out.classes = allExcept(
    (stats.match(/Class(?:es)?:\s*([A-Za-z ,]+?)\s*(?:<br|\n)/i) || [])[1] || "", CLASS_CODES);
  return out;
}

// "ALL" -> [] (anyone); "ALL except A B" -> codes minus the listed; else the listed.
function allExcept(line, codes) {
  const up = line.toUpperCase();
  const listed = up.split(/[\s,]+/).filter((c) => codes.includes(c));
  if (!/\bALL\b/.test(up)) return listed;
  return /\b(EXCEPT|BUT)\b/.test(up) ? codes.filter((c) => !listed.includes(c)) : [];
}

function zoneFloor(zones, zoneLevels) {
  const lvls = zones.map((z) => zoneLevels.get(z)).filter(Boolean);
  return lvls.length ? Math.min(50, ...lvls) : 0;
}

function classifyType(cats, slots, skill) {
  if (skill || cats.some((c) => WEAPON_CATS.has(c))) return "Weapon";
  if (cats.some((c) => /Tradeskill|Ingredient|Foraged/i.test(c))) return "Tradeskill";
  if (cats.some((c) => /^(Bag|Container)\b/i.test(c))) return "Container";
  const s = slots.join(" ").toUpperCase();
  if (/\b(EAR|FINGERS|NECK|FACE)\b/.test(s)) return "Jewelry";
  if (slots.length) return "Armor";
  return "Misc";
}

function parseItem(title, text, zoneSet = new Set(), zoneLevels = new Map()) {
  const statsText = param(text, "statsblock");
  const cats = [...text.matchAll(/\[\[Category:\s*([^\]|]+)/g)].map((m) => m[1].trim());
  const parsed = parseStats(statsText);

  const flags = [];
  if (/NO\s*DROP|NO\s*TRADE/i.test(statsText)) flags.push("NO DROP");
  if (/LORE\s*(ITEM|EQUIPPED)?/i.test(statsText) || cats.some((c) => /^Lore/i.test(c))) flags.push("LORE");
  if (/MAGIC\s*ITEM/i.test(statsText)) flags.push("MAGIC");
  if (/QUEST\s*ITEM/i.test(statsText) || cats.includes("Quest Items")) flags.push("QUEST");
  if (cats.includes("Vendor Sold")) flags.push("VENDOR");
  if (cats.includes("Player Crafted") || param(text, "playercrafted").trim()) flags.push("CRAFTED");
  if (/ATTUN[EA]+BLE/i.test(statsText)) flags.push("ATTUNABLE"); // "Attunable" / "ATTUNEABLE"

  // \s* before <br: some statsblocks write "Skill: SHIELD<br>" with no space.
  const skillRaw = (statsText.match(/Skill:\s*([A-Za-z0-9 ]+?)\s*(?:Atk|<br)/i) || [])[1]?.trim() || "";
  const skill = /^shield$/i.test(skillRaw) ? "Shield" : skillRaw;
  // Shield detection is layered: an explicit "Skill: SHIELD" statsblock line
  // (rare) wins; else shield-words in the name among AC'd no-damage Secondary
  // items; main() adds an icon-propagation pass that catches the rest and
  // warns about anything still ambiguous.
  const isShield = skill === "Shield" || (!skill && !parsed.dmg && parsed.ac > 0 && parsed.slots.includes("Secondary") &&
    /\b(shield|buckler|targe|aegis|bulwark|defender|protector)\b/i.test(param(text, "itemname") || title));

  const zones = [], mobs = [];
  const addZone = (z) => { z = z.trim(); if (z && !zones.includes(z)) zones.push(z); };
  for (const line of param(text, "dropsfrom").split("\n")) {
    const t = line.trim();
    if (t.startsWith("*")) {
      // "* [[mob name]]" bullets under a zone header — the actual droppers
      const m = t.match(/\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]/);
      if (m && !mobs.includes(m[1].trim())) mobs.push(m[1].trim());
      continue;
    }
    const m = t.match(/^\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]/);
    if (m) addZone(m[1]);
  }
  for (const c of cats) if (zoneSet.has(c)) addZone(c);
  zones.sort();

  // Vendor locations: first cell of each {{ItemWhereRow | [[Zone|Label]] | seller | ...}}
  // in |soldby. Prefer the display label ("East Cabilis") over the page name.
  const vendors = [];
  for (const m of param(text, "soldby").matchAll(/\{\{ItemWhereRow\s*\|\s*\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g)) {
    const z = (m[2] || m[1]).trim();
    if (z && !vendors.includes(z)) vendors.push(z);
  }
  vendors.sort();

  const notes = cleanWiki(stripTpl(paramAll(text, "notes").join(" · ")));
  if (vendors.length && !flags.includes("VENDOR")) flags.push("VENDOR");

  // lucy_img_ID is the EQ icon id (dragitem sheet space, ids >= 500).
  const iconRaw = parseInt(param(text, "lucy_img_ID"), 10);
  const icon = iconRaw >= 500 ? iconRaw : 0;

  return {
    name: param(text, "itemname") || title,
    type: classifyType(cats, parsed.slots, isShield ? "" : skill),
    skill: skill || (isShield ? "Shield" : "") || cats.find((c) => WEAPON_CATS.has(c)) || "",
    slots: parsed.slots,
    classes: parsed.classes,
    // Explicit REC/REQ level (rare on the wiki) beats the zone-derived floor:
    // min monster level of the item's cheapest drop zone, capped at 50.
    // Vendor-sold items are buyable at any level, so no zone floor applies.
    level: parsed.level || (vendors.length ? 0 : zoneFloor(zones, zoneLevels)),
    ac: parsed.ac, hp: parsed.hp, mana: parsed.mana,
    dmg: parsed.dmg, dly: parsed.dly, haste: parsed.haste,
    hpRegen: parsed.hpRegen, manaRegen: parsed.manaRegen, endRegen: parsed.endRegen,
    end: parsed.end, backstab: parsed.backstab, charges: parsed.charges,
    range: parsed.range, deity: parsed.deity,
    stats: parsed.stats, resists: parsed.resists,
    wt: parsed.wt, size: parsed.size, races: parsed.races,
    effect: parsed.effect,
    focus: cleanWiki(param(text, "focus_effect")),
    notes,
    icon,
    zones, vendors, flags,
    // used by main()'s mob-level pass, stripped before output
    _mobs: mobs, _explicitLevel: parsed.level,
  };
}

async function main() {
  const args = process.argv.slice(2);
  const inspectIdx = args.indexOf("--inspect");
  if (inspectIdx !== -1) {
    console.log(await wikitext(args[inspectIdx + 1]));
    return;
  }
  const limIdx = args.indexOf("--limit");
  const limit = limIdx !== -1 ? parseInt(args[limIdx + 1], 10) : Infinity;
  const { zoneSet, zoneLevels } = args.includes("--no-zones")
    ? { zoneSet: new Set(), zoneLevels: new Map() }
    : await buildZones();

  process.stderr.write("Enumerating Category:Items...\n");
  // Item pages whose wiki link tables are broken (no category/transclusion
  // records, so no enumeration can find them — a wiki null-edit fixes them
  // for good). Listed manually as reported, like the app's NAME_ALIASES.
  const EXTRA_PAGES = ["Copper Ring"];
  const allItems = [...new Set([...(await categoryMembers("Items")), ...EXTRA_PAGES])];
  const workList = Number.isFinite(limit) ? allItems.slice(0, limit) : allItems;

  const out = [];
  for (let i = 0; i < workList.length; i++) {
    const title = workList[i];
    try {
      const text = await wikitext(title);
      // "{{:Does Not Exist}}" banner = wiki-documented but not actually in
      // the game (non-Legends content) — never list it.
      if (!/\{\{\s*:?\s*Does Not Exist\s*\}\}/i.test(text)) {
        const item = parseItem(title, text, zoneSet, zoneLevels);
        // Only equippable gear matters for BiS — skip stat-less non-equip pages.
        if (item.slots.length || item.dmg) out.push(item);
      }
    } catch (e) { /* skip broken pages */ }
    if (i % 25 === 0) process.stderr.write(`  ${i}/${workList.length}\r`);
    await sleep(120); // ~8 req/s — keep it gentle on the wiki
  }
  // Mob-level pass: zone level ranges are wide (Befallen is enterable at ~5
  // but its end mobs are ~25), so the specific droppers give a far tighter
  // "level to obtain" floor. Fetch each unique mob page once; min level wins.
  const mobNames = [...new Set(out.flatMap((i) => i._mobs))];
  process.stderr.write(`Fetching levels for ${mobNames.length} unique mobs...\n`);
  const mobLv = new Map();
  for (let i = 0; i < mobNames.length; i++) {
    try { mobLv.set(mobNames[i], await mobLevel(mobNames[i])); } catch (e) { /* skip */ }
    if (i % 25 === 0) process.stderr.write(`  ${i}/${mobNames.length}\r`);
    await sleep(120);
  }
  for (const it of out) {
    const floor = Math.min(...it._mobs.map((m) => mobLv.get(m)).filter(Boolean), Infinity);
    if (!it._explicitLevel && !it.vendors.length && Number.isFinite(floor)) it.level = Math.min(50, floor);
    delete it._mobs; delete it._explicitLevel;
  }

  // Shields the wiki doesn't label: shield icons are a small closed set (and
  // new content reuses the classic icon sheet), so propagate skill "Shield" to
  // AC'd no-damage Secondary items sharing an icon with a known shield.
  // Anything AC'd in Secondary still unclassified is likely a held item (orb,
  // totem, instrument) — warn so new content gets a human look, not silence.
  const shieldIcons = new Set(out.filter((i) => i.skill === "Shield" && i.icon).map((i) => i.icon));
  for (const it of out) {
    if (it.skill || it.dmg || !(it.ac > 0) || !it.slots.includes("Secondary")) continue;
    if (shieldIcons.has(it.icon)) it.skill = "Shield";
    else process.stderr.write(`possible unlabeled shield (AC'd Secondary, kept unclassified): ${it.name}\n`);
  }

  process.stderr.write(`\nDone. ${out.length} equippable items.\n`);
  out.sort((a, b) => a.name.localeCompare(b.name));
  console.log(JSON.stringify(out));
}

export { parseItem, parseStats, wikitext, categoryMembers, buildZones, mobLevel };
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => { console.error(e); process.exit(1); });
}
