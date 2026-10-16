"""
Inferno: Kalshi favorite-closer bot with live-game verification + local dashboard.

A bet is placed ONLY if ALL of these pass:
  1. YES ask is within MIN_PRICE..MAX_PRICE cents
  2. The game is LIVE on ESPN's scoreboard (matched by team; no match = no bet)
  3. The team is leading by at least the per-sport margin, late enough in the game
  4. The bot has been started (Start button), and exposure limits aren't hit

Dashboard: http://127.0.0.1:8000  (pause/resume button = kill switch)
Install:  pip install requests cryptography
Defaults are SAFE: demo environment + DRY_RUN=True.
"""
import ast, base64, json, math, os, re, sys, threading, time, uuid, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------------- CONFIG ----------------
# Basic settings (stake, prices, demo/dry-run, keys) live in config.json
# and can be edited from the dashboard's Settings panel.
if getattr(sys, "frozen", False):  # packaged app: keep your files in ~/KalshiBot
    HERE = os.path.join(os.path.expanduser("~"), "KalshiBot")
    os.makedirs(HERE, exist_ok=True)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
LEAGUES = ["NBA", "NFL", "MLB", "NHL", "WNBA", "NCAAF", "NCAAMB", "NCAAWB",
           "SOCCER", "TENNIS", "BTC", "M15", "OTHER"]
DEFAULTS = {"use_demo": True, "dry_run": True, "api_key_id": "",
            "private_key_path": "kalshi_private_key.pem", "stake_usd": 5.0,
            "max_open_bets": 5, "min_price": 90, "max_price": 98,
            "max_minutes_to_close": 360.0, "poll_seconds": 30, "paper_bankroll": 100.0,
            "leagues": list(LEAGUES),
            "min_leads": {"NBA": 12, "NFL": 14, "NHL": 2, "MLB": 3,
                          "WNBA": 10, "NCAAF": 14, "NCAAMB": 10, "NCAAWB": 10},
            "btc_min_sigma": 2.0, "btc_max_minutes": 60, "m15_max_minutes": 2.0,
            "view_hours": 24.0, "stop_loss_on": True, "stop_loss_pct": 60.0,
            "risk": "balanced", "trade_other": False,
            "bet_style": "parlay", "parlay_legs": 3, "leg_min_prob": 90.0, "parlay_min_prob": 80.0,
            "update_url": ""}
VERSION = "4.0"

# Kalshi series ticker -> (ESPN scoreboard path, league name)
SERIES = {
    "KXNBAGAME": ("basketball/nba", "NBA"),
    "KXNFLGAME": ("football/nfl", "NFL"),
    "KXMLBGAME": ("baseball/mlb", "MLB"),
    "KXNHLGAME": ("hockey/nhl", "NHL"),
    "KXWNBAGAME": ("basketball/wnba", "WNBA"),
    "KXNCAAFGAME": ("football/college-football", "NCAAF"),
    "KXNCAAMBGAME": ("basketball/mens-college-basketball", "NCAAMB"),
    "KXNCAAWBGAME": ("basketball/womens-college-basketball", "NCAAWB"),
    # Soccer (match-winner markets). Confirm series tickers on kalshi.com.
    "KXEPLGAME": ("soccer/eng.1", "SOCCER"),
    "KXUCLGAME": ("soccer/uefa.champions", "SOCCER"),
    "KXLALIGAGAME": ("soccer/esp.1", "SOCCER"),
    "KXBUNDESLIGAGAME": ("soccer/ger.1", "SOCCER"),
    "KXSERIEAGAME": ("soccer/ita.1", "SOCCER"),
    "KXLIGUE1GAME": ("soccer/fra.1", "SOCCER"),
    "KXMLSGAME": ("soccer/usa.1", "SOCCER"),
    # Totals ("over X points/goals"). Tickers follow Kalshi's naming; a wrong one just loads nothing.
    "KXNFLTOTAL": ("football/nfl", "NFL"), "KXNBATOTAL": ("basketball/nba", "NBA"),
    "KXMLBTOTAL": ("baseball/mlb", "MLB"), "KXNHLTOTAL": ("hockey/nhl", "NHL"),
    "KXWNBATOTAL": ("basketball/wnba", "WNBA"), "KXNCAAFTOTAL": ("football/college-football", "NCAAF"),
    "KXNCAAMBTOTAL": ("basketball/mens-college-basketball", "NCAAMB"),
    "KXEPLTOTAL": ("soccer/eng.1", "SOCCER"), "KXUCLTOTAL": ("soccer/uefa.champions", "SOCCER"),
    "KXLALIGATOTAL": ("soccer/esp.1", "SOCCER"), "KXBUNDESLIGATOTAL": ("soccer/ger.1", "SOCCER"),
    "KXSERIEATOTAL": ("soccer/ita.1", "SOCCER"), "KXLIGUE1TOTAL": ("soccer/fra.1", "SOCCER"),
    "KXMLSTOTAL": ("soccer/usa.1", "SOCCER"),
    # Tennis match winners
    "KXATPMATCH": ("tennis/atp", "TENNIS"),
    "KXWTAMATCH": ("tennis/wta", "TENNIS"),
    # Bitcoin hourly / daily price markets
    "KXBTCD": (None, "BTC"),
    "KXBTC": (None, "BTC"),
    # 15-minute up/down markets. This is only the built-in starting list: Inferno also asks
    # Kalshi for EVERY series with frequency "fifteen_min" and adds them automatically.
    "KXBTC15M": (None, "M15"),
    "KXETH15M": (None, "M15"),
    "KXSOL15M": (None, "M15"),
    "KXXRP15M": (None, "M15"),
}
# Live-game rules: must be in this period or later, within max_clock_s seconds
# left in the period (None = ignore clock), leading by min_lead or more.
RULES = {
    "NBA": dict(min_period=4, max_clock_s=300, min_lead=12, regulation=4),
    "NFL": dict(min_period=4, max_clock_s=360, min_lead=14, regulation=4),
    "NHL": dict(min_period=3, max_clock_s=480, min_lead=2, regulation=3),
    "MLB": dict(min_period=8, max_clock_s=None, min_lead=3, regulation=9),
    "WNBA": dict(min_period=4, max_clock_s=300, min_lead=10, regulation=4),
    "NCAAF": dict(min_period=4, max_clock_s=360, min_lead=14, regulation=4),
    "NCAAMB": dict(min_period=2, max_clock_s=300, min_lead=10, regulation=2),
    "NCAAWB": dict(min_period=4, max_clock_s=300, min_lead=10, regulation=4),
}
# ESPN abbreviation -> Kalshi abbreviation, where they differ (verify yours!)
ALIAS = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA",
         "WSH": "WAS", "TB": "TB", "SJ": "SJS", "LA": "LAK", "NJ": "NJD"}

STATE_FILE = os.path.join(HERE, "bought.json")
DASH_PORT = 8000
# ----------------------------------------

CFG = dict(DEFAULTS)
PRIVATE_KEY = None
BASE = ""
STATE = {"running": False, "balance": None, "positions": [], "candidates": [],
         "log": [], "last_scan": None, "mode": "", "setup_error": None,
         "markets": [], "spots": {}, "version": VERSION}


def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(CFG, f, indent=2)


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            saved = json.load(open(CONFIG_FILE))
            CFG.update({k: v for k, v in saved.items() if k in DEFAULTS})
            CFG["min_leads"] = {k: v for k, v in {**DEFAULTS["min_leads"], **CFG["min_leads"]}.items()
                                if k in DEFAULTS["min_leads"]}
            if "risk" not in saved:        # upgrading from an older version
                CFG["leagues"] = list(CFG["leagues"]) + [x for x in LEAGUES if x not in CFG["leagues"]]
                if CFG["max_price"] == 95:
                    CFG["max_price"] = 98
        except Exception:
            pass
    else:
        save_config()


def apply_config():
    global PRIVATE_KEY, BASE
    demo_server = CFG["use_demo"] and not CFG["dry_run"]
    BASE = ("https://demo-api.kalshi.co" if demo_server
            else "https://api.elections.kalshi.com") + "/trade-api/v2"
    STATE["mode"] = (f"{'DEMO' if CFG['use_demo'] else 'PRODUCTION'} / "
                     f"{'DRY RUN' if CFG['dry_run'] else 'LIVE ORDERS'}")
    PRIVATE_KEY, STATE["setup_error"] = None, None
    path = CFG["private_key_path"]
    if not os.path.isabs(path):
        path = os.path.join(HERE, path)
    try:
        if not CFG["api_key_id"]:
            raise ValueError("enter your Kalshi API key ID in Settings below")
        with open(path, "rb") as f:
            PRIVATE_KEY = serialization.load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        STATE["setup_error"] = f"Setup needed: private key file not found at {path}"
    except Exception as e:
        STATE["setup_error"] = f"Setup needed: {e}"
    for lg, n in CFG["min_leads"].items():
        if lg in RULES:
            RULES[lg]["min_lead"] = n


def update_config(new):
    clean = {}
    for k, v in new.items():
        if k not in DEFAULTS:
            return f"unknown setting: {k}"
        t = type(DEFAULTS[k])
        if t is bool and not isinstance(v, bool):
            return f"{k} must be true/false"
        if t is str and not isinstance(v, str):
            return f"{k} must be text"
        if t in (int, float):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return f"{k} must be a number"
            v = t(v)
        if k == "leagues":
            if not isinstance(v, list) or any(x not in LEAGUES for x in v):
                return "leagues must be a list of known market types"
        if k == "min_leads":
            if not isinstance(v, dict) or set(v) != set(DEFAULTS["min_leads"]) or \
                    any(isinstance(x, bool) or not isinstance(x, (int, float)) or x < 0 for x in v.values()):
                return "min_leads needs a number >= 0 for each league"
        if k == "update_url" and v and not (isinstance(v, str) and v.startswith("https://") and " " not in v):
            return "the update link must start with https:// and have no spaces"
        if k == "bet_style" and v not in ("parlay", "single"):
            return "bet_style must be parlay or single"
        if k == "risk" and v not in ("safe", "balanced", "aggressive"):
            return "risk must be safe, balanced or aggressive"
        clean[k] = v
    m = {**CFG, **clean}
    if not (1 <= m["min_price"] <= m["max_price"] <= 99):
        return "need 1 <= min price <= max price <= 99"
    if not (2 <= m["parlay_legs"] <= 5) or not (50 <= m["leg_min_prob"] <= 99) or not (30 <= m["parlay_min_prob"] <= 99):
        return "parlay: 2 to 5 legs, each leg at least 50% and the whole parlay at least 30% (both at most 99%)"
    if not (1 <= m["stop_loss_pct"] <= 99):
        return "stop-loss must be between 1 and 99 percent"
    if m["stake_usd"] <= 0 or m["paper_bankroll"] <= 0 or m["btc_min_sigma"] <= 0 or m["btc_max_minutes"] < 1 or m["m15_max_minutes"] < 1 or m["view_hours"] < 1 or m["max_minutes_to_close"] < 1 or m["max_open_bets"] < 1 or m["poll_seconds"] < 10:
        return "check your numbers: bet size above 0, at least 1 open bet, scan at least 10 seconds, at least 1 minute left"
    if (m["dry_run"], m["use_demo"]) != (CFG["dry_run"], CFG["use_demo"]) and STATE["running"]:
        STATE["running"] = False
        log("Mode changed: bot stopped. Press Start when you are ready.")
    CFG.update(m)
    save_config()
    apply_config()
    return None


def do_update():
    """Download the newest bot.py from update_url, check it, keep a backup and restart. Returns (ok, message)."""
    url = CFG.get("update_url", "").strip()
    if getattr(sys, "frozen", False):
        return False, "This is the packaged .exe/.app, which can't replace itself. Run Inferno from bot.py (start.bat) to use Update."
    if not url:
        return False, "Add your update link in Settings first."
    try:
        r = requests.get(url, timeout=20, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        new = r.content
    except Exception as e:
        return False, f"Could not download the update: {e}"
    if len(new) > 5_000_000 or len(new) < 20_000:
        return False, "The downloaded file does not look like Inferno (wrong size). Nothing was changed."
    try:
        text = new.decode("utf-8")
        ast.parse(text)
    except Exception as e:
        return False, f"The downloaded file is not valid Python ({e}). Nothing was changed."
    if not all(x in text for x in ("class Handler", "def update_config", 'PAGE = r"""', "def do_update")):
        return False, "The downloaded file is not a full Inferno (missing parts). Nothing was changed."
    me = os.path.abspath(__file__)
    old = open(me, "rb").read()
    if old.replace(b"\r\n", b"\n") == new.replace(b"\r\n", b"\n"):
        return False, "Already up to date."
    STATE["running"] = False
    with open(me + ".bak", "wb") as f:
        f.write(old)
    tmp = me + ".new"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, me)
    m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    log(f"Updated to version {m.group(1) if m else '?'}; restarting. Backup saved as bot.py.bak")
    threading.Timer(1.0, restart_app).start()
    return True, f"Updated to version {m.group(1) if m else '?'}. Restarting... the page reloads by itself. The bot is stopped: press Start again."


def restart_app():
    os.environ["INFERNO_RESTART"] = "1"
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


def log(msg):
    line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
    try:
        print(line)
    except Exception:
        pass
    STATE["log"] = ([line] + STATE["log"])[:100]


def headers(method, path):
    if PRIVATE_KEY is None:
        raise RuntimeError("private key not loaded")
    ts = str(int(time.time() * 1000))
    msg = (ts + method + "/trade-api/v2" + path.split("?")[0]).encode()
    sig = PRIVATE_KEY.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                           salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": CFG["api_key_id"], "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json"}


def api(method, path, body=None):
    r = requests.request(method, BASE + path, headers=headers(method, path) if PRIVATE_KEY else {"Content-Type": "application/json"},
                         data=json.dumps(body) if body else None, timeout=15)
    r.raise_for_status()
    return r.json()


def cents(m, field):
    if m.get(field) is not None:
        return int(round(float(m[field])))
    d = m.get(field + "_dollars")
    return int(round(float(d) * 100)) if d is not None else None


def load_state():
    try:
        return set(json.load(open(STATE_FILE)))
    except Exception:
        return set()


def _t(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def hours_to_close(m):
    """Hours until the market ends. Uses the EARLIER of close_time and expected_expiration_time:
    for sports, Kalshi's close_time can be weeks after the game; the real end is expected_expiration_time."""
    ts = [t for t in (_t(m.get("close_time")), _t(m.get("expected_expiration_time"))) if t]
    if not ts:
        return 1e9
    return (min(ts) - datetime.now(timezone.utc)).total_seconds() / 3600


def minutes_left(m, league):
    """Minutes until the market ends, or None when unknown (sports without expected_expiration_time)."""
    if league in SPORT_LEAGUES and not _t(m.get("expected_expiration_time")):
        return None
    h = hours_to_close(m)
    return None if h >= 1e7 else h * 60


# ---------- live game data (ESPN's unofficial public scoreboard) ----------
def norm(a):
    return ALIAS.get(a.upper(), a.upper())


def live_games(path):
    """Returns {team_abbr: {state, period, clock_s, lead}} for today's games."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    out = {}
    for ev in requests.get(url, timeout=10).json().get("events", []):
        st = ev["status"]
        comps = ev["competitions"][0]["competitors"]
        if len(comps) != 2:
            continue
        try:
            scores = [int(c["score"]) for c in comps]
        except Exception:
            continue
        for i, c in enumerate(comps):
            out[norm(c["team"]["abbreviation"])] = {
                "state": st["type"]["state"],  # pre / in / post
                "period": st.get("period", 0),
                "clock_s": st.get("clock", 0),
                "lead": scores[i] - scores[1 - i],
                "label": ev.get("shortName", ""),
                "clock": st.get("displayClock", ""),
                "gid": ev.get("id"), "total": sum(scores),
                "abbr": str(c["team"].get("abbreviation", "")).lower(),
                "keys": sorted({k.lower() for k in (c["team"].get("displayName"), c["team"].get("shortDisplayName"),
                                                    c["team"].get("name")) if k}),
            }
    return out


def verify(league, game):
    """Fail closed: returns (ok, reason)."""
    if game is None:
        return False, "no matching live game found"
    if league == "TENNIS":
        return verify_tennis(game)
    if league == "SOCCER":
        return verify_soccer(game)
    r = RULES[league]
    if game["state"] != "in":
        return False, f"game state is '{game['state']}', not live"
    if game["period"] < r["min_period"]:
        return False, f"too early (period {game['period']})"
    if game["period"] > r["regulation"]:
        return False, "overtime/extra innings: skipped"
    if r["max_clock_s"] is not None and game["period"] == r["min_period"] \
            and game["clock_s"] > r["max_clock_s"]:
        return False, f"too much time left ({int(game['clock_s'])}s)"
    if r.get("min_minute") and game.get("minute", 0) < r["min_minute"]:
        return False, f"too early (minute {game.get('minute', 0)})"
    if game["lead"] < r["min_lead"]:
        return False, f"lead {game['lead']} < required {r['min_lead']}"
    return True, f"live, period {game['period']}, leading by {game['lead']}"


# ---------- more markets: soccer, tennis, crypto (hourly/daily + every 15-minute market) ----------
US_LEAGUES = ("NBA", "NFL", "MLB", "NHL", "WNBA", "NCAAF", "NCAAMB", "NCAAWB")
SPORT_LEAGUES = US_LEAGUES + ("SOCCER", "TENNIS")
CRYPTO_LEAGUES = ("BTC", "M15")
# Rough annualized volatility per asset, used only for the probability estimate.
VOL = {"BTC": 0.55, "ETH": 0.75, "SOL": 1.0, "XRP": 1.0, "DOGE": 1.2, "BNB": 0.7}
VOL_DEFAULT = 1.1
WARNED = set()
DYN = {}                       # 15-minute series found on Kalshi at runtime
DISC = {"ts": 0.0}


def warn_once(msg):
    if msg not in WARNED:
        WARNED.add(msg)
        log("warning: " + msg)


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def px(v):
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}" if v >= 1 else f"{v:,.5f}"


def asset_of(series):
    """KXBTC15M -> BTC, KXETHD -> ETH, KXSOL15M -> SOL."""
    s = series[2:] if series.startswith("KX") else series
    for suf in ("15M", "D"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)]
    return s


def discover_series():
    """Ask Kalshi for series we should also scan (refreshed hourly):
    every 15-minute series, plus any soccer / tennis match-winner series not in the built-in list."""
    if time.time() - DISC["ts"] < 3600:
        return
    DISC["ts"] = time.time()
    try:
        data = api("GET", "/series")
    except Exception as e:
        warn_once(f"could not list Kalshi series (using the built-in list): {e}")
        DISC["ts"] = time.time() - 3000      # retry in ~10 minutes
        return
    DYN.clear()
    n15 = ns = nt = 0
    for s in data.get("series", []):
        t = s.get("ticker", "")
        if not t:
            continue
        tags = {str(x).lower() for x in (s.get("tags") or [])}
        sports = str(s.get("category", "")).lower() == "sports"
        if s.get("frequency") == "fifteen_min":
            n15 += 1
            if t not in SERIES:
                DYN[t] = (None, "M15")
        elif t in SERIES:
            continue
        elif sports and "soccer" in tags and t.endswith("GAME"):
            DYN[t] = ("soccer/all", "SOCCER")
            ns += 1
        elif sports and "tennis" in tags and t.endswith("MATCH"):
            DYN[t] = ("tennis/all", "TENNIS")
            nt += 1
    log(f"Discovered on Kalshi: {n15} fifteen-minute series, {ns} extra soccer, {nt} extra tennis")


def _clock_min(st):
    m = re.match(r"\s*(\d+)", st.get("displayClock") or "")
    return int(m.group(1)) if m else int((st.get("clock") or 0) // 60)


def soccer_games(path):
    """List of per-team entries for today's soccer matches (ESPN)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    out = []
    for ev in requests.get(url, timeout=10).json().get("events", []):
        st = ev["status"]
        comps = ev["competitions"][0]["competitors"]
        if len(comps) != 2:
            continue
        try:
            scores = [int(c["score"]) for c in comps]
        except Exception:
            continue
        for i, c in enumerate(comps):
            t = c["team"]
            keys = {k.lower() for k in (t.get("displayName"), t.get("shortDisplayName"),
                                        t.get("name")) if k}
            out.append({"gid": ev.get("id"), "keys": sorted(keys), "state": st["type"]["state"],
                        "period": st.get("period", 0), "clock_s": st.get("clock", 0),
                        "clock": st.get("displayClock", ""), "minute": _clock_min(st),
                        "lead": scores[i] - scores[1 - i], "label": ev.get("shortName", ""),
                        "total": sum(scores)})
    return out


def tennis_games(path):
    """List of per-player entries for today's tennis matches (ESPN)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    out, gid = [], 0
    for ev in requests.get(url, timeout=10).json().get("events", []):
        comps = list(ev.get("competitions", []))
        for gr in ev.get("groupings", []):
            comps += gr.get("competitions", [])
        for c in comps:
            cs = c.get("competitors", [])
            if len(cs) != 2:
                continue
            try:
                state = (c.get("status") or ev["status"])["type"]["state"]
                ls = [[int(x["value"]) for x in p.get("linescores", [])] for p in cs]
            except Exception:
                continue
            if not ls[0] or len(ls[0]) != len(ls[1]):
                continue
            gid += 1
            n = len(ls[0])
            done = n if state == "post" else n - 1
            sets = [sum(1 for i in range(done) if ls[p][i] > ls[1 - p][i]) for p in (0, 1)]
            for p in (0, 1):
                nm = (cs[p].get("athlete") or {}).get("displayName")
                if not nm:
                    continue
                out.append({"gid": f"{path}:{gid}", "keys": sorted({nm.lower(), nm.split()[-1].lower()}),
                            "state": state, "period": n, "clock_s": 0, "clock": "", "minute": 0,
                            "lead": sets[p] - sets[1 - p], "cur_games": ls[p][-1],
                            "cur_opp": ls[1 - p][-1], "cur_lead": ls[p][-1] - ls[1 - p][-1],
                            "label": ""})
    return out


def fetch_games(league, path):
    try:
        if league in US_LEAGUES:
            return live_games(path)
        if league == "TENNIS":
            if path == "tennis/all":
                return tennis_games("tennis/atp") + tennis_games("tennis/wta")
            return tennis_games(path)
        return soccer_games(path)
    except Exception as e:
        warn_once(f"live data for {path}: {e}")
        return {} if league in US_LEAGUES else []


def find_game(league, m, games):
    """Match a Kalshi market to a live game. Ambiguous or missing -> None (fail closed)."""
    if league in US_LEAGUES:
        g = games.get(norm(m["ticker"].rsplit("-", 1)[-1]))
        if g is not None:
            return g
        text = (m.get("yes_sub_title") or "").lower()      # fall back to matching the team name
        hits = [(len(k), g2) for g2 in games.values() for k in g2.get("keys", []) if len(k) >= 4 and k in text]
        if not hits:
            return None
        top = max(n for n, _ in hits)
        best = {id(g2): g2 for n, g2 in hits if n == top}
        return next(iter(best.values())) if len(best) == 1 else None
    if not games or str(m["ticker"]).upper().endswith("-TIE"):
        return None
    text = (m.get("yes_sub_title") or "").lower()
    hits = [(len(k), g) for g in games for k in g["keys"] if len(k) >= 4 and k in text]
    if not hits:
        return None
    top = max(n for n, _ in hits)
    best = {g["gid"]: g for n, g in hits if n == top}
    return next(iter(best.values())) if len(best) == 1 else None


# (minute, goals ahead): the bet qualifies if ANY pair is met. Chosen with the "risk" setting.
SOCCER_RULES = {"safe": [(70, 2), (55, 3)],
                "balanced": [(60, 2), (45, 3)],
                "aggressive": [(80, 1), (55, 2), (35, 3)]}
# (sets ahead, games ahead in the current set, games won in the current set)
TENNIS_RULES = {"safe": (1, 3, 4), "balanced": (1, 2, 4), "aggressive": (1, 1, 3)}


def verify_soccer(g):
    if g["state"] != "in":
        return False, f"game state is '{g['state']}', not live"
    if g["period"] > 2:
        return False, "extra time / penalties: skipped"
    rules = SOCCER_RULES[CFG["risk"]]
    minute, lead = g.get("minute", 0), g["lead"]
    if any(minute >= mn and lead >= ld for mn, ld in rules):
        return True, f"live, minute {minute}, leading by {lead}"
    need = " or ".join(f"{ld}+ goals after minute {mn}" for mn, ld in rules)
    return False, f"lead: {lead:+d} at minute {minute} (needs {need})"


def verify_tennis(g):
    if g["state"] != "in":
        return False, f"game state is '{g['state']}', not live"
    sets, cur, games = TENNIS_RULES[CFG["risk"]]
    if g["lead"] < sets:
        return False, "lead: not ahead on sets yet"
    if g["cur_lead"] < cur or g["cur_games"] < games:
        return False, f"lead: needs {cur}+ games ahead in the current set (now {g['cur_games']}-{g['cur_opp']})"
    return True, f"live, up {g['lead']} set(s), leading current set {g['cur_games']}-{g['cur_opp']}"


# ---------- crypto spot prices ----------
def spot_prices(symbols):
    """{SYMBOL: USD price} from Coinbase's public spot endpoint. Missing = no feed."""
    out = {}
    for sym in sorted(set(symbols)):
        try:
            r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=8)
            out[sym] = float(r.json()["data"]["amount"])
        except Exception:
            warn_once(f"no Coinbase price feed for {sym}: markets on it are never traded")
    return out


def crypto_model(m, spot, sym):
    """Rough probability the YES side wins, from spot, strike and time left (random-walk model)."""
    if spot is None:
        return None
    try:
        st = m.get("strike_type")
        fl = float(m["floor_strike"]) if m.get("floor_strike") is not None else None
        cp = float(m["cap_strike"]) if m.get("cap_strike") is not None else None
    except Exception:
        return None
    mins = hours_to_close(m) * 60
    if mins >= 1e7:
        return None
    sigma_min = VOL.get(sym, VOL_DEFAULT) / math.sqrt(525600)
    s = spot * sigma_min * math.sqrt(max(mins, 0.5))
    if st in ("greater", "greater_or_equal") and fl is not None:
        p, k = phi((spot - fl) / s), fl
    elif st in ("less", "less_or_equal") and cp is not None:
        p, k = phi((cp - spot) / s), cp
    elif st == "between" and fl is not None and cp is not None:
        p, k = phi((cp - spot) / s) - phi((fl - spot) / s), (fl + cp) / 2
    else:
        return None
    return {"p": p, "mins": mins, "strike": k}


def verify_crypto(league, m, spot, sym):
    if spot is None:
        return False, f"no price feed for {sym}"
    mod = crypto_model(m, spot, sym)
    if mod is None:
        return False, "can't read the strike for this market"
    if mod["mins"] < 0.5:
        return False, "market closing in under 30 seconds"
    cap = CFG["m15_max_minutes"] if league == "M15" else CFG["btc_max_minutes"]
    if mod["mins"] > cap:
        return False, f"too early ({int(mod['mins'])} min to close, bot waits for the last {cap})"
    need = phi(CFG["btc_min_sigma"])
    if mod["p"] < need:
        return False, f"model {mod['p']:.0%} below required {need:.0%}"
    return True, f"{sym} ${px(spot)} vs ${px(mod['strike'])}, {mod['mins']:.0f} min left, model {mod['p']:.0%}"


def game_info(league, g):
    if g is None:
        return "No live data"
    if g["state"] == "pre":
        return "Pre-game"
    if g["state"] == "post":
        return "Final"
    if league == "SOCCER":
        return f"LIVE {g['minute']}' · {g['lead']:+d}"
    if league == "TENNIS":
        return f"LIVE sets {g['lead']:+d} · set {g['cur_games']}-{g['cur_opp']}"
    return f"LIVE P{g['period']} {g.get('clock', '')} · {g['lead']:+d}"


# ---------- smart engine: team ratings (standings) + probability models ----------
RATINGS = {}          # espn path -> {"ts": time, "teams": {name/abbr: rating}}
PERIODS = {"NBA": (4, 720), "WNBA": (4, 600), "NFL": (4, 900), "NCAAF": (4, 900),
           "NHL": (3, 1200), "NCAAMB": (2, 1200), "NCAAWB": (4, 600)}
SD_MARGIN = {"NBA": 12.0, "WNBA": 11.0, "NCAAMB": 11.0, "NCAAWB": 11.0,
             "NFL": 13.5, "NCAAF": 15.0, "NHL": 2.3, "MLB": 4.3}
SD_TOTAL = {"NBA": 18.0, "WNBA": 16.0, "NCAAMB": 15.0, "NCAAWB": 14.0,
            "NFL": 13.5, "NCAAF": 16.0, "NHL": 2.4, "MLB": 4.5}
WP_SCALE = {"NBA": 25, "WNBA": 22, "NCAAMB": 30, "NCAAWB": 30, "NFL": 25, "NCAAF": 35, "NHL": 2.0, "MLB": 5.0}


def _num(v):
    try:
        return float(v)
    except Exception:
        return None


def _entries(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("entries"), list):
            out.extend(e for e in node["entries"] if isinstance(e, dict) and isinstance(e.get("team"), dict))
        for v in node.values():
            if isinstance(v, (dict, list)):
                _entries(v, out)
    elif isinstance(node, list):
        for v in node:
            _entries(v, out)


def parse_standings(data, league, soccer):
    """ESPN standings -> {key: {gp, wp, diff (margin per game vs average), tot (points per game, both teams)}}.
    Tolerant: anything missing is simply left out, and a team with no usable numbers gets no rating."""
    ents, teams = [], {}
    _entries(data, ents)
    for e in ents:
        st = {s.get("name"): s.get("value") for s in e.get("stats", []) if isinstance(s, dict)}
        w, l_, ti = _num(st.get("wins")) or 0, _num(st.get("losses")) or 0, _num(st.get("ties", st.get("draws"))) or 0
        gp = _num(st.get("gamesPlayed")) or (w + l_ + ti)
        if not gp:
            continue
        pts = _num(st.get("points"))
        wp = pts / (3 * gp) if soccer and pts is not None else _num(st.get("winPercent"))
        if wp is None:
            wp = (w + 0.5 * ti) / gp
        pf, pa = _num(st.get("pointsFor")), _num(st.get("pointsAgainst"))
        pd = _num(st.get("pointDifferential", st.get("differential")))
        if pd is None and pf is not None and pa is not None:
            pd = pf - pa
        diff = pd / gp if pd is not None else (wp - 0.5) * (2.0 if soccer else WP_SCALE.get(league, 10))
        rating = {"gp": gp, "wp": wp, "diff": diff,
                  "tot": (pf + pa) / gp if pf is not None and pa is not None else None}
        t = e["team"]
        for k in (t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("abbreviation")):
            if k:
                teams[str(k).lower()] = rating
    return teams


def team_rating(path, league, g):
    """Season rating for the team in this live-game entry, or None. Standings are refreshed hourly."""
    if not path or path.endswith("/all") or g is None:
        return None
    ent = RATINGS.get(path)
    if ent is None or time.time() - ent["ts"] > 3600:
        teams, ts = {}, time.time()
        try:
            data = requests.get(f"https://site.api.espn.com/apis/v2/sports/{path}/standings", timeout=8).json()
            teams = parse_standings(data, league, path.startswith("soccer"))
            if not teams:
                warn_once(f"no usable standings for {path}: team strength is ignored there")
        except Exception as e:
            warn_once(f"standings for {path}: {e}")
        if not teams:
            ts -= 3000                       # retry in ~10 minutes
        ent = RATINGS[path] = {"ts": ts, "teams": teams}
    for k in list(g.get("keys", [])) + [g.get("abbr")]:
        if k and k in ent["teams"]:
            return ent["teams"][k]
    return None


def frac_left(league, g):
    """Fraction of regulation still to play (1 = not started), None if overtime / unknown."""
    if g["state"] == "pre":
        return 1.0
    if g["state"] != "in":
        return None
    if league == "MLB":
        return None if g["period"] > 9 else max(0.0, (10 - g["period"]) / 9)
    n, ln = PERIODS[league]
    if g["period"] > n:
        return None
    return ((n - g["period"]) * ln + g.get("clock_s", 0)) / (n * ln)


def _pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def soccer_frac(g):
    if g["state"] == "pre":
        return 1.0
    if g["state"] != "in" or g.get("period", 0) > 2:
        return None
    return max(0.03, (90 - g.get("minute", 0)) / 90)


def win_prob(league, g, r_self, r_opp):
    """Chance this side wins: live score + time left + season strength. Rough model, not a promise."""
    mu = 0.0
    if r_self and r_opp:
        lim = 2 * SD_MARGIN.get(league, 2.0) if league != "SOCCER" else 1.6
        mu = max(-lim, min(lim, r_self["diff"] - r_opp["diff"]))
    if league == "SOCCER":
        f = soccer_frac(g)
        if f is None:
            return None
        la, lb = f * max(0.15, 1.35 + mu / 2), f * max(0.15, 1.35 - mu / 2)
        p = sum(_pois(x, la) * _pois(y, lb) for x in range(11) for y in range(11) if g["lead"] + x - y > 0)
        return min(p, 0.995)
    if league not in SD_MARGIN:
        return None
    f = frac_left(league, g)
    if f is None:
        return None
    f = max(f, 0.004)
    return min(phi((g["lead"] + mu * f) / (SD_MARGIN[league] * math.sqrt(f))), 0.995)


def total_line(m):
    """The points/goals line of an 'over' market, or None when unclear (never guess)."""
    fs, st = _num(m.get("floor_strike")), m.get("strike_type")
    if fs is not None and st == "greater":
        return fs
    if fs is not None and st == "greater_or_equal":
        return fs - 0.5
    mt = re.search(r"over\s+(\d+(?:\.\d+)?)", (m.get("title") or "") + " " + (m.get("yes_sub_title") or ""), re.I)
    ms = re.search(r"-(\d+)$", str(m.get("ticker", "")))
    t_line = float(mt.group(1)) if mt else None
    s_line = int(ms.group(1)) - 0.5 if ms else None
    if t_line is not None and s_line is not None and abs(t_line - s_line) > 0.01:
        return None                          # title and ticker disagree: don't trade it
    return t_line if t_line is not None else s_line


def find_total_game(league, m, games):
    """The two ESPN entries (both teams) of the game a totals market is about, or None."""
    pool = list(games.values()) if isinstance(games, dict) else list(games or [])
    text = ((m.get("title") or "") + " " + (m.get("yes_sub_title") or "")).lower()
    hits = {}
    for g in pool:
        if any(len(k) >= 4 and k in text for k in g.get("keys", [])):
            hits.setdefault(g["gid"], {})[id(g)] = g
    full = [v for v in hits.values() if len(v) == 2]
    if len(full) == 1:
        return list(full[0].values())
    if not full and isinstance(games, dict):
        parts = str(m.get("event_ticker", "")).split("-")
        rest = re.sub(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?", "", parts[1]) if len(parts) > 1 else ""
        for a, ga in games.items():
            for b, gb in games.items():
                if a != b and ga["gid"] == gb["gid"] and a + b == rest:
                    return [ga, gb]
    return None


def total_prob(league, g, line, prior):
    """Chance the combined score ends above `line`."""
    T = g.get("total")
    if T is None:
        return None
    if T > line:
        return 0.999
    if league == "SOCCER":
        f = soccer_frac(g)
        if f is None:
            return None
        lam = f * max(1.8, min(3.6, prior or 2.7))
        need = int(math.floor(line)) - T
        return max(0.0, min(0.995, 1 - sum(_pois(k, lam) for k in range(need + 1))))
    if league not in SD_TOTAL:
        return None
    f = frac_left(league, g)
    if f is None or g["state"] != "in" and g["state"] != "pre":
        return None
    f = max(f, 0.004)
    el = 1 - f
    pace = T / el if el >= 0.2 else None
    if prior and pace:
        rate = f * prior + el * pace
    else:
        rate = prior or pace
    if not rate:
        return None
    mean_final = T + rate * f
    return max(0.0, min(0.995, 1 - phi((line - mean_final) / (SD_TOTAL[league] * math.sqrt(f)))))


def assess_total(league, m, games, series, path):
    ask = cents(m, "yes_ask")
    bad = lambda why: {"ok": False, "why": why, "model": None, "p": None, "live": False, "behind": False,
                       "info": "Total points/goals", "gid": m.get("event_ticker")}
    line = total_line(m)
    if line is None:
        return bad("can't read the line for this totals market")
    pair = find_total_game(league, m, games)
    if pair is None:
        return bad("no matching live game found")
    g = pair[0]
    ra, rb = team_rating(path, league, pair[0]), team_rating(path, league, pair[1])
    prior = (ra["tot"] + rb["tot"]) / 2 if ra and rb and ra.get("tot") and rb.get("tot") else None
    if league == "SOCCER" and prior is not None:
        prior = max(1.8, min(3.6, prior))
    p = total_prob(league, g, line, prior)
    gid = f"{league}:{g['gid']}"
    unit = "goals" if league == "SOCCER" else "points"
    info = (f"Total {g.get('total')} vs line {line:g}" + (" · LIVE" if g["state"] == "in" else " · pre-game"))
    if p is None:
        return {**bad("model has no estimate (overtime or unknown game state)"), "info": info, "gid": gid}
    ok, why = True, f"model {p:.0%} for over {line:g} {unit} (now {g.get('total')})"
    if p * 100 < CFG["leg_min_prob"]:
        ok, why = False, f"model {p:.0%} is under your {CFG['leg_min_prob']:g}% 'obvious' level for over {line:g} {unit}"
    elif ask is not None and ask > p * 100:
        ok, why = False, f"model only {p:.0%} but the price is {ask}¢, not worth paying"
    return {"ok": ok, "why": why, "model": round(p * 100), "p": p, "live": g["state"] == "in",
            "behind": False, "info": info, "gid": gid, "line": line, "unit": unit}


def assess(league, m, games, spots, series):
    """One verdict used by both the bot and the Live Markets tab."""
    ask = cents(m, "yes_ask")
    if league == "OTHER":
        why = ("price-only: there is no live data to confirm this market" if CFG["trade_other"]
               else "no live data for this kind of market (price-only trading is off)")
        return {"ok": CFG["trade_other"], "why": why, "model": None, "live": False,
                "behind": False, "blind": True, "info": "No live data"}
    if league in CRYPTO_LEAGUES:
        sym = asset_of(series)
        spot = spots.get(sym)
        mod = crypto_model(m, spot, sym)
        ok, why = verify_crypto(league, m, spot, sym)
        cap = CFG["m15_max_minutes"] if league == "M15" else CFG["btc_max_minutes"]
        return {"ok": ok, "why": why, "model": round(mod["p"] * 100) if mod else None,
                "live": bool(mod and 0.5 <= mod["mins"] <= cap),
                "behind": bool(mod and ask is not None and mod["p"] * 100 < ask - 10),
                "info": f"{sym} ${px(spot)} vs ${px(mod['strike'])}" if mod else f"no {sym} data",
                "p": mod["p"] if mod else None, "gid": m.get("event_ticker")}
    path = ({**SERIES, **DYN}.get(series) or (None,))[0]
    if "TOTAL" in series.upper() and league in SPORT_LEAGUES and league != "TENNIS":
        return assess_total(league, m, games, series, path)
    game = find_game(league, m, games)
    if game is None:
        tie = str(m["ticker"]).upper().endswith("-TIE")
        ok, why = False, ("tie/draw markets are not traded" if tie else "no matching live game found")
    else:
        ok, why = verify(league, game)
    live = bool(game and game["state"] == "in")
    p = None
    if game is not None and league != "TENNIS":
        try:
            pool = list(games.values()) if isinstance(games, dict) else games
            opp = next((x for x in pool if x.get("gid") == game.get("gid") and x is not game), None)
            p = win_prob(league, game, team_rating(path, league, game), team_rating(path, league, opp))
        except Exception as e:
            warn_once(f"win model failed: {e}")
    return {"ok": ok, "why": why, "model": round(p * 100) if p is not None else None, "p": p, "live": live,
            "behind": bool(live and game["lead"] < 0), "info": game_info(league, game),
            "gid": f"{league}:{game['gid']}" if game is not None else m.get("event_ticker")}


def rate(ask, a):
    """Traffic-light signal. Rule-based, not a prediction or advice."""
    lo, hi = CFG["min_price"], CFG["max_price"]
    if a.get("blind"):
        if ask is not None and ask >= lo:
            return "caution", "No live data to confirm this one" + (
                " (price-only trading is ON)" if CFG["trade_other"] else "")
        return "none", "Not a heavy favorite yet"
    if ask is None or not a["live"]:
        return "none", "Not live yet"
    if a["behind"]:
        if ask >= 50:
            return "stop", f"Avoid: live data says this side is NOT winning, yet it's priced {ask}%"
        return "none", "Underdog right now"
    if a["ok"]:
        if ask > hi:
            return "caution", f"Wait: confirmed, but {ask}% leaves little upside (bot buys up to {hi}%)"
        if ask < lo:
            return "caution", f"Wait: data looks good but {ask}% is under your {lo}% range, so the market sees more risk"
        return "go", "Good setup: " + a["why"]
    if ask >= lo:
        return "caution", "Wait: " + a["why"]
    return "none", "Not a heavy favorite yet"


def name_of(m, league, series=""):
    if league == "M15":
        return f"{asset_of(series)} up in 15 min"
    if league == "OTHER":
        sub = m.get("yes_sub_title")
        return (m.get("title") or m["ticker"]) + (f" \u00b7 {sub}" if sub else "")
    if "TOTAL" in series.upper():
        ln = total_line(m)
        return f"Over {ln:g} {'goals' if league == 'SOCCER' else 'points'} \u00b7 {m.get('title', '')[:40]}" if ln is not None else (m.get("title") or m["ticker"])
    tail = m["ticker"].rsplit("-", 1)[-1]
    return tail if league in US_LEAGUES else (m.get("yes_sub_title") or m.get("title") or tail)


# ---------- orders, history, trading loop ----------
LOCK = threading.RLock()
ORDERS_FILE = os.path.join(HERE, "orders.json")
DECISIONS_FILE = os.path.join(HERE, "decisions.json")
LAST = {}


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return []


def _save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


ORDERS = _load(ORDERS_FILE)
DECISIONS = _load(DECISIONS_FILE)


def mode_name():
    return "paper" if CFG["dry_run"] else "live"


def open_count(mode):
    return sum(1 for o in ORDERS if o["mode"] == mode and o["status"] == "HOLDING")


def decide(ticker, kind, msg):
    """Record a decision once per change (digits ignored so a ticking score doesn't spam)."""
    key = kind + re.sub(r"\d+", "#", msg)
    with LOCK:
        if LAST.get(ticker) == key:
            return
        LAST[ticker] = key
        DECISIONS.insert(0, {"ts": time.time(), "kind": kind, "msg": msg, "ticker": ticker})
        del DECISIONS[300:]
        _save(DECISIONS_FILE, DECISIONS)


def account(bal):
    mode = mode_name()
    mine = [o for o in ORDERS if o["mode"] == mode]
    open_ = [o for o in mine if o["status"] == "HOLDING"]
    open_val = int(round(sum(o.get("value", o["count"] * o["price"]) for o in open_)))
    if mode == "paper":
        realized = sum(o["pnl"] or 0 for o in mine if o["status"] != "HOLDING")
        cash = int(round(CFG["paper_bankroll"] * 100 + realized - sum(o["count"] * o["price"] for o in open_)))
    else:
        cash = bal if bal is not None else 0
    return {"cash": cash, "portfolio": cash + open_val, "mode": mode}


def update_parlay(o):
    """A parlay wins only if EVERY leg wins; one lost leg loses the whole thing right away."""
    lost, all_yes, bids = False, True, []
    for l in o["legs"]:
        res = l.get("result")
        if res not in ("yes", "no"):
            try:
                m = api("GET", f"/markets/{l['ticker']}")["market"]
            except Exception as e:
                log(f"market lookup failed for {l['ticker']}: {e}")
                all_yes = False
                bids.append(None)
                continue
            res = m.get("result")
            if res in ("yes", "no"):
                l["result"] = res
            else:
                l["bid"] = cents(m, "yes_bid")
                bids.append(l["bid"])
        if res == "no":
            lost = True
        if res != "yes":
            all_yes = False
    with LOCK:
        if lost or all_yes:
            o["pnl"] = o["count"] * (100 - o["price"]) if all_yes and not lost else -o["count"] * o["price"]
            o["pnl"] = round(o["pnl"], 2)
            o["status"] = "LOST" if lost else "WON"
            o["closed_ts"] = time.time()
            log(f"{o['status']}: {o['title']} P&L {o['pnl'] / 100:+.2f}")
        elif bids and None not in bids and len(bids) == sum(1 for l in o["legs"] if l.get("result") != "yes"):
            o["bid"] = round(100 * math.prod(b / 100 for b in bids), 2)
        else:
            o["bid"] = None


def update_orders():
    """Mark open orders to market and settle finished ones (P&L is before fees)."""
    for o in [x for x in ORDERS if x["status"] == "HOLDING"]:
        if o.get("kind") == "parlay":
            update_parlay(o)
            continue
        try:
            m = api("GET", f"/markets/{o['ticker']}")["market"]
        except Exception as e:
            log(f"market lookup failed for {o['ticker']}: {e}")
            continue
        with LOCK:
            res = m.get("result")
            if res in ("yes", "no"):
                won = res == "yes"
                o["pnl"] = o["count"] * (100 - o["price"]) if won else -o["count"] * o["price"]
                o["status"] = "WON" if won else "LOST"
                o["closed_ts"] = time.time()
                log(f"{o['status']}: {o['ticker']} P&L {o['pnl'] / 100:+.2f}")
            else:
                o["bid"] = cents(m, "yes_bid")
    with LOCK:
        for o in ORDERS:
            if o["status"] == "HOLDING":
                px = o["bid"] if o.get("bid") is not None else o["price"]
                o["value"] = round(o["count"] * px, 2)
                o["unreal"] = round(o["value"] - o["count"] * o["price"], 2)
        _save(ORDERS_FILE, ORDERS)


GUARD = threading.Lock()


def stop_losses():
    """Sell any open bet that has lost more than stop_loss_pct of what was paid for it."""
    if not CFG["stop_loss_on"]:
        return
    mode = mode_name()
    for o in [x for x in ORDERS if x["status"] == "HOLDING" and x["mode"] == mode]:
        bid, cost = o.get("bid"), o["count"] * o["price"]
        if o.get("kind") == "parlay" and not CFG["dry_run"]:
            continue
        if bid is None or bid < 1:
            continue                      # nobody is buying right now, so it can't be sold yet
        if o["count"] * bid > cost * (1 - CFG["stop_loss_pct"] / 100):
            continue                      # not down far enough
        if not CFG["dry_run"]:
            try:
                api("POST", "/portfolio/orders", {
                    "ticker": o["ticker"], "action": "sell", "side": "yes", "type": "limit",
                    "count": o["count"], "yes_price": bid,
                    "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "inferno-stop-" + o["id"]))})
            except Exception as e:
                warn_once(f"stop-loss sell failed for {o['ticker']}: {e}")
                continue
        with LOCK:
            o["pnl"] = round(o["count"] * bid - cost, 2)
            o["status"] = "STOPPED"
            o["closed_ts"] = time.time()
            o["value"], o["unreal"] = 0, 0
            _save(ORDERS_FILE, ORDERS)
        pct = round(100 * (1 - o["count"] * bid / cost))
        tag = "Sold (paper)" if CFG["dry_run"] else "Sell order sent"
        decide(o["ticker"], "Stop-loss",
               f"{o['title']}: down {pct}% (bought {o['price']}\u00a2, now {bid}\u00a2). {tag}: P&L {o['pnl'] / 100:+.2f}")
        log(f"STOP-LOSS {o['ticker']}: down {pct}%, sold {o['count']} @ {bid}c, P&L {o['pnl'] / 100:+.2f}")


def guard():
    """Runs every few seconds: updates open bets, settles finished ones, applies the stop-loss."""
    if not GUARD.acquire(blocking=False):
        return
    try:
        if CFG["dry_run"] or PRIVATE_KEY is not None:
            update_orders()
            stop_losses()
    finally:
        GUARD.release()


def guard_loop():
    while True:
        try:
            guard()
        except Exception as e:
            log(f"guard error: {e}")
        time.sleep(10)


def scan(series):
    markets, cursor = [], ""
    while True:
        q = f"/markets?status=open&limit=200&series_ticker={series}"
        if cursor:
            q += f"&cursor={cursor}"
        data = api("GET", q)
        markets += data.get("markets", [])
        cursor = data.get("cursor")
        if not cursor:
            return markets


def classify(reason):
    waiting = ("too early", "too much time", "lead", "game state", "model", "closing")
    return "Waiting" if reason.startswith(waiting) else "Blocked"


def place(m, league, team, ask, count, why):
    t = m["ticker"]
    if CFG["dry_run"]:
        tag = "Paper entry accepted"
    else:
        api("POST", "/portfolio/orders", {
            "ticker": t, "action": "buy", "side": "yes", "type": "limit",
            "count": count, "yes_price": ask,
            "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "inferno-" + t))})
        tag = "Order sent"
    with LOCK:
        ml = minutes_left(m, league)
        ORDERS.append({"id": uuid.uuid4().hex[:8], "ts": time.time(), "ticker": t,
                       "title": m.get("title", t), "name": team, "league": league, "side": "YES",
                       "count": count, "price": ask, "mode": mode_name(),
                       "close_ts": (time.time() + ml * 60) if ml is not None else None,
                       "status": "HOLDING", "pnl": None, "closed_ts": None, "bid": None})
        _save(ORDERS_FILE, ORDERS)
    decide(t, "Signal fired", f"{league} {team}: {tag}: {count} YES at {ask}\u00a2 ({why})")
    log(f"{tag}: {count} YES {t} @ {ask}c")


def used_tickers(mode):
    s_ = set()
    for o in ORDERS:
        if o["mode"] == mode:
            s_.add(o["ticker"])
            s_.update(l["ticker"] for l in o.get("legs", []))
    return s_


def place_parlay(legs, price, count, comb):
    names = [x["name"] for x in legs]
    with LOCK:
        closes = [x["close_ts"] for x in legs]
        pid = uuid.uuid4().hex[:8]
        ORDERS.append({"id": pid, "ts": time.time(), "kind": "parlay", "ticker": "PARLAY-" + pid,
                       "title": " + ".join(names), "name": f"Parlay of {len(legs)}", "league": "PARLAY",
                       "side": "YES", "count": count, "price": price, "mode": mode_name(),
                       "close_ts": max(closes) if None not in closes else None,
                       "legs": [{"ticker": x["ticker"], "name": x["name"], "league": x["league"],
                                 "ask": x["ask"], "p": round(x["p"], 3), "bid": x["bid"], "result": None} for x in legs],
                       "status": "HOLDING", "pnl": None, "closed_ts": None, "bid": None})
        _save(ORDERS_FILE, ORDERS)
    why = "; ".join(f"{x['league']} {x['name']} {x['ask']}\u00a2 ({x['why']})" for x in legs)
    decide("PARLAY-" + pid, "Signal fired",
           f"Parlay of {len(legs)} (practice): {count} \u00d7 at {price:g}\u00a2, all legs must win. "
           f"Bot's estimate {comb:.0%}; the price itself implies {price:.0f}%. Legs: {why}")
    log(f"Paper parlay: {len(legs)} legs, {count} @ {price:g}c")


def build_parlays(pool, cash, mode):
    """Combine the best qualifying legs (one per game) into parlays. Returns a short note for the dashboard."""
    if not pool:
        return "no leg qualifies right now"
    if not CFG["dry_run"]:
        decide("PARLAY", "Blocked", "Parlays run in practice mode only for now (Kalshi's combo orders are not "
               "verified here). Set Bet style to 'Single bets' to trade live.")
        return "parlays are practice-only; switch Bet style to single bets for live trading"
    used = used_tickers(mode)
    best = {}
    for x in sorted(pool, key=lambda x: -x["p"]):
        if x["ticker"] not in used:
            best.setdefault(x["gid"], x)
    legs = sorted(best.values(), key=lambda x: -x["p"])
    built, note = 0, ""
    while open_count(mode) < CFG["max_open_bets"] and len(legs) >= 2:
        pick = None
        for n in range(min(CFG["parlay_legs"], len(legs)), 1, -1):
            if math.prod(x["p"] for x in legs[:n]) * 100 >= CFG["parlay_min_prob"]:
                pick = legs[:n]
                break
        if pick is None:
            note = (f"best 2-leg combo is only {math.prod(x['p'] for x in legs[:2]):.0%}, "
                    f"you require {CFG['parlay_min_prob']:g}%")
            break
        price = round(100 * math.prod(x["ask"] / 100 for x in pick), 2)
        count = int(CFG["stake_usd"] * 100 // price)
        if count < 1:
            note = f"stake ${CFG['stake_usd']:.2f} too small for a {price:g}\u00a2 parlay"
            break
        if count * price > cash:
            note = "not enough cash"
            break
        place_parlay(pick, price, count, math.prod(x["p"] for x in pick))
        cash -= count * price
        built += 1
        ids = {id(x) for x in pick}
        legs = [x for x in legs if id(x) not in ids]
    if built == 0 and not note:
        note = "max open bets reached" if open_count(mode) >= CFG["max_open_bets"] else "not enough different games for a parlay"
    return f"{built} parlay(s) built" + (f"; {note}" if note else "")


OTHER = {"ts": 0.0, "markets": []}


def scan_other(skip_series):
    """Every other open market (all categories) that closes soon. Cached for a minute."""
    if time.time() - OTHER["ts"] < max(60, CFG["poll_seconds"]):
        return OTHER["markets"]
    now = int(time.time())
    out, cursor = [], ""
    for _ in range(3):
        q = (f"/markets?status=open&limit=1000&min_close_ts={now}"
             f"&max_close_ts={now + int(CFG['view_hours'] * 3600)}&mve_filter=exclude")
        if cursor:
            q += f"&cursor={cursor}"
        data = api("GET", q)
        for m in data.get("markets", []):
            ser = str(m.get("event_ticker", "")).split("-")[0] or str(m["ticker"]).split("-")[0]
            if ser in skip_series or m.get("mve_collection_ticker") or str(m["ticker"]).startswith("KXMVE"):
                continue
            m["_series"] = ser
            out.append(m)
        cursor = data.get("cursor")
        if not cursor:
            break
    OTHER["ts"], OTHER["markets"] = time.time(), out
    return out


def tick():
    bal = None
    if not CFG["dry_run"]:
        bal = api("GET", "/portfolio/balance").get("balance")
    guard()
    STATE["account"] = account(bal)
    cash, mode, cands, rows = STATE["account"]["cash"], mode_name(), [], []
    if any(l in CFG["leagues"] for l in ("M15", "SOCCER", "TENNIS")):
        discover_series()
    all_series = {**SERIES, **DYN}
    want = [s for s, (_, lg) in all_series.items() if lg in CFG["leagues"]]
    spots = spot_prices([asset_of(s) for s in want if all_series[s][1] in CRYPTO_LEAGUES])
    STATE["spots"] = {k: v for k, v in spots.items() if k in ("BTC", "ETH", "SOL")}
    funnel, reasons, empty, errors = {}, {}, [], []
    pool, used = [], used_tickers(mode)

    def process(m, series, league, games):
        nonlocal cash
        ask, bid, t = cents(m, "yes_ask"), cents(m, "yes_bid"), m["ticker"]
        mins = minutes_left(m, league)
        a = assess(league, m, games, spots, series)
        name = name_of(m, league, series)
        f = funnel.setdefault(league, dict(loaded=0, in_range=0, in_time=0, passed=0, bought=0))
        f["loaded"] += 1
        in_range = ask is not None and CFG["min_price"] <= ask <= CFG["max_price"]
        shown = mins is None or mins <= CFG["view_hours"] * 60
        if shown and (league != "OTHER" or in_range):
            level, note = rate(ask, a)
            rows.append({"ticker": t, "name": name, "title": m.get("title", ""), "league": league,
                         "series": series, "yes": ask, "no": cents(m, "no_ask"),
                         "mins": max(0, round(mins)) if mins is not None else None,
                         "live": a["live"], "info": a["info"], "model": a["model"],
                         "level": level, "note": note})
        if not in_range:
            return
        f["in_range"] += 1
        if mins is not None and mins > CFG["max_minutes_to_close"]:
            r_ = reasons.setdefault("minutes", [0, f"more than {CFG['max_minutes_to_close']:g} minutes left"])
            r_[0] += 1
            return
        f["in_time"] += 1
        held = t in used
        cands.append({"ticker": t, "title": m.get("title", ""), "ask": ask, "ok": a["ok"],
                      "why": a["why"], "held": held})
        if held:
            return
        label = f"{league} {name}"
        if not a["ok"]:
            r_ = reasons.setdefault(re.sub(r"\d+", "#", a["why"]), [0, a["why"]])
            r_[0] += 1
            if league != "OTHER":
                decide(t, classify(a["why"]), f"{label}: {a['why']}")
            return
        f["passed"] += 1
        if not STATE["running"]:
            if league != "OTHER":
                decide(t, "Waiting", f"{label}: looks good, but the bot is stopped (press Start)")
            return
        if CFG["bet_style"] == "parlay" and league != "OTHER":
            lp = a["p"] if a.get("p") is not None else ask / 100
            why_no = None
            if bid is None or ask - bid > 8:
                why_no = f"spread too wide (bid {bid}\u00a2 vs ask {ask}\u00a2), it would be hard to sell"
            elif a.get("p") is not None and lp * 100 < CFG["leg_min_prob"]:
                why_no = f"model {lp:.0%} is under your {CFG['leg_min_prob']:g}% level for a parlay leg"
            if why_no:
                r_ = reasons.setdefault(re.sub(r"\d+", "#", why_no), [0, why_no])
                r_[0] += 1
            else:
                pool.append({"ticker": t, "name": name, "league": league, "ask": ask, "bid": bid, "p": lp,
                             "gid": a.get("gid") or t, "why": a["why"],
                             "close_ts": (time.time() + mins * 60) if mins is not None else None})
            return
        count, block = int(CFG["stake_usd"] * 100 // ask), None
        if open_count(mode) >= CFG["max_open_bets"]:
            block = f"max open bets ({CFG['max_open_bets']}) reached"
        elif count < 1:
            block = f"stake ${CFG['stake_usd']:.2f} too small for {ask}\u00a2"
        elif count * ask > cash:
            block = "not enough cash"
        elif bid is None or ask - bid > 8:
            block = f"spread too wide (bid {bid}\u00a2 vs ask {ask}\u00a2), it would be hard to sell"
        elif not CFG["dry_run"] and PRIVATE_KEY is None:
            block = "no API key loaded"
        if block:
            r_ = reasons.setdefault(re.sub(r"\d+", "#", block), [0, block])
            r_[0] += 1
            decide(t, "Blocked", f"{label}: {block}")
            return
        place(m, league, name, ask, count, a["why"])
        used.add(t)
        f["bought"] += 1
        cash -= count * ask

    cache = {}
    for series in want:
        espn_path, league = all_series[series]
        games = None
        if league not in CRYPTO_LEAGUES:
            if espn_path not in cache:
                cache[espn_path] = fetch_games(league, espn_path)
            games = cache[espn_path]
        try:
            markets = scan(series)
        except Exception as e:
            errors.append(f"{series}: {str(e)[:100]}")
            warn_once(f"could not load series {series} (check the ticker): {e}")
            continue
        if not markets:
            empty.append(series)
        for m in markets:
            process(m, series, league, games)
    if "OTHER" in CFG["leagues"]:
        try:
            for m in scan_other(set(all_series)):
                process(m, m["_series"], "OTHER", None)
        except Exception as e:
            errors.append(f"other markets: {str(e)[:100]}")
            warn_once(f"could not load the other markets: {e}")
    pnote = None
    if CFG["bet_style"] == "parlay" and STATE["running"]:
        pnote = build_parlays(pool, cash, mode)
    rows.sort(key=lambda r: (not r["live"], r["mins"] if r["mins"] is not None else 1e9))
    STATE["markets"] = rows[:600]
    STATE["report"] = {"ts": datetime.now().strftime("%H:%M:%S"), "leagues": funnel,
                       "reasons": [[v[1], v[0]] for v in sorted(reasons.values(), key=lambda v: -v[0])[:8]],
                       "empty": empty, "errors": errors[:6],
                       "parlay": {"style": CFG["bet_style"], "legs": len(pool), "note": pnote},
                       "ratings": {k.split("/")[-1]: len(v["teams"]) for k, v in RATINGS.items()}}
    STATE["account"] = account(bal)
    STATE["candidates"] = cands
    STATE["last_scan"] = datetime.now().strftime("%H:%M:%S")


def bot_loop():
    while True:
        try:
            if not CFG["dry_run"] and PRIVATE_KEY is None:
                time.sleep(3)
                continue
            tick()
        except Exception as e:
            log(f"error: {e}")
        time.sleep(CFG["poll_seconds"])


# ---------- Inferno dashboard ----------
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Inferno</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--panel:#fff;--fg:#14181f;--mut:#6b7280;--line:#e5e7eb;--soft:#f7f7f5;--acc:#1f6f50;--accbg:#e6f2ec;--warn:#9a6700;--warnbg:#fff4d6;--bad:#b42318;--badbg:#fde8e6;--ink:#0f1115}
@media(prefers-color-scheme:dark){:root{--bg:#101214;--panel:#16191c;--fg:#eceff1;--mut:#9aa3ab;--line:#262b30;--soft:#1b1f23;--acc:#4cc38a;--accbg:#173326;--warn:#e3b341;--warnbg:#33290f;--bad:#f85149;--badbg:#3a1714;--ink:#eceff1}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:8px;padding:8px 14px}
button.dark{background:var(--ink);color:var(--bg);border-color:var(--ink)}
select{font:inherit;width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg)}
input[type=text],input[type=number]{font:inherit;width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg)}
header{height:56px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700;letter-spacing:.02em}
.chips{display:flex;gap:10px;align-items:center}
.chip{border:1px solid var(--line);border-radius:8px;padding:4px 14px;display:flex;gap:22px;background:var(--soft)}
.chip span{display:block;font-size:10px;letter-spacing:.07em;color:var(--mut)}.chip b{font-size:14px}
.env{font-size:11px;font-weight:600;letter-spacing:.05em;padding:6px 10px;border-radius:8px;background:var(--soft);border:1px solid var(--line)}
.shell{display:flex;min-height:calc(100vh - 56px - 36px)}
nav{width:226px;border-right:1px solid var(--line);padding:14px 10px;display:flex;flex-direction:column;gap:2px;flex:none}
nav a{padding:8px 10px;border-radius:6px;cursor:pointer;color:var(--mut)}nav a.on{background:var(--soft);color:var(--fg);font-weight:600}
.lbl{font-size:11px;letter-spacing:.08em;color:var(--mut);margin:16px 10px 6px}
.bot{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;background:var(--soft);border-left:3px solid var(--ink)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);display:inline-block}.dot.off{background:var(--mut)}
.tag{font-size:11px;background:var(--accbg);color:var(--acc);padding:1px 8px;border-radius:10px;margin-left:auto}
main{flex:1;min-width:0}
.bar{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap;gap:10px}
.bar h1{font-size:16px;margin:0}.acts{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.cols{display:flex;flex-wrap:wrap}.left{flex:1 1 480px;min-width:0;border-right:1px solid var(--line)}.right{flex:1 1 340px;max-width:460px;background:var(--soft)}
.sec{padding:16px 20px;border-bottom:1px solid var(--line)}
.sec h2{font-size:14px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center}
.mut{color:var(--mut);font-size:12px;font-weight:400}
.stats{display:flex;gap:28px;flex-wrap:wrap;margin-top:12px}.stats span{display:block;font-size:12px;color:var(--mut)}.stats b{font-size:15px}
.pill{display:inline-block;font-size:11px;padding:2px 9px;border-radius:10px;background:var(--soft);border:1px solid var(--line);color:var(--mut)}
.pill.ok{background:var(--accbg);color:var(--acc);border-color:transparent}.pill.warn{background:var(--warnbg);color:var(--warn);border-color:transparent}.pill.bad{background:var(--badbg);color:var(--bad);border-color:transparent}
table{width:100%;border-collapse:collapse;font-size:13px}th{font-size:11px;color:var(--mut);font-weight:500;text-align:left}td,th{padding:7px 4px;border-bottom:1px solid var(--line)}
.pos{color:var(--acc)}.neg{color:var(--bad)}
.dec{border:1px solid var(--line);border-radius:8px;overflow:hidden}
.drow{display:grid;grid-template-columns:120px 1fr auto;gap:10px;padding:9px 12px;border-bottom:1px solid var(--line);align-items:center;font-size:13px}.drow:last-child{border:0}
.k{font-weight:600;display:flex;align-items:center;gap:7px}.k i{width:7px;height:7px;border-radius:50%;display:inline-block}
.Signal i{background:var(--acc)}.Blocked i{background:var(--warn)}.Waiting i{background:var(--mut)}
.sw{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--acc)}
.sw input{appearance:none;-webkit-appearance:none;width:42px;height:24px;border-radius:12px;background:var(--line);position:relative;cursor:pointer;margin:0}
.sw input:checked{background:var(--acc)}.sw input::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:left .15s}.sw input:checked::after{left:21px}
.hero{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:22px 20px;border-bottom:1px solid var(--line)}
.status{display:flex;align-items:center;gap:10px;font-size:20px}.status .dot{width:12px;height:12px}
button.big{font-size:16px;font-weight:700;padding:14px 34px;border-radius:10px;border:0;color:#fff;min-width:170px}
button.big.go{background:var(--acc)}button.big.stop{background:var(--bad)}
.qgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.field{margin:10px 0}.field label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
.page{max-width:980px}
.Stop-loss i{background:var(--bad)}
.pct{font-size:18px;font-weight:700}.bar2{height:4px;background:var(--line);border-radius:2px;margin-top:4px;width:80px}.bar2 i{display:block;height:100%;background:var(--acc);border-radius:2px}
.sig{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600}
.bang{display:inline-flex;width:16px;height:16px;border-radius:50%;align-items:center;justify-content:center;font-size:11px;color:#fff;background:var(--mut)}
.sig.go{color:var(--acc)}.sig.go .bang{background:var(--acc)}.sig.caution{color:var(--warn)}.sig.caution .bang{background:#d98e04}.sig.stop{color:var(--bad)}.sig.stop .bang{background:var(--bad)}.sig.none{color:var(--mut);font-weight:400}
.tiny{font-size:11px;color:var(--mut);font-weight:400;max-width:240px;margin-top:2px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{border:1px solid var(--line);border-radius:10px;padding:12px;background:var(--soft)}.card span{display:block;font-size:12px;color:var(--mut)}.card b{font-size:20px}
#banner{position:sticky;bottom:0;text-align:center;padding:10px;font-size:12px;font-weight:600;letter-spacing:.03em;background:var(--warnbg);color:var(--warn);border-top:1px solid var(--line)}#banner.live{background:var(--badbg);color:var(--bad)}
#setup{padding:10px 20px;background:var(--badbg);color:var(--bad);font-weight:600}
dialog{border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--fg);width:min(560px,94vw);max-height:88vh}
@media(max-width:820px){nav{display:none}.chips .env{display:none}}
</style></head><body>
<header><div class="brand"><svg width="26" height="26" viewBox="0 0 24 24"><path fill="#e8590c" d="M12 2c1 4 6 6 6 11a6 6 0 0 1-12 0c0-2 1-4 2-5 0 2 1 3 2 3 0-4-1-6 2-9z"/><path fill="#ffb454" d="M12 12c1 2 3 3 3 5a3 3 0 0 1-6 0c0-1 .5-2 1-2.500 0 1 1 1.500 1 1.500 0-2-.5-2.500 1-4z"/></svg>Inferno</div>
<div class="chips"><div class="chip"><div><span>CASH</span><b id="cash">-</b></div><div><span>PORTFOLIO</span><b id="port">-</b></div></div><div class="env" id="env">-</div></div></header>
<div class="shell"><nav><a class="on" data-v="dash">Dashboard</a><a data-v="live">Live Markets</a><a data-v="perf">Performance</a>
<div style="flex:1"></div><a id="upd">Update</a><a id="gear">Settings</a><div class="mut" id="ver" style="font-size:11px;padding:4px 10px"></div></nav>
<main><div id="setup" hidden></div>
<section id="v-dash"><div class="page">
<div class="hero"><div><div class="status"><span class="dot" id="dot"></span><b id="stext">Bot is stopped</b></div><div class="mut" id="ssub" style="margin-top:6px;max-width:520px"></div></div>
<div style="display:flex;align-items:center;gap:18px;flex-wrap:wrap"><label class="sw" title="Practice mode simulates every bet with no real money"><span id="plabel">PRACTICE MODE</span><input type="checkbox" id="paper"></label><button class="big go" id="run">Start bot</button></div></div>
<div class="sec" id="position"></div><div class="sec" id="quick"></div><div class="sec" id="report"></div><div class="sec" id="orders"></div><div class="sec" id="decisions"></div></div></section>
<section id="v-live" hidden><div class="bar"><h1>Live Markets</h1><span class="mut" id="lbtc"></span></div>
<div class="sec"><div class="mut" style="line-height:1.6">YES % is the market's implied chance. <span class="sig go"><b class="bang">!</b>Good setup</span> live data supports it and the price is in your range (the bot would buy) &nbsp;
<span class="sig caution"><b class="bang">!</b>Wait</span> not confirmed yet, or poor value &nbsp; <span class="sig stop"><b class="bang">!</b>Avoid</span> live data disagrees with the price.
These are rule-based checks, not predictions or advice, and nothing is guaranteed.</div>
<div id="lchips" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:12px"></div></div><div class="sec" id="ltable"></div></section>
<section id="v-perf" hidden><div class="bar"><h1>Performance</h1><span class="mut" id="pmode"></span></div><div class="sec" id="pcards"></div><div class="sec" id="pchart"></div><div class="sec" id="pleague"></div><div class="sec" id="ptrades"></div></section>
</main></div><div id="banner"></div><dialog id="dlg"></dialog>
<script>
const $=id=>document.getElementById(id);
const h=(t,a,...k)=>{const e=document.createElement(t);for(const x in a||{}){if(x==='on')for(const v in a.on)e.addEventListener(v,a.on[v]);else if(x==='class')e.className=a[x];else e.setAttribute(x,a[x])}k.flat(9).forEach(c=>{if(c!=null&&c!==false)e.append(c)});return e};
Element.prototype.rc=function(...k){this.replaceChildren(...k.flat(9).filter(c=>c!=null&&c!==false))};
const usd=c=>(c<0?'-':'')+'$'+Math.abs(c/100).toFixed(2);
const sgn=c=>(c>=0?'+':'-')+'$'+Math.abs(c/100).toFixed(2);
const cls=c=>c>0?'pos':c<0?'neg':'';
const tm=t=>new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
const dt=t=>new Date(t*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const st=(l,v)=>h('div',0,h('span',0,l),v instanceof Node?v:h('b',0,v));
const LG=['NBA','NFL','MLB','NHL','WNBA','NCAAF','NCAAMB','NCAAWB','SOCCER','TENNIS','BTC','M15','OTHER'],LL=['NBA','NFL','MLB','NHL','WNBA','NCAAF','NCAAMB','NCAAWB'],LN={NBA:'NBA',NFL:'NFL',MLB:'MLB',NHL:'NHL',WNBA:'WNBA',NCAAF:'College football',NCAAMB:'College basketball (men)',NCAAWB:'College basketball (women)',SOCCER:'Soccer',TENNIS:'Tennis',BTC:'Bitcoin',M15:'15-Min',OTHER:'Other',PARLAY:'Parlay'},LV={go:'Good setup',caution:'Wait',stop:'Avoid',none:'—'};
let lf='All',liveOnly=false;
let S=null,C=null,view='dash',filt='All',Q=null,qdirty=false;
const post=(p,b)=>fetch(p,{method:'POST',headers:{'X-Requested-With':'dashboard','Content-Type':'application/json'},body:JSON.stringify(b||{})});
async function refresh(){S=await (await fetch('/api/state')).json();C=S.config;render()}

const pc=s=>s==='WON'?'ok':s==='LOST'?'bad':s==='STOPPED'?'warn':'';
function render(){
 $('ver').textContent='Inferno v'+(S.version||'');
 if(!S)return;const paper=C.dry_run,mode=paper?'paper':'live',on=S.running;
 $('cash').textContent=usd(S.account.cash);$('port').textContent=usd(S.account.portfolio);
 $('env').textContent=paper?'PAPER · REAL DATA':C.use_demo?'DEMO ACCOUNT':'PRODUCTION';
 $('paper').checked=paper;$('plabel').textContent=paper?'PRACTICE MODE':'REAL ORDERS';
 $('dot').className='dot'+(on?'':' off');$('stext').textContent=on?'Bot is running':'Bot is stopped';
 const sl=C.stop_loss_on?('Stop-loss: sells any bet that is down '+C.stop_loss_pct+'%.'):'Stop-loss is off.';
 $('ssub').textContent=(on?'Scanning every '+C.poll_seconds+'s and placing bets automatically. ':'It will not place any new bets. ')+sl+(S.last_scan?' Last scan '+S.last_scan+'.':'');
 const r=$('run');r.textContent=on?'Stop bot':'Start bot';r.className='big '+(on?'stop':'go');
 const b=$('banner');b.className=(!paper&&!C.use_demo)?'live':'';
 b.textContent=paper?'PRACTICE MODE · Real market data, simulated bets on a $'+C.paper_bankroll.toFixed(2)+' practice bankroll · No real money':C.use_demo?'DEMO ACCOUNT · Orders go to Kalshi’s demo exchange (play money)':'LIVE MODE · Real orders with real money';
 const se=$('setup');se.textContent=(!paper&&S.setup_error)?S.setup_error:'';se.hidden=!se.textContent;
 syncQuick();position(mode);report();orders(mode);decisions();if(view==='perf')perf(mode);if(view==='live')live();
}
const SOC={safe:'2+ goals after 70′ or 3+ after 55′',balanced:'2+ goals after 60′ or 3+ after 45′',aggressive:'1+ goal after 80′, 2+ after 55′ or 3+ after 35′'};
const TEN={safe:'up a set and 3+ games ahead in the current set',balanced:'up a set and 2+ games ahead in the current set',aggressive:'up a set and ahead in the current set'};
function quickInit(){
 const qmsg=h('span',{class:'mut'}),dirty=()=>{qdirty=true;qmsg.textContent=''};
 const mk=step=>{const i=h('input',{type:'number',step:step||'any',min:'0'});i.oninput=dirty;return i};
 const minp=mk('1'),maxp=mk('1'),mins=mk('1'),m15=mk('0.5'),stake=mk(),maxb=mk('1'),slp=mk('1');
 const style=h('select'),legsN=mk('1'),legp=mk('1'),parp=mk('1');
 [['parlay','Parlays (automatic)'],['single','Single bets']].forEach(([v,l])=>style.append(h('option',{value:v},l)));style.onchange=dirty;
 const slon=h('input',{type:'checkbox'}),oth=h('input',{type:'checkbox'}),risk=h('select'),hint=h('div',{class:'mut',style:'margin-top:4px'});
 [['safe','Safe'],['balanced','Balanced (recommended)'],['aggressive','Aggressive']].forEach(([v,l])=>risk.append(h('option',{value:v},l)));
 const upd=()=>{hint.textContent='Soccer: '+SOC[risk.value]+'. Tennis: '+TEN[risk.value]+'.'};
 risk.onchange=()=>{dirty();upd()};slon.onchange=dirty;oth.onchange=dirty;
 Q={minp,maxp,mins,m15,stake,maxb,slp,slon,oth,risk,qmsg,upd,style,legsN,legp,parp};
 const f=(l,el)=>h('div',{class:'field'},h('label',0,l),el);
 $('quick').rc(h('h2',0,'Your settings'),
  h('div',{class:'qgrid'},
   f('Only buy when the market’s chance is at least (%)',minp),f('…and at most (%)',maxp),
   f('Only markets with at most this many minutes left',mins),f('15-minute markets: only bet in the last (minutes)',m15),
   h('div',{class:'field'},h('label',0,'Bet style'),style),f('Parlay: most legs',legsN),f('Each leg must be at least this likely (%)',legp),f('Whole parlay must be at least this likely (%)',parp),
   h('div',{class:'field'},h('label',0,'Soccer & tennis: how obvious must the win be?'),risk,hint),
   f('Bet size per trade ($)',stake),f('Max bets at once',maxb),
   h('div',{class:'field'},h('label',{style:'display:flex;gap:6px;align-items:center'},slon,'Stop-loss: automatically sell a bet if it is down'),h('div',{style:'display:flex;gap:6px;align-items:center'},slp,h('span',0,'%'))),
   h('div',{class:'field'},h('label',{style:'display:flex;gap:6px;align-items:center'},oth,'Also bet on all other markets that have no live data (price only, riskier)'))),
  h('div',{style:'display:flex;gap:10px;align-items:center;margin-top:6px'},h('button',{class:'dark',on:{click:saveQuick}},'Save'),qmsg,h('span',{class:'mut',style:'margin-left:auto'},'More options are in Settings')));
}
function syncQuick(){if(!Q)quickInit();if(qdirty)return;
 Q.minp.value=C.min_price;Q.maxp.value=C.max_price;Q.mins.value=C.max_minutes_to_close;Q.m15.value=C.m15_max_minutes;Q.risk.value=C.risk;Q.upd();
 Q.stake.value=C.stake_usd;Q.maxb.value=C.max_open_bets;Q.slp.value=C.stop_loss_pct;Q.slon.checked=C.stop_loss_on;Q.oth.checked=C.trade_other;Q.style.value=C.bet_style;Q.legsN.value=C.parlay_legs;Q.legp.value=C.leg_min_prob;Q.parp.value=C.parlay_min_prob}
async function saveQuick(){
 const r=await post('/api/config',{min_price:Number(Q.minp.value),max_price:Number(Q.maxp.value),max_minutes_to_close:Number(Q.mins.value),m15_max_minutes:Number(Q.m15.value),risk:Q.risk.value,
  stake_usd:Number(Q.stake.value),max_open_bets:Number(Q.maxb.value),stop_loss_on:Q.slon.checked,stop_loss_pct:Number(Q.slp.value),trade_other:Q.oth.checked,bet_style:Q.style.value,parlay_legs:Number(Q.legsN.value),leg_min_prob:Number(Q.legp.value),parlay_min_prob:Number(Q.parp.value)});
 if(r.ok){qdirty=false;Q.qmsg.textContent='Saved.'}else Q.qmsg.textContent=await r.text();refresh()}
function position(mode){
 const o=S.orders.filter(x=>x.mode===mode&&x.status==='HOLDING').sort((a,b)=>b.ts-a.ts);
 const cost=o.reduce((a,x)=>a+x.count*x.price,0),val=o.reduce((a,x)=>a+(x.value??x.count*x.price),0);
 const left=x=>x.close_ts?closes(Math.max(0,Math.round((x.close_ts*1000-Date.now())/60000))):'-';
 const sl=x=>C.stop_loss_on?Math.floor(x.price*(1-C.stop_loss_pct/100)+1e-9)+'¢':'off';
 $('position').rc(h('h2',0,'Current positions',h('span',{class:'mut'},(mode==='paper'?'Practice · simulated bets the bot made':'Live bets the bot made')+' · '+o.length+' open')),
  o.length?[h('div',{class:'stats',style:'margin:0 0 10px'},st('Cost',usd(cost)),st('Value now',usd(val)),st('Unrealized P&L',h('b',{class:cls(val-cost)},sgn(val-cost)))),
   h('div',{style:'overflow-x:auto'},h('table',0,h('tr',0,['Bet','Qty','Bought at','Now','Value','P&L','Closes in','Stop-loss at'].map(x=>h('th',0,x))),o.map(x=>h('tr',0,
    h('td',0,h('b',0,x.name||x.title),x.legs?x.legs.map(l=>h('div',{class:'mut'},'• '+(LN[l.league]||l.league)+' '+l.name+' @'+l.ask+'¢'+(l.result?(' → '+l.result.toUpperCase()):''))):h('div',{class:'mut'},(LN[x.league]||x.league)+' · '+(x.title||x.ticker))),h('td',0,'×'+x.count+' YES'),h('td',0,x.price+'¢'),h('td',0,x.bid==null?'-':x.bid+'¢'),h('td',0,usd(x.value??x.count*x.price)),h('td',{class:cls(x.unreal||0)},sgn(x.unreal||0)),h('td',0,left(x)),h('td',0,sl(x))))))]
  :h('div',{class:'mut'},S.running?'No open bets yet. The bot is watching for a qualifying market.':'No open bets. Press Start bot to begin.'));
}
function report(){
 const r=S.report,box=$('report');
 if(!r){box.rc(h('h2',0,'What the bot sees'),h('div',{class:'mut'},'Waiting for the first scan…'));return}
 const rows=Object.entries(r.leagues).filter(([k,v])=>v.loaded>0),sum=k=>rows.reduce((a,[,v])=>a+v[k],0);
 box.rc(h('h2',0,'What the bot sees',h('span',{class:'mut'},'Last scan '+r.ts)),
  h('div',{style:'margin-bottom:10px'},sum('loaded')+' markets loaded · ',h('b',0,sum('in_range')),' in your price range ('+C.min_price+'–'+C.max_price+'%) · ',h('b',0,sum('in_time')),' within the time limit · ',h('b',0,sum('passed')),' passed the live check · ',h('b',0,sum('bought')),' bought'),
  rows.length?h('div',{style:'overflow-x:auto'},h('table',0,h('tr',0,['Market type','Loaded','In price range','In time limit','Passed live check','Bought'].map(x=>h('th',0,x))),rows.map(([k,v])=>h('tr',0,h('td',0,LN[k]||k),h('td',0,v.loaded),h('td',0,v.in_range),h('td',0,v.in_time),h('td',0,v.passed),h('td',0,v.bought))))):h('div',{class:'mut'},'Kalshi returned no markets. Check your internet connection.'),
  r.reasons.length?[h('div',{class:'mut',style:'margin:12px 0 4px'},'Why markets in your price range were not bought:'),h('ul',{style:'margin:0;padding-left:18px'},r.reasons.map(([t,n])=>h('li',0,n+' × '+t)))]:null,
  r.parlay&&r.parlay.style==='parlay'?h('div',{style:'margin-top:10px'},h('b',0,'Parlay builder: '),r.parlay.legs+' leg'+(r.parlay.legs==1?'':'s')+' qualify'+(r.parlay.note?' · '+r.parlay.note:S.running?'':' · press Start to build parlays')):null,
  r.empty.length?h('div',{class:'mut',style:'margin-top:10px'},'No open markets right now in: '+r.empty.join(', ')):null,
  r.errors.length?h('div',{style:'margin-top:10px;color:var(--bad)'},'Problems: '+r.errors.join(' | ')):null);
}
function orders(mode){
 const o=S.orders.filter(x=>x.mode===mode).sort((a,b)=>b.ts-a.ts),hold=o.filter(x=>x.status==='HOLDING').length;
 $('orders').rc(h('h2',0,'Orders & P&L',h('span',{class:'mut'},o.length+' order'+(o.length==1?'':'s')+' · '+hold+' holding')),
  o.length?h('table',0,h('tr',0,['Time','Qty','Side','Price','Market','Status','P&L'].map(x=>h('th',0,x))),o.slice(0,50).map(x=>{const p=x.status==='HOLDING'?x.unreal:x.pnl;
   return h('tr',0,h('td',0,dt(x.ts)),h('td',0,'×'+x.count),h('td',0,x.side),h('td',0,'@'+x.price+'¢'),h('td',0,x.title||x.ticker),h('td',0,h('span',{class:'pill '+pc(x.status)},x.status)),h('td',{class:cls(p||0)},p==null?'-':sgn(p)))}))
  :h('div',{class:'mut'},'No orders yet.'));
}
function decisions(){
 const pills=['All','Signal fired','Stop-loss','Blocked','Waiting'].map(k=>h('button',{class:'pill'+(filt===k?' ok':''),on:{click:()=>{filt=k;render()}}},k));
 const rows=S.decisions.filter(d=>filt==='All'||d.kind===filt).slice(0,40);
 $('decisions').rc(h('h2',0,'Activity'),
  h('div',{style:'display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center'},pills,h('span',{class:'mut'},S.decisions.length+' recorded')),
  rows.length?h('div',{class:'dec'},rows.map(d=>h('div',{class:'drow '+d.kind.split(' ')[0]},h('span',{class:'k'},h('i'),d.kind),h('span',0,d.msg),h('span',{class:'mut'},tm(d.ts))))):h('div',{class:'mut'},'Nothing recorded yet.'));
}
function closes(m){if(m==null)return '-';if(m<60)return m+'m';if(m<1440)return Math.floor(m/60)+'h '+(m%60)+'m';return Math.floor(m/1440)+'d'}
function live(){
 const rows=S.markets||[];
 $('lbtc').textContent=Object.entries(S.spots||{}).map(([k,v])=>k+' $'+Math.round(v).toLocaleString()).join(' · ');
 const cb=h('input',{type:'checkbox'});cb.checked=liveOnly;cb.onchange=()=>{liveOnly=cb.checked;live()};
 $('lchips').rc(['All',...LG.filter(k=>C.leagues.includes(k))].map(k=>h('button',{class:'pill'+(lf===k?' ok':''),on:{click:()=>{lf=k;live()}}},k==='All'?'All':LN[k])),
  h('label',{class:'mut',style:'display:flex;gap:5px;align-items:center;margin-left:10px'},cb,'Live only'),h('span',{class:'mut'},rows.length+' markets'));
 const shown=rows.filter(r=>(lf==='All'||r.league===lf)&&(!liveOnly||r.live));
 $('ltable').rc(shown.length?h('table',0,h('tr',0,['Market','YES','NO','Live status','Closes','Signal'].map(x=>h('th',0,x))),shown.slice(0,200).map(r=>h('tr',0,
  h('td',0,h('b',0,r.name||r.title),h('div',{class:'mut'},LN[r.league]+' · '+(r.series||'')+' · '+r.title)),
  h('td',0,h('div',{class:'pct'},r.yes==null?'-':r.yes+'%'),h('div',{class:'bar2'},h('i',{style:'width:'+(r.yes||0)+'%'}))),
  h('td',0,r.no==null?'-':r.no+'%'),
  h('td',0,r.live?h('span',{class:'pill ok'},'LIVE'):'',' ',h('span',{class:'mut'},r.info+(r.model!=null?' · model '+r.model+'%':''))),
  h('td',0,closes(r.mins)),
  h('td',{title:r.note},h('span',{class:'sig '+r.level},r.level==='none'?'':h('b',{class:'bang'},'!'),LV[r.level]),h('div',{class:'tiny'},r.note)))))
  :h('div',{class:'mut'},'No markets to show yet. The first scan takes a few seconds, and only leagues enabled in the Market step are loaded.'));
}
function perf(mode){
 $('pmode').textContent=(mode==='paper'?'Paper':'Live')+' trading \u00b7 P&L is before Kalshi fees';
 const all=S.orders.filter(o=>o.mode===mode),closed=all.filter(o=>o.status!=='HOLDING').sort((a,b)=>a.closed_ts-b.closed_ts),open=all.filter(o=>o.status==='HOLDING');
 const real=closed.reduce((a,o)=>a+o.pnl,0),unr=open.reduce((a,o)=>a+(o.unreal||0),0),wins=closed.filter(o=>o.pnl>0).length,n=closed.length;
 const cards=[['Realized P&L',h('b',{class:cls(real)},sgn(real))],['Unrealized P&L',h('b',{class:cls(unr)},sgn(unr))],['Win rate',n?Math.round(100*wins/n)+'%':'-'],['Closed trades',n],['Avg per trade',n?sgn(real/n):'-'],['Best / worst',n?sgn(Math.max(...closed.map(o=>o.pnl)))+' / '+sgn(Math.min(...closed.map(o=>o.pnl))):'-']];
 $('pcards').rc(h('div',{class:'cards'},cards.map(([l,v])=>h('div',{class:'card'},h('span',0,l),v instanceof Node?v:h('b',0,v)))));
 const W=640,Ht=180,P=12,NS='http://www.w3.org/2000/svg',svg=document.createElementNS(NS,'svg');svg.setAttribute('viewBox','0 0 '+W+' '+Ht);svg.setAttribute('preserveAspectRatio','none');svg.style.cssText='width:100%;height:200px';
 const mk=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);svg.append(e)};
 if(n){let run=0;const pts=[0,...closed.map(o=>run+=o.pnl)],mn=Math.min(...pts),mx=Math.max(...pts),rg=(mx-mn)||1;
  const X=i=>P+i*(W-2*P)/n,Y=v=>Ht-P-(v-mn)/rg*(Ht-2*P);
  mk('line',{x1:P,x2:W-P,y1:Y(0),y2:Y(0),stroke:'currentColor','stroke-opacity':'.25','stroke-dasharray':'4 4'});
  mk('polyline',{points:pts.map((v,i)=>X(i)+','+Y(v)).join(' '),fill:'none',stroke:'var(--acc)','stroke-width':'2.5','stroke-linejoin':'round','vector-effect':'non-scaling-stroke'})}
 $('pchart').rc(h('h2',0,'Cumulative realized P&L'),n?svg:h('div',{class:'mut'},'The chart appears once your first trade settles.'));
 const lg={};closed.forEach(o=>{const g=lg[o.league]||(lg[o.league]={n:0,w:0,p:0});g.n++;g.w+=o.pnl>0;g.p+=o.pnl});
 $('pleague').rc(h('h2',0,'By league'),Object.keys(lg).length?h('table',0,h('tr',0,['League','Trades','Win rate','P&L'].map(x=>h('th',0,x))),Object.entries(lg).map(([k,g])=>h('tr',0,h('td',0,k),h('td',0,g.n),h('td',0,Math.round(100*g.w/g.n)+'%'),h('td',{class:cls(g.p)},sgn(g.p))))):h('div',{class:'mut'},'No settled trades yet.'));
 $('ptrades').rc(h('h2',0,'Closed trades'),n?h('table',0,h('tr',0,['Closed','Market','Qty','Entry','Result','P&L'].map(x=>h('th',0,x))),[...closed].reverse().slice(0,100).map(o=>h('tr',0,h('td',0,dt(o.closed_ts)),h('td',0,o.title||o.ticker),h('td',0,'\u00d7'+o.count),h('td',0,'@'+o.price+'\u00a2'),h('td',0,h('span',{class:'pill '+pc(o.status)},o.status)),h('td',{class:cls(o.pnl)},sgn(o.pnl))))):h('div',{class:'mut'},'Nothing yet.'));
}
function fld(k,l,t,ctl){
 if(t==='b'){const i=h('input',{type:'checkbox'});i.checked=C[k];ctl[k]=()=>i.checked;return h('div',{class:'field'},h('label',{style:'display:flex;gap:6px;align-items:center'},i,l))}
 if(t==='n'||t==='t'){const i=h('input',{type:t==='n'?'number':'text',step:'any'});i.value=C[k];ctl[k]=()=>t==='n'?Number(i.value):i.value;return h('div',{class:'field'},h('label',0,l),i)}
 if(t==='lg'){const bs=LG.map(g=>{const i=h('input',{type:'checkbox'});i.checked=C.leagues.includes(g);return [i,g]});ctl[k]=()=>bs.filter(x=>x[0].checked).map(x=>x[1]);
  return h('div',{class:'field'},h('label',0,l),h('div',{style:'display:flex;gap:14px;flex-wrap:wrap'},bs.map(([i,g])=>h('label',{style:'display:flex;gap:5px;align-items:center'},i,LN[g]))))}
 const ins=LL.map(g=>[h('input',{type:'number',value:C.min_leads[g]}),g]);ctl[k]=()=>Object.fromEntries(ins.map(([i,g])=>[g,Number(i.value)]));
 return h('div',{class:'field'},h('label',0,l),h('div',{style:'display:grid;grid-template-columns:repeat(4,1fr);gap:8px'},ins.map(([i,g])=>h('div',0,h('span',{class:'mut'},LN[g]),i))),h('div',{class:'mut',style:'margin-top:6px'},'Points for basketball and football, goals for NHL, runs for MLB. Soccer and tennis follow the setting on the main page.'))
}
function settings(){
 const ctl={},msg=h('div',{class:'mut'}),d=$('dlg');
 const acct=[['use_demo','Use Kalshi demo account (play money) when practice mode is off','b'],['api_key_id','Kalshi API key ID','t'],['private_key_path','Private key file (.pem)','t'],['update_url','Update link (https link to the newest bot.py)','t']];
 const adv=[['leagues','Markets to trade and show','lg'],['min_leads','Minimum lead required to buy','ld'],['btc_min_sigma','Crypto: required distance from strike (sigmas)','n'],['btc_max_minutes','Bitcoin hourly/daily: only bet in the last N minutes','n'],['poll_seconds','Scan every (seconds, min 10)','n'],['paper_bankroll','Practice bankroll ($)','n'],['view_hours','Live Markets: show markets closing within (hours)','n']];
 d.rc(h('h3',{style:'margin-top:0'},'Settings'),h('div',{class:'mut'},'Account (only needed for demo or real orders)'),acct.map(a=>fld(...a,ctl)),
  h('details',0,h('summary',{style:'cursor:pointer;margin:12px 0 4px;font-weight:600'},'Advanced'),adv.map(a=>fld(...a,ctl))),msg,
  h('div',{style:'display:flex;gap:8px;margin-top:12px;flex-wrap:wrap'},
   h('button',{class:'dark',on:{click:async()=>{const b={};for(const k in ctl)b[k]=ctl[k]();if(!b.use_demo&&!C.dry_run&&!confirm('Production mode with practice mode OFF places REAL orders. Continue?'))return;const r=await post('/api/config',b);if(r.ok){d.close();refresh()}else msg.textContent=await r.text()}}},'Save'),
   h('button',{on:{click:async()=>{if(confirm('Delete all practice orders and practice P&L history?')){await post('/api/reset_paper');d.close();refresh()}}}},'Reset practice history'),
   h('button',{on:{click:()=>d.close()}},'Close')));d.showModal();
}
$('gear').onclick=settings;
$('upd').onclick=async()=>{
 if(!C.update_url){alert('Paste your update link in Settings first (under Account). It is a direct https link to the newest bot.py.');settings();return}
 if(!confirm('Download the newest version from your update link and restart Inferno?\n\nOnly do this if you control that link: the file it points to runs on your computer. The bot will be stopped and your old version is saved as bot.py.bak.'))return;
 $('upd').textContent='Updating…';
 let r;try{r=await (await post('/api/update')).json()}catch(e){r={ok:false,msg:String(e)}}
 $('upd').textContent='Update';alert(r.msg);
 if(r.ok)setTimeout(function w(){fetch('/api/state').then(x=>x.ok?location.reload():setTimeout(w,1500)).catch(()=>setTimeout(w,1500))},2500)};
$('run').onclick=async()=>{const on=!S.running;
 if(on&&!C.dry_run&&!C.use_demo&&!confirm('Start trading with REAL money on your Kalshi account?'))return;
 const r=await post('/api/run',{on});if(!r.ok)alert(await r.text());refresh()};
$('paper').onchange=async e=>{const on=e.target.checked;if(!on&&!C.use_demo&&!confirm('Turn OFF practice mode? Inferno will use REAL money on your Kalshi account once you press Start.')){e.target.checked=true;return}
 const r=await post('/api/config',{dry_run:on});if(!r.ok)alert(await r.text());refresh()};
document.querySelectorAll('nav a[data-v]').forEach(a=>a.onclick=()=>{view=a.dataset.v;document.querySelectorAll('nav a[data-v]').forEach(x=>x.classList.toggle('on',x===a));$('v-dash').hidden=view!=='dash';$('v-perf').hidden=view!=='perf';$('v-live').hidden=view!=='live';render()});
refresh();setInterval(refresh,4000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send("bad host", "text/plain", 403)
        if self.path == "/api/state":
            with LOCK:
                body = json.dumps({**STATE, "orders": ORDERS, "decisions": DECISIONS[:200],
                                   "config": CFG})
            self._send(body)
        else:
            self._send(PAGE, "text/html")

    def do_POST(self):
        # Header + host checks stop other websites from driving the bot.
        if not self._host_ok() or self.headers.get("X-Requested-With") != "dashboard":
            return self._send("forbidden", "text/plain", 403)
        if self.path == "/api/run":
            try:
                n = int(self.headers.get("Content-Length", 0))
                on = bool(json.loads(self.rfile.read(n)).get("on"))
            except Exception:
                return self._send("bad request", "text/plain", 400)
            if on and not CFG["dry_run"] and PRIVATE_KEY is None:
                return self._send(STATE["setup_error"] or "Add your Kalshi API key in Settings first", "text/plain", 400)
            STATE["running"] = on
            log(f"BOT STARTED ({STATE['mode']})" if on else "BOT STOPPED")
            return self._send("{}")
        if self.path == "/api/update":
            ok, msg = do_update()
            return self._send(json.dumps({"ok": ok, "msg": msg}))
        if self.path == "/api/reset_paper":
            with LOCK:
                ORDERS[:] = [o for o in ORDERS if o["mode"] != "paper"]
                _save(ORDERS_FILE, ORDERS)
                STATE["account"] = account(STATE["account"]["cash"])
            log("Paper history reset")
            return self._send("{}")
        if self.path == "/api/config":
            try:
                n = int(self.headers.get("Content-Length", 0))
                err = update_config(json.loads(self.rfile.read(n)))
            except Exception as e:
                err = f"bad request: {e}"
            if err:
                return self._send(err, "text/plain", 400)
            with LOCK:
                STATE["account"] = account(STATE["account"]["cash"])
            log(f"Settings saved. Mode: {STATE['mode']}")
            return self._send("{}")
        self._send("not found", "text/plain", 404)


if __name__ == "__main__":
    load_config()
    apply_config()
    STATE["account"] = account(None)
    log(f"Starting Inferno: {STATE['mode']}")
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=guard_loop, daemon=True).start()
    url = f"http://127.0.0.1:{DASH_PORT}"
    print("Dashboard:", url)
    if not os.environ.get("INFERNO_RESTART"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    HTTPServer(("127.0.0.1", DASH_PORT), Handler).serve_forever()
