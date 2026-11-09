"""
Rivalz - Matchmaking bot for Duels and Spars
Requires: discord.py >= 2.6 (Components V2 + modal selects), aiohttp, python-dotenv, Pillow
Run:      py main.py
Needs the MESSAGE CONTENT INTENT enabled in the Developer Portal (Bot tab) for the gank image step.
"""
import asyncio
import hashlib
import io
import json
import math
import random
from collections import deque
import shutil
import sqlite3
import tempfile
import os
import re
import subprocess
import sys
import time
import uuid
import threading
import atexit
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote as urlquote, urlparse


def _bootstrap():
    """Install missing libraries automatically (fixes hosts that skip requirements.txt)."""
    def ok():
        try:
            import aiohttp, dotenv, PIL  # noqa: F401
            import discord
            return hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "Label")
        except ImportError:
            return False

    if ok():
        return
    pkgs = ["discord.py>=2.6.0", "aiohttp>=3.10", "python-dotenv>=1.0", "Pillow>=10.0"]
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"] + pkgs
    print("Installing required libraries...")
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        subprocess.check_call(base + ["--break-system-packages"])


_bootstrap()

import aiohttp  # noqa: E402
import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import commands, tasks  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
START_TIME = time.time()
try:  # changes whenever you upload a new main.py -> the bot knows it was updated and posts fresh panels/leaderboards
    with open(os.path.abspath(__file__), "rb") as _fh:
        CODE_HASH = hashlib.sha1(_fh.read()).hexdigest()[:12]
except OSError:
    CODE_HASH = "unknown"


# ============================================================ config
def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = env_int("OWNER_ID")
STAFF_ROLE_ID = env_int("STAFF_ROLE_ID")
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "").strip() or (f"https://discord.com/users/{OWNER_ID}" if OWNER_ID else "")

# Your ORIGINAL server's channels. They are only used once, to move that server onto the new per-server
# setup (see migrate_legacy). Every other server gets its channels from /build.
LEGACY = {
    "panel": 1568343031294459904, "log": 1568525004164042842, "results": 1568527659334377492,
    "duel_lb": 1568637988164862133, "spar_lb": 1568637960855879680,
    "cat_main": 1568343167437115423, "cat_tickets": 1568527718419664927,
    "ongoing": 1568968016493027328, "ongoing_role": 1568968211083567195,
}

EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
GANK_SECONDS = 120
# Spectators have NO limit. A member can only spectate ONE live match at a time (see active_spectating).
RIVALZ_COOLDOWN = 3           # seconds between "Rivalz <command>" messages from the same person (the developer is exempt)
LB_RESET_ID = ""   # leaderboard wipe is DISABLED. Nothing is ever reset or deleted by an update.
MSG_NOTICE_EVERY = 15         # the "Match Details" card is posted at 15 messages, again at 30 (old one removed), 45, ...
LB_MODES = ("duel", "spar")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def find_gif() -> str:
    for p in (os.path.join(BASE_DIR, "panel.gif"), os.path.join(BASE_DIR, "assets", "panel.gif")):
        if os.path.isfile(p):
            return p
    return ""


GIF_PATH = find_gif()
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID"))
VOLUME_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
# Order: DATA_DIR you set > the Railway volume (auto) > the code folder (WIPED on every Railway deploy!)
DATA_DIR = os.getenv("DATA_DIR", "").strip() or VOLUME_DIR or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
EPHEMERAL_DISK = ON_RAILWAY and not (os.getenv("DATA_DIR", "").strip() or VOLUME_DIR)
DATA_FILE = os.path.join(DATA_DIR, "rivalz_data.json")

MODES = {
    "duel": {
        "name": "Duel",
        "prefix": "clash",
        "taunt": "> Prove who is stronger. Accept to open a private ticket and voice channel.",
        "colour": 0xE74C3C,
    },
    "spar": {
        "name": "Spar",
        "prefix": "warmup",
        "taunt": "> Warm up and sharpen your skills together. Accept to open a private ticket and voice channel.",
        "colour": 0x3498DB,
    },
}
GREEN, RED, GREY = 0x2ECC71, 0xE74C3C, 0x95A5A6
STATUS_TEXT = {"open": "Open", "ended": "Ended", "finished": "Recorded"}


# ============================================================ storage
DB_FILE = os.path.join(DATA_DIR, "rivalz.sqlite3")


def open_db():
    db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; FULL made every save wait on slow volume disks and delayed replies
    db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS matches (
        g INTEGER NOT NULL, m TEXT NOT NULL, n INTEGER NOT NULL, ts INTEGER NOT NULL,
        w INTEGER NOT NULL, l INTEGER NOT NULL, ws INTEGER, ls INTEGER, PRIMARY KEY (g, m, n))""")
    db.commit()
    return db


DB = open_db()


def daily_backup():
    """One consistent copy of the whole database per day in DATA_DIR/backups (the last 14 are kept)."""
    try:
        folder = os.path.join(DATA_DIR, "backups")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, datetime.now(timezone.utc).strftime("rivalz_%Y-%m-%d.sqlite3"))
        if os.path.exists(dest):
            return
        out = sqlite3.connect(dest)
        try:
            DB.backup(out)
        finally:
            out.close()
        for old in sorted(os.listdir(folder))[:-14]:
            os.remove(os.path.join(folder, old))
    except Exception as exc:
        print(f"Daily backup failed: {exc!r}")


# ============================================================ optional free cloud database (Supabase)
# The bot keeps working from its local SQLite file. When SUPABASE_URL and SUPABASE_KEY are set, every change is also
# copied to a free Supabase (Postgres) database in the background, and on start-up the cloud copy is restored first.
# So a redeploy, a wiped disk or a new host never loses the leaderboard, tickets or settings.
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
CLOUD_ON = bool(SUPABASE_URL and SUPABASE_KEY)


def cloud_split(top: str, text: str) -> dict:
    """One local kv row -> cloud rows. A dict value is split into one row per entry ('top:sub'), so saving one ticket
    uploads one small row, not every ticket. Anything else (lists, text, lb_meta) is one row."""
    if top == "lb_meta":
        return {top: text}
    try:
        val = json.loads(text)
    except Exception:
        return {top: text}
    if isinstance(val, dict):
        return {f"{top}:{sub}": json.dumps(v, separators=(",", ":")) for sub, v in val.items()}
    return {top: text}


def cloud_join(rows: dict) -> dict:
    """Cloud rows -> local kv rows (the reverse of cloud_split)."""
    whole, parts = {}, {}
    for k, v in rows.items():
        if ":" in k:
            top, sub = k.split(":", 1)
            try:
                parts.setdefault(top, {})[sub] = json.loads(v)
            except Exception:
                continue
        else:
            whole[k] = v
    for top, d in parts.items():
        whole[top] = json.dumps(d, separators=(",", ":"))
    return whole


class Cloud:
    def __init__(self):
        self.ready = False
        self.saved = {}          # cloud key -> text as last confirmed in the cloud
        self.lock = threading.Lock()
        self.pend_kv = {}        # cloud key -> text (None = delete)
        self.pend_up = {}        # (g, m, n) -> match row tuple
        self.pend_del = set()    # (g, m, n)
        self.wake = threading.Event()
        self.last_error = ""
        self.last_ok = 0.0

    def _call(self, method, path, body=None, extra=None, timeout=25):
        headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
        if SUPABASE_KEY.startswith("eyJ"):          # old style (JWT) keys also go in Authorization
            headers["Authorization"] = f"Bearer {SUPABASE_KEY}"
        if extra:
            headers.update(extra)
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw) if raw else None

    def _fetch_all(self, table, order):
        rows, offset = [], 0
        while True:
            chunk = self._call("GET", f"{table}?select=*&order={order}&limit=1000&offset={offset}") or []
            rows.extend(chunk)
            if len(chunk) < 1000:
                return rows
            offset += 1000

    def start(self):
        if not CLOUD_ON:
            print("Storage: local SQLite file only. Set SUPABASE_URL and SUPABASE_KEY for the free cloud database.")
            return
        try:
            kv_rows = {r["k"]: r["v"] for r in self._fetch_all("rivalz_kv", "k.asc")}
            m_rows = self._fetch_all("rivalz_matches", "g.asc,m.asc,n.asc")
        except Exception as exc:
            self.last_error = repr(exc)
            print(f"Cloud database: could not reach Supabase ({exc!r}). Running on the local file for now, will keep retrying.")
            threading.Thread(target=self._late_connect, daemon=True, name="cloud-retry").start()
            return
        if kv_rows or m_rows:   # the cloud copy is the truth: restore it into the local file
            try:
                if DB.execute("SELECT COUNT(*) FROM kv").fetchone()[0]:
                    dest = os.path.join(DATA_DIR, f"rivalz_before_cloud_restore_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.sqlite3")
                    out = sqlite3.connect(dest)
                    try:
                        DB.backup(out)
                    finally:
                        out.close()
            except Exception as exc:
                print(f"Cloud database: could not keep a copy of the local file first: {exc!r}")
            joined = cloud_join(kv_rows)
            with DB:
                DB.execute("DELETE FROM kv")
                DB.execute("DELETE FROM matches")
                DB.executemany("INSERT INTO kv (key, value) VALUES (?, ?)", list(joined.items()))
                DB.executemany("INSERT OR REPLACE INTO matches (g, m, n, ts, w, l, ws, ls) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               [(r["g"], r["m"], r["n"], r["ts"], r["w"], r["l"], r.get("ws"), r.get("ls")) for r in m_rows])
            self.saved = dict(kv_rows)
            print(f"Cloud database: restored {len(kv_rows)} records and {len(m_rows)} matches from Supabase.")
        else:                   # first run with an empty cloud: upload what is local
            for key, val in DB.execute("SELECT key, value FROM kv").fetchall():
                self.pend_kv.update(cloud_split(key, val))
            for row in DB.execute("SELECT g, m, n, ts, w, l, ws, ls FROM matches").fetchall():
                self.pend_up[(row[0], row[1], row[2])] = tuple(row)
            print(f"Cloud database: it is empty, uploading {len(self.pend_kv)} records and {len(self.pend_up)} matches.")
        self.ready = True
        threading.Thread(target=self._worker, daemon=True, name="cloud-sync").start()
        atexit.register(self.flush_now)
        if self.pend_kv or self.pend_up:
            self.wake.set()

    def _late_connect(self):
        """Supabase was down at start-up: retry every 30s. Once it answers, what this run saved locally is uploaded
        (nothing in the cloud is overwritten or deleted by this)."""
        while not self.ready:
            time.sleep(30)
            try:
                self._fetch_all("rivalz_kv", "k.asc")
            except Exception as exc:
                self.last_error = repr(exc)
                continue
            try:
                for key, val in DB.execute("SELECT key, value FROM kv").fetchall():
                    self.pend_kv.update(cloud_split(key, val))
                for row in DB.execute("SELECT g, m, n, ts, w, l, ws, ls FROM matches").fetchall():
                    self.pend_up[(row[0], row[1], row[2])] = tuple(row)
            except Exception as exc:
                print(f"Cloud database: late upload prepare failed: {exc!r}")
            self.ready = True
            threading.Thread(target=self._worker, daemon=True, name="cloud-sync").start()
            atexit.register(self.flush_now)
            self.wake.set()
            print("Cloud database: Supabase is reachable now, uploading the local data.")

    def put_top(self, top, text):
        if not self.ready:
            return
        want = cloud_split(top, text)
        prefix = top + ":"
        with self.lock:
            def eff(k):
                return self.pend_kv[k] if k in self.pend_kv else self.saved.get(k)
            keys = {k for k in list(self.saved) + list(self.pend_kv) if k == top or k.startswith(prefix)}
            for k, v in want.items():
                if eff(k) != v:
                    self.pend_kv[k] = v
            for k in keys:
                if k not in want and eff(k) is not None:
                    self.pend_kv[k] = None
        self.wake.set()

    def put_matches(self, rows, gone):
        if not self.ready:
            return
        with self.lock:
            for r in rows:
                self.pend_up[(r[0], r[1], r[2])] = tuple(r)
                self.pend_del.discard((r[0], r[1], r[2]))
            for k in gone:
                self.pend_up.pop(tuple(k), None)
                self.pend_del.add(tuple(k))
        self.wake.set()

    def pending(self) -> int:
        with self.lock:
            return len(self.pend_kv) + len(self.pend_up) + len(self.pend_del)

    def _flush(self) -> bool:
        with self.lock:
            kv, up, dele = dict(self.pend_kv), dict(self.pend_up), set(self.pend_del)
            self.pend_kv.clear()
            self.pend_up.clear()
            self.pend_del.clear()
        if not (kv or up or dele):
            return True
        try:
            merge = {"Prefer": "resolution=merge-duplicates,return=minimal"}
            rows = [{"k": k, "v": v} for k, v in kv.items() if v is not None]
            for i in range(0, len(rows), 100):
                self._call("POST", "rivalz_kv?on_conflict=k", rows[i:i + 100], merge)
            for k, v in kv.items():
                if v is None:
                    self._call("DELETE", "rivalz_kv?k=eq." + urlquote(k, safe=""), None, {"Prefer": "return=minimal"})
            mrows = [{"g": r[0], "m": r[1], "n": r[2], "ts": r[3], "w": r[4], "l": r[5], "ws": r[6], "ls": r[7]} for r in up.values()]
            for i in range(0, len(mrows), 100):
                self._call("POST", "rivalz_matches?on_conflict=g,m,n", mrows[i:i + 100], merge)
            for g, m, n in dele:
                self._call("DELETE", f"rivalz_matches?g=eq.{int(g)}&m=eq.{urlquote(str(m), safe='')}&n=eq.{int(n)}", None,
                           {"Prefer": "return=minimal"})
        except Exception as exc:
            with self.lock:   # put back what failed, without overwriting anything newer
                for k, v in kv.items():
                    self.pend_kv.setdefault(k, v)
                for k, r in up.items():
                    if k not in self.pend_del:
                        self.pend_up.setdefault(k, r)
                for k in dele:
                    if k not in self.pend_up:
                        self.pend_del.add(k)
            if str(exc) != self.last_error:
                print(f"Cloud database: upload failed, will retry ({exc!r})")
            self.last_error = str(exc)
            return False
        with self.lock:
            for k, v in kv.items():
                if v is None:
                    self.saved.pop(k, None)
                else:
                    self.saved[k] = v
        self.last_error = ""
        self.last_ok = time.time()
        return True

    def _worker(self):
        fails = 0
        while True:
            self.wake.wait()
            time.sleep(1.5)          # collect a burst of changes into one upload
            self.wake.clear()
            if self._flush():
                fails = 0
            else:
                fails += 1
                time.sleep(min(60, 5 * fails))
                self.wake.set()

    def flush_now(self):
        if self.ready:
            for _ in range(3):
                if self._flush():
                    break

    def status_text(self) -> str:
        local = f"{len(store.d.get('tickets', {}))} tickets, {len(lbstore.d['matches'])} leaderboard matches stored locally"
        if not CLOUD_ON:
            return f"Storage: local file only ({DB_FILE}). {local}. Set SUPABASE_URL and SUPABASE_KEY to turn on the free cloud database."
        if not self.ready:
            return f"Storage: Supabase is set up but was not reachable at start-up, so this run uses the local file only. {local}. Last error: {self.last_error or 'unknown'}"
        ago = f"{int(time.time() - self.last_ok)}s ago" if self.last_ok else "nothing uploaded yet"
        err = f" Last error: {self.last_error}" if self.last_error else ""
        return f"Storage: Supabase connected. {local}. Waiting to upload: {self.pending()}. Last upload: {ago}.{err}"


CLOUD = Cloud()
CLOUD.start()


class Store:
    """Everything except the leaderboard matches. Same store.d dict as before, saved into SQLite (table kv),
    one row per top-level key. An old rivalz_data.json is imported once when the database is new."""
    def __init__(self, path: str):
        self.legacy = path
        self.d = {}
        self._saved = {}
        rows = DB.execute("SELECT key, value FROM kv WHERE key != 'lb_meta'").fetchall()
        for k, v in rows:
            try:
                self.d[k] = json.loads(v)
                self._saved[k] = v
            except Exception as exc:
                print(f"Could not read saved data '{k}': {exc}")
        if not rows and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.d = json.load(fh)
                print(f"Imported {path} into the SQLite database.")
            except Exception as exc:
                print(f"Could not read {path}, starting fresh: {exc}")
                self.d = {}
        self.d.setdefault("counters", {})
        for mode in ("duel", "spar"):
            self.d["counters"].setdefault(mode, 0)
        for key in ("challenges", "profiles", "tickets", "archive", "guilds"):
            self.d.setdefault(key, {})
        self.d.setdefault("matches", [])
        self.d.setdefault("trusted", [])   # users the developer trusted (R ta @user): they can use every owner command
        self.save()

    def save(self):
        changed = []
        with DB:  # one transaction: all or nothing
            for k, val in self.d.items():
                txt = json.dumps(val, separators=(",", ":"))
                if self._saved.get(k) != txt:
                    DB.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, txt))
                    self._saved[k] = txt
                    changed.append((k, txt))
        for k, txt in changed:   # copy to the free cloud database too (background, never blocks the bot)
            try:
                CLOUD.put_top(k, txt)
            except Exception as exc:
                print(f"Cloud sync error: {exc!r}")


store = Store(DATA_FILE)


# ============================================================ roblox helpers
class RobloxClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session

    async def _post(self, url, body):
        try:
            async with self.s.post(url, json=body, headers={"Content-Type": "application/json"},
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
                try:
                    return r.status, await r.json(content_type=None)
                except Exception:
                    return r.status, None
        except Exception:
            return 0, None

    async def user_by_name(self, name: str):
        st, data = await self._post(
            "https://users.roblox.com/v1/usernames/users",
            {"usernames": [name], "excludeBannedUsers": False},
        )
        try:
            u = data["data"][0]
            return {"id": int(u["id"]), "name": u["name"], "display": u.get("displayName") or u["name"]}
        except Exception:
            return None

    async def avatars(self, ids):
        """Headshot image urls keyed by Roblox user id."""
        try:
            async with self.s.get(
                "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                params={"userIds": ",".join(str(i) for i in ids), "size": "150x150", "format": "Png", "isCircular": "false"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json(content_type=None)
            return {int(x["targetId"]): x["imageUrl"] for x in data.get("data", []) if x.get("imageUrl")}
        except Exception:
            return {}


# ============================================================ per-server setup + authorization
def gcfg(gid) -> dict:
    """Saved setup for one server: authorized flag, channel ids, category ids, leaderboard message ids."""
    g = store.d["guilds"].setdefault(str(gid), {})
    g.setdefault("authorized", False)
    for key in ("ch", "cat", "lb_msg", "lb_period", "role"):
        g.setdefault(key, {})
    return g


def is_authorized(gid) -> bool:
    return bool(gid) and bool(store.d["guilds"].get(str(gid), {}).get("authorized"))


# Authorized servers are ALSO remembered in a tiny separate file, in the AUTHORIZED_GUILDS variable, and any server
# owned by the developer is trusted automatically. So an update / redeploy / wiped database never makes the bot say
# "not authorized yet" in a server that was already set up.
AUTH_BACKUP = os.path.join(DATA_DIR, "authorized_guilds.json")
ENV_AUTH_GUILDS = {int(x) for x in re.findall(r"\d{15,20}", os.getenv("AUTHORIZED_GUILDS", ""))}


def sync_auth_backup():
    try:
        ids = sorted(int(g) for g, c in store.d["guilds"].items() if c.get("authorized"))
        with open(AUTH_BACKUP, "w", encoding="utf-8") as fh:
            json.dump(ids, fh)
    except Exception as exc:
        print(f"Could not save the authorized-server list: {exc!r}")


def authorize_guild(gid, by=None):
    cfg = gcfg(gid)
    cfg["authorized"] = True
    if by:
        cfg["authorized_by"], cfg["authorized_at"] = by, int(time.time())
    store.save()
    sync_auth_backup()


def restore_authorization(guilds):
    """Run at start-up (after every update): re-authorize servers that were authorized before."""
    remembered = set(ENV_AUTH_GUILDS)
    try:
        with open(AUTH_BACKUP, "r", encoding="utf-8") as fh:
            remembered |= {int(x) for x in json.load(fh)}
    except Exception:
        pass
    changed = False
    for guild in guilds:
        if is_authorized(guild.id):
            continue
        if guild.id in remembered or (OWNER_ID and guild.owner_id == OWNER_ID):
            gcfg(guild.id)["authorized"] = True
            changed = True
            print(f"Re-authorized {guild.name} automatically.")
    if changed:
        store.save()
    sync_auth_backup()


def cfg_channel_id(gid, key):
    return gcfg(gid)["ch"].get(key)


# ============================================================ manual server link (set while requesting)
# Where a share link is opened through Roseal. If Roseal's page for share links uses another address, change it
# here (or set ROSEAL_SHARE_URL in .env). {code} and {type} are filled in for you.
ROSEAL_SHARE_URL = os.getenv("ROSEAL_SHARE_URL", "").strip() or "https://www.roseal.live/join?code={code}&type={type}"
LINK_HOSTS = {"roblox.com", "www.roblox.com", "web.roblox.com", "roseal.live", "www.roseal.live"}
JOB_RE = re.compile(r"^[0-9a-fA-F-]{32,40}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SERVER_HELP = (
    "> Paste a Roblox server link. These work:\n"
    "> - a **Roseal** join link (`roseal.live/join?placeId=...&gameInstanceId=...`)\n"
    "> - a Roblox join link (`roblox.com/games/start?placeId=...&gameInstanceId=...`)\n"
    "> - a **private server** link (`roblox.com/games/ID/name?privateServerLinkCode=...`)\n"
    "> - a Roblox **share** link (`roblox.com/share?code=...&type=ExperienceInvite`)")


def parse_server_link(raw):
    """Turn whatever the player pasted into a clean server dict, or None. We only ever keep ids, never the
    pasted text, and rebuild every URL ourselves, so a link can not send anyone to another website."""
    raw = (raw or "").strip().strip("<>").strip()
    if not raw:
        return None
    params, path = {}, ""
    low = raw.lower()
    if low.startswith("roblox://"):
        query = raw.split("?", 1)[1] if "?" in raw else raw[len("roblox://"):]
        params = {k.lower(): v[0] for k, v in parse_qs(query).items() if v}
    else:
        if "://" not in raw:
            raw = "https://" + raw
        try:
            u = urlparse(raw)
        except ValueError:
            return None
        if (u.hostname or "").lower() not in LINK_HOSTS:
            return None
        params = {k.lower(): v[0] for k, v in parse_qs(u.query).items() if v}
        path = u.path or ""

    if path.rstrip("/").lower() == "/share" or ("code" in params and params.get("type")):
        code, stype = params.get("code", ""), params.get("type", "")
        kinds = {"experienceinvite": "ExperienceInvite", "server": "Server"}
        if CODE_RE.match(code) and stype.lower() in kinds:
            return {"kind": "share", "code": code, "stype": kinds[stype.lower()]}
        return None

    place = params.get("placeid", "")
    if not place:
        m = re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/(\d+)", path, re.I)
        place = m.group(1) if m else ""
    if not place.isdigit():
        return None
    job = params.get("gameinstanceid", "")
    code = params.get("linkcode") or params.get("privateserverlinkcode") or ""
    if job and JOB_RE.match(job):
        return {"kind": "public", "place": place, "job": job}
    if code and CODE_RE.match(code):
        return {"kind": "private", "place": place, "code": code}
    return None


def server_urls(srv):
    """Returns (roseal_url or None, roblox_url) for a saved server dict."""
    if not srv:
        return None, None
    kind = srv.get("kind")
    if kind == "public":
        return (f"https://www.roseal.live/join?placeId={srv['place']}&gameInstanceId={srv['job']}",
                f"https://www.roblox.com/games/start?placeId={srv['place']}&gameInstanceId={srv['job']}")
    if kind == "private":
        return None, f"https://www.roblox.com/games/start?placeId={srv['place']}&linkCode={srv['code']}"
    if kind == "share":
        stype = srv.get("stype", "ExperienceInvite")
        roseal = ROSEAL_SHARE_URL.format(code=srv["code"], type=stype)
        return roseal, f"https://www.roblox.com/share?code={srv['code']}&type={stype}"
    return None, None


def server_label(srv) -> str:
    return {"public": "Public server", "private": "Private server", "share": "Share link"}.get((srv or {}).get("kind"), "Not set")


# ============================================================ card builders
def esc(v) -> str:
    return str(v).replace("`", "'")


def md(v) -> str:
    return re.sub(r"([\\_*~`|>])", r"\\\1", str(v))


def field(label: str, value) -> str:
    return f"**{label}:** `{esc(value)}`"


def rounds_text(n: int) -> str:
    return f"First to {n}"


def max_total_rounds(n: int) -> int:
    """First to N: the most rounds a match can last is 2N-1 (first to 5 -> 9 rounds)."""
    return max(1, 2 * n - 1)


def add_buttons(container, buttons):
    container.add_item(discord.ui.Separator(visible=False))
    for i in range(0, len(buttons), 5):
        row = discord.ui.ActionRow()
        for b in buttons[i:i + 5]:
            row.add_item(b)
        container.add_item(row)


def build_card(title, sections, *, accent=None, buttons=None, top_text=None, media=None, timeout=None):
    view = discord.ui.LayoutView(timeout=timeout)
    if top_text:
        view.add_item(discord.ui.TextDisplay(top_text))
    container = discord.ui.Container(accent_colour=accent)
    container.add_item(discord.ui.TextDisplay(f"## {title}"))
    for sec in sections:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(sec if isinstance(sec, str) else "\n".join(sec)))
    if media:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=media)
        container.add_item(gallery)
    if buttons:
        add_buttons(container, buttons)
    view.add_item(container)
    return view


async def safe_reply(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


async def notice(interaction: discord.Interaction, title: str, text, *, accent=RED):
    """Ephemeral card reply that works whether or not the interaction was already answered."""
    view = build_card(title, [text if isinstance(text, (list, str)) else str(text)], accent=accent)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)
    except discord.HTTPException:
        pass


async def guard(interaction: discord.Interaction, gid=None) -> bool:
    """True when this server is authorized. Otherwise replies and returns False. The developer always passes."""
    gid = gid or interaction.guild_id
    if is_owner(interaction.user) or is_authorized(gid):
        return True
    await notice(interaction, "Not Authorized",
                 "> Rivalz is not authorized in this server yet. The bot owner needs to run `/authorize`.", accent=GREY)
    return False


async def try_dm(user, view):
    try:
        return await user.send(view=view)
    except (discord.Forbidden, discord.HTTPException):
        return None


async def edit_dm(ch, view):
    try:
        dm = bot.get_partial_messageable(ch["dm_channel_id"], type=discord.ChannelType.private)
        await dm.get_partial_message(ch["dm_message_id"]).edit(view=view)
    except Exception:
        pass


# ============================================================ ticket state helpers
def is_real_owner(user) -> bool:
    """Only the developer (OWNER_ID): the one person who can trust or untrust others."""
    return bool(OWNER_ID) and getattr(user, "id", None) == OWNER_ID


def is_trusted(user) -> bool:
    return getattr(user, "id", None) in set(store.d.get("trusted") or [])


def is_owner(user) -> bool:
    """The developer and every trusted user (R ta @user) bypass every restriction and can use every owner command."""
    return is_real_owner(user) or is_trusted(user)


def is_staff(user) -> bool:
    if is_owner(user):
        return True
    if isinstance(user, discord.Member):
        if user.guild_permissions.manage_channels:
            return True
        if STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in user.roles):
            return True
    return False


def side_of(t, uid):
    for i, p in enumerate(t["p"]):
        if p["discord_id"] == uid:
            return i
    return None


def can_act(t, user) -> bool:
    return side_of(t, user.id) is not None or is_staff(user)


def scoreboard(t) -> str:
    p0, p1 = t["p"]
    s0, s1 = t["scores"]

    def f(s):
        return "-" if s is None else str(s)

    return f"**{md(p0['roblox_name'])}** `{f(s0)}` - `{f(s1)}` **{md(p1['roblox_name'])}**"


def result_text(t) -> str:
    s0, s1 = t["scores"]
    p0, p1 = t["p"]
    if s0 is None or s1 is None:
        return "Not recorded"
    if s0 > s1:
        return f"{p0['roblox_name']} wins"
    if s1 > s0:
        return f"{p1['roblox_name']} wins"
    return "Draw"


def profile_url(p) -> str:
    return f"https://www.roblox.com/users/{p['roblox_id']}/profile"


def vc_url(t):
    if t.get("vc_id"):
        return f"https://discord.com/channels/{t['guild_id']}/{t['vc_id']}"
    return None


# ============================================================ time + result helpers
def fmt_duration(seconds) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def match_duration(t) -> str:
    return fmt_duration((t.get("ended_at") or time.time()) - t["created"])


def scores_plain(t) -> str:
    s0, s1 = t["scores"]
    return f"{'-' if s0 is None else s0} - {'-' if s1 is None else s1}"


def winner_index(t):
    s0, s1 = t["scores"]
    if s0 is None or s1 is None or s0 == s1:
        return None
    return 0 if s0 > s1 else 1


def profile_buttons(players):
    return [discord.ui.Button(label=f"{p['roblox_name']}'s Profile"[:80], url=profile_url(p)) for p in players]


def ticket_jump(t) -> str:
    return f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}"


# ============================================================ ticket card
def ticket_view(t, ping=False):
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    status = t["status"]
    view = discord.ui.LayoutView(timeout=None)
    if ping:
        view.add_item(discord.ui.TextDisplay(f"<@{p0['discord_id']}> <@{p1['discord_id']}>"))

    c = discord.ui.Container(accent_colour=m["colour"] if status == "open" else GREY)
    c.add_item(discord.ui.TextDisplay(f"## {m['name']} Ticket #{t['number']:03d}"))

    for i, (label, p) in enumerate((("Challenger", p0), ("Rival", p1))):
        c.add_item(discord.ui.Separator())
        body = discord.ui.TextDisplay(
            f"### [{md(p['roblox_display'])}]({profile_url(p)}) (`@{esc(p['roblox_name'])}`)\n"
            f"> **{label}:** <@{p['discord_id']}>")
        if p.get("avatar"):
            c.add_item(discord.ui.Section(body, accessory=discord.ui.Thumbnail(p["avatar"])))
        else:
            c.add_item(body)
        if status == "open":  # each player adds their own score right under their name
            row = discord.ui.ActionRow()
            row.add_item(TicketButton(f"s{i}"))
            c.add_item(row)

    info = (
        "### Match Info\n"
        f"> **Mode:** `{m['name']}`\n"
        f"> **Rounds:** `{rounds_text(t['rounds'])}`\n"
        f"> **Status:** `{STATUS_TEXT[status]}`\n")
    if status == "open":
        info += f"> **Live Timer:** <t:{int(t['created'])}:R>\n> **Opened:** <t:{int(t['created'])}:t>"
    else:
        info += f"> **Duration:** `{match_duration(t)}`"
    if t.get("server"):
        info += f"\n> **Server:** `{server_label(t['server'])} set`"
    if t.get("vc_id"):
        info += f"\n> **Voice:** <#{t['vc_id']}>"
    specs = t.get("spectators") or []
    if specs:
        info += f"\n> **Spectators:** `{spec_count(t)}`"
        for s in specs[:12]:
            who = f" (`@{esc(s['roblox_name'])}`)" if s.get("roblox_name") else ""
            info += f"\n> - <@{s['discord_id']}>{who}"
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(info))

    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"### Scoreboard\n> {scoreboard(t)}"))

    if status != "open":
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"### Result\n> `{esc(result_text(t))}`"))

    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(
        f"-# Ticket opened by <@{p0['discord_id']}> | `{esc(p0['roblox_name'])}` | {STATUS_TEXT[status]}"))

    if status == "open":
        buttons = [TicketButton("join"), TicketButton("prof"), TicketButton("end")]
    else:
        buttons = [TicketButton("prof")]
    if vc_url(t):
        buttons.append(discord.ui.Button(label="Join VC", url=vc_url(t)))
    add_buttons(c, buttons)
    view.add_item(c)
    return view


async def refresh_ticket(t):
    try:
        channel = bot.get_channel(t["channel_id"]) or await bot.fetch_channel(t["channel_id"])
        await channel.get_partial_message(t["message_id"]).edit(view=ticket_view(t))
    except discord.HTTPException as exc:
        print(f"Could not refresh ticket card: {exc!r}")


def summary_sections(ch, requester_mention, opponent_mention):
    m = MODES[ch["mode"]]
    return [[
        f"**Challenger:** {requester_mention}",
        f"**Rival:** {opponent_mention}",
        field("Mode", m["name"]),
        field("Rounds", rounds_text(ch["rounds"])),
    ]]


def challenge_dm_view(ch, requester):
    m = MODES[ch["mode"]]
    sections = [
        [f"> **{requester.display_name}** has challenged you to a **{m['name']}**.", m["taunt"]],
        [
            f"**Challenger:** {requester.mention} (`@{esc(requester.name)}`)",
            field("Mode", m["name"]),
            field("Rounds", rounds_text(ch["rounds"])),
            field("Server", server_label(ch.get("server"))),
        ],
        [f"> This challenge expires <t:{int(ch['expires'])}:R>."],
    ]
    return build_card("You Have Been Challenged", sections, accent=m["colour"],
                      buttons=[ChallengeButton("acc", ch["id"]), ChallengeButton("dec", ch["id"])])


# ============================================================ logs + results channels
NO_PING = discord.AllowedMentions.none()


async def get_channel_safe(cid: int):
    try:
        return bot.get_channel(cid) or await bot.fetch_channel(cid)
    except discord.HTTPException as exc:
        print(f"Could not reach channel {cid}: {exc!r}")
        return None


async def send_to(cid: int, view, file=None, files=None):
    chan = await get_channel_safe(cid)
    if not chan:
        return None
    try:
        kwargs = {"view": view, "allowed_mentions": NO_PING}
        if files:
            kwargs["files"] = files
        elif file:
            kwargs["file"] = file
        return await chan.send(**kwargs)
    except discord.HTTPException as exc:
        print(f"Send to {cid} failed: {exc!r}")
        return None


async def send_cfg(gid, key, view, file=None, files=None):
    """Send to one of this server's saved channels (panel, log, results, ...)."""
    cid = cfg_channel_id(gid, key)
    if not cid:
        return None
    return await send_to(cid, view, file=file, files=files)


async def log_opened(t):
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    sections = [
        [f"**Challenger:** <@{p0['discord_id']}>",
         f"**Rival:** <@{p1['discord_id']}>",
         field("Rounds", rounds_text(t["rounds"])),
         f"**Opened:** <t:{int(t['created'])}:F>"],
        [f"-# Ticket: <#{t['channel_id']}>"],
        ["-# Rivalz"],
    ]
    buttons = [discord.ui.Button(label="More Info", url=ticket_jump(t))]
    await send_cfg(t["guild_id"], "log", build_card(f"{m['name']}-ticket #{t['number']:03d} Opened", sections,
                                             accent=m["colour"], buttons=buttons))


async def log_closed(t, by_id):
    m = MODES[t["mode"]]
    sections = [
        [field("Result", result_text(t)),
         field("Score", scores_plain(t)),
         field("Duration", match_duration(t)),
         f"**Ended By:** <@{by_id}>"],
        ["-# Check the full result in the results channel."],
        ["-# Rivalz"],
    ]
    buttons = [discord.ui.Button(label="More Info", url=ticket_jump(t)),
               LogSpectatorsButton(t["guild_id"], t["mode"], t["number"])]
    await send_cfg(t["guild_id"], "log", build_card(f"{m['name']}-ticket #{t['number']:03d} Closed", sections,
                                             accent=GREY, buttons=buttons))


def archive_of(t) -> dict:
    return {
        "mode": t["mode"], "number": t["number"], "rounds": t["rounds"], "p": t["p"], "scores": t["scores"],
        "guild_id": t["guild_id"], "created": t["created"], "result_url": t.get("result_url"), "server": t.get("server"),
        "reopened": False, "deleted_by": None, "deleted_at": 0, "spectators": list(t.get("spectators") or []),
    }


def deleted_view(a, key, reopened_by=None):
    m = MODES[a["mode"]]
    fields = [f"**Deleted By:** <@{a['deleted_by']}>", f"**Deleted:** <t:{int(a['deleted_at'])}:F>"]
    if reopened_by:
        fields.append(f"**Re-Opened By:** <@{reopened_by}>")
    buttons = []
    if not reopened_by:
        buttons.append(ReopenButton(key))
    if a.get("result_url"):
        buttons.append(discord.ui.Button(label="More Info", url=a["result_url"]))
    buttons.append(discord.ui.Button(label="Open Profile", url=profile_url(a["p"][0])))  # the requester
    buttons.append(LogSpectatorsButton(a["guild_id"], a["mode"], a["number"]))
    return build_card(f"{m['name']}-ticket #{a['number']:03d} Deleted", [fields, ["-# Rivalz"]],
                      accent=GREY, buttons=buttons)


def find_spectators(gid: int, mode: str, number: int):
    """Spectators of a match: from the live ticket, else from the archive. None = that match is not tracked any more."""
    for t in store.d["tickets"].values():
        if t.get("guild_id") == gid and t.get("mode") == mode and t.get("number") == number:
            return list(t.get("spectators") or [])
    found = None
    for a in store.d["archive"].values():
        if a.get("guild_id") == gid and a.get("mode") == mode and a.get("number") == number:
            found = a
    return None if found is None else list(found.get("spectators") or [])


class LogSpectatorsButton(discord.ui.DynamicItem[discord.ui.Button],
                          template=r"rv:logspec:(?P<gid>\d+):(?P<mode>duel|spar):(?P<num>\d+)"):
    """On the closed / deleted ticket log cards: shows who watched that match."""
    def __init__(self, gid: int, mode: str, num: int):
        super().__init__(discord.ui.Button(label="Spectators", style=discord.ButtonStyle.secondary,
                                           custom_id=f"rv:logspec:{gid}:{mode}:{num}"))
        self.gid, self.mode, self.num = int(gid), mode, int(num)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["gid"]), match["mode"], int(match["num"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LogSpectatorsButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        specs = find_spectators(self.gid, self.mode, self.num)
        title = f"{MODES[self.mode]['name']} #{self.num:03d} Spectators"
        if specs is None:
            return await notice(interaction, "Not Tracked", "> This match is no longer stored, so its spectators are unknown.",
                                accent=GREY)
        if not specs:
            return await interaction.response.send_message(view=build_card(
                title, ["> Nobody watched this match."], accent=GREY), ephemeral=True)
        lines = []
        for s in specs[:12]:
            who = f" (`@{esc(s['roblox_name'])}`)" if s.get("roblox_name") else ""
            tag = " - Developer" if s.get("dev") else ""
            joined = f" - joined <t:{int(s['joined'])}:R>" if s.get("joined") else ""
            lines.append(f"> <@{s['discord_id']}>{who}{tag}{joined}")
        await interaction.response.send_message(view=build_card(
            title, [[field("Spectators", len(specs))], lines], accent=GREY), ephemeral=True)


async def delete_result_message(url) -> bool:
    """Deletes a posted result card, given its https://discord.com/channels/<guild>/<channel>/<message> link."""
    m = re.search(r"/channels/\d+/(\d+)/(\d+)", url or "")
    if not m:
        return False
    chan = await get_channel_safe(int(m.group(1)))
    if not chan:
        return False
    try:
        await chan.get_partial_message(int(m.group(2))).delete()
        return True
    except discord.HTTPException:
        return False


class ReopenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:reopen:(?P<key>[0-9a-f]{8})"):
    def __init__(self, key: str):
        super().__init__(discord.ui.Button(label="Re-Open Ticket", style=discord.ButtonStyle.primary,
                                           custom_id=f"rv:reopen:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["key"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ReopenButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not await guard(interaction):
            return
        a = store.d["archive"].get(self.key)
        if not a:
            return await notice(interaction, "Not Available", "> This ticket can no longer be re-opened.")
        if a.get("reopened"):
            return await notice(interaction, "Already Re-Opened", "> This ticket was already re-opened.")
        allowed = any(p["discord_id"] == interaction.user.id for p in a["p"]) or is_staff(interaction.user)
        if not allowed:
            return await notice(interaction, "Not Allowed", "> Only the two fighters or staff can re-open this ticket.")
        await interaction.response.defer(ephemeral=True)
        a["reopened"] = True  # guard against double clicks
        store.save()
        p0, p1 = a["p"]
        ch = {
            "id": "reopen", "mode": a["mode"], "rounds": a["rounds"], "server": a.get("server"),
            "requester": p0["discord_id"], "opponent": p1["discord_id"],
            "req": {k: p0.get(k) for k in ("roblox_id", "roblox_name", "roblox_display")},
            "opp": {k: p1.get(k) for k in ("roblox_id", "roblox_name", "roblox_display")},
        }
        try:
            channel, _t = await create_ticket(interaction.guild, ch, number=a["number"])
        except Exception as exc:
            a["reopened"] = False
            store.save()
            print(f"Re-open failed: {exc!r}")
            return await notice(interaction, "Re-Open Failed",
                                "> The ticket could not be re-opened. Both players must still be in the server.")
        unrecord_match(a["guild_id"], a["mode"], a["number"])
        schedule_lb_refresh(a["guild_id"])
        if a.get("result_url"):  # the old result must not stay in the results channel: the new one replaces it
            await delete_result_message(a["result_url"])
            a["result_url"] = None
            store.save()
        try:
            await interaction.message.edit(view=deleted_view(a, self.key, reopened_by=interaction.user.id),
                                           allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass
        await interaction.followup.send(view=build_card(
            "Ticket Re-Opened", [f"> Your ticket is ready: {channel.mention}"], accent=GREEN,
            buttons=[discord.ui.Button(label="Open Ticket", url=channel.jump_url)]), ephemeral=True)


def result_view(t):
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    final = t["status"] != "open"
    w = winner_index(t)
    if None in t["scores"]:
        head, accent = "> **Not recorded**", GREY
    elif w is None:
        head, accent = "> **Draw**", GREY
    else:
        wp = t["p"][w]
        head, accent = f"> **Winner:** <@{wp['discord_id']}> (`{esc(wp['roblox_name'])}`)", GREEN
    end_ts = (t.get("ended_at") if final else None) or t.get("scored_at") or int(time.time())
    by = (t.get("ended_by") if final else None) or t.get("scored_by")
    bullets = [
        f"- **Players** - <@{p0['discord_id']}> vs <@{p1['discord_id']}>",
        f"- **Score** - {scoreboard(t)}",
        f"- **Rounds** - `{rounds_text(t['rounds'])}`",
        f"- **Duration** - `{fmt_duration(end_ts - t['created'])}`",
        f"- **{'Ended At' if final else 'Updated At'}** - <t:{int(end_ts)}:F>",
    ]
    if by:
        bullets.append(f"- **{'Ended By' if final else 'Updated By'}** - <@{by}>")
    note = ("-# This match is closed. The result is final." if final
            else "-# Live result. It becomes final when the ticket is ended.")
    buttons, media = [], None
    if t.get("gank_card"):
        media = f"attachment://{t['gank_card']}"
        buttons.append(OpenGankButton())
    buttons += profile_buttons(t["p"])
    return build_card(f"{m['name']} #{t['number']:03d} Result", [[head], bullets, [note]],
                      accent=accent, buttons=buttons, media=media)


async def update_result(t, by_id=None, force=False, files=None) -> bool:
    """Post (or edit) the result card. Returns True when the card is up to date."""
    complete = None not in t["scores"]
    if not complete and not t.get("result_msg") and not force:
        return False
    if t["status"] == "open" and complete:
        t["scored_at"] = int(time.time())
        t["scored_by"] = by_id
        store.save()
    view = result_view(t)
    results_id = cfg_channel_id(t["guild_id"], "results")
    chan = await get_channel_safe(results_id) if results_id else None
    if not chan:
        return False
    if t.get("result_msg"):
        kwargs = {"attachments": files} if files else {}
        if not files and t.get("gank_card"):   # an edit must never drop the gank picture
            try:
                old = await chan.fetch_message(t["result_msg"])
                if old.attachments:
                    kwargs = {"attachments": list(old.attachments)}
            except discord.HTTPException:
                pass
        try:
            await chan.get_partial_message(t["result_msg"]).edit(view=view, allowed_mentions=NO_PING, **kwargs)
            return True
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            print(f"Could not edit the result card: {exc!r}")
            return False
    msg = await send_to(results_id, view, files=files)
    if msg:
        t["result_msg"], t["result_url"] = msg.id, msg.jump_url
        store.save()
        return True
    return False


# ============================================================ ticket creation
async def create_ticket(guild: discord.Guild, ch, number=None):
    m = MODES[ch["mode"]]
    if number is None:
        store.d["counters"][ch["mode"]] += 1
        n = store.d["counters"][ch["mode"]]
        store.save()
    else:
        n = number
    name = f"{m['prefix']}-{n:03d}"
    vc_name = f"{m['prefix'].title()} {n:03d}"

    cfg_g = gcfg(guild.id)
    cat_id = cfg_g["cat"].get("tickets")
    category = guild.get_channel(cat_id) if cat_id else None
    if not isinstance(category, discord.CategoryChannel):  # deleted or never built: make the tickets category again
        try:
            staff_r = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
            cat_ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                            connect=True, speak=True)}
            if staff_r:
                cat_ow[staff_r] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            category = await guild.create_category("TICKETS", overwrites=cat_ow, reason="Rivalz: tickets category was missing")
            cfg_g["cat"]["tickets"] = category.id
            store.save()
        except discord.HTTPException as exc:
            print(f"Could not recreate the tickets category: {exc!r}")
            category = None
    members = []
    for uid in (ch["requester"], ch["opponent"]):
        members.append(guild.get_member(uid) or await guild.fetch_member(uid))
    staff_role = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None

    text_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                              embed_links=True, read_message_history=True, attach_files=True),
    }
    for mem in members:
        text_ow[mem] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                   read_message_history=True, attach_files=True)
    if staff_role:
        text_ow[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                          read_message_history=True)
    channel = await guild.create_text_channel(
        name, category=category, overwrites=text_ow,
        topic=f"{m['name']} #{n:03d} | challenge {ch['id']}", reason=f"Rivalz {m['name']} ticket")

    vc = None
    try:
        voice_ow = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, manage_channels=True),
        }
        for mem in members:
            voice_ow[mem] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        if staff_role:
            voice_ow[staff_role] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
        vc = await guild.create_voice_channel(vc_name, category=category, overwrites=voice_ow,
                                              reason=f"Rivalz {m['name']} voice channel")
    except discord.HTTPException as exc:
        print(f"Could not create the voice channel (give the bot Connect and Speak): {exc!r}")

    req, opp = ch["req"], ch["opp"]
    avatars = await bot.roblox.avatars([req["roblox_id"], opp["roblox_id"]])
    players = []
    for discord_id, prof in ((ch["requester"], req), (ch["opponent"], opp)):
        players.append({
            "discord_id": discord_id, "roblox_id": prof["roblox_id"], "roblox_name": prof["roblox_name"],
            "roblox_display": prof["roblox_display"], "avatar": avatars.get(prof["roblox_id"]),
        })

    t = {
        "challenge": ch["id"], "guild_id": guild.id, "channel_id": channel.id, "vc_id": vc.id if vc else None,
        "mode": ch["mode"], "number": n, "name": name, "rounds": ch["rounds"], "created": int(time.time()),
        "status": "open", "p": players, "scores": [None, None], "message_id": None, "server": ch.get("server"),
        "ended_at": None, "ended_by": None, "scored_at": None, "scored_by": None,
        "result_msg": None, "result_url": None,
        "gank_url": None, "gank_by": None, "gank_deadline": 0, "gank_msg": None,
        "spectators": [], "ongoing_msg": None, "msgs": 0, "notice_msg": None,
    }
    msg = await channel.send(view=ticket_view(t, ping=True))
    t["message_id"] = msg.id
    store.d["tickets"][str(channel.id)] = t
    store.save()
    await log_opened(t)
    await post_ongoing(t)  # Ongoing-Ping: live card with a Join as Spectator button
    return channel, t


async def process_accept(ch, rival, prof):
    """Create the ticket for an accepted challenge. Returns (channel, error)."""
    ch["status"] = "accepted"
    ch["opp"] = {k: prof.get(k) for k in ("roblox_id", "roblox_name", "roblox_display")}
    store.save()
    guild = bot.get_guild(ch["guild_id"])
    try:
        channel, t = await create_ticket(guild, ch)
    except Exception as exc:
        ch["status"] = "pending"
        store.save()
        print(f"Ticket creation failed: {exc!r}")
        return None, exc

    m = MODES[ch["mode"]]
    await edit_dm(ch, build_card(
        "Challenge Accepted",
        summary_sections(ch, f"<@{ch['requester']}>", f"<@{ch['opponent']}>") + [f"> Your ticket is ready: {channel.mention}"],
        accent=GREEN, buttons=[discord.ui.Button(label="Open Ticket", url=channel.jump_url)]))
    try:
        requester = await bot.fetch_user(ch["requester"])
        await try_dm(requester, build_card(
            "Challenge Accepted",
            [f"> **{rival.display_name}** accepted your **{m['name']}** request.",
             [f"**Ticket Channel:** {channel.mention}", field("Rounds", rounds_text(ch["rounds"]))]],
            accent=GREEN, buttons=[discord.ui.Button(label="Open Ticket", url=channel.jump_url)]))
    except discord.HTTPException:
        pass
    return channel, None


# ============================================================ challenge DM buttons
class ChallengeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:(?P<act>acc|dec):(?P<cid>[0-9a-f]{8})"):
    def __init__(self, act: str, cid: str):
        accept = act == "acc"
        super().__init__(discord.ui.Button(
            label="Accept" if accept else "Decline",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
            custom_id=f"rv:{act}:{cid}",
        ))
        self.act, self.cid = act, cid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], match["cid"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ChallengeButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        ch = store.d["challenges"].get(self.cid)
        if not ch or ch["status"] != "pending":
            return await interaction.response.edit_message(
                view=build_card("Challenge Closed", ["> This challenge is no longer active."], accent=GREY))
        if interaction.user.id != ch["opponent"]:
            return await interaction.response.send_message("This challenge is not for you.", ephemeral=True)
        if not await guard(interaction, ch["guild_id"]):
            return

        m = MODES[ch["mode"]]
        if self.act == "dec":
            ch["status"] = "declined"
            store.save()
            await interaction.response.edit_message(view=build_card(
                "Challenge Declined",
                summary_sections(ch, f"<@{ch['requester']}>", f"<@{ch['opponent']}>") + ["> You declined this challenge."],
                accent=RED))
            try:
                requester = await bot.fetch_user(ch["requester"])
                await try_dm(requester, build_card(
                    "Challenge Declined",
                    [f"> **{interaction.user.display_name}** declined your **{m['name']}** request.",
                     [field("Mode", m["name"]), field("Rounds", rounds_text(ch["rounds"]))]],
                    accent=RED))
            except discord.HTTPException:
                pass
            return

        prof = store.d["profiles"].get(str(interaction.user.id))
        if not prof or not prof.get("roblox_name"):
            return await interaction.response.send_modal(RivalNameModal(self.cid))
        await interaction.response.defer()
        channel, err = await process_accept(ch, interaction.user, prof)
        if err:
            await notice(interaction, "Ticket Failed", "The ticket could not be opened right now. Press Accept again in a moment.")


class RivalNameModal(discord.ui.Modal):
    """Asked once when a rival accepts without having saved a Roblox username."""
    def __init__(self, cid: str):
        super().__init__(title="Roblox Username", timeout=600)
        self.cid = cid
        self.name = discord.ui.TextInput(placeholder="Your Roblox username", min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", description="Saved to your Settings, so you only enter it once.",
                                       component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ch = store.d["challenges"].get(self.cid)
        if not ch or ch["status"] != "pending" or interaction.user.id != ch["opponent"]:
            return await notice(interaction, "Challenge Closed", "> This challenge is no longer active.")
        rb = await bot.roblox.user_by_name(self.name.value.strip())
        if not rb:
            return await notice(interaction, "Not Found", "> That Roblox username was not found. Press Accept and try again.")
        prof = store.d["profiles"].setdefault(str(interaction.user.id), {})
        prof.update({"roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"]})
        store.save()
        channel, err = await process_accept(ch, interaction.user, prof)
        if err:
            return await notice(interaction, "Ticket Failed", "The ticket could not be opened right now. Press Accept again in a moment.")
        await interaction.followup.send(view=build_card(
            "Challenge Accepted", [f"> Username saved and your ticket is ready: {channel.mention}"], accent=GREEN,
            buttons=[discord.ui.Button(label="Open Ticket", url=channel.jump_url)]), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"RivalNameModal error: {error!r}")


# ============================================================ request flow (panel -> modal -> challenge)
async def submit_challenge(interaction: discord.Interaction, mode: str, rival, rounds: int, server: dict):
    """interaction must already be deferred (ephemeral)."""
    guild = interaction.guild
    if rival.id == interaction.user.id:
        return await notice(interaction, "Request Failed", "> You cannot challenge yourself.")
    if rival.bot:
        return await notice(interaction, "Request Failed", "> You cannot challenge a bot.")
    try:
        await guild.fetch_member(rival.id)
    except discord.NotFound:
        return await notice(interaction, "Request Failed", "> That rival is not in this server.")

    prof = store.d["profiles"].get(str(interaction.user.id))
    if not prof or not prof.get("roblox_name"):
        return await notice(interaction, "Username Needed", "> Enter your Roblox username and try again.")

    pending = [c for c in store.d["challenges"].values()
               if c["status"] == "pending" and c["requester"] == interaction.user.id]
    if not is_owner(interaction.user):  # developer bypass
        if any(c["opponent"] == rival.id and c["mode"] == mode for c in pending):
            return await notice(interaction, "Request Failed", "> You already have a pending request to this rival.")
        if len(pending) >= 3:
            return await notice(interaction, "Request Failed", "> You have too many pending requests. Wait for a reply first.")

    ch = {
        "id": uuid.uuid4().hex[:8], "mode": mode, "status": "pending", "guild_id": guild.id,
        "requester": interaction.user.id, "opponent": rival.id, "rounds": rounds, "server": server,
        "req": {k: prof.get(k) for k in ("roblox_id", "roblox_name", "roblox_display")},
        "created": time.time(), "expires": time.time() + EXPIRY_MINUTES * 60,
    }
    rival_user = await bot.fetch_user(rival.id)
    msg = await try_dm(rival_user, challenge_dm_view(ch, interaction.user))
    if not msg:
        return await notice(interaction, "Request Failed",
                            "> That rival has direct messages closed, so the challenge could not be delivered.")
    ch["dm_channel_id"], ch["dm_message_id"] = msg.channel.id, msg.id
    store.d["challenges"][ch["id"]] = ch
    store.save()

    m = MODES[mode]
    await interaction.followup.send(view=build_card(
        "Challenge Sent",
        [[f"> Your **{m['name']}** request was delivered to {rival.mention}.",
          "> You will be notified once they respond."],
         [field("Mode", m["name"]), field("Rounds", rounds_text(rounds)), field("Server", server_label(server))],
         [f"> Expires <t:{int(ch['expires'])}:R>."]],
        accent=m["colour"]), ephemeral=True)


class ChallengeModal(discord.ui.Modal):
    def __init__(self, mode: str, prof: dict = None):
        super().__init__(title=f"{MODES[mode]['name']} Request", timeout=600)
        self.mode = mode

        self.username = discord.ui.TextInput(placeholder="Your Roblox username",
                                             default=(prof or {}).get("roblox_name") or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", description="Saved so you only type it once.",
                                       component=self.username))

        self.opponent = discord.ui.UserSelect(placeholder="Select your rival", min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Opponent / Rival", description="The player you want to face.",
                                       component=self.opponent))

        self.rounds = discord.ui.Select(
            placeholder="Select first to how many rounds",
            options=[
                discord.SelectOption(label="First to 1", value="1", description="Single deciding round."),
                discord.SelectOption(label="First to 3", value="3", description="First to win 3 rounds (5 rounds at most)."),
                discord.SelectOption(label="First to 5", value="5", description="First to win 5 rounds (9 rounds at most)."),
                discord.SelectOption(label="Custom", value="custom", description="Type your own number next."),
            ],
        )
        self.add_item(discord.ui.Label(text="First To", description="How many rounds a player must win first.",
                                       component=self.rounds))

        self.server = discord.ui.TextInput(placeholder="Roseal / Roblox server link", min_length=10, max_length=400)
        self.add_item(discord.ui.Label(text="Server Link", description="Your server link. Your rival joins it from the ticket.",
                                       component=self.server))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not is_authorized(interaction.guild_id) and not is_owner(interaction.user):
            return await notice(interaction, "Not Authorized", "> Rivalz is not authorized in this server yet.", accent=GREY)
        rival = self.opponent.values[0]
        choice = self.rounds.values[0] if self.rounds.values else None
        if choice is None:
            return await notice(interaction, "Request Failed", "> Select the number of rounds.")
        server = parse_server_link(self.server.value)
        if not server:
            return await notice(interaction, "Invalid Server Link", [SERVER_HELP, "> Copy the link again and press the mode again."])

        name = self.username.value.strip()
        saved = store.d["profiles"].get(str(interaction.user.id), {})
        if not (saved.get("roblox_id") and str(saved.get("roblox_name", "")).lower() == name.lower()):
            rb = await bot.roblox.user_by_name(name)
            if not rb:
                return await notice(interaction, "Not Found", "> That Roblox username was not found.")
            prof = store.d["profiles"].setdefault(str(interaction.user.id), {})
            prof.update({"roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"]})
            store.save()

        if choice != "custom":
            return await submit_challenge(interaction, self.mode, rival, int(choice), server)

        mode, rival_id = self.mode, rival.id
        button = discord.ui.Button(label="Enter Rounds", style=discord.ButtonStyle.primary)

        async def open_rounds(i: discord.Interaction):
            await i.response.send_modal(CustomRoundsModal(mode, rival_id, server))

        button.callback = open_rounds
        await interaction.followup.send(view=build_card(
            "Custom Rounds",
            ["> Press **Enter Rounds** and type how many rounds a player must win first (**first to**).",
             f"> Allowed: `1` to `{MAX_ROUNDS}`. A first to `N` match lasts `2N-1` rounds at most."],
            accent=MODES[mode]["colour"], buttons=[button], timeout=600), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ChallengeModal error: {error!r}")


class CustomRoundsModal(discord.ui.Modal):
    def __init__(self, mode: str, rival_id: int, server: dict):
        super().__init__(title="Custom Rounds", timeout=600)
        self.mode, self.rival_id, self.server = mode, rival_id, server
        self.value = discord.ui.TextInput(placeholder=f"1 to {MAX_ROUNDS}", min_length=1, max_length=3)
        self.add_item(discord.ui.Label(text="First To", description="Rounds a player must win first.", component=self.value))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = self.value.value.strip()
        if not raw.isdigit() or not (1 <= int(raw) <= MAX_ROUNDS):
            return await notice(interaction, "Request Failed", f"> First to must be a number from `1` to `{MAX_ROUNDS}`.")
        try:
            rival = await interaction.guild.fetch_member(self.rival_id)
        except discord.NotFound:
            return await notice(interaction, "Request Failed", "> That rival is not in this server.")
        await submit_challenge(interaction, self.mode, rival, int(raw), self.server)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"CustomRoundsModal error: {error!r}")


# ============================================================ Ongoing-Ping + spectators + match details card
_SPEC_LOCK = None


def spec_lock():
    global _SPEC_LOCK
    if _SPEC_LOCK is None:
        _SPEC_LOCK = asyncio.Lock()
    return _SPEC_LOCK


def spec_list(t) -> list:
    return t.setdefault("spectators", [])


def spec_count(t) -> int:
    """Spectators that use up a slot (the developer never does)."""
    return sum(1 for s in spec_list(t) if not s.get("dev"))


def ongoing_view(t, role_id=None):
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    view = discord.ui.LayoutView(timeout=None)
    if role_id:
        view.add_item(discord.ui.TextDisplay(f"<@&{role_id}>"))
    c = discord.ui.Container(accent_colour=m["colour"])
    c.add_item(discord.ui.TextDisplay(f"## Ongoing {m['name']} #{t['number']:03d}"))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"> A **{m['name']}** is live right now. Join as a spectator to watch it."))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay("\n".join([
        f"**Challenger:** <@{p0['discord_id']}> (`@{esc(p0['roblox_name'])}`)",
        f"**Rival:** <@{p1['discord_id']}> (`@{esc(p1['roblox_name'])}`)",
        field("Mode", m["name"]),
        field("Rounds", rounds_text(t["rounds"])),
        field("Server", server_label(t.get("server"))),
        f"**Started:** <t:{int(t['created'])}:R>",
        field("Spectators", spec_count(t)),
    ])))
    specs = spec_list(t)
    if specs:
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("**Spectating**\n" + "\n".join(f"> <@{s['discord_id']}>" for s in specs[:12])))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay("-# Rivalz | Ongoing"))
    add_buttons(c, [SpectateButton(t["channel_id"])])
    view.add_item(c)
    return view


async def post_ongoing(t):
    """Send the live-match card to this server's ongoing channel, pinging only the Ongoing role."""
    try:
        cid = cfg_channel_id(t["guild_id"], "ongoing")
        if not cid:
            return
        chan = await get_channel_safe(cid)
        if not chan:
            return
        role_id = gcfg(t["guild_id"])["role"].get("ongoing")
        mentions = discord.AllowedMentions(everyone=False, users=False,
                                           roles=[discord.Object(id=role_id)] if role_id else False)
        msg = await chan.send(view=ongoing_view(t, role_id), allowed_mentions=mentions)
        t["ongoing_msg"] = msg.id
        store.save()
    except Exception as exc:
        print(f"Could not post the ongoing ping: {exc!r}")


async def update_ongoing(t):
    mid = t.get("ongoing_msg")
    cid = cfg_channel_id(t["guild_id"], "ongoing")
    if not mid or not cid:
        return
    try:
        chan = await get_channel_safe(cid)
        if chan:
            role_id = gcfg(t["guild_id"])["role"].get("ongoing")
            await chan.get_partial_message(mid).edit(view=ongoing_view(t, role_id), allowed_mentions=NO_PING)
    except discord.HTTPException as exc:
        print(f"Could not update the ongoing ping: {exc!r}")


async def clear_ongoing(t):
    mid = t.get("ongoing_msg")
    if not mid:
        return
    t["ongoing_msg"] = None
    store.save()
    cid = cfg_channel_id(t["guild_id"], "ongoing")
    if not cid:
        return
    try:
        chan = await get_channel_safe(cid)
        if chan:
            await chan.get_partial_message(mid).delete()
    except discord.HTTPException:
        pass


def safe_overwrite(**kw):
    """PermissionOverwrite that ignores any permission name this discord.py version does not know."""
    valid = getattr(discord.Permissions, "VALID_FLAGS", None)
    if valid:
        kw = {k: v for k, v in kw.items() if k in valid}
    return discord.PermissionOverwrite(**kw)


def spectator_overwrite(upload=False):
    """Spectators can only look at the ticket. They cannot type, react, use commands or start threads.
    While the match waits for its gank (upload=True) they may also send the gank image."""
    return safe_overwrite(
        view_channel=True, read_message_history=True, send_messages=upload, attach_files=upload,
        send_messages_in_threads=False, create_public_threads=False, create_private_threads=False,
        add_reactions=False, embed_links=False, use_application_commands=False, mention_everyone=False,
        use_external_emojis=False, use_external_stickers=False, send_tts_messages=False)


def spectator_voice_overwrite():
    return safe_overwrite(view_channel=True, connect=True, speak=False, stream=False,
                          use_voice_activation=False, use_embedded_activities=False)


async def member_of(guild, uid: int):
    m = guild.get_member(uid)
    if m:
        return m
    try:
        return await guild.fetch_member(uid)
    except discord.HTTPException:
        return None


async def grant_spectator(guild, t, member) -> bool:
    """Let the spectator see (and read) the ticket and join its voice channel, nothing else."""
    channel = guild.get_channel(t["channel_id"])
    if not channel:
        return False
    try:
        await channel.set_permissions(member, reason="Rivalz spectator",
                                      overwrite=spectator_overwrite(upload=t.get("status") == "ended"))
    except discord.HTTPException as exc:
        print(f"Could not give spectator access (the bot needs Manage Channels and Manage Roles): {exc!r}")
        return False
    vc = guild.get_channel(t["vc_id"]) if t.get("vc_id") else None
    if vc:
        try:
            await vc.set_permissions(member, reason="Rivalz spectator", overwrite=spectator_voice_overwrite())
        except discord.HTTPException as exc:
            print(f"Could not give spectator voice access: {exc!r}")
    return True


async def set_spectator_upload(guild, t, allow: bool):
    """Spectators may send the gank image only while the match waits for it."""
    channel = guild.get_channel(t["channel_id"]) if guild else None
    if not channel:
        return
    for s in list(spec_list(t)):
        m = await member_of(guild, s["discord_id"])
        if not m:
            continue
        try:
            await channel.set_permissions(m, reason="Rivalz spectator gank window", overwrite=spectator_overwrite(upload=allow))
        except discord.HTTPException as exc:
            print(f"Could not change spectator upload access: {exc!r}")


async def ensure_ticket_access(guild, t):
    """The requester, the rival and every spectator can open the ticket (spectators read only). Safe to run again."""
    channel = guild.get_channel(t["channel_id"])
    vc = guild.get_channel(t["vc_id"]) if t.get("vc_id") else None
    if not channel:
        return

    async def put(chan, member, overwrite):
        try:
            await chan.set_permissions(member, overwrite=overwrite, reason="Rivalz ticket access")
        except discord.HTTPException as exc:
            print(f"Could not set ticket access: {exc!r}")

    for p in t["p"]:
        m = await member_of(guild, p["discord_id"])
        if not m:
            continue
        await put(channel, m, discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                          read_message_history=True, attach_files=True))
        if vc:
            await put(vc, m, discord.PermissionOverwrite(view_channel=True, connect=True, speak=True))
    for s in spec_list(t):
        m = await member_of(guild, s["discord_id"])
        if not m:
            continue
        await put(channel, m, spectator_overwrite(upload=t.get("status") == "ended"))
        if vc:
            await put(vc, m, spectator_voice_overwrite())


async def refresh_guild_cards(guild):
    """Fresh leaderboards plus every ticket card and live card brought up to date. Used by /update and on a new version."""
    tickets = cards = 0
    for t in list(store.d["tickets"].values()):
        if t.get("guild_id") != guild.id:
            continue
        tickets += 1
        await refresh_ticket(t)
        if t.get("status") != "finished":
            await ensure_ticket_access(guild, t)
        if t.get("status") == "open":
            if t.get("ongoing_msg"):
                await update_ongoing(t)
            else:
                await post_ongoing(t)
                cards += 1
        await asyncio.sleep(0.3)
    await refresh_leaderboards(guild, repost=True)
    return tickets, cards


async def send_spectator_card(interaction, t, entry, already=False):
    roseal, roblox = server_urls(t.get("server"))
    m = MODES[t["mode"]]
    buttons = [discord.ui.Button(label="Open Ticket", url=ticket_jump(t))]
    if roseal:
        buttons.append(discord.ui.Button(label="Open Roseal", url=roseal))
    if roblox:
        buttons.append(discord.ui.Button(label="Open Roblox Directly", url=roblox))
    top = ["> You are already spectating this match." if already
           else "> You are now a spectator. The ticket is open to you."]
    top.append("> You can watch, use the join links and send the gank image after the match. Everything else is locked.")
    info = [field("Match", f"{m['name']} #{t['number']:03d}")]
    if entry.get("roblox_name"):
        info.append(field("Roblox", entry["roblox_name"]))
    info.append(field("Spectators Watching", spec_count(t)))
    if roblox:
        how = ["> Tap **Open Roseal** first and pick how to launch Roblox, or use **Open Roblox Directly**."]
        if not roseal:
            how = ["> Tap **Open Roblox Directly** to launch Roblox. Roseal is not available for private server links."]
    else:
        how = ["> This ticket has no server link yet. Ask a fighter to send it in the ticket."]
    view = build_card("Spectator Access", [top, info, how], accent=GREEN, buttons=buttons, timeout=600)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)
    except discord.HTTPException:
        pass


def active_spectating(uid: int, exclude_cid=None):
    """The live (open) match this member is already spectating, or None. Ended matches do not count, so as soon
    as the first match ends the member is free to spectate a new one. Matches whose channel is gone are ignored."""
    for other in store.d["tickets"].values():
        if other.get("status") != "open" or other.get("channel_id") == exclude_cid:
            continue
        if not any(s.get("discord_id") == uid for s in (other.get("spectators") or [])):
            continue
        if bot.get_channel(other["channel_id"]) is None:   # ticket deleted by hand: not live any more
            continue
        return other
    return None


async def already_spectating_notice(interaction, other):
    m = MODES[other["mode"]]
    return await notice(
        interaction, "Already Spectating",
        [f"> You are already spectating **{m['name']} #{other['number']:03d}**.",
         "> You can only watch one live match at a time. Once that match ends you can spectate a new one.",
         f"> Ticket: {ticket_jump(other)}"], accent=GREY)


async def add_spectator(interaction, t, rb):
    """interaction must already be deferred (ephemeral). rb is the spectator's Roblox dict, or None (developer)."""
    user, guild = interaction.user, interaction.guild
    dev = is_owner(user)
    if rb is None and dev:  # the developer does not have to give a username, but a saved one is used when there is one
        prof = store.d["profiles"].get(str(user.id), {})
        if prof.get("roblox_name"):
            rb = {"id": prof.get("roblox_id"), "name": prof["roblox_name"], "display": prof.get("roblox_display")}
    async with spec_lock():
        t = store.d["tickets"].get(str(t["channel_id"]))
        if not t or t["status"] != "open":
            return await notice(interaction, "Match Ended", "> This match is no longer live.", accent=GREY)
        have = next((s for s in spec_list(t) if s["discord_id"] == user.id), None)
        if have:
            return await send_spectator_card(interaction, t, have, already=True)
        if not dev:
            other = active_spectating(user.id, t["channel_id"])
            if other:
                return await already_spectating_notice(interaction, other)
        entry = {"discord_id": user.id, "dev": dev, "joined": int(time.time())}
        if rb:
            entry.update(roblox_id=rb.get("id"), roblox_name=rb.get("name"), roblox_display=rb.get("display"))
        spec_list(t).append(entry)
        store.save()
    ok = await grant_spectator(guild, t, user)
    if not ok:
        async with spec_lock():
            spec_list(t)[:] = [s for s in spec_list(t) if s["discord_id"] != user.id]
            store.save()
        return await notice(interaction, "Could Not Add You",
                            "> Rivalz could not open the ticket for you. Ask staff to check its **Manage Channels** and **Manage Roles** permissions.")
    await refresh_ticket(t)
    await update_ongoing(t)
    try:
        chan = guild.get_channel(t["channel_id"])
        if chan:
            who = f"<@{user.id}>" + (f" (`@{esc(entry['roblox_name'])}`)" if entry.get("roblox_name") else "")
            await chan.send(view=build_card("Spectator Joined", [f"> {who} joined as a spectator."], accent=GREY),
                            allowed_mentions=NO_PING)
    except discord.HTTPException:
        pass
    await send_spectator_card(interaction, t, entry)


class SpectatorModal(discord.ui.Modal):
    def __init__(self, cid: int, prof: dict):
        super().__init__(title="Join as Spectator", timeout=600)
        self.cid = cid
        self.username = discord.ui.TextInput(placeholder="Your Roblox username", default=(prof or {}).get("roblox_name") or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", description="Used so you can join the live server and watch.",
                                       component=self.username))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        t = store.d["tickets"].get(str(self.cid))
        if not t or t["status"] != "open":
            return await notice(interaction, "Match Ended", "> This match is no longer live.", accent=GREY)
        rb = await bot.roblox.user_by_name(self.username.value.strip())
        if not rb:
            return await notice(interaction, "Not Found", "> That Roblox username was not found. Press **Join as Spectator** and try again.")
        prof = store.d["profiles"].setdefault(str(interaction.user.id), {})
        if not prof.get("roblox_name"):  # first time: remember it so it is prefilled next time
            prof.update({"roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"]})
            store.save()
        await add_spectator(interaction, t, rb)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SpectatorModal error: {error!r}")


class SpectateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:spec:(?P<cid>\d+)"):
    """Join as Spectator, on the Ongoing-Ping card."""
    def __init__(self, cid: int):
        super().__init__(discord.ui.Button(label="Join as Spectator", style=discord.ButtonStyle.success,
                                           custom_id=f"rv:spec:{cid}"))
        self.cid = int(cid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["cid"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SpectateButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(self.cid))
        if not t or t["status"] != "open":
            return await notice(interaction, "Match Ended", "> This match is no longer live.", accent=GREY)
        if interaction.guild_id != t["guild_id"] or not await guard(interaction, t["guild_id"]):
            return
        user = interaction.user
        if side_of(t, user.id) is not None:
            return await notice(interaction, "You Are Fighting", "> You are one of the fighters in this match.", accent=GREY)
        have = next((s for s in spec_list(t) if s["discord_id"] == user.id), None)
        if have:
            return await send_spectator_card(interaction, t, have, already=True)
        if is_owner(user):  # developer bypass: no username, no slot limit
            await interaction.response.defer(ephemeral=True)
            return await add_spectator(interaction, t, None)
        other = active_spectating(user.id, t["channel_id"])
        if other:
            return await already_spectating_notice(interaction, other)
        await interaction.response.send_modal(SpectatorModal(self.cid, store.d["profiles"].get(str(user.id), {})))


def match_notice_view(t):
    """The small 'Match Details' card posted in the ticket at 15 messages, 30, 45 ..."""
    m = MODES[t["mode"]]
    jump = f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['message_id']}"
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=m["colour"])
    c.add_item(discord.ui.TextDisplay(f"## [Match Details]({jump})"))
    c.add_item(discord.ui.TextDisplay(
        f"-# **Fair play = Better matches**, add your own score honestly under your name, then press **End** "
        f"to finish. `Format` - `{rounds_text(t['rounds'])}`"))
    view.add_item(c)
    row = discord.ui.ActionRow()
    row.add_item(discord.ui.Button(label="Ticket Info", url=jump))
    row.add_item(TicketButton("prof"))
    view.add_item(row)
    return view


async def count_ticket_message(message):
    if message.author.bot or message.guild is None:
        return
    t = store.d["tickets"].get(str(message.channel.id))
    if not t or t.get("status") != "open":
        return
    n = t["msgs"] = t.get("msgs", 0) + 1
    if n % MSG_NOTICE_EVERY:
        if n % 5 == 0:
            store.save()
        return
    old = t.get("notice_msg")
    try:
        msg = await message.channel.send(view=match_notice_view(t), allowed_mentions=NO_PING)
    except discord.HTTPException as exc:
        print(f"Could not post the match details card: {exc!r}")
        store.save()
        return
    t["notice_msg"] = msg.id
    store.save()
    if old:  # remove the previous one so only the newest stays
        try:
            await message.channel.get_partial_message(old).delete()
        except discord.HTTPException:
            pass


def adopt_legacy_extras(guild) -> bool:
    """Your original server already has an Ongoing-Ping channel and role: use those ids. Other servers get theirs from /build."""
    g = gcfg(guild.id)
    changed = False
    if not g["ch"].get("ongoing") and guild.get_channel(LEGACY["ongoing"]):
        g["ch"]["ongoing"] = LEGACY["ongoing"]
        changed = True
    if not g["role"].get("ongoing") and guild.get_role(LEGACY["ongoing_role"]):
        g["role"]["ongoing"] = LEGACY["ongoing_role"]
        changed = True
    if changed:
        store.save()
    return changed


# ============================================================ "Rivalz <command>" (type it instead of using /)
# No AI and no API key. The bot only reacts when a message has the word "Rivalz" plus a command it knows
# (ping, open a channel, list channels, rank, help, owner commands). Anything else is ignored: it never chats.
RIVALZ_WORD = re.compile(r"(?<![\w'])rivalz(?![\w'])", re.I)
R_LEAD = re.compile(r"^\s*r[\s,:;.!-]+(?=\S)", re.I)   # a message that starts with "R " works like "Rivalz"
RIVALZ_LAST = {}

# every unicode emoji block + custom discord emojis + :shortcodes:  -> removed from anything the bot says
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002300-\U000023FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F"
    "\U0000200D\U000020E3\U00002190-\U000021FF\U00002900-\U0000297F\U00003030\U0000303D\U000000A9\U000000AE"
    "\U00002122\U00002139\U000E0020-\U000E007F]"
    "|<a?:\\w{2,32}:\\d{15,25}>|(?<![\\w:]):[A-Za-z_]{3,32}:(?![\\w:])"
)


def strip_emoji(text: str) -> str:
    text = EMOJI_RE.sub("", text or "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# ---------- channels, categories and short forms
OPEN_QUICK = re.compile(r"\b(open|go to|goto|take me to|bring me to|jump to|link)\b", re.I)
CHANNEL_FILLER = {"the", "a", "an", "channel", "channels", "chat", "room", "vc", "voice", "text", "for", "me", "please", "pls",
                  "plz", "bro", "to", "in", "it", "up", "this", "that", "open", "show", "rivalz", "go", "goto", "link",
                  "send", "get", "jump", "where", "is", "s", "take", "bring", "my", "us", "and", "now", "quick", "fast", "ok",
                  "okay", "yo", "hey", "boss", "sir", "also", "then"}
CAT_WORDS = {"all", "every", "channels", "channel", "under", "in", "inside", "of", "within", "categories", "category",
             "the", "this", "here", "that", "current", "these", "from", "server", "whole", "everything", "me", "show",
             "please", "pls", "plz", "bro", "and", "now", "boss", "sir", "its", "it", "s"}


def _core(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _tokens(name: str):
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]


def _subseq(q: str, s: str) -> bool:
    it = iter(s)
    return all(ch in it for ch in q)


def match_score(query: str, name: str) -> int:
    """How well a typed short form matches a channel/category name (0 = no match)."""
    q = _core(query)
    if not q:
        return 0
    core, toks = _core(name), _tokens(name)
    if core == q:
        return 100
    if core.startswith(q):
        return 85
    if q in toks:
        return 80
    if any(t.startswith(q) for t in toks):
        return 70
    if len(q) >= 3 and q in core:
        return 60
    if len(q) >= 2 and "".join(t[0] for t in toks).startswith(q):
        return 55
    if len(q) >= 3 and core and q[0] == core[0] and _subseq(q, core):
        return 40
    return 0


def best_matches(query: str, items, namefn, cap=5):
    scored = [(match_score(query, namefn(i)), i) for i in items]
    best = max((s for s, _ in scored), default=0)
    if best <= 0:
        return []
    return [i for s, i in scored if s == best][:cap]


def clean_query(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9_-]+", (text or "").lower().lstrip("#")) if w not in CHANNEL_FILLER]
    return " ".join(words) or (text or "").strip().lstrip("#")


def can_see(channel, member) -> bool:
    try:
        return bool(channel.permissions_for(member).view_channel)
    except Exception:
        return False


def channel_pool(guild, member):
    pool = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel) and can_see(c, member)]
    return sorted(pool, key=lambda c: ((c.category.position if c.category else -1), c.position))



OPEN_LEADS = ("here you go", "there you go", "found it", "say less", "opened it for you", "right here, click it", "got it")
OPEN_MISS = ("I looked everywhere, there is no channel like that that you can see.",
             "No channel with that name that you can see. Check the spelling.",
             "That channel does not exist or you cannot see it.")

CMD_PING = re.compile(r"^(?:ping|pong|latency|status|are you (?:online|up|alive|there))$")
CMD_HELP = re.compile(r"^(?:help|commands|what can you do)$")
CMD_RANK = re.compile(r"^(?:my\s+)?(?:rank|stats?)\b(?:\s+.*)?$|^what(?:'s| is) my rank$")
CMD_LB = re.compile(
    r"^(?:(?:show|check|see|get|view|what(?:'s| is)|whats)\s+(?:me\s+)?(?:the\s+)?)?"
    r"(?:current\s+|global\s+|all.?time\s+|daily\s+|weekly\s+|today'?s?\s+)*"
    r"(?:lb|leaderboards?|standings|rankings?|scoreboard|top\s*#?\d*|best\s+players?)\b.*$"
    r"|^who(?:'s|s| is| are)\b.*\b(?:top|first|number\s*(?:one|1)|#?1|best|leading|champion|king|winning)\b.*$")
CMD_TRUST_LIST = re.compile(r"^(?:trusted|trust\s+list|who(?:'s| is| are)\s+trusted)$")
CMD_TRUST_DEL = re.compile(r"^(?:tr|untrust|trust\s+(?:remove|del(?:ete)?)|remove\s+trust)\b")
CMD_TRUST_ADD = re.compile(r"^(?:ta|trust(?:\s+add)?|add\s+trust)\b")
CMD_OWNER = re.compile(r"^(authorize|build|panel|update|backup|deauthorize|deauth|db|database|storage)$")
CMD_LIST = re.compile(r"^(?:(?:list|show|open)\s+)?(?:all\s+)?(?:the\s+)?channels?"
                      r"(?:\s+(?:in|under|inside|of|from)\s+(?:the\s+)?(?P<cat>.+?))?(?:\s+category)?$")
CMD_OPEN = re.compile(r"^(?:open|go to|goto|take me to|bring me to|jump to|link|show me|show|join|get)\s+"
                      r"(?:the\s+)?(?:channel\s+)?(?P<name>.+)$")
LEAD_FILLER = re.compile(r"^(?:(?:hey|hi|yo|ok|okay|please|pls|plz|just|can you|could you|will you|now)\s+)+")
TAIL_FILLER = re.compile(r"(?:\s+(?:please|pls|plz|bro|now|thanks|thx|for me))+$")
CAT_WORD = re.compile(r"\b(?:categor(?:y|ies)|cat)\b")
PERIOD_NAMES = {"all": "All-time", "week": "Weekly", "day": "Daily"}


def command_text(message) -> str:
    """The message without 'R' / 'Rivalz', lower case and tidy: 'R, open #general please' -> 'open #general'."""
    raw = message.clean_content or ""
    raw = R_LEAD.sub(" ", raw, count=1)
    text = RIVALZ_WORD.sub(" ", raw).lower()
    text = re.sub(r"\s+", " ", text).strip(" ,.!?:;-")
    text = LEAD_FILLER.sub("", text)
    text = TAIL_FILLER.sub("", text).strip(" ,.!?:;-")
    return text.lstrip("/").strip()


def ping_view(api_ms):
    pending = sum(1 for c in store.d["challenges"].values() if c["status"] == "pending")
    open_tickets = sum(1 for t in store.d["tickets"].values() if t["status"] != "finished")
    first = [field("Gateway", f"{round(bot.latency * 1000)} ms")]
    if api_ms is not None:
        first.append(field("API Round Trip", f"{api_ms} ms"))
    first.append(field("Uptime", fmt_uptime(time.time() - START_TIME)))
    return build_card("Rivalz Online", [first, [field("Servers", len(bot.guilds)), field("Pending Challenges", pending),
                                                field("Active Tickets", open_tickets)]], accent=GREEN)


def rank_view(uid: int, mention: bool = False):
    sections = []
    if mention:
        sections.append([f"> <@{uid}>"])
    for mode in LB_MODES:
        info = rank_info(standings(mode, "all"), uid)
        name = MODES[mode]["name"]
        if info:
            sections.append([f"**{name}**", f"> **Rank:** #{info[0]} (Top {info[3]:.2f}%)",
                             f"> **Wins:** {info[1]}  |  **Losses:** {info[2]}"])
        else:
            sections.append([f"**{name}**", "> Unranked. Win a match to get on the board."])
    return build_card("Rank (All-time)" if mention else "Your Rank (All-time)", sections, accent=GREEN)


def lb_query(text: str):
    """'top 3 duel weekly' / 'who is the best' / 'leaderboard' -> (mode or None, period, how many)."""
    mode = "duel" if re.search(r"\bduels?\b", text) else "spar" if re.search(r"\bspar(?:s|ring)?\b", text) else None
    period = "day" if re.search(r"\b(?:daily|today|day)\b", text) else "week" if re.search(r"\b(?:weekly|week)\b", text) else "all"
    m = re.search(r"\b(?:top|first)\s*#?(\d{1,2})\b", text) or re.search(r"#(\d{1,2})\b", text)
    if m:
        n = int(m.group(1))
    elif re.match(r"^who", text) or re.search(r"\bnumber\s*(?:one|1)\b|\bbest\b|\bchampion\b|\bking\b", text):
        n = 1
    else:
        n = 3
    return mode, period, max(1, min(10, n))


def lb_stats_view(mode, period, n):
    modes = [mode] if mode else list(LB_MODES)
    since = period_start(period)
    sections = []
    for md_ in modes:
        rows = standings(md_, period)
        total = sum(1 for x in lbstore.d["matches"] if x["m"] == md_ and x["ts"] >= since)
        lines = [f"**{MODES[md_]['name']}** - `{total}` matches, `{len(rows)}` players"]
        if not rows:
            lines.append("> Nobody is on this board yet.")
        for i, (uid, w, l) in enumerate(rows[:n]):
            rb = (store.d["profiles"].get(str(uid)) or {}).get("roblox_name")
            who = f"<@{uid}>" + (f" (`@{esc(rb)}`)" if rb else "")
            pct = round(w / (w + l) * 100) if (w + l) else 0
            lines.append(f"> **#{i + 1}** {who} - {w}W / {l}L ({pct}%)")
        sections.append(lines)
    return build_card(f"Leaderboard - {PERIOD_NAMES[period]}", sections, accent=GREEN)


def help_view():
    return build_card("Rivalz Commands", [
        ["> Start a message with **R** or **Rivalz** and a command instead of using `/`."],
        ["`R ping` - check that Rivalz is online",
         "`R leaderboard` / `R top 3` / `R who is top 1` - current standings (add `duel`, `spar`, `weekly`, `daily`)",
         "`R rank` or `R rank @user` - Duel and Spar rank",
         "`R open general` - posts the channel link and a button that jumps straight to it (short names work, like `ann`)",
         "`R open Divisions category` - lists every channel in that category",
         "`R channels` / `R channels in <category>` - list the channels you can see"],
        ["> Developer and trusted users: `R update`, `R panel`, `R build`, `R authorize`, `R backup`, `R db`, `R deauthorize`.",
         "> Developer only: `R ta @user` trusts someone (full owner access), `R tr @user` removes it, `R trusted` lists them."],
    ], accent=GREY)


async def reply_text(message, text: str, view=None):
    chunks, cur = [], ""
    for line in (text or "").split("\n"):
        while len(line) > 1900:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:1900])
            line = line[1900:]
        if cur and len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)
    chunks = chunks[:6] or ["done."]
    for i, chunk in enumerate(chunks):
        kw = {"allowed_mentions": NO_PING}
        if view is not None and i == len(chunks) - 1:
            kw["view"] = view
        if i == 0:
            await message.reply(chunk, mention_author=False, **kw)
        else:
            await message.channel.send(chunk, **kw)


def list_channels_text(message, category: str = ""):
    """Returns (text, error). Only channels the person can see are listed."""
    guild, member = message.guild, message.author
    q = (category or "").strip().lower()
    if q in ("this", "here", "current", "this category", "current category"):
        cat = getattr(message.channel, "category", None)
        if not cat:
            return None, "This channel is not inside a category."
        groups = [(cat.name, [c for c in cat.channels if can_see(c, member)])]
    elif q:
        cats = [c for c in guild.categories if can_see(c, member)]
        query = clean_query(q)
        found = best_matches(query, cats, lambda c: c.name, cap=6)
        if not found and query.endswith("s") and len(query) > 3:      # "divisions" -> "division 1", "division 2" ...
            found = best_matches(query[:-1], cats, lambda c: c.name, cap=6)
        if not found:
            return None, "No category that you can see matches that name."
        groups = [(c.name, [x for x in c.channels if can_see(x, member)]) for c in found]
    else:
        groups = []
        loose = [c for c in channel_pool(guild, member) if c.category_id is None]
        if loose:
            groups.append(("No category", loose))
        for cat in guild.categories:
            groups.append((cat.name, [c for c in cat.channels if can_see(c, member)]))
    lines = []
    for name, chans in groups:
        if chans:
            lines.append(f"**{strip_emoji(name) or name}** ({len(chans)})")
            lines.append(" ".join(c.mention for c in chans))
    if not lines:
        return None, "There are no channels that you can see there."
    return "\n".join(lines), None


async def open_channels(message, name: str):
    """'R open general' -> the clickable channel mention + a link button. 'R open Divisions category' -> every channel in it."""
    guild, member = message.guild, message.author
    base = CAT_WORD.sub(" ", name).strip()
    if CAT_WORD.search(name) and base:
        text, err = list_channels_text(message, base)
        return await reply_text(message, text or err)
    found = [c for c in message.channel_mentions if can_see(c, member)]   # a real #channel mention always wins
    if not found:
        found = best_matches(clean_query(name), channel_pool(guild, member), lambda c: c.name, cap=5)
    if not found:
        text, _err = list_channels_text(message, name)     # maybe the name was a category ("R open divisions")
        if text:
            return await reply_text(message, text)
        return await reply_text(message, random.choice(OPEN_MISS))
    found = found[:5]
    view = discord.ui.View(timeout=None)
    for c in found:
        label = strip_emoji(c.name)[:60] or "channel"
        view.add_item(discord.ui.Button(label=f"Open {label}", url=f"https://discord.com/channels/{guild.id}/{c.id}"))
    await reply_text(message, f"{random.choice(OPEN_LEADS)}: " + " ".join(c.mention for c in found), view=view)


async def do_update(guild) -> list:
    """Same as /update, for 'R update'. Never removes anything."""
    perms = guild.me.guild_permissions
    if not (perms.manage_channels and perms.manage_roles):
        return ["Rivalz needs Manage Channels and Manage Roles first."]
    created, kept = await build_guild(guild)
    await post_panel(guild)
    tickets, cards = await refresh_guild_cards(guild)
    try:
        await bot.tree.sync()
    except discord.HTTPException:
        pass
    return [f"Updated: panel and leaderboards reposted, {tickets} ticket card(s) refreshed, {cards} new live card(s), "
            f"{len(created)} channel(s) created."]


def trust_add(uid: int) -> bool:
    lst = store.d.setdefault("trusted", [])
    if uid in lst:
        return False
    lst.append(uid)
    store.save()
    return True


def trust_remove(uid: int) -> bool:
    lst = store.d.setdefault("trusted", [])
    if uid not in lst:
        return False
    lst.remove(uid)
    store.save()
    return True


def target_of(message):
    """Who a command points at: the first @mention (not the bot), else a typed user id."""
    for u in message.mentions:
        if u.id != bot.user.id and not u.bot:
            return u.id
    m = re.search(r"\b(\d{15,20})\b", message.content or "")
    return int(m.group(1)) if m else None


async def owner_command(message, command: str) -> str:
    g, c = message.guild, (command or "").strip().lower().lstrip("/")
    if c == "deauth":
        c = "deauthorize"
    if c in ("db", "database", "storage"):
        return CLOUD.status_text()
    if c == "authorize":
        cfg = gcfg(g.id)
        was = cfg["authorized"]
        authorize_guild(g.id, message.author.id)
        return "This server was already authorized." if was else "Authorized this server. Say R build next."
    if c == "build":
        if not is_authorized(g.id):
            authorize_guild(g.id, message.author.id)
        perms = g.me.guild_permissions
        if not (perms.manage_channels and perms.manage_roles):
            return "The bot needs Manage Channels and Manage Roles first."
        created, kept = await build_guild(g)
        await post_panel(g)
        await refresh_leaderboards(g, repost=True)
        return f"Built: {len(created)} created, {len(kept)} already there."
    if c == "panel":
        return "Panel reposted." if await post_panel(g) else "Could not post the panel. Say R build first."
    if c == "update":
        if not is_authorized(g.id):
            authorize_guild(g.id, message.author.id)
        return " ".join(await do_update(g))
    if c == "backup":
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as fh:
                tmp = fh.name
            out = sqlite3.connect(tmp)
            try:
                DB.backup(out)
            finally:
                out.close()
            with open(tmp, "rb") as fh:
                blob = fh.read()
            owner = await bot.fetch_user(OWNER_ID)
            await owner.send(f"Backup: {len(lbstore.d['matches'])} matches.",
                             file=discord.File(io.BytesIO(blob), filename="rivalz.sqlite3"))
            return "Backup sent to the developer's DMs."
        except Exception as exc:
            print(f"Backup failed: {exc!r}")
            return "Backup failed (the file may be too large for Discord)."
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    if c == "deauthorize":
        await message.channel.send(
            "This deletes every channel, category, ticket, voice channel and role Rivalz created in this server, then I leave. "
            "Leaderboard scores are kept. Press the button to confirm.", view=DeauthorizeConfirm(g.id))
        return "Waiting for your confirmation. Nothing was deleted yet."
    return "Unknown command."


async def rivalz_text(message):
    text = command_text(message)
    if not text:
        return
    m_open = CMD_OPEN.match(text)
    m_list = CMD_LIST.match(text)
    if CMD_PING.match(text):
        kind = "ping"
    elif CMD_HELP.match(text):
        kind = "help"
    elif CMD_TRUST_LIST.match(text):
        kind = "trust_list"
    elif CMD_TRUST_DEL.match(text):
        kind = "trust_remove"
    elif CMD_TRUST_ADD.match(text):
        kind = "trust_add"
    elif CMD_OWNER.match(text):
        kind = "owner"
    elif CMD_RANK.match(text):
        kind = "rank"
    elif CMD_LB.match(text):
        kind = "lb"
    elif m_list:
        kind = "list"
    elif m_open:
        kind = "open"
    else:
        return                       # not a command: stay silent, never chat
    uid = message.author.id
    now = time.time()
    if now - RIVALZ_LAST.get(uid, 0) < RIVALZ_COOLDOWN and not is_owner(message.author):
        return
    RIVALZ_LAST[uid] = now
    if kind == "ping":
        t0 = time.perf_counter()
        try:
            await bot.application_info()
            api_ms = round((time.perf_counter() - t0) * 1000)
        except Exception:
            api_ms = None
        return await message.reply(view=ping_view(api_ms), mention_author=False, allowed_mentions=NO_PING)
    if kind == "help":
        return await message.reply(view=help_view(), mention_author=False, allowed_mentions=NO_PING)
    if kind == "rank":
        target = target_of(message)
        return await message.reply(view=rank_view(target or uid, mention=bool(target and target != uid)),
                                   mention_author=False, allowed_mentions=NO_PING)
    if kind == "lb":
        mode, period, n = lb_query(text)
        return await message.reply(view=lb_stats_view(mode, period, n), mention_author=False, allowed_mentions=NO_PING)
    if kind in ("trust_add", "trust_remove", "trust_list"):
        if not is_real_owner(message.author):
            return await reply_text(message, "Only the developer can manage trusted users.")
        if kind == "trust_list":
            ids = store.d.get("trusted") or []
            return await reply_text(message, "Trusted: " + " ".join(f"<@{i}>" for i in ids) if ids else "Nobody is trusted yet.")
        target = target_of(message)
        if not target:
            return await reply_text(message, "Mention who, like `R ta @name` or `R tr @name`.")
        if target == OWNER_ID:
            return await reply_text(message, "That is you, the developer. You already have full access.")
        if kind == "trust_add":
            if trust_add(target):
                return await reply_text(message, f"<@{target}> is trusted now. They can use every owner command and the full bot.")
            return await reply_text(message, f"<@{target}> is already trusted.")
        if trust_remove(target):
            return await reply_text(message, f"<@{target}> is not trusted any more.")
        return await reply_text(message, f"<@{target}> was not trusted.")
    if kind == "owner":
        if not is_owner(message.author):
            return await reply_text(message, "Owner only.")
        async with message.channel.typing():
            out = await owner_command(message, CMD_OWNER.match(text).group(1))
        return await reply_text(message, out)
    if kind == "list":
        out, err = list_channels_text(message, (m_list.group("cat") or "").strip())
        return await reply_text(message, out or err)
    await open_channels(message, m_open.group("name"))


async def rivalz_task(message):
    try:
        await rivalz_text(message)
    except Exception as exc:
        print(f"Rivalz text command error: {exc!r}")


# ============================================================ ticket buttons
TICKET_BUTTONS = {
    "join": ("Join", discord.ButtonStyle.success),
    "prof": ("Profile", discord.ButtonStyle.secondary),
    "s0": ("Add Score", discord.ButtonStyle.primary),
    "s1": ("Add Score", discord.ButtonStyle.primary),
    "score": ("Add Score", discord.ButtonStyle.primary),
    "end": ("End", discord.ButtonStyle.danger),
    "skip": ("Skip Gank", discord.ButtonStyle.secondary),
    "delete": ("Delete Ticket", discord.ButtonStyle.danger),
    # kept only so buttons on older tickets keep working
    "guide": ("Guide", discord.ButtonStyle.secondary),
    "gank": ("Upload Gank", discord.ButtonStyle.primary),
    "finish": ("Finish", discord.ButtonStyle.success),
}


class TicketButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"rv:t:(?P<act>join|prof|s0|s1|score|end|skip|delete|guide|gank|finish)"):
    def __init__(self, act: str):
        label, style = TICKET_BUTTONS[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rv:t:{act}"))
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"TicketButton({self.act}) error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(interaction.channel_id))
        if not t:
            return await safe_reply(interaction, "This ticket is no longer tracked by Rivalz.")
        if not await guard(interaction, t["guild_id"]):
            return
        user = interaction.user
        if self.act not in ("join", "prof") and is_spectator(t, user) and side_of(t, user.id) is None:
            # a spectator (even one with staff powers) can only look: no End, no Add Score, nothing else
            return await notice(interaction, "Spectator", "> Spectators can only use **Join**, **Profile** and **Join VC**.",
                                accent=GREY)
        await TICKET_HANDLERS[self.act](interaction, t)


# ---------- join (manual server link -> Roseal -> Roblox)
def is_spectator(t, user) -> bool:
    return any(s.get("discord_id") == user.id for s in (t.get("spectators") or []))


def spectator_only(t, user) -> bool:
    """True for a spectator who is not one of the two fighters (even with staff powers): they can only watch."""
    return is_spectator(t, user) and side_of(t, user.id) is None


async def spectator_refused(interaction):
    return await notice(interaction, "Spectator", "> Spectators can only watch. They cannot end, score, skip or delete a match.",
                        accent=GREY)


async def h_join(interaction, t):
    if not (can_act(t, interaction.user) or is_spectator(t, interaction.user)):
        return await notice(interaction, "Not Allowed", "> Only the two fighters and the spectators can use the join link.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> This match has already ended.")
    srv = t.get("server")
    roseal, roblox = server_urls(srv)
    if not roblox:
        return await notice(interaction, "No Server Link",
                            "> This ticket has no server link (it was opened before links were added). "
                            "Ask the challenger to send their server link in this channel.", accent=GREY)
    host = t["p"][0]
    buttons = []
    if roseal:
        buttons.append(discord.ui.Button(label="Open Roseal", url=roseal))
    buttons.append(discord.ui.Button(label="Open Roblox Directly", url=roblox))
    lines = [f"> Server link from **{md(host['roblox_name'])}**."]
    if roseal:
        lines += ["> Tap **Open Roseal**. Roseal opens first and lets you pick how to launch Roblox.",
                  "> If Roseal does not open, use **Open Roblox Directly**."]
    else:
        lines += ["> Tap **Open Roblox Directly** to launch Roblox.",
                  "> Roseal is not available for private server links."]
    await interaction.response.send_message(view=build_card(
        "Join Match", [lines, [field("Server Type", server_label(srv))]],
        accent=GREEN, timeout=600, buttons=buttons), ephemeral=True)


async def h_prof(interaction, t):
    await interaction.response.send_message(view=build_card(
        "Open Profile", ["> Choose whose Roblox profile you want to open."],
        buttons=profile_buttons(t["p"]), timeout=600), ephemeral=True)


async def h_guide(interaction, t):  # older tickets only
    await notice(interaction, "Ticket Guide",
                 ["> Add your own score under your name, then press **End**.",
                  "> Send the gank image if you have one, then press **Delete Ticket**."], accent=GREY)


# ---------- scores
class ScoreModal(discord.ui.Modal):
    def __init__(self, cid: int, side: int, t):
        super().__init__(title="Add Score", timeout=600)
        self.cid, self.side = cid, side
        self.add_item(discord.ui.TextDisplay(
            "**Use some sense and add your score honestly.**\n"
            "Enter only the rounds you personally won. Your rival adds their own score, and neither of you can edit "
            "the other's. Fake or inflated scores can get you blacklisted from Rivalz."))
        current = t["scores"][side]
        self.score = discord.ui.TextInput(placeholder=f"0 to {t['rounds']}", default=None if current is None else str(current),
                                          min_length=1, max_length=3)
        self.add_item(discord.ui.Label(
            text="Your Score",
            description=f"First to {t['rounds']} ({max_total_rounds(t['rounds'])} rounds at most). Rounds you won."[:100],
            component=self.score))

    async def on_submit(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(self.cid))
        if not t or t["status"] != "open":
            return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
        raw = self.score.value.strip()
        rounds = t["rounds"]
        if not raw.isdigit() or int(raw) > rounds:
            return await notice(interaction, "Invalid Score", f"> Enter a whole number from `0` to `{rounds}` (first to {rounds}).")
        value = int(raw)
        other = t["scores"][1 - self.side]

        most = max_total_rounds(rounds)
        if other is not None and value + other > most:
            # first to N: only one player can reach N, so the most a match can last is 2N-1 rounds (5 and 5 is impossible)
            t["scores"] = [None, None]
            store.save()
            await notice(interaction, "Scores Reset",
                         [f"> Your score `{value}` and your rival's score `{other}` add up to `{value + other}` rounds.",
                          f"> A **first to {rounds}** match can last `{most}` rounds at most, because only one player can reach `{rounds}`.",
                          "> Both scores were reset. Add them again honestly."], accent=RED)
            await refresh_ticket(t)
            await update_result(t)
            try:
                await interaction.channel.send(view=build_card(
                    "Score Conflict",
                    [f"> Both scores reached the limit of a **first to {rounds}** match (`{most}` rounds at most), so both were reset.",
                     "> Both players, press **Add Score** under your name and enter your score again."], accent=RED))
            except discord.HTTPException:
                pass
            return

        t["scores"][self.side] = value
        store.save()
        await interaction.response.send_message(view=build_card(
            "Score Saved", [[field("Your Score", value), f"> {scoreboard(t)}"]], accent=GREEN), ephemeral=True)
        await refresh_ticket(t)
        await update_result(t, interaction.user.id)
        try:
            await interaction.channel.send(view=build_card(
                "Score Recorded", [f"> **{md(t['p'][self.side]['roblox_name'])}** added their score."], accent=GREY))
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ScoreModal error: {error!r}")


async def add_score(interaction, t, side):
    mine = side_of(t, interaction.user.id)
    if is_owner(interaction.user):  # developer bypass: can add either player's score
        mine = side if side is not None else (mine if mine is not None else 0)
    elif mine is None:
        return await notice(interaction, "Not Allowed", "> Only the two fighters can add a score.")
    elif side is not None and mine != side:
        return await notice(interaction, "Not Your Score",
                            f"> This button is for **{md(t['p'][side]['roblox_name'])}**. You can only add your own score.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
    await interaction.response.send_modal(ScoreModal(t["channel_id"], mine, t))


async def h_s0(interaction, t):
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    await add_score(interaction, t, 0)


async def h_s1(interaction, t):
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    await add_score(interaction, t, 1)


async def h_score(interaction, t):  # older tickets only
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    await add_score(interaction, t, None)


# ---------- end -> optional gank -> delete
GANK_TASKS = {}


def image_attachment(message):
    for a in message.attachments:
        ct = (a.content_type or "").lower()
        if ct.startswith("image/") or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return a
    return None


def gank_view(t, state="waiting"):
    sections = [[f"> **Result:** `{esc(result_text(t))}`", f"> **Score:** {scoreboard(t)}"]]
    buttons = None
    if state == "waiting":
        sections.append(["> Send the gank image in this channel within `2 minutes` (the fighters or a spectator can).",
                         "> This step is optional. Press **Skip Gank** to continue without it.",
                         f"> Closes <t:{int(t['gank_deadline'])}:R>."])
        buttons = [TicketButton("skip")]
    else:
        sections.append(["> Gank saved." if t.get("gank_url") else "> Continued without a gank image."])
    return build_card("Match Ended", sections, accent=GREY, buttons=buttons)


async def edit_gank_prompt(t, state):
    try:
        channel = bot.get_channel(t["channel_id"]) or await bot.fetch_channel(t["channel_id"])
        await channel.get_partial_message(t["gank_msg"]).edit(view=gank_view(t, state))
    except discord.HTTPException as exc:
        print(f"Could not edit the gank prompt: {exc!r}")


async def download_bytes(url: str, limit: int = 25_000_000):
    try:
        async with bot.session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status == 200:
                data = await r.read()
                if len(data) <= limit:
                    return data
    except Exception as exc:
        print(f"Could not download the gank image: {exc!r}")
    return None


def normalize_gank(data: bytes):
    """Fit any screenshot onto the same 16:9 canvas so every gank in the results looks the same size."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        w, h = 1280, 720
        ratio = min(w / im.width, h / im.height)
        im = im.resize((max(1, round(im.width * ratio)), max(1, round(im.height * ratio))), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (30, 31, 34))
        canvas.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception as exc:
        print(f"Could not resize the gank image: {exc!r}")
        return None


async def attach_gank_to_result(t):
    """Put the gank in the results channel: an even-sized preview plus the full image behind Open Gank."""
    if not t.get("gank_url"):
        return
    raw = await download_bytes(t["gank_url"])
    if not raw:
        return
    ext = os.path.splitext(urlparse(t["gank_url"]).path)[1].lower() or ".png"
    full_name = f"gank_full{ext}"
    card = await asyncio.to_thread(normalize_gank, raw)
    can_full = len(raw) < 8_000_000
    if not card and not can_full:
        return

    def make_files(with_full=True):
        files = []
        if with_full and can_full:
            files.append(discord.File(io.BytesIO(raw), filename=full_name))
        if card:
            files.append(discord.File(io.BytesIO(card), filename="gank_card.jpg"))
        return files

    t["gank_card"] = "gank_card.jpg" if card else full_name
    ok = await update_result(t, force=True, files=make_files())
    if not ok and card and can_full:
        ok = await update_result(t, force=True, files=make_files(with_full=False))
    if not ok:
        t["gank_card"] = None
    elif t.get("result_msg"):
        store.d.setdefault("gank_urls", {})[str(t["result_msg"])] = t["gank_url"]
    store.save()


async def finalize_match(channel, t):
    """Gank received or skipped: record the match and offer Delete Ticket straight away."""
    if t["status"] != "ended":
        return
    t["status"] = "finished"
    store.save()
    GANK_TASKS.pop(t["channel_id"], None)
    await set_spectator_upload(getattr(channel, "guild", None), t, False)  # spectators go back to read only
    await edit_gank_prompt(t, "done")
    try:
        await channel.send(view=build_card(
            "This match has been recorded",
            ["> You can delete this ticket now. Its voice channel is removed with it."],
            accent=GREY, buttons=[TicketButton("delete")]))
    except discord.HTTPException as exc:
        print(f"Could not send the delete card: {exc!r}")
    await refresh_ticket(t)
    await attach_gank_to_result(t)


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
IMG_URL = re.compile(r"https?://\S+", re.I)


def gank_image(message):
    """The gank picture of a message: an uploaded image, an image link, or an image embed. None when there is none."""
    a = image_attachment(message)
    if a is not None:
        return a.url
    for u in IMG_URL.findall(message.content or ""):
        u = u.strip("<>()[]\"'.,")
        path = urlparse(u).path.lower()
        host = urlparse(u).netloc.lower()
        if path.endswith(IMG_EXT) or host in ("cdn.discordapp.com", "media.discordapp.net", "i.imgur.com"):
            return u
    for e in message.embeds:
        if e.image and e.image.url:
            return e.image.url
    return None


async def gank_timer(cid: int):
    """Waits out the 2 minutes, then carries on without a gank. Resumed after a restart (see on_ready)."""
    try:
        t = store.d["tickets"].get(str(cid))
        if not t:
            return
        await asyncio.sleep(max(0.0, t.get("gank_deadline", 0) - time.time()))
        t = store.d["tickets"].get(str(cid))
        if not t or t["status"] != "ended":
            return
        channel = bot.get_channel(cid)
        if channel is None:
            try:
                channel = await bot.fetch_channel(cid)
            except discord.HTTPException:
                channel = None
        if channel is None:  # the ticket channel is gone
            t["status"] = "finished"
            store.save()
            return
        await finalize_match(channel, t)  # the gank is optional, so just carry on
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"Gank timer error: {exc!r}")


def start_gank(channel, t):
    cid = t["channel_id"]
    old = GANK_TASKS.pop(cid, None)
    if old:
        old.cancel()
    GANK_TASKS[cid] = asyncio.create_task(gank_timer(cid))


async def handle_gank_message(message):
    """Called for every message. While a ticket waits for its gank, the first image from a fighter, a spectator or
    staff is saved as the gank. Spectators can send nothing else: their other messages are removed."""
    if message.author.bot or message.guild is None:
        return
    t = store.d["tickets"].get(str(message.channel.id))
    if not t or t.get("status") != "ended":
        return
    uid = message.author.id
    fighter = side_of(t, uid) is not None
    spectator = is_spectator(t, message.author)
    staff = is_staff(message.author)
    url = gank_image(message)
    if spectator and not fighter and not staff and not url:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return
    if not (fighter or spectator or staff) or not url:
        return
    if time.time() > t.get("gank_deadline", 0) + 5:
        return
    task = GANK_TASKS.pop(t["channel_id"], None)
    if task:
        task.cancel()
    t["gank_url"] = url
    t["gank_by"] = uid
    store.save()
    await finalize_match(message.channel, t)


def auto_fill_scores(t) -> bool:
    """First to N: a player who added N has won, even when the rival never added a score. The rival's score counts as 0
    so the match is still calculated and recorded."""
    n = t["rounds"]
    s = t["scores"]
    for i in (0, 1):
        if s[i] is not None and s[i] >= n and s[1 - i] is None:
            s[1 - i] = 0
            t["score_assumed"] = 1 - i
            store.save()
            return True
    return False


async def h_end(interaction, t):
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can end this match.")
    if t["status"] != "open":
        return await notice(interaction, "Already Ended", "> This match has already ended.")
    auto_fill_scores(t)
    if None in t["scores"] and not is_staff(interaction.user):
        return await notice(interaction, "Scores Missing",
                            "> Both players must add their own score before the match can end.", accent=RED)
    t["status"] = "ended"
    t["ended_at"] = int(time.time())
    t["ended_by"] = interaction.user.id
    t["gank_deadline"] = time.time() + GANK_SECONDS
    t["gank_url"] = None
    store.save()
    if record_match(t):
        schedule_lb_refresh(t["guild_id"])
    await interaction.response.edit_message(view=ticket_view(t))
    msg = await interaction.followup.send(view=gank_view(t, "waiting"), wait=True)
    t["gank_msg"] = msg.id
    store.save()
    start_gank(interaction.channel, t)
    await set_spectator_upload(interaction.guild, t, True)  # spectators may send the gank image now
    await update_result(t, force=True)
    await log_closed(t, interaction.user.id)
    await clear_ongoing(t)  # the match is no longer live


async def h_skip(interaction, t):
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can skip the gank.")
    if t["status"] != "ended":
        return await notice(interaction, "Not Ready", "> End the match first.")
    task = GANK_TASKS.pop(t["channel_id"], None)
    if task:
        task.cancel()
    await interaction.response.defer()
    await finalize_match(interaction.channel, t)


class OpenGankButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:gank:open"):
    """Shown on the result card. Opens the full-size gank image."""
    def __init__(self):
        super().__init__(discord.ui.Button(label="Open Gank", style=discord.ButtonStyle.secondary,
                                           custom_id="rv:gank:open"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"OpenGankButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        msg = interaction.message
        atts = list(msg.attachments) if msg else []
        if not atts and msg is not None:   # the click can arrive without the attachments: read the message again
            try:
                msg = await interaction.channel.fetch_message(msg.id)
                atts = list(msg.attachments)
            except Exception as exc:
                print(f"Open Gank: could not re-read the result card: {exc!r}")
        att = (next((a for a in atts if a.filename.startswith("gank_full")), None)
               or next((a for a in atts if a.filename.startswith("gank_card")), None)
               or next((a for a in atts if a.filename.lower().endswith(IMG_EXT)), None))
        url = att.url if att else None
        if not url and msg is not None:    # the picture shown inside the card itself
            url = gank_media_url(getattr(msg, "components", None))
        if not url and msg is not None:    # last resort: the link saved when the gank was attached
            url = (store.d.get("gank_urls") or {}).get(str(msg.id))
        if not url:
            return await notice(interaction, "No Gank", "> The gank image is no longer available.", accent=GREY)
        await interaction.response.send_message(view=build_card(
            "Gank", ["> Full-size gank image. Tap the image to enlarge it."],
            media=url, accent=GREY, timeout=600,
            buttons=[discord.ui.Button(label="Open Original", url=url)]), ephemeral=True)


def gank_media_url(components):
    """The first picture URL inside a Components V2 message (media galleries), or None."""
    found = []

    def walk(c):
        for item in getattr(c, "items", None) or []:
            media = getattr(item, "media", None)
            u = getattr(media, "url", None) or getattr(media, "proxy_url", None)
            if isinstance(u, str) and u.startswith("http"):
                found.append(u)
        for ch in getattr(c, "children", None) or []:
            walk(ch)

    for c in components or []:
        walk(c)
    return found[0] if found else None


async def h_delete(interaction, t):
    if spectator_only(t, interaction.user):
        return await spectator_refused(interaction)
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters or staff can delete this ticket.")
    if t["status"] != "finished":
        return await notice(interaction, "Not Ready", "> The match is not finished yet.")
    await interaction.response.send_message(view=build_card(
        "Ticket Deleting", ["> This ticket and its voice channel will be removed in `5` seconds."], accent=GREY))
    guild = interaction.guild

    a = archive_of(t)
    a["deleted_by"], a["deleted_at"] = interaction.user.id, int(time.time())
    key = uuid.uuid4().hex[:8]
    store.d["archive"][key] = a
    while len(store.d["archive"]) > 300:
        store.d["archive"].pop(next(iter(store.d["archive"])))
    store.d["tickets"].pop(str(t["channel_id"]), None)
    store.save()
    await send_cfg(t["guild_id"], "log", deleted_view(a, key))

    await asyncio.sleep(5)
    if t.get("vc_id"):
        vc = guild.get_channel(t["vc_id"])
        if vc:
            try:
                await vc.delete(reason="Rivalz ticket deleted")
            except discord.HTTPException:
                pass
    try:
        await interaction.channel.delete(reason=f"Rivalz ticket deleted by {interaction.user}")
    except discord.HTTPException:
        pass


TICKET_HANDLERS = {
    "join": h_join, "prof": h_prof, "s0": h_s0, "s1": h_s1, "score": h_score, "end": h_end,
    "skip": h_skip, "delete": h_delete,
    "guide": h_guide, "gank": h_skip, "finish": h_skip,  # older tickets
}


# ============================================================ settings + help + main panel
class ConfigModal(discord.ui.Modal):
    def __init__(self, prof: dict):
        super().__init__(title="Configuration", timeout=600)
        self.username = discord.ui.TextInput(placeholder="Your Roblox username", default=prof.get("roblox_name") or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Username", component=self.username))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rb = await bot.roblox.user_by_name(self.username.value.strip())
        if not rb:
            return await notice(interaction, "Not Saved", "> That Roblox username was not found.")
        prof = store.d["profiles"].setdefault(str(interaction.user.id), {})
        prof.update({"roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"]})
        store.save()
        await interaction.followup.send(view=build_card(
            "Configuration Saved",
            [[field("Roblox", f"{rb['name']} (Display: {rb['display']})")]],
            accent=GREEN, buttons=[discord.ui.Button(label="Roblox Profile", url=f"https://www.roblox.com/users/{rb['id']}/profile")]),
            ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ConfigModal error: {error!r}")


HELP_SECTIONS = [
    ["> Pick **Duel** or **Spar**, choose a rival and rounds. Your rival accepts in their DMs."],
    ["`1` Select a mode, enter your Roblox username (saved for next time), pick **first to** how many rounds and paste your **server link**.",
     "`2` Your rival presses **Accept** and a private ticket with a voice channel opens.",
     "`3` Fight, then each player adds their own score. First to the chosen rounds wins (first to 5 lasts 9 rounds at most).",
     "`4` Press **End**. Send the gank image if you have one (optional), then press **Delete Ticket**."],
    [f"> Live matches are announced in the **ongoing-ping** channel. Press **Join as Spectator** to watch (no limit, one match at a time)."],
    ["> **Settings** is optional. Fake challenges or scores lead to a blacklist."],
]

PANEL_INTRO = "> Challenge a rival to a **Duel** or train in a **Spar**. Private ticket, voice channel and tracked scores."
PANEL_MODES = (
    "**Duel** `Competitive` A match with a winner and a loser.\n"
    "**Spar** `Practice` Warm up and train together.\n"
    "**Format** `First to` Pick first to 1, 3, 5 or your own number. The first player to reach it wins."
)
PANEL_HOW = (
    "### How it works\n"
    "> `1` Pick **Duel** or **Spar** below\n"
    "> `2` Enter your username, rival, first to (rounds) and server link\n"
    "> `3` Your rival accepts in their DMs\n"
    "> `4` Fight, add scores and finish"
)


class SettingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Settings", style=discord.ButtonStyle.secondary, custom_id="rv:settings")

    async def callback(self, interaction: discord.Interaction):
        if not await guard(interaction):
            return
        prof = store.d["profiles"].get(str(interaction.user.id), {})
        await interaction.response.send_modal(ConfigModal(prof))


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Help", style=discord.ButtonStyle.primary, custom_id="rv:help")

    async def callback(self, interaction: discord.Interaction):
        if not await guard(interaction):
            return
        await interaction.response.send_message(
            view=build_card("Matchmaking Guide", HELP_SECTIONS, timeout=600), ephemeral=True)


class ModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            custom_id="rv:mode",
            placeholder="Select a mode...",
            options=[
                discord.SelectOption(label="Duel", value="duel", description="Competitive match against a rival you pick."),
                discord.SelectOption(label="Spar", value="spar", description="Practice round to warm up and train together."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        if not await guard(interaction):
            return
        mode = self.values[0]
        prof = store.d["profiles"].get(str(interaction.user.id), {})
        await interaction.response.send_modal(ChallengeModal(mode, prof))
        try:  # reset the menu so the same mode can be picked again
            await interaction.message.edit(view=PanelView(), attachments=list(interaction.message.attachments))
        except discord.HTTPException:
            pass


class PanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container()
        c.add_item(discord.ui.TextDisplay("## Matchmaking"))
        if GIF_PATH:
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media="attachment://panel.gif")
            c.add_item(gallery)
        c.add_item(discord.ui.TextDisplay(PANEL_INTRO))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(PANEL_MODES))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(PANEL_HOW))
        row = discord.ui.ActionRow()
        row.add_item(SettingsButton())
        row.add_item(HelpButton())
        c.add_item(row)
        row2 = discord.ui.ActionRow()
        row2.add_item(ModeSelect())
        c.add_item(row2)
        self.add_item(c)


# ============================================================ bot
class GuardedTree(app_commands.CommandTree):
    """Every slash command is owner only, except /ping (it works for everyone, in any server)."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_owner(interaction.user):
            return True
        cmd = interaction.command
        if cmd is not None and cmd.qualified_name in ("ping", "my rank"):     # /ping and /my rank work for everyone
            return True
        await safe_reply(interaction, "Owner only.")
        return False


def migrate_legacy():
    """Your original server keeps working: it is marked authorized and keeps its existing channels and categories."""
    for guild in bot.guilds:
        if not guild.get_channel(LEGACY["panel"]):
            continue
        if str(guild.id) in store.d["guilds"] and is_authorized(guild.id):
            continue
        g = gcfg(guild.id)
        g["authorized"], g["legacy"] = True, True
        for key in ("panel", "log", "results", "duel_lb", "spar_lb"):
            if guild.get_channel(LEGACY[key]):
                g["ch"][key] = LEGACY[key]
        g["cat"].update(main=LEGACY["cat_main"], tickets=LEGACY["cat_tickets"])
        print(f"Moved {guild.name} onto the per-server setup.")
    store.save()


class Rivalz(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read the gank image and the "Rivalz <command>" messages
        intents.members = os.getenv("MEMBERS_INTENT", "").strip().lower() in ("1", "true", "yes")  # optional, see README note
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=GuardedTree)
        self.session: aiohttp.ClientSession = None
        self.roblox: RobloxClient = None
        self._ready_once = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.session)
        self.add_dynamic_items(ChallengeButton, TicketButton, ReopenButton, OpenGankButton, LbButton, LbPage, LbSelect,
                               SpectateButton, LogSpectatorsButton)
        await self.tree.sync()
        expiry_loop.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Rivalz online as {self.user} ({self.user.id})")
        if self._ready_once:
            return
        self._ready_once = True
        updated = store.d.get("code_hash") != CODE_HASH   # a new main.py was uploaded
        print(f"Data folder: {DATA_DIR} | version {CODE_HASH}{' (new version)' if updated else ''}")
        store.d["code_hash"] = CODE_HASH   # remembered right away, so a crash and restart is not treated as a new version
        store.save()
        migrate_legacy()
        restore_authorization(self.guilds)
        recover_matches()
        backfill_matches()
        if EPHEMERAL_DISK and not CLOUD_ON:
            print("WARNING: Railway disk is temporary and no Volume / Supabase is set. Every deploy forgets /authorize and /build! "
                  "Add a Railway Volume (mount path /data) or set SUPABASE_URL + SUPABASE_KEY.")
            await tell_owner("WARNING: Rivalz has no permanent storage on Railway, so every update forgets `/authorize` and `/build`. "
                             "Add a **Volume** to the service (mount path `/data`) or set `SUPABASE_URL` and `SUPABASE_KEY`.")
        for guild in self.guilds:
            try:
                gcfg(guild.id)
                adopt_legacy_extras(guild)
                if is_authorized(guild.id) and gcfg(guild.id)["ch"].get("panel"):
                    await post_panel(guild, force=updated)
            except Exception as exc:
                print(f"Start-up panel check failed in {guild.name}: {exc!r}")
        if not lbstore.d["matches"] and not lbstore.d.get("reset_id"):  # nothing saved: rebuild from the result cards
            for guild in self.guilds:
                rid = gcfg(guild.id)["ch"].get("results") or (LEGACY["results"] if guild.get_channel(LEGACY["results"]) else None)
                rchan = guild.get_channel(rid) if rid else None
                if not rchan:
                    continue
                try:
                    scanned, added = await rebuild_from_results(guild, rchan)
                    print(f"Leaderboard rebuilt from #{rchan.name} in {guild.name}: {added} matches ({scanned} result cards read).")
                except Exception as exc:
                    print(f"Could not rebuild the leaderboard from {guild.name}: {exc!r}")
        store.save()
        if updated:  # new version: fresh leaderboards, and every open ticket gets the newest layout and permissions
            for guild in self.guilds:
                if is_authorized(guild.id):
                    try:
                        await refresh_guild_cards(guild)
                    except Exception as exc:
                        print(f"Could not refresh {guild.name} after the update: {exc!r}")
        for t in list(store.d["tickets"].values()):  # a restart must not lose a gank that is waiting
            if t.get("status") == "ended":
                start_gank(None, t)
        store.d["code_hash"] = CODE_HASH
        store.save()
        lb_loop.start()  # draws the leaderboards now and then every 5 minutes
        health_loop.start()  # every 10 minutes: every authorized server still has its panel, boards and channels

    async def on_message(self, message: discord.Message):
        try:
            await count_ticket_message(message)  # 15 / 30 / 45 ... messages -> Match Details card
        except Exception as exc:
            print(f"Message counter error: {exc!r}")
        try:  # a gank image sent while the match is waiting for it (fighters, spectators, staff)
            await handle_gank_message(message)
        except Exception as exc:
            print(f"Gank handler error: {exc!r}")
        try:  # "R ping", "Rivalz open general" ... only known commands are answered
            if (message.guild and not message.author.bot and message.content
                    and (RIVALZ_WORD.search(message.content) or R_LEAD.match(message.content))):
                asyncio.create_task(rivalz_task(message))
        except Exception as exc:
            print(f"Rivalz trigger error: {exc!r}")
        await super().on_message(message)

    async def on_guild_channel_delete(self, channel):
        t = store.d["tickets"].get(str(getattr(channel, "id", 0)))
        if t and t.get("ongoing_msg"):  # a ticket removed by hand: take its live card down too
            await clear_ongoing(t)

    async def on_guild_join(self, guild: discord.Guild):
        gcfg(guild.id)
        store.save()
        if is_authorized(guild.id):   # the server is already set up (its data was kept): just repair and repost
            try:
                await build_guild(guild)
                await post_panel(guild)
                await refresh_leaderboards(guild, repost=True)
                await tell_owner(f"Rivalz re-joined **{guild.name}** (`{guild.id}`). It was already authorized, so the channels, panel and leaderboards were repaired automatically.")
            except Exception as exc:
                print(f"Re-join repair failed in {guild.name}: {exc!r}")
                await tell_owner(f"Rivalz re-joined **{guild.name}** (`{guild.id}`) but could not repair it: `{exc!r}`. Run `/build`.")
            return
        await tell_owner(f"Rivalz joined **{guild.name}** (`{guild.id}`). Run `/authorize` there, then `/build`.")


bot = Rivalz()


async def post_panel(guild, force=True) -> bool:
    """force=False (a plain restart): the panel is left alone when it is already up to date."""
    channel_id = cfg_channel_id(guild.id, "panel")
    if not channel_id:
        print(f"{guild.name}: no panel channel yet (run /build).")
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        pcfg = gcfg(guild.id)
        if not force and pcfg.get("panel_ver") == CODE_HASH and pcfg.get("panel_msg"):
            try:
                await channel.fetch_message(pcfg["panel_msg"])
                print(f"Panel in {guild.name} is already up to date, left alone.")
                return True
            except discord.HTTPException:
                pass  # it is gone: post it again below
        try:
            async for message in channel.history(limit=50):
                if message.author.id == bot.user.id:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
        except discord.Forbidden:
            print("Missing Read Message History in the panel channel; old panels were not removed.")
        if GIF_PATH and os.path.exists(GIF_PATH):
            sent = await channel.send(view=PanelView(), file=discord.File(GIF_PATH, filename="panel.gif"))
        else:
            print("panel.gif not found next to main.py (or in assets/), posting the panel without the banner.")
            sent = await channel.send(view=PanelView())
        pcfg["panel_msg"], pcfg["panel_ver"] = sent.id, CODE_HASH
        store.save()
        print(f"Panel posted in {guild.name}.")
        return True
    except discord.Forbidden:
        print("Could not post the panel: the bot needs View Channel and Send Messages in the panel channel.")
    except discord.NotFound:
        print("Could not post the panel: the panel channel no longer exists (run /build).")
    except Exception as exc:
        print(f"Could not post the panel: {exc!r}")
    return False


@tasks.loop(seconds=60)
async def expiry_loop():
    now = time.time()
    for ch in list(store.d["challenges"].values()):
        if ch["status"] != "pending" or ch["expires"] > now:
            continue
        ch["status"] = "expired"
        store.save()
        m = MODES[ch["mode"]]
        await edit_dm(ch, build_card(
            "Challenge Expired",
            summary_sections(ch, f"<@{ch['requester']}>", f"<@{ch['opponent']}>") + ["> You did not respond in time."],
            accent=GREY))
        try:
            requester = await bot.fetch_user(ch["requester"])
            await try_dm(requester, build_card(
                "Challenge Expired",
                [f"> Your **{m['name']}** request got no reply and has expired.",
                 [field("Mode", m["name"]), field("Rounds", rounds_text(ch["rounds"]))]],
                accent=GREY))
        except discord.HTTPException:
            pass


@expiry_loop.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


# ============================================================ leaderboard images (Pillow)
FONT_DIRS = (os.path.join(BASE_DIR, "assets", "fonts"), os.path.join(BASE_DIR, "fonts"), BASE_DIR)
FONT_FILES = {
    "bold": ("Poppins-Bold.ttf", "DejaVuSans-Bold.ttf"),
    "medium": ("Poppins-Medium.ttf", "DejaVuSans.ttf"),
    "regular": ("Poppins-Regular.ttf", "DejaVuSans.ttf"),
}
_FONTS = {}


def lb_font(weight: str, size: int):
    from PIL import ImageFont
    key = (weight, size)
    if key in _FONTS:
        return _FONTS[key]
    font = None
    for name in FONT_FILES[weight]:
        for folder in FONT_DIRS + ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
                                   "C:\\Windows\\Fonts"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:
                    font = None
        if font:
            break
    if font is None:
        try:
            font = ImageFont.load_default(size=size)  # Pillow 10.1+
        except TypeError:
            font = ImageFont.load_default()
    _FONTS[key] = font
    return font


LB_W = 1000
LB_PAGE = 10  # fighters per page (page 1 = podium + ranks 4 to 10)
LB_PERIODS = {"day": "Daily", "week": "Weekly", "all": "All-time"}
C_CARD, C_CARD_LINE = (17, 38, 46), (31, 100, 112)
C_PANEL, C_ROW, C_ROW_LINE = (18, 53, 64), (22, 38, 46), (42, 68, 82)
C_TEXT, C_MUTED, C_CYAN = (236, 244, 247), (130, 150, 160), (34, 211, 238)
C_GOLD, C_SILVER, C_BRONZE = (246, 212, 66), (168, 182, 198), (217, 138, 43)


def _rounded_avatar(data, size, radius, fallback_text="?", tint=(60, 90, 110)):
    from PIL import Image, ImageDraw
    im = None
    if data:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            im = None
    if im is None:
        im = Image.new("RGBA", (size, size), tint + (255,))
        d = ImageDraw.Draw(im)
        d.text((size / 2, size / 2), (fallback_text or "?")[:1].upper(), font=lb_font("bold", int(size * 0.5)),
               fill=C_TEXT, anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _fit_text(draw, text, weight, size, max_w, min_size=14):
    while size > min_size:
        f = lb_font(weight, size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 2
    f = lb_font(weight, min_size)
    while text and draw.textlength(text + "...", font=f) > max_w:
        text = text[:-1]
    return f, text + "..."


def _crown(draw, cx, base_y, w=120, h=64):
    x0, x1 = cx - w // 2, cx + w // 2
    top = base_y - h
    pts = [(x0, base_y), (x0 - 8, top + 14), (x0 + w * 0.22, top + h * 0.45), (cx, top),
           (x1 - w * 0.22, top + h * 0.45), (x1 + 8, top + 14), (x1, base_y)]
    draw.polygon(pts, fill=C_GOLD)
    draw.rectangle((x0, base_y - 10, x1, base_y), fill=(214, 172, 36))
    for px, py in ((x0 - 8, top + 14), (cx, top), (x1 + 8, top + 14)):
        draw.ellipse((px - 7, py - 7, px + 7, py + 7), fill=(255, 232, 120))


def _medal(draw, cx, cy, colour, r=24):
    pts = [(cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    draw.polygon(pts, fill=colour)
    inner = [(cx + (r - 8) * math.cos(math.radians(60 * i - 30)), cy + (r - 8) * math.sin(math.radians(60 * i - 30)))
             for i in range(6)]
    draw.polygon(inner, fill=tuple(max(0, c - 70) for c in colour))


LB_OUT_W = 400  # finished picture width in pixels; fits a narrow Discord window


def _shrink(im):
    from PIL import Image
    return im.resize((LB_OUT_W, max(1, round(im.height * LB_OUT_W / im.width))), Image.LANCZOS)


def render_podium(mode_name, period_label, podium, avatars):
    """Title + the top 3 with their profile pictures. podium: list of dicts (rank, uid, name, w, l)."""
    from PIL import Image, ImageDraw, ImageFilter
    H = 560
    im = Image.new("RGBA", (LB_W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    d.rounded_rectangle((20, 8, LB_W - 20, H - 8), radius=34, fill=C_CARD + (255,), outline=C_CARD_LINE, width=3)
    d.text((LB_W / 2, 52), "Leaderboard", font=lb_font("bold", 46), fill=C_TEXT, anchor="mm")
    d.text((LB_W / 2, 100), f"{mode_name}  |  {period_label}", font=lb_font("medium", 27), fill=C_CYAN, anchor="mm")
    top, bottom = 130, H - 20
    d.rounded_rectangle((42, top, LB_W - 42, bottom), radius=30, fill=C_PANEL, outline=(34, 150, 170), width=2)
    slots = {1: (500, 300, 190, C_GOLD), 2: (240, 312, 140, C_SILVER), 3: (760, 312, 140, C_BRONZE)}
    by_rank = {e["rank"]: e for e in podium}
    for rank, (cx, cy, size, colour) in slots.items():
        e = by_rank.get(rank)
        y0 = cy - size // 2
        glow = Image.new("RGBA", (size + 80, size + 80), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle((40, 40, size + 40, size + 40), radius=34, fill=colour + (70,))
        glow = glow.filter(ImageFilter.GaussianBlur(14))
        im.alpha_composite(glow, (cx - size // 2 - 40, y0 - 40))
        d = ImageDraw.Draw(im, "RGBA")
        border = 6 if rank == 1 else 5
        d.rounded_rectangle((cx - size // 2 - border, y0 - border, cx + size // 2 + border, y0 + size + border),
                            radius=int(size * 0.2) + border, fill=colour if e else (60, 80, 90))
        av = _rounded_avatar(avatars.get(e["uid"]) if e else None, size, int(size * 0.2), (e or {}).get("name", "?"),
                             tint={1: (14, 130, 160), 2: (50, 90, 150), 3: (170, 110, 40)}[rank])
        im.alpha_composite(av, (cx - size // 2, y0))
        d = ImageDraw.Draw(im, "RGBA")
        if rank == 1:
            _crown(d, cx, y0 - 6, w=110, h=56)
        else:
            _medal(d, cx, y0 - 26, colour, r=22)
        ny = y0 + size + 24
        if e:
            f, txt = _fit_text(d, f"@{e['name']}", "medium", 29 if rank == 1 else 26, 260)
            d.text((cx, ny), txt, font=f, fill=colour, anchor="mm")
            d.text((cx, ny + 36), f"{e['w']} Wins", font=lb_font("bold", 34 if rank == 1 else 30), fill=colour, anchor="mm")
            d.text((cx, ny + 70), f"W {e['w']}   L {e['l']}", font=lb_font("regular", 22), fill=C_MUTED, anchor="mm")
        else:
            d.text((cx, ny + 10), "No one yet", font=lb_font("medium", 24), fill=C_MUTED, anchor="mm")
    d.text((LB_W / 2, bottom - 20), f"Ranked by wins  |  {mode_name}", font=lb_font("regular", 21), fill=C_MUTED, anchor="mm")
    out = io.BytesIO()
    _shrink(im).save(out, "PNG")
    return out.getvalue()


def render_list(entries, avatars):
    """Rows like the mock-up (rank, picture, name over W/L, wins). entries: dicts (rank, uid, name, w, l)."""
    from PIL import Image, ImageDraw
    row_h, gap = 84, 9
    H = 26 + len(entries) * (row_h + gap) + 18
    im = Image.new("RGBA", (LB_W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im, "RGBA")
    d.rounded_rectangle((20, 0, LB_W - 20, H - 4), radius=34, fill=C_CARD + (255,), outline=C_CARD_LINE, width=3)
    y = 22
    for e in entries:
        d.rounded_rectangle((50, y, LB_W - 50, y + row_h), radius=24, fill=C_ROW, outline=C_ROW_LINE, width=2)
        d.text((100, y + row_h / 2), str(e["rank"]), font=lb_font("bold", 32), fill=C_TEXT, anchor="mm")
        av = _rounded_avatar(avatars.get(e["uid"]), 62, 14, e.get("name", "?"))
        im.alpha_composite(av, (146, y + 11))
        d = ImageDraw.Draw(im, "RGBA")
        f, txt = _fit_text(d, f"@{e['name']}", "medium", 28, 520)
        d.text((226, y + 29), txt, font=f, fill=C_TEXT, anchor="lm")
        d.text((226, y + 61), f"W {e['w']}  L {e['l']}", font=lb_font("regular", 21), fill=C_MUTED, anchor="lm")
        d.text((LB_W - 90, y + row_h / 2), f"{e['w']} Wins", font=lb_font("bold", 30), fill=C_CYAN, anchor="rm")
        y += row_h + gap
    out = io.BytesIO()
    _shrink(im).save(out, "PNG")
    return out.getvalue()


# ============================================================ leaderboard storage (its own file, with a backup copy)
class LbStore:
    """Every decided match is a row in the SQLite table `matches` (one row per ticket), apart from the ticket data,
    so ticket clean-ups can never touch it. self.d["matches"] is the in-memory copy; save() writes only what changed."""
    def __init__(self, legacy_json: str):
        self.legacy = legacy_json
        self.d = {"matches": [], "backfilled": False, "adjust": {}}
        self._keys = set()
        for g, m, n, ts, w, l, ws, ls in DB.execute("SELECT g, m, n, ts, w, l, ws, ls FROM matches"):
            self.d["matches"].append({"g": g, "m": m, "n": n, "ts": ts, "w": w, "l": l, "ws": ws, "ls": ls})
            self._keys.add((g, m, n))
        self.d["reset_id"] = None
        meta = DB.execute("SELECT value FROM kv WHERE key = 'lb_meta'").fetchone()
        if meta:
            try:
                info = json.loads(meta[0])
                self.d["backfilled"] = bool(info.get("backfilled"))
                self.d["reset_id"] = info.get("reset_id")
                self.d["adjust"] = info.get("adjust") or {}
            except Exception:
                pass
        if not self.d["matches"] and not self.d["reset_id"]:  # after a reset, old files are never pulled back in
            self._import_json()

    def _import_json(self):
        """New database: pull in an older leaderboard_data.json (or its .bak / snapshots) once."""
        folder = os.path.join(os.path.dirname(self.legacy), "lb_snapshots")
        snaps = sorted((os.path.join(folder, f) for f in os.listdir(folder)), reverse=True) if os.path.isdir(folder) else []
        for cand in [self.legacy, self.legacy + ".bak"] + snaps:
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data.get("matches"), list) and data["matches"]:
                    self.d["matches"] = data["matches"]
                    self.d["backfilled"] = bool(data.get("backfilled"))
                    self.save()
                    print(f"Leaderboard: imported {len(self.d['matches'])} matches from {cand} into SQLite.")
                    return
            except Exception:
                continue

    def save(self):
        cur = {}
        for x in self.d["matches"]:
            try:
                row = (int(x["g"]), str(x["m"]), int(x["n"]), int(x["ts"]), int(x["w"]), int(x["l"]),
                       int(x.get("ws") or 0), int(x.get("ls") or 0))
            except Exception:
                continue
            cur[(row[0], row[1], row[2])] = row
        gone = [k for k in self._keys if k not in cur]
        new = [cur[k] for k in cur if k not in self._keys]
        meta = json.dumps({"backfilled": bool(self.d.get("backfilled")), "reset_id": self.d.get("reset_id"),
                           "adjust": self.d.get("adjust") or {}})
        with DB:  # one transaction: all or nothing
            DB.executemany("DELETE FROM matches WHERE g = ? AND m = ? AND n = ?", gone)
            DB.executemany("INSERT OR REPLACE INTO matches (g, m, n, ts, w, l, ws, ls) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", new)
            DB.execute("INSERT INTO kv (key, value) VALUES ('lb_meta', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (meta,))
        self._keys = set(cur)
        try:   # copy to the free cloud database too
            CLOUD.put_matches(new, gone)
            CLOUD.put_top("lb_meta", meta)
        except Exception as exc:
            print(f"Cloud sync error: {exc!r}")
        daily_backup()


lbstore = LbStore(os.path.join(DATA_DIR, "leaderboard_data.json"))
if store.d.get("matches") and not lbstore.d["matches"]:  # matches saved by the earlier version move over once
    lbstore.d["matches"] = store.d["matches"]
    store.d["matches"] = []
    lbstore.save()
    store.save()


def apply_lb_reset():
    """One time only: wipes every leaderboard match so the boards start fresh. A copy of the old database is kept
    next to the bot first (rivalz_before_reset_<time>.sqlite3). Old result cards / tickets are NOT re-imported."""
    if not LB_RESET_ID or lbstore.d.get("reset_id") == LB_RESET_ID:
        return
    old = len(lbstore.d["matches"])
    try:
        dest = os.path.join(DATA_DIR, f"rivalz_before_reset_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}.sqlite3")
        out = sqlite3.connect(dest)
        try:
            DB.backup(out)
        finally:
            out.close()
        print(f"Leaderboard reset: safety copy saved as {dest}")
    except Exception as exc:
        print(f"Leaderboard reset: could not save a safety copy ({exc!r}), resetting anyway.")
    lbstore.d["matches"] = []
    lbstore.d["backfilled"] = True   # nothing may be rebuilt from old tickets
    lbstore.d["reset_id"] = LB_RESET_ID
    store.d["matches"] = []
    lbstore.save()
    store.save()
    print(f"Leaderboard reset: {old} matches cleared. Starting fresh.")


apply_lb_reset()


# ============================================================ match stats (feed the leaderboards)
def period_start(period: str) -> float:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight.timestamp()
    if period == "week":
        return (midnight - timedelta(days=now.weekday())).timestamp()
    return 0.0


def unrecord_match(gid, mode, number) -> int:
    before = len(lbstore.d["matches"])
    lbstore.d["matches"] = [x for x in lbstore.d["matches"] if not (x["g"] == gid and x["m"] == mode and x["n"] == number)]
    removed = before - len(lbstore.d["matches"])
    if removed:
        lbstore.save()
    return removed


def match_row(t, ts):
    w = winner_index(t)
    if w is None:
        return None
    return {"g": t["guild_id"], "m": t["mode"], "n": t["number"], "ts": int(ts),
            "w": t["p"][w]["discord_id"], "l": t["p"][1 - w]["discord_id"],
            "ws": t["scores"][w], "ls": t["scores"][1 - w]}


def record_match(t) -> bool:
    """Called when a ticket ends. Only decided matches count (a draw or a missing score counts for nobody)."""
    row = match_row(t, time.time())
    if not row:
        return False
    unrecord_match(row["g"], row["m"], row["n"])  # never count one ticket twice
    lbstore.d["matches"].append(row)
    lbstore.save()
    return True


def backfill_matches():
    """One time: turn matches that were already saved (deleted + still open tickets) into leaderboard records."""
    if lbstore.d.get("backfilled"):
        return
    seen = {(x["g"], x["m"], x["n"]) for x in lbstore.d["matches"]}
    sources = [(a, a.get("created", 0)) for a in store.d["archive"].values()]
    sources += [(t, t.get("ended_at") or t["created"]) for t in store.d["tickets"].values()
                if t.get("status") in ("ended", "finished")]
    for src, ts in sources:
        try:
            row = match_row(src, ts)
        except Exception:
            row = None
        if row and (row["g"], row["m"], row["n"]) not in seen:
            seen.add((row["g"], row["m"], row["n"]))
            lbstore.d["matches"].append(row)
    lbstore.d["backfilled"] = True
    lbstore.save()


def message_text(components) -> str:
    """All the text of a Components V2 message (containers, sections, text displays)."""
    out = []

    def walk(c):
        content = getattr(c, "content", None)
        if isinstance(content, str):
            out.append(content)
        for ch in getattr(c, "children", None) or []:
            walk(ch)
        acc = getattr(c, "accessory", None)
        if acc is not None:
            walk(acc)

    for c in components or []:
        walk(c)
    return "\n".join(out)


def parse_result(text):
    """A final (closed) result card -> one match row, or None (live cards, draws and 'not recorded' are skipped)."""
    title = re.search(r"(Duel|Spar) #(\d+) Result", text)
    if not title or "This match is closed" not in text:
        return None
    win = re.search(r"\*\*Winner:\*\* <@!?(\d+)>", text)
    pl = re.search(r"<@!?(\d+)> vs <@!?(\d+)>", text)
    if not win or not pl:
        return None
    wid, a, b = int(win.group(1)), int(pl.group(1)), int(pl.group(2))
    if wid not in (a, b):
        return None
    when = re.search(r"Ended At\*\* - <t:(\d+):F>", text)
    sc = re.search(r"`(\d+|-)` - `(\d+|-)`", text)
    ws = ls = 0
    if sc and sc.group(1).isdigit() and sc.group(2).isdigit():
        ws, ls = (int(sc.group(1)), int(sc.group(2))) if wid == a else (int(sc.group(2)), int(sc.group(1)))
    return {"m": title.group(1).lower(), "n": int(title.group(2)), "ts": int(when.group(1)) if when else 0,
            "w": wid, "l": b if wid == a else a, "ws": ws, "ls": ls}


async def rebuild_from_results(guild, chan, limit=None):
    """Read the bot's own result cards in a results channel and add every finished match that is missing.
    Discord keeps those cards, so this works even when the bot's data files were lost."""
    seen = {(x["g"], x["m"], x["n"]) for x in lbstore.d["matches"]}
    scanned = added = 0
    async for message in chan.history(limit=limit, oldest_first=True):
        if message.author.id != bot.user.id:
            continue
        scanned += 1
        row = parse_result(message_text(message.components) or message.content or "")
        if not row:
            continue
        key = (guild.id, row["m"], row["n"])
        if key in seen:
            continue
        seen.add(key)
        lbstore.d["matches"].append({"g": guild.id, **row})
        added += 1
    if added:
        lbstore.save()
    return scanned, added


def recover_matches():
    """Prints how many matches the database holds. If it is empty, rebuilds from saved tickets/archive (backfill)."""
    if lbstore.d["matches"]:
        print(f"Leaderboard: {len(lbstore.d['matches'])} matches loaded from {DB_FILE}")
        return
    if not lbstore.d.get("reset_id"):
        lbstore.d["backfilled"] = False
        print(f"Leaderboard: no saved matches in {DB_FILE} (rebuilding from tickets if any).")
    else:
        print("Leaderboard: fresh start, no matches yet.")


def standings(mode, period):
    """GLOBAL standings: every server's decided matches count, so every server shows the same board."""
    since = period_start(period)
    tally = {}
    for x in lbstore.d["matches"]:
        if x["m"] != mode or x["ts"] < since:
            continue
        tally.setdefault(x["w"], [0, 0])[0] += 1
        tally.setdefault(x["l"], [0, 0])[1] += 1
    if period == "all":   # counts set by hand with /set count d|s (all-time board only)
        for uid, (dw, dl) in ((lbstore.d.get("adjust") or {}).get(mode) or {}).items():
            e = tally.setdefault(int(uid), [0, 0])
            e[0], e[1] = max(0, e[0] + dw), max(0, e[1] + dl)
    return sorted(((uid, w, l) for uid, (w, l) in tally.items() if w or l), key=lambda r: (-r[1], r[2], r[0]))


def rank_info(rows, uid):
    """(rank, wins, losses, top_percent) for a user, or None when unranked."""
    for i, (u, w, l) in enumerate(rows):
        if u == uid:
            return i + 1, w, l, (i + 1) / len(rows) * 100
    return None


# ============================================================ names + profile pictures (always current)
USER_CACHE = {}


async def lookup_user(uid):
    """Name and profile picture. Looked up fresh (cached for 1 minute) and the picture is re-downloaded
    the moment its hash changes, so a new profile picture shows on the next refresh."""
    hit = USER_CACHE.get(uid) or {}
    if hit and time.time() - hit["ts"] < 60:
        return hit["name"], hit["data"]
    saved_name = (store.d.get("names") or {}).get(str(uid))
    name, data, key = hit.get("name") or saved_name or f"user{str(uid)[-4:]}", hit.get("data"), hit.get("key")
    try:
        user = await bot.fetch_user(uid)
        name = user.name
        names = store.d.setdefault("names", {})
        if names.get(str(uid)) != name:   # saved, so the right username survives restarts and Discord hiccups
            names[str(uid)] = name
            store.save()
        akey = user.display_avatar.key
        if akey != key or data is None:
            data = await user.display_avatar.replace(size=128, format="png").read()
            key = akey
    except Exception:
        pass
    USER_CACHE[uid] = {"ts": time.time(), "name": name, "data": data, "key": key}
    return name, data


def page_slice(rows, page):
    """(page, pages, podium_rows, list_rows) with global ranks attached. Page 1 = ranks 1-10 (podium + 4-10),
    page 2+ = the next 10 ranks as a list."""
    pages = max(1, -(-len(rows) // LB_PAGE))
    page = min(max(1, page), pages)
    if page == 1:
        podium = [(i + 1, r) for i, r in enumerate(rows[:3])]
        listed = [(i + 4, r) for i, r in enumerate(rows[3:LB_PAGE])]
    else:
        start = (page - 1) * LB_PAGE
        podium = []
        listed = [(start + i + 1, r) for i, r in enumerate(rows[start:start + LB_PAGE])]
    return page, pages, podium, listed


async def board_message(guild, mode, period, page=1):
    """Builds the whole leaderboard message: the layout plus its pictures. The board is global (all servers)."""
    rows = standings(mode, period)
    page, pages, podium_rows, list_rows = page_slice(rows, page)
    ids = [r[0] for _, r in podium_rows + list_rows]
    found = await asyncio.gather(*(lookup_user(u) for u in ids))
    names = {u: f[0] for u, f in zip(ids, found)}
    avatars = {u: f[1] for u, f in zip(ids, found) if f[1]}

    def entry(rank, r):
        return {"rank": rank, "uid": r[0], "name": names.get(r[0], "unknown"), "w": r[1], "l": r[2]}

    files = []
    if page == 1:
        podium = [entry(rk, r) for rk, r in podium_rows]
        files.append(discord.File(io.BytesIO(await asyncio.to_thread(
            render_podium, MODES[mode]["name"], LB_PERIODS[period], podium, avatars)), filename="podium.png"))
    entries = [entry(rk, r) for rk, r in list_rows]
    if entries:
        files.append(discord.File(io.BytesIO(await asyncio.to_thread(render_list, entries, avatars)), filename="list.png"))
    return lb_layout(guild, mode, period, page == 1, bool(entries), page, pages), files


# ============================================================ leaderboard message: dropdown (with My Rank), pages
class LbSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"rv:lbsel:(?P<mode>duel|spar)(?::(?P<page>\d+))?"):
    """The dropdown at the top: Daily / Weekly / All-time."""
    def __init__(self, mode, period="all", page=1):
        super().__init__(discord.ui.Select(
            custom_id=f"rv:lbsel:{mode}:{page}", placeholder="Global Leaderboard",
            options=[
                discord.SelectOption(label="All-time Leaderboard", value="all", description="Every win ever recorded.", default=period == "all"),
                discord.SelectOption(label="Weekly Leaderboard", value="week", description="Wins since Monday (UTC).", default=period == "week"),
                discord.SelectOption(label="Daily Leaderboard", value="day", description="Wins since midnight (UTC).", default=period == "day"),
            ]))
        self.mode = mode
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["mode"], page=int(match["page"] or 1))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LbSelect error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await guard(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        choice = values[0] if values else "all"
        cfg = gcfg(interaction.guild.id)
        await interaction.response.defer()
        period = choice if choice in LB_PERIODS else "all"
        cfg["lb_period"][self.mode] = period
        store.save()
        view, files = await board_message(interaction.guild, self.mode, period)
        await interaction.edit_original_response(view=view, attachments=files)


class LbButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:lbme:(?P<mode>duel|spar)"):
    """Small My Rank button at the top of the board (answers privately, the board is not touched)."""
    def __init__(self, mode):
        super().__init__(discord.ui.Button(label="My Rank", style=discord.ButtonStyle.secondary,
                                           custom_id=f"rv:lbme:{mode}"))
        self.mode = mode

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["mode"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LbButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        period = gcfg(interaction.guild.id)["lb_period"].get(self.mode, "all")
        await send_my_rank(interaction, self.mode, period)


class LbPage(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:lbpg:(?P<mode>duel|spar):(?P<page>\d+):(?P<slot>[a-z0-9]+)"):
    """Page buttons under the board: < 1 2 3 >"""
    def __init__(self, mode, target, slot, label=None, current=False, disabled=False):
        super().__init__(discord.ui.Button(
            label=label or "\u200b", style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
            custom_id=f"rv:lbpg:{mode}:{target}:{slot}", disabled=disabled))
        self.mode = mode
        self.target = target

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["mode"], int(match["page"]), match["slot"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LbPage error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await guard(interaction):
            return
        await interaction.response.defer()
        period = gcfg(interaction.guild.id)["lb_period"].get(self.mode, "all")
        view, files = await board_message(interaction.guild, self.mode, period, self.target)
        await interaction.edit_original_response(view=view, attachments=files)


def page_row(mode, page, pages):
    """The page buttons (Discord allows 5 per row): previous, up to 3 page numbers, next."""
    first = min(max(1, page - 1), max(1, pages - 2))
    row = discord.ui.ActionRow()
    row.add_item(LbPage(mode, max(1, page - 1), "prev", "<", disabled=page <= 1))
    for i, n in enumerate(range(first, min(pages, first + 2) + 1)):
        row.add_item(LbPage(mode, n, f"p{i}", str(n), current=n == page))
    row.add_item(LbPage(mode, min(pages, page + 1), "next", ">", disabled=page >= pages))
    return row


def lb_layout(guild, mode, period, has_podium, has_list, page=1, pages=1):
    m = MODES[mode]
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=m["colour"])
    row = discord.ui.ActionRow()
    row.add_item(LbSelect(mode, period, page))
    c.add_item(row)
    where = f"Top {LB_PAGE}" if pages == 1 else f"Page {page}/{pages}"
    intro = discord.ui.TextDisplay(
        f"Leaderboard for the number of **{m['name']}** wins.\n-# Showing {LB_PERIODS[period]} | {where}")
    try:
        c.add_item(discord.ui.Section(intro, accessory=LbButton(mode)))  # small My Rank button, top right
    except Exception:
        c.add_item(intro)
        r2 = discord.ui.ActionRow()
        r2.add_item(LbButton(mode))
        c.add_item(r2)
    c.add_item(discord.ui.TextDisplay(f"**Server:** {md(guild.name)}"))
    c.add_item(discord.ui.Separator())
    if has_podium:
        top = discord.ui.MediaGallery()
        top.add_item(media="attachment://podium.png")
        c.add_item(top)
    if has_list:
        if has_podium:
            c.add_item(discord.ui.Separator())
        lst = discord.ui.MediaGallery()
        lst.add_item(media="attachment://list.png")
        c.add_item(lst)
    elif has_podium:
        c.add_item(discord.ui.TextDisplay("-# Nobody else is ranked yet."))
    if pages > 1:
        c.add_item(discord.ui.Separator())
        c.add_item(page_row(mode, page, pages))
    view.add_item(c)
    return view


async def send_my_rank(interaction: discord.Interaction, mode: str, period: str):
    """Private (ephemeral) My Rank card. The interaction is already answered by the dropdown callback."""
    user = interaction.user
    m = MODES[mode]
    info = rank_info(standings(mode, period), user.id)
    everything = rank_info(standings(mode, "all"), user.id)
    if info:
        top = [f"> **User:** `@{esc(user.name)}`",
               f"> **Rank:** #{info[0]} (Top {info[3]:.2f}%)",
               f"> **Wins:** {info[1]}",
               f"> **Losses:** {info[2]}"]
    else:
        top = [f"> **User:** `@{esc(user.name)}`", "> **Rank:** Unranked", "> **Wins:** 0",
               f"> Win a {m['name']} to get on the {LB_PERIODS[period]} board."]
    sections = [top]
    if everything and period != "all":
        sections.append([f"> **All-time:** #{everything[0]} (Top {everything[3]:.2f}%) | {everything[1]}W {everything[2]}L"])
    await interaction.followup.send(view=build_card(
        f"My Rank ({m['name']} | {LB_PERIODS[period]})", sections, accent=m["colour"]), ephemeral=True)


LB_SIG = {}  # what each channel's board last showed, so an unchanged board is not reposted every 5 minutes


async def refresh_leaderboards(guild, repost=False):
    """Posts a fresh Duel and Spar board in their channels and removes the previous one. A board that has not
    changed since it was last posted is left alone (so there is no pointless delete + repost)."""
    cfg = gcfg(guild.id)
    for mode in LB_MODES:
        cid = cfg["ch"].get(f"{mode}_lb")
        chan = guild.get_channel(cid) if cid else None
        if not chan:
            continue
        period = "all"  # a freshly posted board always starts on the global All-time view
        cfg["lb_period"][mode] = "all"
        sig = (period, tuple(standings(mode, period)))
        skey = hashlib.sha1(repr(sig).encode()).hexdigest()[:16]
        if not repost and LB_SIG.get((guild.id, mode)) == sig:
            continue
        if not repost and (guild.id, mode) not in LB_SIG and cfg.get("lb_sig", {}).get(mode) == skey and cfg["lb_msg"].get(mode):
            try:  # a restart: the posted board still shows these standings, leave it alone
                await chan.fetch_message(cfg["lb_msg"][mode])
                LB_SIG[(guild.id, mode)] = sig
                continue
            except discord.HTTPException:
                pass
        try:
            view, files = await board_message(guild, mode, period)
        except Exception as exc:
            print(f"Could not draw the {mode} leaderboard: {exc!r}")
            continue
        try:
            msg = await chan.send(view=view, files=files)
        except discord.HTTPException as exc:
            print(f"Could not post the {mode} leaderboard (needs View Channel, Send Messages, Attach Files): {exc!r}")
            continue
        LB_SIG[(guild.id, mode)] = sig
        cfg.setdefault("lb_sig", {})[mode] = skey
        old_id = cfg["lb_msg"].get(mode)
        cfg["lb_msg"][mode] = msg.id
        store.save()
        try:  # remove the previous leaderboard(s) once the new one is up
            async for message in chan.history(limit=30):
                if message.author.id == bot.user.id and message.id != msg.id:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
        except discord.HTTPException:
            pass
        if old_id and old_id != msg.id:
            try:
                await chan.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass


LB_TASKS = {}


def schedule_lb_refresh(gid=None):
    """The board is global, so a new result refreshes the board in EVERY authorized server, a few seconds later
    (several matches in a row become one refresh)."""
    old = LB_TASKS.get("all")
    if old and not old.done():
        return

    async def later():
        await asyncio.sleep(8)
        for guild in list(bot.guilds):
            if is_authorized(guild.id):
                try:
                    await refresh_leaderboards(guild)
                except Exception as exc:
                    print(f"Leaderboard refresh failed in {guild.name}: {exc!r}")

    LB_TASKS["all"] = asyncio.create_task(later())


async def check_guild_health(guild):
    """Makes sure one authorized server still has its panel message (reposts it if missing) and its leaderboards."""
    cfg = gcfg(guild.id)
    cid = cfg["ch"].get("panel")
    if not cid:
        return
    chan = guild.get_channel(cid)
    if chan is None:   # the panel channel was deleted: recreate whatever is missing, then repost
        await build_guild(guild)
        await post_panel(guild)
        await refresh_leaderboards(guild, repost=True)
        return
    mid = cfg.get("panel_msg")
    try:
        if not mid:
            raise discord.NotFound(None, "no panel saved")
        await chan.fetch_message(mid)
    except discord.NotFound:
        await post_panel(guild)
    except discord.HTTPException:
        pass   # a Discord hiccup: look again in 10 minutes
    await refresh_leaderboards(guild)


@tasks.loop(minutes=10)
async def health_loop():
    for guild in list(bot.guilds):
        if not is_authorized(guild.id):
            continue
        try:
            await check_guild_health(guild)
        except Exception as exc:
            print(f"Health check failed in {guild.name}: {exc!r}")


@health_loop.before_loop
async def _health_wait():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def lb_loop():
    for guild in list(bot.guilds):
        if is_authorized(guild.id):
            try:
                await refresh_leaderboards(guild)  # new wins, new profile pictures and Daily/Weekly roll-overs
            except Exception as exc:
                print(f"Leaderboard refresh failed in {guild.name}: {exc!r}")


# ============================================================ /authorize + /build


async def tell_owner(text):
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(text)
    except Exception:
        pass


async def build_guild(guild):
    """Create (or reuse) every category and channel Rivalz needs. Safe to run again: it only fills what is missing."""
    cfg = gcfg(guild.id)
    chs, cats = cfg["ch"], cfg["cat"]
    created, kept = [], []
    me = guild.me
    staff = guild.get_role(STAFF_ROLE_ID) if STAFF_ROLE_ID else None
    bot_ow = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, attach_files=True,
                                         read_message_history=True, manage_channels=True)
    read_only = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False,
                                                        add_reactions=False, create_public_threads=False,
                                                        create_private_threads=False),
        me: bot_ow,
    }
    private = {guild.default_role: discord.PermissionOverwrite(view_channel=False), me: bot_ow}
    if staff:
        private[staff] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)

    async def category(key, name, overwrites):
        c = guild.get_channel(cats[key]) if cats.get(key) else None
        if isinstance(c, discord.CategoryChannel):
            kept.append(f"Category **{c.name}**")
            return c
        c = await guild.create_category(name, overwrites=overwrites, reason="Rivalz /build")
        cats[key] = c.id
        created.append(f"Category **{c.name}**")
        return c

    async def text(key, name, parent, overwrites, topic, move=True):
        c = guild.get_channel(chs[key]) if chs.get(key) else None
        if isinstance(c, discord.TextChannel):
            if move and parent and c.category_id != parent.id:  # e.g. an old LEADERBOARDS category: pull it into the main one
                try:
                    await c.edit(category=parent, sync_permissions=False, reason="Rivalz /build")
                    created.append(f"{c.mention} (moved into **{parent.name}**)")
                    return c
                except discord.HTTPException:
                    pass
            kept.append(c.mention)
            return c
        c = await guild.create_text_channel(name, category=parent, overwrites=overwrites, topic=topic, reason="Rivalz /build")
        chs[key] = c.id
        created.append(c.mention)
        return c

    # Category 1: the 5 base channels
    main = await category("main", "RIVALZ", {})
    await text("panel", "matchmaking", main, read_only, "Start a Duel or Spar from here.")
    await text("results", "results", main, read_only, "Finished match results.")
    await text("log", "match-logs", main, private, "Rivalz ticket logs (staff only).")
    await text("duel_lb", "duel-leaderboard", main, read_only, "Duel leaderboard, ranked by wins.")
    await text("spar_lb", "spar-leaderboard", main, read_only, "Spar leaderboard, ranked by wins.")
    # Ongoing-Ping: the channel that announces live matches + the role that gets pinged for them
    adopt_legacy_extras(guild)  # your original server keeps its existing channel and role
    await text("ongoing", "ongoing-ping", main, read_only, "Live Duels and Spars. Join as a spectator to watch.", move=False)
    roles = cfg["role"]
    orole = guild.get_role(roles["ongoing"]) if roles.get("ongoing") else None
    if orole:
        if not orole.mentionable:  # the ping only works when the role can be mentioned
            try:
                await orole.edit(mentionable=True, reason="Rivalz /build: the Ongoing role must be mentionable")
            except discord.HTTPException:
                pass
        kept.append(f"Role {orole.mention}")
    else:
        orole = await guild.create_role(name="Ongoing Ping", mentionable=True, reason="Rivalz /build")
        roles["ongoing"] = orole.id
        created.append(f"Role {orole.mention}")
    # Category 2: tickets (every Duel/Spar ticket is created in here)
    await category("tickets", "TICKETS", private)
    # An old LEADERBOARDS category from the previous layout is removed once it is empty
    old_lb = guild.get_channel(cats["lb"]) if cats.get("lb") else None
    if isinstance(old_lb, discord.CategoryChannel):
        if not old_lb.channels:
            try:
                await old_lb.delete(reason="Rivalz /build: leaderboards now live in the main category")
                created.append("Removed the empty old LEADERBOARDS category")
            except discord.HTTPException:
                pass
    cats.pop("lb", None)
    store.save()
    return created, kept


# ============================================================ slash commands
def fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    return f"{d}d {h}h {m}m {sec}s" if d else f"{h}h {m}m {sec}s"


@bot.tree.command(name="ping", description="Check that Rivalz is online and how fast it responds.")
async def ping(interaction: discord.Interaction):
    started = time.perf_counter()
    await interaction.response.defer()  # public: everyone in the channel sees it
    api_ms = round((time.perf_counter() - started) * 1000)
    await interaction.followup.send(view=ping_view(api_ms))


@bot.tree.command(name="ping-roblox", description="Check the Roblox connection.")
async def ping_roblox(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    t = time.perf_counter()
    user = await bot.roblox.user_by_name("Roblox")
    api = f"Online ({round((time.perf_counter() - t) * 1000)} ms)" if user else "Unreachable"
    await interaction.followup.send(view=build_card(
        "Roblox Status",
        [[field("Roblox API", api)],
         ["> Used to look up usernames and avatars. Server links are now pasted by the player when requesting."]],
        accent=GREEN if user else RED), ephemeral=True)


@bot.tree.command(name="panel", description="Owner only: repost the Rivalz panel in this server.")
@app_commands.guild_only()
async def panel(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ok = await post_panel(interaction.guild)
    await interaction.followup.send("Panel reposted." if ok else "Could not post the panel. Run /build first.", ephemeral=True)


@bot.tree.command(name="authorize", description="Owner only: authorize Rivalz to be used in this server.")
@app_commands.guild_only()
async def authorize(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = gcfg(interaction.guild.id)
    was = cfg["authorized"]
    authorize_guild(interaction.guild.id, interaction.user.id)
    built = bool(cfg["ch"].get("panel"))
    lines = ["> Rivalz is now authorized in this server." if not was else "> This server was already authorized."]
    lines.append("> Everything is set up. Use `/build` any time to repair missing channels." if built
                 else "> Next, run `/build` to create the categories and channels.")
    await interaction.followup.send(view=build_card("Server Authorized", [lines], accent=GREEN), ephemeral=True)


@bot.tree.command(name="build", description="Owner only: create the Rivalz categories and channels (run /authorize first).")
@app_commands.guild_only()
async def build(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    guild = interaction.guild
    if not is_authorized(guild.id):
        authorize_guild(guild.id, interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    perms = guild.me.guild_permissions
    if not (perms.manage_channels and perms.manage_roles):
        return await notice(interaction, "Missing Permission",
                            "> Rivalz needs **Manage Channels** and **Manage Roles** (it sets the private/read-only channel permissions). Give the bot those, then run `/build` again.")
    try:
        created, kept = await build_guild(guild)
    except discord.HTTPException as exc:
        print(f"Build failed: {exc!r}")
        return await notice(interaction, "Build Failed",
                            f"> Discord refused one of the channels: `{exc.status} / {exc.code}: {exc.text}`\n"
                            "> Check that Rivalz has **Manage Channels**, **Manage Roles**, **View Channel**, **Send Messages**, **Embed Links** and **Attach Files**, then run `/build` again.")
    await post_panel(guild)
    await refresh_leaderboards(guild, repost=True)
    sections = [["> Rivalz is built and running in this server."]]
    if created:
        sections.append(["**Created**"] + [f"> {x}" for x in created])
    if kept:
        sections.append(["**Already there**"] + [f"> {x}" for x in kept])
    await interaction.followup.send(view=build_card("Build Complete", sections, accent=GREEN), ephemeral=True)


DEAUTH_KEEP_LEGACY = True   # your original server's hand-made channels (LEGACY ids) are never deleted by /deauthorize


async def deauthorize_guild(guild) -> list:
    """Deletes everything Rivalz made in this server (tickets, voice channels, the /build channels and categories,
    the Ongoing Ping role), forgets the server's setup and leaves it. Leaderboard scores are NOT touched."""
    cfg = store.d["guilds"].get(str(guild.id)) or {}
    keep = set(LEGACY.values()) if DEAUTH_KEEP_LEGACY else set()
    removed, failed, skipped = 0, 0, 0

    async def kill(obj):
        nonlocal removed, failed, skipped
        if obj is None:
            return
        if obj.id in keep:
            skipped += 1
            return
        try:
            await obj.delete(reason="Rivalz /deauthorize")
            removed += 1
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            failed += 1
            print(f"Deauthorize: could not delete {getattr(obj, 'name', obj)}: {exc!r}")

    # 1) tickets and their voice channels (and the live "ongoing" cards)
    mine = [(cid, t) for cid, t in list(store.d["tickets"].items()) if t.get("guild_id") == guild.id]
    for cid, t in mine:
        try:
            await clear_ongoing(t)
        except Exception:
            pass
        await kill(guild.get_channel(t.get("channel_id") or 0))
        if t.get("vc_id"):
            await kill(guild.get_channel(t["vc_id"]))
    # 2) the channels from /build, then the categories (after their channels are gone)
    for cid in list((cfg.get("ch") or {}).values()):
        await kill(guild.get_channel(cid))
    for cid in list((cfg.get("cat") or {}).values()):
        cat = guild.get_channel(cid)
        if isinstance(cat, discord.CategoryChannel):
            for child in list(cat.channels):      # tickets/voice channels that were created inside the category
                if child.id not in keep:
                    await kill(child)
            await kill(cat)
    # 3) the Ongoing Ping role
    rid = (cfg.get("role") or {}).get("ongoing")
    if rid and rid != LEGACY["ongoing_role"]:
        await kill(guild.get_role(rid))
    elif rid:
        skipped += 1
    # 4) forget this server (leaderboard matches stay: they are global and kept in their own table)
    for cid, _t in mine:
        store.d["tickets"].pop(cid, None)
    for c in store.d["challenges"].values():
        if c.get("guild_id") == guild.id and c.get("status") == "pending":
            c["status"] = "expired"
    store.d["guilds"].pop(str(guild.id), None)
    store.save()
    ENV_AUTH_GUILDS.discard(guild.id)
    sync_auth_backup()
    summary = [f"Removed {removed} channel(s), categories or roles from {guild.name}."]
    if failed:
        summary.append(f"{failed} could not be deleted (check the bot's Manage Channels / Manage Roles permission).")
    if skipped:
        summary.append(f"{skipped} original-server item(s) were kept on purpose.")
    summary.append("Leaderboard scores were not touched.")
    return summary


class DeauthorizeConfirm(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Remove everything and leave", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.used:
            return
        self.used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Removing everything, then leaving...", view=self)
        guild = bot.get_guild(self.guild_id)
        if not guild:
            return
        name = guild.name
        summary = await deauthorize_guild(guild)
        text = "\n".join(f"> {x}" for x in summary) + f"\n> Rivalz is leaving **{name}** now."
        try:
            await interaction.edit_original_response(content=text, view=None)   # fails if this channel was deleted
        except discord.HTTPException:
            pass
        await tell_owner(text)
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            print(f"Deauthorize: could not leave {name}: {exc!r}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.used = True
        await interaction.response.edit_message(content="Cancelled. Nothing was removed.", view=None)


@bot.tree.command(name="deauthorize", description="Owner only: delete every channel Rivalz created in this server, then leave it.")
@app_commands.guild_only()
async def deauthorize(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.send_message(
        "This deletes every channel, category, ticket, voice channel and role Rivalz created in this server, forgets the "
        "server's setup and removes the bot from it. Leaderboard scores are kept. Continue?",
        view=DeauthorizeConfirm(interaction.guild.id), ephemeral=True)


@bot.tree.command(name="restoreboard", description="Owner only: put leaderboard wins back from an old data file.")
@app_commands.describe(file="leaderboard_data.json, leaderboard_data.json.bak or rivalz_data.json from your old folder")
async def restoreboard(interaction: discord.Interaction, file: discord.Attachment):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if file.size > 25 * 1024 * 1024:
        return await notice(interaction, "File Too Big", "> That file is too large.")
    raw = await file.read()
    data = {}
    if raw.startswith(b"SQLite format 3\x00"):  # a database backup (from /backup or the backups folder)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as fh:
                fh.write(raw)
                tmp = fh.name
            src_db = sqlite3.connect(tmp)
            try:
                cols = ("g", "m", "n", "ts", "w", "l", "ws", "ls")
                data["matches"] = [dict(zip(cols, r)) for r in src_db.execute("SELECT g, m, n, ts, w, l, ws, ls FROM matches")]
            finally:
                src_db.close()
        except Exception:
            return await notice(interaction, "Not A Data File", "> That is not a valid Rivalz database file.")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
            assert isinstance(data, dict)
        except Exception:
            return await notice(interaction, "Not A Data File", "> That is not a valid JSON or database file.")
    rows = [r for r in data.get("matches", []) if isinstance(r, dict)]
    sources = [(a, a.get("created", 0)) for a in (data.get("archive") or {}).values() if isinstance(a, dict)]
    sources += [(t, t.get("ended_at") or t.get("created", 0)) for t in (data.get("tickets") or {}).values()
                if isinstance(t, dict) and t.get("status") in ("ended", "finished")]
    for src, ts in sources:
        try:
            row = match_row(src, ts)
        except Exception:
            row = None
        if row:
            rows.append(row)
    seen = {(x["g"], x["m"], x["n"]) for x in lbstore.d["matches"]}
    added = 0
    for r in rows:
        try:
            key = (r["g"], r["m"], r["n"])
            if r["m"] not in MODES or key in seen:
                continue
            lbstore.d["matches"].append({"g": r["g"], "m": r["m"], "n": r["n"], "ts": int(r["ts"]), "w": int(r["w"]),
                                         "l": int(r["l"]), "ws": r.get("ws", 0), "ls": r.get("ls", 0)})
            seen.add(key)
            added += 1
        except Exception:
            continue
    lbstore.save()
    schedule_lb_refresh()
    await notice(interaction, "Leaderboard Restored",
                 [f"> Added **{added}** matches ({len(lbstore.d['matches'])} saved in total).",
                  "> The boards refresh in a few seconds."], accent=GREEN)


@bot.tree.command(name="rebuildboard", description="Owner only: rebuild the leaderboard from the result cards in a results channel.")
@app_commands.describe(channel="The results channel to read (default: this server's results channel)")
async def rebuildboard(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    if not interaction.guild:
        return await interaction.response.send_message("Use this inside a server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    chan = channel
    if chan is None:
        rid = gcfg(interaction.guild.id)["ch"].get("results") or (LEGACY["results"] if interaction.guild.get_channel(LEGACY["results"]) else None)
        chan = interaction.guild.get_channel(rid) if rid else None
    if not chan:
        return await notice(interaction, "No Results Channel", "> Pick the results channel with the `channel` option.")
    try:
        scanned, added = await rebuild_from_results(interaction.guild, chan)
    except discord.HTTPException as exc:
        print(f"Rebuild failed: {exc!r}")
        return await notice(interaction, "Could Not Read The Channel",
                            "> Rivalz needs **View Channel** and **Read Message History** in that channel.")
    schedule_lb_refresh()
    await notice(interaction, "Leaderboard Rebuilt",
                 [f"> Read **{scanned}** result cards from {chan.mention}.",
                  f"> Added **{added}** matches ({len(lbstore.d['matches'])} saved in total).",
                  "> The boards refresh in a few seconds."], accent=GREEN)


@bot.tree.command(name="backup", description="Owner only: download a copy of the Rivalz database.")
async def backup_cmd(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as fh:
            tmp = fh.name
        out = sqlite3.connect(tmp)
        try:
            DB.backup(out)
        finally:
            out.close()
        with open(tmp, "rb") as fh:
            blob = fh.read()
        await interaction.followup.send(
            f"Backup: {len(lbstore.d['matches'])} matches. Keep this file safe; `/restoreboard` can load it back.",
            file=discord.File(io.BytesIO(blob), filename="rivalz.sqlite3"), ephemeral=True)
    except Exception as exc:
        print(f"Backup failed: {exc!r}")
        await notice(interaction, "Backup Failed", "> Could not make or send the backup (it may be too large for Discord).")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


@bot.tree.command(name="addmatch", description="Owner only: add one past result to the global leaderboard.")
@app_commands.describe(mode="Duel or Spar", winner="Who won", loser="Who lost", days_ago="How many days ago it happened (default 0)")
@app_commands.choices(mode=[app_commands.Choice(name="Duel", value="duel"), app_commands.Choice(name="Spar", value="spar")])
async def addmatch(interaction: discord.Interaction, mode: app_commands.Choice[str], winner: discord.User,
                   loser: discord.User, days_ago: app_commands.Range[int, 0, 3650] = 0):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    if winner.id == loser.id:
        return await notice(interaction, "Invalid", "> The winner and loser must be different people.")
    await interaction.response.defer(ephemeral=True)
    lbstore.d["matches"].append({"g": interaction.guild_id or 0, "m": mode.value, "n": -int(time.time() * 1000),
                                 "ts": int(time.time()) - days_ago * 86400, "w": winner.id, "l": loser.id, "ws": 0, "ls": 0})
    lbstore.save()
    schedule_lb_refresh()
    await notice(interaction, "Match Added",
                 [f"> **{mode.name}:** {winner.mention} beat {loser.mention}", "> The boards refresh in a few seconds."],
                 accent=GREEN)


@bot.tree.command(name="update", description="Owner only: update Rivalz in this server to its current features. Nothing is removed.")
@app_commands.guild_only()
async def update_cmd(interaction: discord.Interaction):
    if not is_owner(interaction.user):
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    guild = interaction.guild
    if not is_authorized(guild.id):
        authorize_guild(guild.id, interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    perms = guild.me.guild_permissions
    if not (perms.manage_channels and perms.manage_roles):
        return await notice(interaction, "Missing Permission",
                            "> Rivalz needs **Manage Channels** and **Manage Roles**. Give the bot those, then run `/update` again.")
    try:
        created, kept = await build_guild(guild)  # only fills what is missing, never deletes channels or roles
    except discord.HTTPException as exc:
        print(f"Update failed: {exc!r}")
        return await notice(interaction, "Update Failed", f"> Discord refused one step: `{exc.status} / {exc.code}: {exc.text}`")
    await post_panel(guild)
    tickets, cards = await refresh_guild_cards(guild)
    try:
        await bot.tree.sync()
        synced = "Slash commands synced."
    except discord.HTTPException as exc:
        print(f"Command sync failed: {exc!r}")
        synced = "Slash commands could not be synced right now (they sync again on restart)."
    sections = [["> Rivalz is up to date in this server. Nothing was removed, all data is kept."],
                [f"> Panel, leaderboards and `{tickets}` ticket card(s) refreshed.",
                 f"> New live cards posted: `{cards}`.", f"> {synced}"]]
    if created:
        sections.append(["**Created**"] + [f"> {x}" for x in created])
    await interaction.followup.send(view=build_card("Update Complete", sections, accent=GREEN), ephemeral=True)


@bot.tree.command(name="help", description="See what Rivalz can do.")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(view=build_card("Rivalz Help", [
        [f"> Challenge rivals to a **Duel** or **Spar** from the matchmaking panel. First to the chosen rounds wins."],
        ["`/ping` - check that Rivalz is online (everyone)",
         "`/my rank` - your Duel and Spar rank, wins, losses and win rate (everyone)",
         "`/set count d`, `/set count s` - set a player's leaderboard count (developer only)",
         "`/rank`, `/panel`, `/authorize`, `/build`, `/update`, `/backup`, `/deauthorize` ... - owner only"],
        ["> You can also start a message with **R** or **Rivalz**: `R ping`, `R leaderboard`, `R top 3`, `R rank`, `R open general`, "
         "`R open Divisions category`, `R channels`.",
         "> Developer and trusted users: `R update`, `R panel`, `R build`, `R authorize`, `R backup`, `R db`, `R deauthorize`. "
         "Developer only: `/trust add`, `/trust remove` (or `R ta @user`, `R tr @user`).",
         "> Watch a live match from the ongoing-ping channel with **Join as Spectator** (no limit, one match at a time). Spectators can only use Join, Profile and Join VC, plus send the gank image."],
    ], accent=GREY), ephemeral=True)


@bot.tree.command(name="rank", description="See your global Duel and Spar rank.")
async def rank_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid = interaction.user.id
    sections = []
    for mode in LB_MODES:
        info = rank_info(standings(mode, "all"), uid)
        name = MODES[mode]["name"]
        if info:
            sections.append([f"**{name}**", f"> **Rank:** #{info[0]} (Top {info[3]:.2f}%)",
                             f"> **Wins:** {info[1]}  |  **Losses:** {info[2]}"])
        else:
            sections.append([f"**{name}**", "> Unranked. Win a match to get on the board."])
    await interaction.followup.send(view=build_card("Your Rank (All-time)", sections, accent=GREEN), ephemeral=True)


my_group = app_commands.Group(name="my", description="Your own Rivalz stats.")


def win_rate_text(w: int, l: int) -> str:
    """Win rate as 00.00% (wins / (wins + losses))."""
    return f"{(w / (w + l) * 100) if (w + l) else 0.0:05.2f}%"


@my_group.command(name="rank", description="See your Duel and Spar rank, wins, losses and win rate.")
async def my_rank_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    user = interaction.user
    embed = discord.Embed(title=user.display_name, colour=GREEN)
    embed.set_thumbnail(url=user.display_avatar.url)
    tw = tl = 0
    for mode in LB_MODES:
        name = MODES[mode]["name"]
        info = rank_info(standings(mode, "all"), user.id)
        if info:
            rank, w, l, top = info
            tw, tl = tw + w, tl + l
            body = (f"**Rank:** `#{rank}` (Top {top:.2f}%)\n"
                    f"**Wins:** `{w}`\n**Losses:** `{l}`\n**Matches:** `{w + l}`\n"
                    f"**Win Rate:** `{win_rate_text(w, l)}`")
        else:
            body = ("**Rank:** Unranked\n**Wins:** `0`\n**Losses:** `0`\n**Matches:** `0`\n"
                    f"**Win Rate:** `{win_rate_text(0, 0)}`")
        embed.add_field(name=name, value=body, inline=True)
    embed.add_field(name="Overall", value=(f"**Wins:** `{tw}`\n**Losses:** `{tl}`\n**Matches:** `{tw + tl}`\n"
                                            f"**Win Rate:** `{win_rate_text(tw, tl)}`"), inline=False)
    embed.set_footer(text="Rivalz | Global all-time leaderboard")
    await interaction.followup.send(embed=embed)


bot.tree.add_command(my_group)


set_group = app_commands.Group(name="set", description="Developer only: leaderboard tools.")
count_group = app_commands.Group(name="count", description="Developer only: set a player's leaderboard count.", parent=set_group)


async def set_count(interaction: discord.Interaction, mode: str, user: discord.User, wins, losses):
    if not is_real_owner(interaction.user):
        return await interaction.response.send_message("Only the developer can use this command.", ephemeral=True)
    if wins is None and losses is None:
        return await interaction.response.send_message("Give at least `wins` or `losses`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    # what the saved matches already give this player on the all-time board (without any earlier manual change)
    adj = lbstore.d.setdefault("adjust", {}).setdefault(mode, {})
    adj.pop(str(user.id), None)
    cur_w, cur_l = next(((w, l) for u, w, l in standings(mode, "all") if u == user.id), (0, 0))
    new_w = cur_w if wins is None else wins
    new_l = cur_l if losses is None else losses
    adj[str(user.id)] = [new_w - cur_w, new_l - cur_l]
    if adj[str(user.id)] == [0, 0]:
        adj.pop(str(user.id), None)
    lbstore.save()
    schedule_lb_refresh()
    await notice(interaction, "Leaderboard Count Set",
                 [f"> **{MODES[mode]['name']}:** {user.mention} is now `{new_w}` wins / `{new_l}` losses (all-time).",
                  "> The boards refresh in a few seconds. Matches already recorded are kept."], accent=GREEN)


@count_group.command(name="d", description="Developer only: set a player's DUEL wins / losses on the leaderboard.")
@app_commands.describe(user="Whose count to set", wins="Total duel wins", losses="Total duel losses")
async def set_count_d(interaction: discord.Interaction, user: discord.User,
                      wins: app_commands.Range[int, 0, 100000] = None, losses: app_commands.Range[int, 0, 100000] = None):
    await set_count(interaction, "duel", user, wins, losses)


@count_group.command(name="s", description="Developer only: set a player's SPAR wins / losses on the leaderboard.")
@app_commands.describe(user="Whose count to set", wins="Total spar wins", losses="Total spar losses")
async def set_count_s(interaction: discord.Interaction, user: discord.User,
                      wins: app_commands.Range[int, 0, 100000] = None, losses: app_commands.Range[int, 0, 100000] = None):
    await set_count(interaction, "spar", user, wins, losses)


bot.tree.add_command(set_group)


trust_group = app_commands.Group(name="trust", description="Developer only: trusted users get full owner access.", guild_only=True)


@trust_group.command(name="add", description="Developer only: trust a user so they can use every owner command and the full bot.")
@app_commands.describe(user="Who to trust")
async def trust_add_cmd(interaction: discord.Interaction, user: discord.User):
    if not is_real_owner(interaction.user):
        return await interaction.response.send_message("Only the developer can manage trusted users.", ephemeral=True)
    if user.bot or user.id == OWNER_ID:
        return await interaction.response.send_message("Pick a real user other than yourself.", ephemeral=True)
    text = f"{user.mention} is trusted now. They can use every owner command and the full bot." if trust_add(user.id) \
        else f"{user.mention} is already trusted."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="remove", description="Developer only: remove a user's trust.")
@app_commands.describe(user="Who to remove")
async def trust_remove_cmd(interaction: discord.Interaction, user: discord.User):
    if not is_real_owner(interaction.user):
        return await interaction.response.send_message("Only the developer can manage trusted users.", ephemeral=True)
    text = f"{user.mention} is not trusted any more." if trust_remove(user.id) else f"{user.mention} was not trusted."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="list", description="Developer only: see who is trusted.")
async def trust_list_cmd(interaction: discord.Interaction):
    if not is_real_owner(interaction.user):
        return await interaction.response.send_message("Only the developer can manage trusted users.", ephemeral=True)
    ids = store.d.get("trusted") or []
    await interaction.response.send_message("Trusted: " + " ".join(f"<@{i}>" for i in ids) if ids else "Nobody is trusted yet.",
                                            ephemeral=True, allowed_mentions=NO_PING)


bot.tree.add_command(trust_group)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error: {error!r}")
    await safe_reply(interaction, "Something went wrong. Please try again.")


if __name__ == "__main__":
    if not TOKEN or TOKEN.startswith("PUT_"):
        raise SystemExit("Set DISCORD_TOKEN (in .env or your host's Variables) first.")
    if CLOUD_ON:  # a redeploy stops the bot with SIGTERM: copy the last changes to the cloud first
        import signal

        def _flush_and_stop(signum, frame):
            try:
                CLOUD.flush_now()
            finally:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
        try:
            signal.signal(signal.SIGTERM, _flush_and_stop)
        except (ValueError, OSError, AttributeError):
            pass
    try:
        bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Turn on MESSAGE CONTENT INTENT in the Discord Developer Portal (your app > Bot > Privileged Gateway "
            "Intents), save, then restart the bot.")