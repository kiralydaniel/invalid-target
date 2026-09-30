#!/usr/bin/env python3
"""
Invalid Target - Mythic leaderboard builder
Pulls every Mythic boss pull of the season from the Warcraft Logs API (v2)
and writes a self-contained leaderboard page next to this script.

Needs: Python 3.8+ (no extra packages) and a free Warcraft Logs API client:
  https://www.warcraftlogs.com/api/clients  ->  "Create Client"

Run:   python wcl_leaderboard.py
It asks for the Client ID and Client Secret (or set WCL_CLIENT_ID / WCL_CLIENT_SECRET).
"""
import base64, getpass, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- settings
GUILD = "Invalid Target"
REALM = "ragnaros"          # realm slug
REGION = "eu"
SEASON_START = "2026-08-19"  # Midnight Season 2 (The Venomous Abyss), EU reset. Change for a new season.
MYTHIC = 5
DUPLICATE_WINDOW_MS = 20000  # two logs of the same pull start within this many ms of each other
OUT_BASE = "invalid-target"
# --------------------------------------------------------------------------

API = "https://www.warcraftlogs.com/api/v2/client"
TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"
HERE = os.path.dirname(os.path.abspath(__file__))


def get_token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=b"grant_type=client_credentials",
                                 headers={"Authorization": "Basic " + auth,
                                          "Content-Type": "application/x-www-form-urlencoded",
                                          "User-Agent": "invalid-target-leaderboard"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        sys.exit(f"Could not log in to the Warcraft Logs API ({e.code}). Check the Client ID and Secret.")


class WCL:
    def __init__(self, token):
        self.token = token

    def q(self, query, variables=None, tries=6):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(tries):
            req = urllib.request.Request(API, data=body, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                "User-Agent": "invalid-target-leaderboard"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    out = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 60 * (attempt + 1)
                    print(f"  rate limited, waiting {wait}s ...")
                    time.sleep(wait); continue
                if e.code >= 500 and attempt < tries - 1:
                    time.sleep(5 * (attempt + 1)); continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < tries - 1:
                    time.sleep(5 * (attempt + 1)); continue
                raise
            if out.get("errors") and not out.get("data"):
                raise RuntimeError(json.dumps(out["errors"])[:800])
            if out.get("errors"):
                print("  warning:", json.dumps(out["errors"])[:300])
            return out["data"]
        raise RuntimeError("Gave up after repeated errors")


Q_REPORTS = """
query($g:String!,$s:String!,$r:String!,$st:Float!,$page:Int!){
  reportData{ reports(guildName:$g, guildServerSlug:$s, guildServerRegion:$r, startTime:$st, limit:100, page:$page){
    has_more_pages
    data{ code title startTime endTime zone{ id name } }
  } }
}"""

Q_FIGHTS = """
query($code:String!){
  reportData{ report(code:$code){
    code startTime endTime
    fights(killType:Encounters){ id encounterID name difficulty kill startTime endTime bossPercentage fightPercentage friendlyPlayers }
    masterData{ actors(type:"Player"){ id name server subType } abilities{ gameID name } }
  } }
}"""

Q_DETAIL = """
query($code:String!,$ids:[Int]!,$kills:[Int]!,$end:Float!,$withRank:Boolean!){
  reportData{ report(code:$code){
    rankings(fightIDs:$kills) @include(if:$withRank)
    deathTable: table(dataType:Deaths, fightIDs:$ids, hostilityType:Friendlies, startTime:0, endTime:$end)
    events(dataType:Deaths, fightIDs:$ids, hostilityType:Friendlies, startTime:0, endTime:$end, limit:10000){ data nextPageTimestamp }
  } }
}"""

Q_EVENTS = """
query($code:String!,$ids:[Int]!,$st:Float!,$end:Float!){
  reportData{ report(code:$code){
    events(dataType:Deaths, fightIDs:$ids, hostilityType:Friendlies, startTime:$st, endTime:$end, limit:10000){ data nextPageTimestamp }
  } }
}"""


def as_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def ability_name(obj):
    if isinstance(obj, dict):
        return obj.get("name")
    return None


def main():
    cid = os.environ.get("WCL_CLIENT_ID") or input("Warcraft Logs Client ID: ").strip()
    secret = os.environ.get("WCL_CLIENT_SECRET") or getpass.getpass("Warcraft Logs Client Secret (hidden): ").strip()
    api = WCL(get_token(cid, secret))

    season_ms = int(datetime.strptime(SEASON_START, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    print(f"Listing {GUILD} ({REGION.upper()}-{REALM}) logs since {SEASON_START} ...")
    reports, page = [], 1
    while True:
        d = api.q(Q_REPORTS, {"g": GUILD, "s": REALM, "r": REGION, "st": season_ms, "page": page})
        rp = d["reportData"]["reports"]
        reports += rp["data"]
        if not rp["has_more_pages"]:
            break
        page += 1
    print(f"  {len(reports)} logs found")
    if not reports:
        sys.exit("No logs found. Check the guild name, realm and SEASON_START at the top of the script.")

    # 1) fights of every report
    detailed = []
    for i, rep in enumerate(reports, 1):
        d = api.q(Q_FIGHTS, {"code": rep["code"]})["reportData"]["report"]
        mythic = [f for f in (d.get("fights") or []) if f.get("difficulty") == MYTHIC and f.get("encounterID")]
        print(f"  [{i}/{len(reports)}] {rep['code']} {rep.get('title','')[:40]!r}: {len(mythic)} mythic pulls")
        if mythic:
            d["mythic"] = mythic
            detailed.append(d)

    # 2) drop duplicate logs of the same pull; fuller logs win
    detailed.sort(key=lambda r: len(r["mythic"]), reverse=True)
    starts = {}  # encounterID -> [abs start]
    for rep in detailed:
        keep = []
        for f in rep["mythic"]:
            abs_start = rep["startTime"] + f["startTime"]
            lst = starts.setdefault(f["encounterID"], [])
            if any(abs(abs_start - s) < DUPLICATE_WINDOW_MS for s in lst):
                continue
            lst.append(abs_start)
            keep.append(f)
        rep["keep"] = keep

    players, pidx = [], {}

    def player(name, server=None, cls=None):
        if name not in pidx:
            pidx[name] = len(players)
            players.append({"name": name, "server": server, "cls": cls})
        p = players[pidx[name]]
        if cls and not p["cls"]:
            p["cls"] = cls
        return pidx[name]

    pulls, parses = [], []
    todo = [r for r in detailed if r["keep"]]
    print(f"Reading deaths and parses from {len(todo)} logs ...")
    for i, rep in enumerate(todo, 1):
        code = rep["code"]
        actors = {a["id"]: a for a in (rep.get("masterData") or {}).get("actors") or []}
        abil = {a["gameID"]: a["name"] for a in (rep.get("masterData") or {}).get("abilities") or []}
        ids = [f["id"] for f in rep["keep"]]
        kills = [f["id"] for f in rep["keep"] if f.get("kill")]
        end = float(rep["endTime"] - rep["startTime"] + 1)
        d = api.q(Q_DETAIL, {"code": code, "ids": ids, "kills": kills or [0], "end": end,
                             "withRank": bool(kills)})["reportData"]["report"]
        events = list(d["events"]["data"] or [])
        nxt = d["events"].get("nextPageTimestamp")
        while nxt:
            e2 = api.q(Q_EVENTS, {"code": code, "ids": ids, "st": float(nxt), "end": end})["reportData"]["report"]["events"]
            events += e2["data"] or []
            nxt = e2.get("nextPageTimestamp")

        # killing-blow names from the deaths table, matched to events by player + time
        table = as_json(d.get("deathTable"))
        entries = (table.get("data") or table).get("entries") or []
        by_actor = {}
        for en in entries:
            nm = ability_name(en.get("killingBlow")) or ability_name(en.get("ability"))
            by_actor.setdefault(en.get("id"), []).append([en.get("deathTime"), nm, False])

        fights = {f["id"]: f for f in rep["keep"]}
        pull_of = {}
        for f in rep["keep"]:
            pid = f"{code}:{f['id']}"
            pl = []
            for aid in f.get("friendlyPlayers") or []:
                a = actors.get(aid)
                if a:
                    pl.append(player(a["name"], a.get("server"), a.get("subType")))
            pull = {"id": pid, "enc": f["encounterID"], "boss": f["name"],
                    "start": rep["startTime"] + f["startTime"], "dur": f["endTime"] - f["startTime"],
                    "kill": bool(f.get("kill")), "pct": f.get("bossPercentage"),
                    "players": sorted(set(pl)), "deaths": []}
            pulls.append(pull)
            pull_of[f["id"]] = pull

        for ev in sorted(events, key=lambda e: e.get("timestamp", 0)):
            if ev.get("type") != "death":
                continue
            a = actors.get(ev.get("targetID"))
            f = fights.get(ev.get("fight"))
            if not a or not f:
                continue
            ts = ev["timestamp"]
            if ts < f["startTime"] or ts > f["endTime"]:
                continue
            name = None
            for cand in by_actor.get(a["id"], []):
                if not cand[2] and cand[0] is not None and abs(cand[0] - ts) < 2000:
                    cand[2] = True; name = cand[1]; break
            if not name:
                gid = ev.get("killingAbilityGameID") or ev.get("abilityGameID")
                name = abil.get(gid) if gid else None
            pull_of[f["id"]]["deaths"].append({"p": player(a["name"], a.get("server"), a.get("subType")),
                                               "t": ts - f["startTime"], "a": name or "Unknown"})

        rk = as_json(d.get("rankings"))
        for fr in rk.get("data") or []:
            pull = pull_of.get(fr.get("fightID"))
            if not pull:
                continue
            for role, grp in (fr.get("roles") or {}).items():
                for ch in (grp or {}).get("characters") or []:
                    if ch.get("rankPercent") is None or not ch.get("name"):
                        continue
                    parses.append({"pull": pull["id"], "p": player(ch["name"], None, ch.get("class")),
                                   "role": role, "spec": ch.get("spec") or "", "pct": round(float(ch["rankPercent"]), 1)})
        n_d = sum(len(pull_of[x]["deaths"]) for x in pull_of)
        print(f"  [{i}/{len(todo)}] {code}: {len(ids)} pulls, {len(kills)} kills, {n_d} deaths")

    try:
        rl = api.q("{ rateLimitData{ limitPerHour pointsSpentThisHour } }")["rateLimitData"]
        print(f"API points used this hour: {rl['pointsSpentThisHour']:.0f} / {rl['limitPerHour']}")
    except Exception:
        pass

    for p in players:
        if p["cls"]:
            p["cls"] = p["cls"].replace(" ", "")
    data = {"guild": GUILD, "realm": REALM.capitalize(), "region": REGION, "seasonStart": SEASON_START,
            "generatedAt": int(time.time() * 1000), "players": players, "pulls": pulls, "parses": parses}
    out_dir = os.environ.get("OUT_DIR")  # set by the GitHub workflow: writes index.html for the website
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    js = os.path.join(out_dir or HERE, OUT_BASE + "-data.json")
    with open(js, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    page = TEMPLATE.replace("__WCL_DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
    html = os.path.join(out_dir, "index.html") if out_dir else os.path.join(HERE, OUT_BASE + "-leaderboard.html")
    with open(html, "w", encoding="utf-8") as fh:
        fh.write('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0">'
                 + page + "</body></html>")
    print(f"\nDone: {len(pulls)} pulls, {sum(p['kill'] for p in pulls)} kills, {len(players)} players.")
    print("Open:", html)
    print("Share file (send this to Claude to publish the page):", js)


TEMPLATE = r'''<title>Invalid Target Mythic Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cinzel:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one column of stacked boards under a sticky filter bar; tables scroll sideways on their own at phone width. */
:root{
  --bg:#eef0f4; --surface:#ffffff; --surface-2:#f5f6f9; --fg:#1b1f2a; --muted:#5d6475; --line:#d9dce4;
  --accent:#b35a00; --accent-soft:#fbe9d6; --danger:#b3261e; --bar:#c9cfdb;
  /* Warcraft Logs parse colours, darkened for a light ground */
  --p-grey:#6b6b6b; --p-green:#1a8f00; --p-blue:#0062c4; --p-purple:#8a2fc9; --p-orange:#c46200; --p-pink:#c9357f; --p-gold:#a88a1c;
  --font-display:"Cinzel",Georgia,serif; --font-body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --font-mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#10131b; --surface:#171b26; --surface-2:#1d2230; --fg:#e6e8ef; --muted:#9aa2b5; --line:#2a3042;
  --accent:#ff9a3c; --accent-soft:#3a2614; --danger:#ff7a70; --bar:#343c52;
  --p-grey:#9d9d9d; --p-green:#1eff00; --p-blue:#0070ff; --p-purple:#a335ee; --p-orange:#ff8000; --p-pink:#e268a8; --p-gold:#e5cc80;
  color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#10131b; --surface:#171b26; --surface-2:#1d2230; --fg:#e6e8ef; --muted:#9aa2b5; --line:#2a3042;
  --accent:#ff9a3c; --accent-soft:#3a2614; --danger:#ff7a70; --bar:#343c52;
  --p-grey:#9d9d9d; --p-green:#1eff00; --p-blue:#0070ff; --p-purple:#a335ee; --p-orange:#ff8000; --p-pink:#e268a8; --p-gold:#e5cc80;
  color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font-family:var(--font-body);font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding-inline:16px;padding-block:28px 64px;display:flex;flex-direction:column;gap:28px}
header.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px 24px}
.eyebrow{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:var(--font-display);font-weight:700;font-size:clamp(28px,5vw,42px);line-height:1.1;margin:4px 0 0;text-wrap:balance}
h2{font-family:var(--font-display);font-weight:600;font-size:22px;margin:0;text-wrap:balance}
.meta{color:var(--muted);font-size:13px;display:flex;flex-wrap:wrap;gap:4px 16px}
.meta b{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}
.bar{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);padding-block:10px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--surface);flex-wrap:wrap}
.seg button{font:inherit;font-size:13px;border:0;background:transparent;color:var(--muted);padding:7px 12px;cursor:pointer;border-right:1px solid var(--line)}
.seg button:last-child{border-right:0}
.seg button[aria-pressed="true"]{background:var(--accent-soft);color:var(--fg);font-weight:600;box-shadow:inset 0 -2px 0 var(--accent)}
.seg button:focus-visible,select:focus-visible,input:focus-visible,.sortable:focus-visible,tr.row:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
label.ctl{display:inline-flex;gap:8px;align-items:center;font-size:13px;color:var(--muted)}
select{font:inherit;font-size:13px;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--fg);max-width:100%}
.range{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
section.board{display:flex;flex-direction:column;gap:12px}
.board-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:10px 20px}
.board-head p{margin:4px 0 0;color:var(--muted);max-width:68ch}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:720px;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
th{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600;background:var(--surface-2)}
th.l,td.l{text-align:left}
tbody tr:last-child td{border-bottom:0}
.sortable{cursor:pointer;user-select:none}
.sortable[aria-sort="descending"]::after{content:" ↓";color:var(--accent)}
.sortable[aria-sort="ascending"]::after{content:" ↑";color:var(--accent)}
.rank{color:var(--muted);width:36px;font-family:var(--font-mono);font-size:12px}
.player{display:flex;align-items:center;gap:8px;font-weight:600}
.cls{width:4px;height:18px;border-radius:2px;flex:none}
.spec{color:var(--muted);font-weight:400;font-size:12px}
.num{font-family:var(--font-mono);font-size:13px}
.score{font-family:var(--font-mono);font-weight:500;font-size:15px}
.dim td{opacity:.55}
.p-grey{color:var(--p-grey)}.p-green{color:var(--p-green)}.p-blue{color:var(--p-blue)}.p-purple{color:var(--p-purple)}.p-orange{color:var(--p-orange)}.p-pink{color:var(--p-pink)}.p-gold{color:var(--p-gold)}
.meter{display:inline-block;width:70px;height:6px;background:var(--bar);border-radius:3px;vertical-align:middle;margin-left:8px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--danger)}
.chips{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-start}
.chip{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--surface-2);border:1px solid var(--line);white-space:nowrap}
.chip b{font-family:var(--font-mono);font-weight:500;color:var(--danger);margin-left:4px}
td.mech{white-space:normal;min-width:260px;text-align:left}
tr.row{cursor:pointer}
tr.detail td{background:var(--surface-2);text-align:left;white-space:normal}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:4px 24px;font-size:13px}
.detail-grid div{display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed var(--line);padding-block:3px}
.detail-grid span.b{color:var(--muted)}
.empty{padding:28px 16px;color:var(--muted);text-align:center}
.method{color:var(--muted);font-size:13px;max-width:78ch}
.method code{font-family:var(--font-mono);font-size:12px;background:var(--surface-2);padding:1px 5px;border-radius:4px;color:var(--fg)}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--muted)}
.legend span b{font-family:var(--font-mono);font-weight:500}
@media (prefers-reduced-motion:no-preference){.seg button{transition:background .15s}}
</style>

<div class="wrap">
  <header class="top">
    <div>
      <div class="eyebrow" id="sub">Mythic raid leaderboard</div>
      <h1 id="guild">Invalid Target</h1>
    </div>
    <div class="meta" id="meta"></div>
  </header>

  <div class="bar" role="toolbar" aria-label="Filters">
    <div class="seg" id="tf" aria-label="Timeframe">
      <button type="button" data-tf="season" aria-pressed="true">Season</button>
      <button type="button" data-tf="7d" aria-pressed="false">Last 7 days</button>
      <button type="button" data-tf="2d" aria-pressed="false">Last 2 days</button>
      <button type="button" data-tf="last" aria-pressed="false">Last raid</button>
    </div>
    <label class="ctl" for="boss">Boss <select id="boss"></select></label>
    <span class="range" id="range"></span>
  </div>

  <section class="board" aria-labelledby="h-perf">
    <div class="board-head">
      <div>
        <h2 id="h-perf">Performance</h2>
        <p>Score is each player's average Mythic parse, pulled toward the guild average until they have enough kills to stand on their own.</p>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center">
        <div class="seg" id="role" aria-label="Role">
          <button type="button" data-role="dps" aria-pressed="true">DPS</button>
          <button type="button" data-role="healers" aria-pressed="false">Healers</button>
          <button type="button" data-role="tanks" aria-pressed="false">Tanks</button>
        </div>
        <label class="ctl" for="pen"><input type="checkbox" id="pen"> Penalize deaths</label>
      </div>
    </div>
    <div class="panel"><div class="scroll"><table id="perf"></table></div></div>
    <div class="legend" aria-label="Parse colours">
      <span class="p-grey">■ <b>0–24</b></span><span class="p-green">■ <b>25–49</b></span><span class="p-blue">■ <b>50–74</b></span><span class="p-purple">■ <b>75–94</b></span><span class="p-orange">■ <b>95–98</b></span><span class="p-pink">■ <b>99</b></span><span class="p-gold">■ <b>100</b></span>
    </div>
  </section>

  <section class="board" aria-labelledby="h-deaths">
    <div class="board-head">
      <div>
        <h2 id="h-deaths">First to fall</h2>
        <p>How often each player was the 1st or 2nd death of a pull, wipes and kills alike, and what killed them. Click a row for the full breakdown.</p>
      </div>
      <label class="ctl" for="dsort">Sort by
        <select id="dsort"><option value="total">Times in first two</option><option value="rate">Share of pulls</option><option value="first">First deaths</option></select>
      </label>
    </div>
    <div class="panel"><div class="scroll"><table id="deaths"></table></div></div>
  </section>

  <section class="board" aria-labelledby="h-mech">
    <div class="board-head">
      <div>
        <h2 id="h-mech">Mechanics behind early deaths</h2>
        <p>Every ability that landed a 1st or 2nd death in the selected pulls.</p>
      </div>
    </div>
    <div class="panel"><div class="scroll"><table id="mechs"></table></div></div>
  </section>

  <p class="method" id="method"></p>
</div>

<script id="wcl-data" type="application/json">__WCL_DATA__</script>
<script>
(function(){
"use strict";
var D;
try{D=JSON.parse(document.getElementById("wcl-data").textContent)}catch(e){D=null}
if(!D||!D.pulls){document.querySelector(".wrap").innerHTML='<div class="empty">No data in this page. Run the fetch script to generate it.</div>';return}

var C_PRIOR=8, ALPHA=10, RAID_GAP=5*3600e3, MIN_KILLS=3;
var CLASS={DeathKnight:"#C41E3A",DemonHunter:"#A330C9",Druid:"#FF7C0A",Evoker:"#33937F",Hunter:"#AAD372",Mage:"#3FC7EB",Monk:"#00FF98",Paladin:"#F48CBA",Priest:"#D8D8D8",Rogue:"#FFF468",Shaman:"#0070DD",Warlock:"#8788EE",Warrior:"#C69B6D"};
var state={tf:"season",boss:"all",role:"dps",pen:false,dsort:"total",open:{}};
try{var saved=JSON.parse(localStorage.getItem("wcl-board")||"{}");["tf","role","dsort"].forEach(function(k){if(saved[k])state[k]=saved[k]});state.pen=!!saved.pen}catch(e){}
function save(){try{localStorage.setItem("wcl-board",JSON.stringify({tf:state.tf,role:state.role,dsort:state.dsort,pen:state.pen}))}catch(e){}}

var players=D.players, pulls=D.pulls.slice().sort(function(a,b){return a.start-b.start});
// group pulls into raid nights
var night=0; pulls.forEach(function(p,i){if(i&&p.start-pulls[i-1].start>RAID_GAP)night++;p.night=night});
var lastNight=pulls.length?pulls[pulls.length-1].night:0;
var pullById={}; pulls.forEach(function(p){pullById[p.id]=p});

function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]})}
function pc(v){v=Math.floor(v);return v>=100?"p-gold":v>=99?"p-pink":v>=95?"p-orange":v>=75?"p-purple":v>=50?"p-blue":v>=25?"p-green":"p-grey"}
function fmtDate(ms){return new Date(ms).toLocaleDateString(undefined,{month:"short",day:"numeric"})}
function fmtDT(ms){return new Date(ms).toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"})}
function pname(i){return players[i]?players[i].name:"?"}
function pcol(i){return CLASS[(players[i]&&players[i].cls)||""]||"var(--muted)"}
function playerCell(i,spec){return '<span class="player"><span class="cls" style="background:'+pcol(i)+'"></span>'+esc(pname(i))+(spec?' <span class="spec">'+esc(spec)+'</span>':'')+'</span>'}
function mean(a){return a.reduce(function(s,x){return s+x},0)/a.length}
function median(a){var b=a.slice().sort(function(x,y){return x-y}),m=b.length>>1;return b.length%2?b[m]:(b[m-1]+b[m])/2}
function sd(a){if(a.length<2)return 0;var m=mean(a);return Math.sqrt(a.reduce(function(s,x){return s+(x-m)*(x-m)},0)/(a.length-1))}

// boss select
var bosses=[],seen={};
pulls.forEach(function(p){if(!seen[p.enc]){seen[p.enc]=1;bosses.push({enc:p.enc,name:p.boss,order:p.start})}});
var bsel=document.getElementById("boss");
bsel.innerHTML='<option value="all">All bosses</option>'+bosses.map(function(b){return '<option value="'+b.enc+'">'+esc(b.name)+'</option>'}).join("");

function inScope(p){
  if(state.boss!=="all"&&String(p.enc)!==state.boss)return false;
  if(state.tf==="7d")return p.start>=D.generatedAt-7*864e5;
  if(state.tf==="2d")return p.start>=D.generatedAt-2*864e5;
  if(state.tf==="last")return p.night===lastNight;
  return true;
}

function renderMeta(scope){
  document.getElementById("guild").textContent=D.guild;
  document.getElementById("sub").textContent=(D.region||"").toUpperCase()+"-"+D.realm+" · Mythic";
  var kills=scope.filter(function(p){return p.kill}).length;
  var nights={};scope.forEach(function(p){nights[p.night]=1});
  document.getElementById("meta").innerHTML=
    '<span><b>'+scope.length+'</b> pulls</span><span><b>'+kills+'</b> kills</span><span><b>'+Object.keys(nights).length+'</b> raid nights</span><span>Data as of <b>'+fmtDT(D.generatedAt)+'</b></span>';
  document.getElementById("range").textContent=scope.length?fmtDate(scope[0].start)+" – "+fmtDate(scope[scope.length-1].start):"";
}

function renderPerf(scope){
  var ok={};scope.forEach(function(p){if(p.kill)ok[p.id]=1});
  var rows={},all=[];
  D.parses.forEach(function(r){
    if(!ok[r.pull]||r.role!==state.role||r.pct==null)return;
    var k=r.p; if(!rows[k])rows[k]={p:k,v:[],specs:{}};
    rows[k].v.push(r.pct); rows[k].specs[r.spec]=(rows[k].specs[r.spec]||0)+1; all.push(r.pct);
  });
  var el=document.getElementById("perf");
  if(!all.length){el.innerHTML='<tbody><tr><td class="empty">No ranked Mythic kills for this role in the selected pulls.</td></tr></tbody>';return}
  var mu=mean(all);
  // deaths per kill (all deaths, not only first two)
  var dk={};
  scope.forEach(function(p){if(!p.kill)return;p.deaths.forEach(function(d){dk[d.p]=(dk[d.p]||0)+1})});
  var list=Object.keys(rows).map(function(k){
    var r=rows[k],n=r.v.length,s=(r.v.reduce(function(a,b){return a+b},0)+C_PRIOR*mu)/(n+C_PRIOR);
    var dpk=(dk[r.p]||0)/n;
    var spec=Object.keys(r.specs).sort(function(a,b){return r.specs[b]-r.specs[a]})[0];
    return {p:r.p,n:n,avg:mean(r.v),med:median(r.v),best:Math.max.apply(null,r.v),sd:sd(r.v),dpk:dpk,spec:spec,score:state.pen?s-ALPHA*dpk:s};
  }).sort(function(a,b){return b.score-a.score});
  el.innerHTML='<thead><tr><th class="l">#</th><th class="l">Player</th><th>Kills</th><th>Avg parse</th><th>Median</th><th>Best</th><th title="Standard deviation of parses; lower is steadier">Spread ±</th><th>Deaths / kill</th><th>Score</th></tr></thead><tbody>'+
    list.map(function(r,i){
      return '<tr'+(r.n<MIN_KILLS?' class="dim" title="Fewer than '+MIN_KILLS+' kills: score is mostly the guild average"':'')+'><td class="l rank">'+(i+1)+'</td><td class="l">'+playerCell(r.p,r.spec)+'</td><td class="num">'+r.n+'</td>'+
      '<td class="num '+pc(r.avg)+'">'+r.avg.toFixed(1)+'</td><td class="num '+pc(r.med)+'">'+r.med.toFixed(0)+'</td><td class="num '+pc(r.best)+'">'+r.best.toFixed(0)+'</td>'+
      '<td class="num">'+r.sd.toFixed(1)+'</td><td class="num">'+r.dpk.toFixed(2)+'</td><td class="score '+pc(Math.max(0,r.score))+'">'+r.score.toFixed(1)+'</td></tr>';
    }).join("")+'</tbody>';
}

function renderDeaths(scope){
  var stats={},mech={},attended={};
  scope.forEach(function(p){
    p.players.forEach(function(i){attended[i]=(attended[i]||0)+1});
    p.deaths.slice(0,2).forEach(function(d,idx){
      var s=stats[d.p]||(stats[d.p]={p:d.p,first:0,second:0,m:{}});
      if(idx===0)s.first++;else s.second++;
      var key=d.a||"Unknown";
      var mk=key+"\u0000"+p.boss;
      s.m[mk]=(s.m[mk]||0)+1;
      var g=mech[mk]||(mech[mk]={a:key,boss:p.boss,n:0,first:0,who:{}});
      g.n++; if(idx===0)g.first++; g.who[d.p]=(g.who[d.p]||0)+1;
    });
  });
  var list=Object.keys(stats).map(function(k){var s=stats[k];s.total=s.first+s.second;s.pulls=attended[s.p]||s.total;s.rate=s.total/s.pulls;return s});
  var key=state.dsort;
  list.sort(function(a,b){return (key==="rate"?b.rate-a.rate:key==="first"?b.first-a.first:b.total-a.total)||b.total-a.total||b.rate-a.rate});
  var maxRate=Math.max.apply(null,list.map(function(s){return s.rate}).concat([0.0001]));
  var el=document.getElementById("deaths");
  if(!list.length){el.innerHTML='<tbody><tr><td class="empty">No deaths in the selected pulls.</td></tr></tbody>'}
  else el.innerHTML='<thead><tr><th class="l">#</th><th class="l">Player</th><th>Pulls</th><th>1st death</th><th>2nd death</th><th>Total</th><th>Share of pulls</th><th class="l">Killed by</th></tr></thead><tbody>'+
    list.map(function(s,i){
      var ms=Object.keys(s.m).sort(function(a,b){return s.m[b]-s.m[a]});
      var chips=ms.slice(0,3).map(function(k){var a=k.split("\u0000");return '<span class="chip" title="'+esc(a[1])+'">'+esc(a[0])+'<b>'+s.m[k]+'</b></span>'}).join("")+(ms.length>3?'<span class="chip">+'+(ms.length-3)+' more</span>':'');
      var open=!!state.open[s.p];
      var row='<tr class="row" tabindex="0" aria-expanded="'+open+'" data-p="'+s.p+'"><td class="l rank">'+(i+1)+'</td><td class="l">'+playerCell(s.p)+'</td><td class="num">'+s.pulls+'</td><td class="num">'+s.first+'</td><td class="num">'+s.second+'</td><td class="num"><b>'+s.total+'</b></td>'+
        '<td class="num">'+(s.rate*100).toFixed(1)+'%<span class="meter" aria-hidden="true"><i style="width:'+(s.rate/maxRate*100).toFixed(1)+'%"></i></span></td><td class="mech"><div class="chips">'+chips+'</div></td></tr>';
      if(open)row+='<tr class="detail"><td colspan="8"><div class="detail-grid">'+ms.map(function(k){var a=k.split("\u0000");return '<div><span>'+esc(a[0])+' <span class="b">· '+esc(a[1])+'</span></span><b class="num">'+s.m[k]+'</b></div>'}).join("")+'</div></td></tr>';
      return row;
    }).join("")+'</tbody>';

  var ml=Object.keys(mech).map(function(k){return mech[k]}).sort(function(a,b){return b.n-a.n});
  var tot=ml.reduce(function(s,m){return s+m.n},0)||1;
  var me=document.getElementById("mechs");
  if(!ml.length){me.innerHTML='<tbody><tr><td class="empty">No early deaths in the selected pulls.</td></tr></tbody>';return}
  me.innerHTML='<thead><tr><th class="l">Mechanic</th><th class="l">Boss</th><th>1st death</th><th>Early deaths</th><th>Share</th><th class="l">Most often</th></tr></thead><tbody>'+
    ml.slice(0,40).map(function(m){
      var who=Object.keys(m.who).sort(function(a,b){return m.who[b]-m.who[a]}).slice(0,3).map(function(p){return esc(pname(+p))+' ('+m.who[p]+')'}).join(", ");
      return '<tr><td class="l"><b>'+esc(m.a)+'</b></td><td class="l">'+esc(m.boss)+'</td><td class="num">'+m.first+'</td><td class="num">'+m.n+'</td><td class="num">'+(m.n/tot*100).toFixed(1)+'%<span class="meter" aria-hidden="true"><i style="width:'+(m.n/ml[0].n*100).toFixed(1)+'%"></i></span></td><td class="l">'+who+'</td></tr>';
    }).join("")+'</tbody>';
}

function render(){
  document.querySelectorAll("#tf button").forEach(function(b){b.setAttribute("aria-pressed",String(b.dataset.tf===state.tf))});
  document.querySelectorAll("#role button").forEach(function(b){b.setAttribute("aria-pressed",String(b.dataset.role===state.role))});
  document.getElementById("pen").checked=state.pen;
  document.getElementById("dsort").value=state.dsort;
  bsel.value=state.boss;
  var scope=pulls.filter(inScope);
  renderMeta(scope); renderPerf(scope); renderDeaths(scope);
  document.getElementById("method").innerHTML='Score = <code>(sum of parses + '+C_PRIOR+' × guild average) ÷ (kills + '+C_PRIOR+')</code>, using Warcraft Logs rank percentiles (DPS for damage dealers and tanks, HPS for healers) on Mythic kills only. '+
    'With “Penalize deaths” on, '+ALPHA+' points are subtracted per death per kill. Duplicate logs of the same pull are counted once. Players with fewer than '+MIN_KILLS+' kills are greyed out. “Last raid” is the most recent raid night (pulls less than 5 hours apart), and the day windows count back from when the data was pulled.';
}

document.getElementById("tf").addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;state.tf=b.dataset.tf;state.open={};save();render()});
document.getElementById("role").addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;state.role=b.dataset.role;save();render()});
document.getElementById("pen").addEventListener("change",function(e){state.pen=e.target.checked;save();render()});
document.getElementById("dsort").addEventListener("change",function(e){state.dsort=e.target.value;save();render()});
bsel.addEventListener("change",function(e){state.boss=e.target.value;state.open={};render()});
function toggle(tr){var p=tr.dataset.p;state.open[p]=!state.open[p];render()}
document.getElementById("deaths").addEventListener("click",function(e){var tr=e.target.closest("tr.row");if(tr)toggle(tr)});
document.getElementById("deaths").addEventListener("keydown",function(e){if(e.key!=="Enter"&&e.key!==" ")return;var tr=e.target.closest("tr.row");if(tr){e.preventDefault();toggle(tr);var n=document.querySelector('#deaths tr.row[data-p="'+tr.dataset.p+'"]');if(n)n.focus()}});
render();
})();
</script>
'''

if __name__ == "__main__":
    main()
