"""
Rivalz - Matchmaking bot for Duels and Squad Up
Requires: discord.py >= 2.6 (Components V2 + modal selects), aiohttp, python-dotenv, Pillow
Run:      py main.py
Needs the MESSAGE CONTENT INTENT enabled in the Developer Portal (Bot tab) for the "Rivalz <command>" messages.
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
    "duel_lb": 1568637988164862133,
    "cat_main": 1568343167437115423, "cat_tickets": 1568527718419664927,
    "ongoing": 1568968016493027328, "ongoing_role": 1568968211083567195,
    "streaks": 1570471881520451795,
}

EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
# Spectators have NO limit. A member can only spectate ONE live match at a time (see active_spectating).
RIVALZ_COOLDOWN = 3           # seconds between "Rivalz <command>" messages from the same person (the developer is exempt)
LB_RESET_ID = ""   # leaderboard wipe is DISABLED. Nothing is ever reset or deleted by an update.
MSG_NOTICE_AT = 10            # the "Match Details" card is posted at the 10th message of a ticket ...
MSG_NOTICE_REMOVE_AT = 20     # ... and removed again at the 20th message
LB_MODES = ("duel",)

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
        for mode in ("duel",):
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
    await send_cfg(t["guild_id"], "log", build_card(f"🎫 {m['name']}-ticket #{t['number']:03d} Opened", sections,
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
    return build_card(f"🗑️ {m['name']}-ticket #{a['number']:03d} Deleted", [fields, ["-# Rivalz"]],
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
                          template=r"rv:logspec:(?P<gid>\d+):(?P<mode>duel):(?P<num>\d+)"):
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
    return build_card(f"{m['name']} #{t['number']:03d} Result", [[head], bullets, [note]],
                      accent=accent, buttons=profile_buttons(t["p"]))


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

    try:   # a fighter on a streak: tell the streaks channel it is on the line
        asyncio.create_task(announce_on_the_line(t))
    except Exception as exc:
        print(f"Streak on the line task error: {exc!r}")
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
    """Every spectator watching the match (developers included, so the number matches the list)."""
    return len(spec_list(t))


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
    top.append("> You can watch and use the join links. Everything else is locked.")
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


class RemoveSpectatorButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:specrm:(?P<cid>\d+):(?P<uid>\d+)"):
    """On the 'Spectator Joined' card inside the ticket. The requester and the rival can press it."""
    def __init__(self, cid: int, uid: int):
        super().__init__(discord.ui.Button(label="Remove Spectator", style=discord.ButtonStyle.danger,
                                           custom_id=f"rv:specrm:{cid}:{uid}"))
        self.cid, self.uid = int(cid), int(uid)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["cid"]), int(match["uid"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"RemoveSpectatorButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(self.cid))
        if not t or t.get("status") == "finished":
            return await notice(interaction, "Match Over", "> This match is already closed.", accent=GREY)
        if side_of(t, interaction.user.id) is None and not is_owner(interaction.user):
            return await notice(interaction, "Fighters Only",
                                "> Only the requester and the rival can remove a spectator.", accent=GREY)
        await interaction.response.defer()
        async with spec_lock():
            entry = next((s for s in spec_list(t) if s["discord_id"] == self.uid), None)
            if entry:
                spec_list(t)[:] = [s for s in spec_list(t) if s["discord_id"] != self.uid]
                gone = t.setdefault("removed_spectators", [])
                if self.uid not in gone:
                    gone.append(self.uid)     # cannot come back to THIS match, but is free to spectate any other
                store.save()
        guild = interaction.guild
        if entry and guild:
            member = await member_of(guild, self.uid)
            if member:
                for chan_id in (t["channel_id"], t.get("vc_id")):
                    chan = guild.get_channel(chan_id) if chan_id else None
                    if not chan:
                        continue
                    try:
                        await chan.set_permissions(member, overwrite=None, reason="Rivalz: spectator removed by a fighter")
                    except discord.HTTPException as exc:
                        print(f"Could not remove spectator access: {exc!r}")
                    if getattr(chan, "type", None) == discord.ChannelType.voice and member.voice and member.voice.channel \
                            and member.voice.channel.id == chan.id:
                        try:
                            await member.move_to(None, reason="Rivalz: spectator removed")
                        except discord.HTTPException:
                            pass
            await refresh_ticket(t)
            await update_ongoing(t)
        text = (f"> <@{self.uid}> was removed as a spectator by {interaction.user.mention}. "
                "They can no longer see this ticket.") if entry else f"> <@{self.uid}> is not a spectator any more."
        try:
            await interaction.edit_original_response(view=build_card("Spectator Removed", [text], accent=GREY),
                                                     allowed_mentions=NO_PING)
        except discord.HTTPException:
            pass


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
        if not dev and user.id in (t.get("removed_spectators") or []):
            return await notice(interaction, "Removed", "> A fighter removed you from this match, so you can't spectate it again. "
                                "You can spectate any other live match.", accent=GREY)
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
            await chan.send(view=build_card("Spectator Joined", [f"> {who} joined as a spectator.",
                                                                 "> The requester or the rival can remove them with the button below."],
                                            accent=GREY, buttons=[RemoveSpectatorButton(t["channel_id"], user.id)]),
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
        if user.id in (t.get("removed_spectators") or []):
            return await notice(interaction, "Removed", "> A fighter removed you from this match, so you can't spectate it again. "
                                "You can spectate any other live match.", accent=GREY)
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
    if n == MSG_NOTICE_AT:
        try:
            msg = await message.channel.send(view=match_notice_view(t), allowed_mentions=NO_PING)
            t["notice_msg"] = msg.id
        except discord.HTTPException as exc:
            print(f"Could not post the match details card: {exc!r}")
        store.save()
        return
    if n == MSG_NOTICE_REMOVE_AT:
        old = t.get("notice_msg")
        t["notice_msg"] = None
        store.save()
        if old:
            try:
                await message.channel.get_partial_message(old).delete()
            except discord.HTTPException:
                pass
        return
    if n % 5 == 0:
        store.save()


def adopt_legacy_extras(guild) -> bool:
    """Your original server already has an Ongoing-Ping channel and role: use those ids. Other servers get theirs from /build."""
    g = gcfg(guild.id)
    changed = False
    if not g["ch"].get("ongoing") and guild.get_channel(LEGACY["ongoing"]):
        g["ch"]["ongoing"] = LEGACY["ongoing"]
        changed = True
    if not g["ch"].get("streaks") and guild.get_channel(LEGACY["streaks"]):   # your original server's streak channel
        g["ch"]["streaks"] = LEGACY["streaks"]
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
CMD_STREAK = re.compile(r"^(?:my\s+|win\s+)?streaks?\b(?:\s+.*)?$|^what(?:'s| is) my streak$")
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
    mode = "duel" if re.search(r"\bduels?\b", text) else None
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
         "`R leaderboard` / `R top 3` / `R who is top 1` - current standings (add `duel`, `weekly`, `daily`)",
         "`R rank` or `R rank @user` - Duel rank",
         "`R streak` or `R streak @user` - current win streak, best streak and the server record",
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
    elif CMD_STREAK.match(text):
        kind = "streak"
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
    if kind == "streak":
        target = target_of(message)
        return await message.reply(view=streak_card(target or uid),
                                   mention_author=False, allowed_mentions=NO_PING)
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
                  "> Then press **Delete Ticket**."], accent=GREY)


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
    await set_spectator_upload(getattr(channel, "guild", None), t, False)  # spectators stay read only
    try:
        await channel.send(view=build_card(
            "This match has been recorded",
            ["> You can delete this ticket now. Its voice channel is removed with it."],
            accent=GREY, buttons=[TicketButton("delete")]))
    except discord.HTTPException as exc:
        print(f"Could not send the delete card: {exc!r}")
    await refresh_ticket(t)


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
    return   # the gank step is gone: nothing to collect
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
    store.save()
    if record_match(t):
        schedule_lb_refresh(t["guild_id"])
    await interaction.response.edit_message(view=ticket_view(t))
    await update_result(t, force=True)
    await log_closed(t, interaction.user.id)
    await clear_ongoing(t)  # the match is no longer live
    await finalize_match(interaction.channel, t)   # no gank step: record it and offer Delete Ticket straight away


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
    ["> Pick **Duel** or **Squad Up**, choose a rival and rounds. Your rival accepts in their DMs."],
    ["`1` Select a mode, enter your Roblox username (saved for next time), pick **first to** how many rounds and paste your **server link**.",
     "`2` Your rival presses **Accept** and a private ticket with a voice channel opens.",
     "`3` Fight, then each player adds their own score. First to the chosen rounds wins (first to 5 lasts 9 rounds at most).",
     "`4` Press **End**, then press **Delete Ticket**."],
    ["> **Squad Up** is for team matches: pick the size (2v2, 3v3, 5v5 or custom) and first to how many rounds, then **Manual selection** (you choose both teams; you are Captain A and the first rival to accept becomes Captain B) or **Random matchmaking** (a lobby with a Join button; when it is full the bot splits everyone into equal teams)."],
    [f"> Live matches are announced in the **ongoing-ping** channel. Press **Join as Spectator** to watch (no limit, one match at a time)."],
    ["> **Settings** is optional. Fake challenges or scores lead to a blacklist."],
]

PANEL_INTRO = "> Challenge a rival to a **Duel** or team up with **Squad Up**. Private ticket, voice channel and tracked scores."
PANEL_MODES = (
    "**Duel** `Competitive` A match with a winner and a loser.\n"
    "**Squad Up** `Team` 2v2, 3v3, 5v5 or custom. Pick the teams yourself or let the bot split a lobby fairly.\n"
    "**Format** `First to` Pick first to 1, 3, 5 or your own number. The first player to reach it wins."
)
PANEL_HOW = (
    "### How it works\n"
    "> `1` Pick **Duel** or **Squad Up** below\n"
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
                discord.SelectOption(label="Squad Up", value="squad", description="Team match: 2v2, 3v3, 5v5 or custom."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        if not await guard(interaction):
            return
        mode = self.values[0]
        if mode == "squad":
            await interaction.response.defer(ephemeral=True)      # the card image takes a moment to draw
            state = await squad_new_state(interaction.user)
            view, file = await squad_setup_payload(state)
            await interaction.followup.send(view=view, file=file, ephemeral=True)
            state["origin"] = interaction
        else:
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


# ============================================================ Squad Up (2v2 / 3v3 / 5v5 / custom: lobby + team ticket)
# Panel -> Squad Up -> team size (2v2, 3v3, 5v5, Custom) -> Manual selection or Random matchmaking -> a lobby card with a
# Join button and a live list ("3 of 6 joined"). When the lobby is full the teams are announced and a squad ticket
# (text channel + voice channel) opens. Squad matches are tracked in their own tables and never touch the Duel board.
SQUAD_COLOUR = 0x9B59B6
SQUAD_MIN, SQUAD_MAX = 2, 10          # players per team (custom size)
SQUAD_TEAMS = ("Team A", "Team B")
SQUAD_PICK = {"manual": "Manual selection", "random": "Random matchmaking"}
# Squad lobby cards are posted here (in the server that owns this channel). Other servers use the channel Squad Up was started in.
SQUAD_LOBBY_CHANNEL_ID = env_int("SQUAD_LOBBY_CHANNEL_ID", 1571580816595623976)
_SQUAD_LOCK = None


def squad_lock():
    global _SQUAD_LOCK
    if _SQUAD_LOCK is None:
        _SQUAD_LOCK = asyncio.Lock()
    return _SQUAD_LOCK


def squad_lobbies() -> dict:
    return store.d.setdefault("squad_lobbies", {})


def squad_tickets() -> dict:
    return store.d.setdefault("squad_tickets", {})


def squad_format(size: int) -> str:
    return f"{size}v{size}"


def roblox_of(uid):
    """The saved Roblox profile of a Discord user (from Settings), or None."""
    prof = store.d["profiles"].get(str(uid)) or {}
    if prof.get("roblox_id") and prof.get("roblox_name"):
        return {k: prof.get(k) for k in ("roblox_id", "roblox_name", "roblox_display")}
    return None


def lobby_total(L) -> int:
    return L["size"] * 2


def lobby_ids(L) -> list:
    return [p["discord_id"] for p in L["players"]]


def assigned_ids(L) -> list:
    return [uid for team in (L.get("assigned") or []) for uid in team]


def squad_open_lobby_of(uid: int, exclude=None):
    for lid, L in squad_lobbies().items():
        if lid != exclude and L["status"] in ("open", "starting") and uid in lobby_ids(L):
            return L
    return None


def squad_save_profile(uid: int, rb: dict):
    prof = store.d["profiles"].setdefault(str(uid), {})
    prof.update({"roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"]})
    store.save()


# ---------- lobby card
def lobby_view(L, top_text=None):
    size, total, status = L["size"], lobby_total(L), L["status"]
    joined = {p["discord_id"]: p for p in L["players"]}
    manual = L["pick"] == "manual"
    view = discord.ui.LayoutView(timeout=None)
    if top_text:
        view.add_item(discord.ui.TextDisplay(top_text))
    colour = {"open": SQUAD_COLOUR, "starting": SQUAD_COLOUR, "started": GREEN}.get(status, GREY)
    c = discord.ui.Container(accent_colour=colour)
    c.add_item(discord.ui.TextDisplay(f"## Squad Up | {squad_format(size)}"))
    c.add_item(discord.ui.Separator())
    info = (f"> **Format:** `{squad_format(size)}` ({size} players per team)\n"
            f"> **Teams:** `{SQUAD_PICK[L['pick']]}`\n"
            f"> **Host:** <@{L['host']}>\n"
            f"> **Rounds:** `{rounds_text(L['rounds'])}`\n"
            f"> **Server:** `{server_label(L.get('server'))} set`")
    if manual and status in ("open", "starting"):
        cb = L.get("captain_b")
        info += f"\n> **Captain A:** <@{L['host']}>\n> **Captain B:** " + (f"<@{cb}>" if cb else "`first rival to accept`")
    if status == "open":
        info += f"\n> **Expires:** <t:{int(L['expires'])}:R>"
    c.add_item(discord.ui.TextDisplay(info))
    c.add_item(discord.ui.Separator())

    if status == "started":
        caps = L.get("captains") or [None, None]
        lines = ["### Teams Locked", f"> **Captain A:** <@{caps[0]}>  |  **Captain B:** <@{caps[1]}>"]
        for i, team in enumerate(L.get("final") or []):
            lines.append(f"**{SQUAD_TEAMS[i]}**")
            lines += [f"> <@{p['discord_id']}> (`@{esc(p['roblox_name'])}`)" + (" **(Captain)**" if p["discord_id"] == caps[i] else "")
                      for p in team]
        c.add_item(discord.ui.TextDisplay("\n".join(lines)))
    else:
        lines = [f"### Lobby\n> **{len(joined)} of {total} joined**"]
        if manual:
            for i, team in enumerate(L["assigned"]):
                lines.append(f"**{SQUAD_TEAMS[i]}**")
                for uid in team:
                    tag = " **(Captain)**" if (i == 0 and uid == L["host"]) or (i == 1 and uid == L.get("captain_b")) else ""
                    if uid in joined:
                        lines.append(f"> <@{uid}> (`@{esc(joined[uid]['roblox_name'])}`) - Ready{tag}")
                    else:
                        lines.append(f"> <@{uid}> - Waiting")
        else:
            for n, p in enumerate(L["players"], 1):
                lines.append(f"`{n}.` <@{p['discord_id']}> (`@{esc(p['roblox_name'])}`)")
            for n in range(len(L["players"]) + 1, total + 1):
                lines.append(f"`{n}.` Open slot")
            waiting = [u for u in (L.get("invited") or []) if u not in joined]
            if waiting:
                lines.append("**Invited:** " + " ".join(f"<@{u}>" for u in waiting[:25]))
        c.add_item(discord.ui.TextDisplay("\n".join(lines)))

    note = {
        "open": ("> Fixed teams: only the players listed above can join. Rivals get an invite in their DMs, and the first rival to press **Accept** becomes Captain B." if manual else
                 "> Press **Join** to take a slot. When the lobby is full the bot splits everyone into equal teams."),
        "starting": "> The lobby is full. Creating the teams and the ticket...",
        "started": "> The ticket is open. Good luck.",
        "cancelled": "> This lobby was cancelled by the host.",
        "expired": "> This lobby expired before it filled up.",
    }.get(status, "")
    if note:
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(note))

    buttons = []
    if status == "open":
        buttons = [SquadLobbyButton("join", L["id"]), SquadLobbyButton("leave", L["id"]), SquadLobbyButton("cancel", L["id"])]
    elif status == "started" and L.get("ticket_channel"):
        buttons = [discord.ui.Button(label="Open Ticket", url=f"https://discord.com/channels/{L['guild_id']}/{L['ticket_channel']}")]
    if buttons:
        add_buttons(c, buttons)
    view.add_item(c)
    return view


async def refresh_lobby(L, top_text=None):
    try:
        chan = bot.get_channel(L["channel_id"]) or await bot.fetch_channel(L["channel_id"])
        await chan.get_partial_message(L["message_id"]).edit(view=lobby_view(L, top_text=top_text))
    except discord.HTTPException as exc:
        print(f"Could not refresh the squad lobby card: {exc!r}")


# ---------- first to accept: DM invites to the rival team (manual mode)
def invite_view(L, host):
    return build_card(
        "Squad Invite",
        [[f"> **{md(host.display_name)}** invited you to a **{squad_format(L['size'])}** squad match on the other team.",
          "> The first rival to press **Accept** becomes **Captain B**."],
         [f"**Captain A:** {host.mention}", field("Format", squad_format(L["size"])), field("Rounds", rounds_text(L["rounds"])),
          field("Server", server_label(L.get("server")))],
         [f"> This invite closes <t:{int(L['expires'])}:R>."]],
        accent=SQUAD_COLOUR, buttons=[SquadInviteButton("acc", L["id"]), SquadInviteButton("dec", L["id"])])


def invite_done_view(L):
    cb = L.get("captain_b")
    return build_card(
        "Invite Accepted",
        [f"> You joined the **{squad_format(L['size'])}** lobby.",
         f"> **Captain A:** <@{L['host']}>\n> **Captain B:** " + (f"<@{cb}>" if cb else "`first rival to accept`")],
        accent=GREEN)


class SquadInviteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:sqi:(?P<act>acc|dec):(?P<lid>[0-9a-f]{8})"):
    def __init__(self, act: str, lid: str):
        accept = act == "acc"
        super().__init__(discord.ui.Button(label="Accept" if accept else "Decline",
                                           style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
                                           custom_id=f"rv:sqi:{act}:{lid}"))
        self.act, self.lid = act, lid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], match["lid"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadInviteButton({self.act}) error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        L = squad_lobbies().get(self.lid)
        if not L or L["status"] != "open":
            return await interaction.response.edit_message(
                view=build_card("Invite Closed", ["> This squad lobby is no longer open."], accent=GREY))
        user = interaction.user
        if L["pick"] != "manual" or user.id not in (L.get("assigned") or [[], []])[1]:
            return await interaction.response.send_message("This invite is not for you.", ephemeral=True)
        if not await guard(interaction, L["guild_id"]):
            return
        if self.act == "dec":
            await interaction.response.edit_message(view=build_card("Invite Declined", ["> You declined this squad invite."], accent=RED))
            return await sq_say(L, "Invite Declined", f"> **{md(user.display_name)}** declined the invite. They can still press **Join** in the lobby.", RED)
        err = sq_join_error(L, user)
        if err:
            return await notice(interaction, "Cannot Accept", err)
        prof = roblox_of(user.id)
        if not prof:
            return await interaction.response.send_modal(SquadNameModal(L["id"], invite=True))
        await interaction.response.defer()
        if await sq_add_player(interaction, L["id"], user, prof):
            try:
                await interaction.edit_original_response(view=invite_done_view(squad_lobbies()[L["id"]]))
            except discord.HTTPException:
                pass


# ---------- lobby buttons
class SquadLobbyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:sq:(?P<act>join|leave|cancel):(?P<lid>[0-9a-f]{8})"):
    LOOK = {"join": ("Join", discord.ButtonStyle.success),
            "leave": ("Leave", discord.ButtonStyle.secondary),
            "cancel": ("Cancel Lobby", discord.ButtonStyle.danger)}

    def __init__(self, act: str, lid: str):
        label, style = self.LOOK[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rv:sq:{act}:{lid}"))
        self.act, self.lid = act, lid

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"], match["lid"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadLobbyButton({self.act}) error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        L = squad_lobbies().get(self.lid)
        if not L or L["status"] != "open":
            return await notice(interaction, "Lobby Closed", "> This squad lobby is no longer open.", accent=GREY)
        if not await guard(interaction, L["guild_id"]):
            return
        await {"join": sq_join, "leave": sq_leave, "cancel": sq_cancel}[self.act](interaction, L)


def sq_join_error(L, user):
    if L is None or L["status"] != "open":
        return "> This squad lobby is no longer open."
    if user.bot:
        return "> Bots cannot join a squad."
    if user.id in lobby_ids(L):
        return "> You are already in this lobby."
    if L["pick"] == "manual" and user.id not in assigned_ids(L):
        return "> This lobby has fixed teams chosen by the host. Only the listed players can join."
    if len(L["players"]) >= lobby_total(L):
        return "> This lobby is full."
    if squad_open_lobby_of(user.id, exclude=L["id"]) and not is_owner(user):
        return "> You are already in another squad lobby. Leave it first."
    return None


async def sq_join(interaction: discord.Interaction, L):
    err = sq_join_error(L, interaction.user)
    if err:
        return await notice(interaction, "Cannot Join", err)
    prof = roblox_of(interaction.user.id)
    if not prof:
        return await interaction.response.send_modal(SquadNameModal(L["id"]))
    await interaction.response.defer()
    await sq_add_player(interaction, L["id"], interaction.user, prof)


async def sq_add_player(interaction: discord.Interaction, lid: str, user, prof: dict):
    """interaction must already be answered (deferred)."""
    async with squad_lock():
        L = squad_lobbies().get(lid)
        err = sq_join_error(L, user)
        if err:
            await notice(interaction, "Cannot Join", err)
            return False
        L["players"].append({"discord_id": user.id, **prof})
        if L["pick"] == "manual" and not L.get("captain_b") and user.id in (L.get("assigned") or [[], []])[1]:
            L["captain_b"] = user.id            # the first rival to accept is the captain of the other team
        full = len(L["players"]) >= lobby_total(L)
        if full:
            L["status"] = "starting"
        store.save()
    await refresh_lobby(L)
    if full:
        await sq_start(L, interaction.guild or bot.get_guild(L["guild_id"]))
    return True


class SquadNameModal(discord.ui.Modal):
    """Asked once when someone presses Join without a saved Roblox username."""
    def __init__(self, lid: str, invite: bool = False):
        super().__init__(title="Roblox Username", timeout=600)
        self.lid, self.invite = lid, invite
        self.name = discord.ui.TextInput(placeholder="Your Roblox username", min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", description="Saved to your Settings, so you only enter it once.",
                                       component=self.name))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        L = squad_lobbies().get(self.lid)
        err = sq_join_error(L, interaction.user)
        if err:
            return await notice(interaction, "Cannot Join", err)
        rb = await bot.roblox.user_by_name(self.name.value.strip())
        if not rb:
            return await notice(interaction, "Not Found", "> That Roblox username was not found. Press **Join** and try again.")
        squad_save_profile(interaction.user.id, rb)
        if await sq_add_player(interaction, self.lid, interaction.user, roblox_of(interaction.user.id)):
            if self.invite and interaction.message:      # accepted from the DM invite: close the invite card
                try:
                    await interaction.message.edit(view=invite_done_view(L))
                except discord.HTTPException:
                    pass
            await interaction.followup.send(view=build_card(
                "Joined", [f"> Username saved and you joined the **{squad_format(L['size'])}** lobby."], accent=GREEN), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadNameModal error: {error!r}")


async def sq_leave(interaction: discord.Interaction, L):
    if interaction.user.id == L["host"]:
        return await notice(interaction, "Host", "> You opened this lobby. Press **Cancel Lobby** to close it.", accent=GREY)
    await interaction.response.defer()
    async with squad_lock():
        L = squad_lobbies().get(L["id"])
        if not L or L["status"] != "open":
            return
        before = len(L["players"])
        L["players"] = [p for p in L["players"] if p["discord_id"] != interaction.user.id]
        if len(L["players"]) == before:
            return await notice(interaction, "Not In Lobby", "> You are not in this lobby.", accent=GREY)
        store.save()
    await refresh_lobby(L)


async def sq_cancel(interaction: discord.Interaction, L):
    if interaction.user.id != L["host"] and not is_staff(interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the host or staff can cancel this lobby.")
    await interaction.response.defer()
    async with squad_lock():
        L = squad_lobbies().get(L["id"])
        if not L or L["status"] != "open":
            return
        L["status"] = "cancelled"
        store.save()
    await refresh_lobby(L)


# ---------- teams + ticket
def sq_make_teams(L):
    size = L["size"]
    if L["pick"] == "manual":
        by_id = {p["discord_id"]: p for p in L["players"]}
        return [[by_id[u] for u in team if u in by_id] for team in L["assigned"]]
    pool = list(L["players"])
    random.SystemRandom().shuffle(pool)       # a fair, unpredictable split
    a, b = pool[:size], pool[size:size * 2]
    if any(p["discord_id"] == L["host"] for p in b):    # the host is always on Team A, as its captain
        a, b = b, a
    return [a, b]


def sq_captains(L, teams) -> list:
    """Captain A is always the host. Captain B is the first rival who accepted (manual) or joined (random)."""
    ids = [[p["discord_id"] for p in team] for team in teams]
    cap_a = L["host"] if L["host"] in ids[0] else ids[0][0]
    cap_b = L.get("captain_b") if L.get("captain_b") in ids[1] else None
    if cap_b is None:
        cap_b = next((u for u in lobby_ids(L) if u in ids[1]), ids[1][0])
    return [cap_a, cap_b]


async def sq_start(L, guild):
    """The lobby is full (status 'starting'): fix the teams, open the ticket, announce."""
    try:
        missing = []
        for p in L["players"]:     # everybody must still be in the server
            try:
                if not (guild.get_member(p["discord_id"]) or await guild.fetch_member(p["discord_id"])):
                    missing.append(p["discord_id"])
            except discord.HTTPException:
                missing.append(p["discord_id"])
        if missing:
            async with squad_lock():
                L["players"] = [p for p in L["players"] if p["discord_id"] not in missing]
                L["status"] = "open"
                store.save()
            await refresh_lobby(L)
            return await sq_say(L, "Lobby Reopened", "> Someone left the server, so their slot is open again.", RED)
        teams = sq_make_teams(L)
        captains = sq_captains(L, teams)
        channel, st = await create_squad_ticket(guild, L, teams, captains)
    except Exception as exc:
        print(f"Squad ticket creation failed: {exc!r}")
        async with squad_lock():
            if L["pick"] == "random" and L["players"]:
                L["players"].pop()           # the last joiner can press Join again
            elif L["players"] and L["players"][-1]["discord_id"] != L["host"]:
                L["players"].pop()
            L["status"] = "open"
            store.save()
        await refresh_lobby(L)
        return await sq_say(L, "Ticket Failed", "> The squad ticket could not be opened. The last player to join can press **Join** again.", RED)
    async with squad_lock():
        L["status"] = "started"
        L["final"] = [[dict(p) for p in team] for team in teams]
        L["captains"] = captains
        L["ticket_channel"] = channel.id
        store.save()
    try:   # the first embed (the lobby card) is removed once the teams are chosen
        chan = bot.get_channel(L["channel_id"]) or await bot.fetch_channel(L["channel_id"])
        await chan.get_partial_message(L["message_id"]).delete()
    except discord.HTTPException as exc:
        print(f"Could not delete the squad lobby card: {exc!r}")
    lines = []
    for i, team in enumerate(teams):
        lines.append(f"**{SQUAD_TEAMS[i]}:** " + ", ".join(f"<@{p['discord_id']}>" for p in team))
    caps = [f"**Captain A:** <@{captains[0]}>", f"**Captain B:** <@{captains[1]}>"]
    try:   # the second embed now starts with the VS picture showing every player's profile (captains on top)
        vs_file, vs_name = await squad_vs_file(guild, [sorted(t, key=lambda p: p['discord_id'] != captains[k]) for k, t in enumerate(teams)])
    except Exception as exc:
        print(f"Could not draw the VS picture: {exc!r}")
        vs_file, vs_name = None, None
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(" ".join(f"<@{u}>" for u in lobby_ids(L))))
    c = discord.ui.Container(accent_colour=GREEN)
    if vs_name:
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=f"attachment://{vs_name}")
        c.add_item(gallery)
    c.add_item(discord.ui.TextDisplay(f"## Teams Announced | {squad_format(L['size'])}"))
    for sec in ([caps, lines, [f"> Your ticket is ready: {channel.mention}"]]):
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("\n".join(sec)))
    add_buttons(c, [discord.ui.Button(label="Open Ticket", url=channel.jump_url)])
    view.add_item(c)
    try:
        chan = bot.get_channel(L["channel_id"]) or await bot.fetch_channel(L["channel_id"])
        kwargs = {"file": vs_file} if vs_file else {}
        await chan.send(view=view, **kwargs)
    except discord.HTTPException as exc:
        print(f"Could not post the squad announcement: {exc!r}")


async def sq_say(L, title, text, accent, top_text=None, buttons=None):
    try:
        chan = bot.get_channel(L["channel_id"]) or await bot.fetch_channel(L["channel_id"])
        sections = text if isinstance(text, list) else [text]
        await chan.send(view=build_card(title, sections, accent=accent, top_text=top_text, buttons=buttons))
    except discord.HTTPException as exc:
        print(f"Could not post the squad announcement: {exc!r}")


async def create_squad_ticket(guild: discord.Guild, L, teams, captains=None):
    store.d["counters"]["squad"] = store.d["counters"].get("squad", 0) + 1
    n = store.d["counters"]["squad"]
    store.save()
    name, vc_name = f"squad-{n:03d}", f"Squad {n:03d}"

    cfg_g = gcfg(guild.id)
    cat_id = cfg_g["cat"].get("tickets")
    category = guild.get_channel(cat_id) if cat_id else None
    if not isinstance(category, discord.CategoryChannel):
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
    for team in teams:
        for p in team:
            members.append(guild.get_member(p["discord_id"]) or await guild.fetch_member(p["discord_id"]))
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
        text_ow[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    channel = await guild.create_text_channel(
        name, category=category, overwrites=text_ow,
        topic=f"Squad {squad_format(L['size'])} #{n:03d} | lobby {L['id']}", reason="Rivalz squad ticket")

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
        vc = await guild.create_voice_channel(vc_name, category=category, overwrites=voice_ow, reason="Rivalz squad voice channel")
    except discord.HTTPException as exc:
        print(f"Could not create the squad voice channel (give the bot Connect and Speak): {exc!r}")

    st = {
        "lobby": L["id"], "guild_id": guild.id, "channel_id": channel.id, "vc_id": vc.id if vc else None,
        "number": n, "name": name, "size": L["size"], "pick": L["pick"], "rounds": L["rounds"], "server": L.get("server"),
        "host": L["host"], "captains": captains, "created": int(time.time()), "status": "open",
        "teams": [[dict(p) for p in team] for team in teams], "scores": [None, None],
        "message_id": None, "ended_at": None, "ended_by": None,
    }
    try:
        msg = await channel.send(view=squad_ticket_view(st, ping=True))
    except Exception:
        for ch_ in (channel, vc):     # do not leave empty channels behind when the card could not be sent
            if ch_:
                try:
                    await ch_.delete(reason="Rivalz squad ticket failed")
                except discord.HTTPException:
                    pass
        raise
    st["message_id"] = msg.id
    squad_tickets()[str(channel.id)] = st
    store.save()
    await squad_log(st, "Opened")
    return channel, st


async def squad_log(st, what: str):
    try:
        await send_cfg(st["guild_id"], "log", build_card(
            f"Squad #{st['number']:03d} {what}",
            [[field("Format", squad_format(st["size"])), field("Teams", SQUAD_PICK[st["pick"]]),
              field("Rounds", rounds_text(st["rounds"])),
              f"**Result:** `{esc(squad_result_text(st))}`" if what != "Opened" else f"**Opened:** <t:{st['created']}:F>"],
             [f"-# Ticket: <#{st['channel_id']}>"]],
            accent=SQUAD_COLOUR,
            buttons=[discord.ui.Button(label="More Info", url=f"https://discord.com/channels/{st['guild_id']}/{st['channel_id']}")]))
    except Exception as exc:
        print(f"Squad log failed: {exc!r}")


# ---------- squad ticket card
def squad_side_of(st, uid):
    for i, team in enumerate(st["teams"]):
        if any(p["discord_id"] == uid for p in team):
            return i
    return None


def squad_can_act(st, user) -> bool:
    return squad_side_of(st, user.id) is not None or is_staff(user)


def squad_scoreboard(st) -> str:
    s0, s1 = st["scores"]

    def f(s):
        return "-" if s is None else str(s)

    return f"**{SQUAD_TEAMS[0]}** `{f(s0)}` - `{f(s1)}` **{SQUAD_TEAMS[1]}**"


def squad_result_text(st) -> str:
    s0, s1 = st["scores"]
    if s0 is None or s1 is None:
        return "Not recorded"
    if s0 > s1:
        return f"{SQUAD_TEAMS[0]} wins"
    if s1 > s0:
        return f"{SQUAD_TEAMS[1]} wins"
    return "Draw"


def squad_ticket_view(st, ping=False):
    status = st["status"]
    view = discord.ui.LayoutView(timeout=None)
    if ping:
        view.add_item(discord.ui.TextDisplay(" ".join(f"<@{p['discord_id']}>" for team in st["teams"] for p in team)))
    c = discord.ui.Container(accent_colour=SQUAD_COLOUR if status == "open" else GREY)
    c.add_item(discord.ui.TextDisplay(f"## Squad Ticket #{st['number']:03d} | {squad_format(st['size'])}"))
    caps = st.get("captains")
    if caps:
        c.add_item(discord.ui.TextDisplay(f"> **Captain A:** <@{caps[0]}>\n> **Captain B:** <@{caps[1]}>"))
    for i, team in enumerate(st["teams"]):
        c.add_item(discord.ui.Separator())
        lines = [f"### {SQUAD_TEAMS[i]}"]
        for p in team:
            tag = " **(Captain)**" if caps and p["discord_id"] == caps[i] else ""
            lines.append(f"> <@{p['discord_id']}> - [{md(p['roblox_name'])}]({profile_url(p)}){tag}")
        c.add_item(discord.ui.TextDisplay("\n".join(lines)))
        if status == "open":
            row = discord.ui.ActionRow()
            row.add_item(SquadTicketButton(f"s{i}"))
            c.add_item(row)

    info = ("### Match Info\n"
            f"> **Mode:** `Squad {squad_format(st['size'])}`\n"
            f"> **Teams:** `{SQUAD_PICK[st['pick']]}`\n"
            f"> **Rounds:** `{rounds_text(st['rounds'])}`\n"
            f"> **Status:** `{STATUS_TEXT.get(status, status.title())}`\n")
    if status == "open":
        info += f"> **Live Timer:** <t:{int(st['created'])}:R>\n> **Opened:** <t:{int(st['created'])}:t>"
    else:
        info += f"> **Duration:** `{fmt_duration((st.get('ended_at') or time.time()) - st['created'])}`"
    if st.get("server"):
        info += f"\n> **Server:** `{server_label(st['server'])} set`"
    if st.get("vc_id"):
        info += f"\n> **Voice:** <#{st['vc_id']}>"
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(info))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"### Scoreboard\n> {squad_scoreboard(st)}"))
    if status != "open":
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"### Result\n> `{esc(squad_result_text(st))}`"))
    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"-# Squad opened by <@{st['host']}> | {SQUAD_PICK[st['pick']]} | {STATUS_TEXT.get(status, status.title())}"))

    if status == "open":
        buttons = [SquadTicketButton("join"), SquadTicketButton("prof"), SquadTicketButton("end")]
    else:
        buttons = [SquadTicketButton("prof"), SquadTicketButton("delete")]
    vcu = f"https://discord.com/channels/{st['guild_id']}/{st['vc_id']}" if st.get("vc_id") else None
    if vcu:
        buttons.append(discord.ui.Button(label="Join VC", url=vcu))
    add_buttons(c, buttons)
    view.add_item(c)
    return view


async def refresh_squad_ticket(st):
    try:
        channel = bot.get_channel(st["channel_id"]) or await bot.fetch_channel(st["channel_id"])
        await channel.get_partial_message(st["message_id"]).edit(view=squad_ticket_view(st))
    except discord.HTTPException as exc:
        print(f"Could not refresh the squad ticket card: {exc!r}")


class SquadTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:sqt:(?P<act>join|prof|s0|s1|end|delete)"):
    LOOK = {"join": ("Join", discord.ButtonStyle.success),
            "prof": ("Profiles", discord.ButtonStyle.secondary),
            "s0": ("Add Score: Team A", discord.ButtonStyle.primary),
            "s1": ("Add Score: Team B", discord.ButtonStyle.primary),
            "end": ("End", discord.ButtonStyle.danger),
            "delete": ("Delete Ticket", discord.ButtonStyle.danger)}

    def __init__(self, act: str):
        label, style = self.LOOK[act]
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"rv:sqt:{act}"))
        self.act = act

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["act"])

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadTicketButton({self.act}) error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        st = squad_tickets().get(str(interaction.channel_id))
        if not st:
            return await safe_reply(interaction, "This squad ticket is no longer tracked by Rivalz.")
        if not await guard(interaction, st["guild_id"]):
            return
        await SQUAD_HANDLERS[self.act](interaction, st)


async def sqh_join(interaction, st):
    if not squad_can_act(st, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the players in this match can use the join link.")
    if st["status"] != "open":
        return await notice(interaction, "Match Ended", "> This match has already ended.")
    srv = st.get("server")
    roseal, roblox = server_urls(srv)
    if not roblox:
        return await notice(interaction, "No Server Link", "> This ticket has no server link.", accent=GREY)
    host = next((p for team in st["teams"] for p in team if p["discord_id"] == st["host"]), None)
    buttons = []
    if roseal:
        buttons.append(discord.ui.Button(label="Open Roseal", url=roseal))
    buttons.append(discord.ui.Button(label="Open Roblox Directly", url=roblox))
    who = f" from **{md(host['roblox_name'])}**" if host else ""
    lines = [f"> Server link{who}."]
    if roseal:
        lines += ["> Tap **Open Roseal**. Roseal opens first and lets you pick how to launch Roblox.",
                  "> If Roseal does not open, use **Open Roblox Directly**."]
    else:
        lines += ["> Tap **Open Roblox Directly** to launch Roblox.", "> Roseal is not available for private server links."]
    await interaction.response.send_message(view=build_card(
        "Join Match", [lines, [field("Server Type", server_label(srv))]], accent=GREEN, timeout=600, buttons=buttons), ephemeral=True)


async def sqh_prof(interaction, st):
    buttons = [discord.ui.Button(label=f"{p['roblox_name']}"[:80], url=profile_url(p)) for team in st["teams"] for p in team]
    await interaction.response.send_message(view=build_card(
        "Open Profile", ["> Choose whose Roblox profile you want to open."], buttons=buttons, timeout=600), ephemeral=True)


class SquadScoreModal(discord.ui.Modal):
    def __init__(self, cid: int, side: int, st):
        super().__init__(title=f"Add Score: {SQUAD_TEAMS[side]}", timeout=600)
        self.cid, self.side = cid, side
        self.add_item(discord.ui.TextDisplay(
            "**Add your team's score honestly.**\n"
            "Enter only the rounds your team won. Each team adds its own score. Fake or inflated scores can get you blacklisted from Rivalz."))
        current = st["scores"][side]
        self.score = discord.ui.TextInput(placeholder=f"0 to {st['rounds']}", default=None if current is None else str(current),
                                          min_length=1, max_length=3)
        self.add_item(discord.ui.Label(
            text="Team Score",
            description=f"First to {st['rounds']} ({max_total_rounds(st['rounds'])} rounds at most). Rounds your team won."[:100],
            component=self.score))

    async def on_submit(self, interaction: discord.Interaction):
        st = squad_tickets().get(str(self.cid))
        if not st or st["status"] != "open":
            return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
        raw = self.score.value.strip()
        rounds = st["rounds"]
        if not raw.isdigit() or int(raw) > rounds:
            return await notice(interaction, "Invalid Score", f"> Enter a whole number from `0` to `{rounds}` (first to {rounds}).")
        value, other = int(raw), st["scores"][1 - self.side]
        most = max_total_rounds(rounds)
        if other is not None and value + other > most:
            st["scores"] = [None, None]
            store.save()
            await notice(interaction, "Scores Reset",
                         [f"> Both scores add up to `{value + other}` rounds, but a **first to {rounds}** match lasts `{most}` rounds at most.",
                          "> Both scores were reset. Add them again honestly."], accent=RED)
            return await refresh_squad_ticket(st)
        st["scores"][self.side] = value
        store.save()
        await interaction.response.send_message(view=build_card(
            "Score Saved", [[field(f"{SQUAD_TEAMS[self.side]} Score", value), f"> {squad_scoreboard(st)}"]], accent=GREEN), ephemeral=True)
        await refresh_squad_ticket(st)
        try:
            await interaction.channel.send(view=build_card(
                "Score Recorded", [f"> **{SQUAD_TEAMS[self.side]}** added their score."], accent=GREY))
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadScoreModal error: {error!r}")


async def sq_score(interaction, st, side):
    if st["status"] != "open":
        return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
    mine = squad_side_of(st, interaction.user.id)
    if is_owner(interaction.user):
        pass                                   # the developer can add either team's score
    elif mine is None:
        return await notice(interaction, "Not Allowed", "> Only the players in this match can add a score.")
    elif mine != side:
        return await notice(interaction, "Not Your Score", f"> This button is for **{SQUAD_TEAMS[side]}**. You can only add your own team's score.")
    await interaction.response.send_modal(SquadScoreModal(st["channel_id"], side, st))


async def sqh_s0(interaction, st):
    await sq_score(interaction, st, 0)


async def sqh_s1(interaction, st):
    await sq_score(interaction, st, 1)


async def sqh_end(interaction, st):
    if not squad_can_act(st, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the players in this match can end it.")
    if st["status"] != "open":
        return await notice(interaction, "Already Ended", "> This match has already ended.")
    n, s = st["rounds"], st["scores"]
    for i in (0, 1):     # first to N: a team that added N has won even if the other never added a score
        if s[i] is not None and s[i] >= n and s[1 - i] is None:
            s[1 - i] = 0
    if None in s and not is_staff(interaction.user):
        return await notice(interaction, "Scores Missing", "> Both teams must add their score before the match can end.")
    st["status"] = "ended"
    st["ended_at"] = int(time.time())
    st["ended_by"] = interaction.user.id
    store.save()
    await interaction.response.edit_message(view=squad_ticket_view(st))
    await squad_log(st, "Ended")


async def sqh_delete(interaction, st):
    if not squad_can_act(st, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the players in this match or staff can delete this ticket.")
    if st["status"] != "ended":
        return await notice(interaction, "Not Ready", "> The match is not finished yet.")
    await interaction.response.send_message(view=build_card(
        "Ticket Deleting", ["> This ticket and its voice channel will be removed in `5` seconds."], accent=GREY))
    guild = interaction.guild
    squad_tickets().pop(str(st["channel_id"]), None)
    store.save()
    await asyncio.sleep(5)
    if st.get("vc_id"):
        vc = guild.get_channel(st["vc_id"])
        if vc:
            try:
                await vc.delete(reason="Rivalz squad ticket deleted")
            except discord.HTTPException:
                pass
    try:
        await interaction.channel.delete(reason=f"Rivalz squad ticket deleted by {interaction.user}")
    except discord.HTTPException:
        pass


SQUAD_HANDLERS = {"join": sqh_join, "prof": sqh_prof, "s0": sqh_s0, "s1": sqh_s1, "end": sqh_end, "delete": sqh_delete}


async def squad_expiry():
    """Called every minute: lobbies that did not fill up in time are closed."""
    try:
        now = time.time()
        for L in list(squad_lobbies().values()):
            if L["status"] == "open" and L["expires"] <= now:
                L["status"] = "expired"
                store.save()
                await refresh_lobby(L)
        for lid, L in list(squad_lobbies().items()):     # keep the table small: closed lobbies are dropped after a day
            if L["status"] in ("cancelled", "expired", "started") and now - L["created"] > 86400:
                squad_lobbies().pop(lid, None)
        store.save()
    except Exception as exc:
        print(f"Squad expiry error: {exc!r}")


# ---------- setup screen: a game-mode style card picker (panel -> Squad Up)
# One private screen: captain profile + basic details, a card image (Manual / Random / your format), then the team size
# and "first to" dropdowns (like Duel) and the two mode buttons. The last form only asks for the username,
# the players (manual) or friends (random) and the server link.
def render_squad_card(size, rounds, cap_name) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter
    W, H = 1000, 400
    img = Image.new("RGBA", (W, H))
    px = ImageDraw.Draw(img)
    for y in range(H):                                   # night-blue gradient
        t = y / (H - 1)
        px.line([(0, y), (W, y)], fill=(int(8 + 8 * t), int(16 + 26 * t), int(42 + 52 * t), 255))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-120, 120, 360, 520), fill=(40, 120, 255, 120))
    g.ellipse((560, -160, 1060, 260), fill=(255, 60, 90, 70))
    g.ellipse((380, 220, 880, 560), fill=(30, 200, 220, 70))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))
    d = ImageDraw.Draw(img)

    # title with a lightning bolt, slanted like the game's menu headers
    d.polygon([(46, 70), (86, 28), (74, 58), (104, 58), (60, 104), (72, 74)], fill=(30, 150, 255, 255))
    layer = Image.new("RGBA", (460, 100), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((10, 8), "SQUAD UP", font=lb_font("bold", 62), fill=(236, 240, 247, 255))
    layer = layer.transform(layer.size, Image.AFFINE, (1, 0.28, -14, 0, 1, 0), resample=Image.BICUBIC)
    img.alpha_composite(layer, (116, 20))

    # captain chip (top right)
    chip = (668, 28, 960, 88)
    d.rounded_rectangle(chip, radius=6, fill=(226, 232, 238, 255))
    d.rectangle((chip[0], chip[1], chip[0] + 8, chip[3]), fill=(30, 150, 255, 255))
    d.text((chip[0] + 24, chip[1] + 11), "CAPTAIN A", font=lb_font("bold", 14), fill=(90, 110, 130, 255))
    f, nm = _fit_text(d, cap_name, "bold", 24, chip[2] - chip[0] - 44, min_size=14)
    d.text((chip[0] + 24, chip[1] + 29), nm, font=f, fill=(16, 24, 40, 255))

    def card(x, y, w, h, top_h, top_fill):
        d.rounded_rectangle((x, y, x + w, y + h), radius=8, fill=(236, 238, 242, 255))
        d.rounded_rectangle((x, y, x + w, y + top_h), radius=8, fill=top_fill)
        d.rectangle((x, y + top_h - 10, x + w, y + top_h), fill=top_fill)
        ring = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        cx, cy, r = x + w // 2, y + top_h // 2, min(w, top_h) // 2 - 12
        ImageDraw.Draw(ring).ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 70), width=3)
        img.alpha_composite(ring)
        return cx, cy

    def person(cx, cy, s, colour):
        d.ellipse((cx - s * 0.32, cy - s * 0.95, cx + s * 0.32, cy - s * 0.31), fill=colour)
        d.pieslice((cx - s * 0.62, cy - s * 0.25, cx + s * 0.62, cy + s * 0.95), 180, 360, fill=colour)

    def text_block(x, y, w, title, sub, sub_colour, desc):
        f1, t1 = _fit_text(d, title, "bold", 21, w - 28, min_size=14)
        d.text((x + w // 2, y), t1, font=f1, fill=(16, 22, 38, 255), anchor="mt")
        f2, t2 = _fit_text(d, sub, "bold", 16, w - 28, min_size=11)
        d.text((x + w // 2, y + 32), t2, font=f2, fill=sub_colour, anchor="mt")
        for i, line in enumerate(desc):
            d.text((x + w // 2, y + 62 + i * 20), line, font=lb_font("medium", 14), fill=(60, 70, 85, 255), anchor="mt")

    top, h, tp = 112, 268, 150
    # Manual selection
    x, w = 40, 330
    cx, cy = card(x, top, w, h, tp, (28, 140, 255, 255))
    for k, dx in enumerate((-112, -56)):
        person(cx + dx, cy + 26, 52, (14, 30, 70, 255))
    for k, dx in enumerate((56, 112)):
        person(cx + dx, cy + 26, 52, (10, 70, 90, 255))
    d.text((cx, cy - 6), "VS", font=lb_font("bold", 24), fill=(255, 255, 255, 255), anchor="mm")
    text_block(x, top + tp + 18, w, "MANUAL SELECTION", "YOU PICK BOTH TEAMS", (28, 140, 255, 255),
               ["Choose every player for your team", "and for the other team."])
    # Random matchmaking
    x = 390
    cx, cy = card(x, top, w, h, tp, (30, 190, 160, 255))
    d.rounded_rectangle((cx - 42, cy - 42, cx + 42, cy + 42), radius=14, fill=(12, 40, 60, 255))
    for ox, oy in ((-22, -22), (22, -22), (0, 0), (-22, 22), (22, 22)):
        d.ellipse((cx + ox - 7, cy + oy - 7, cx + ox + 7, cy + oy + 7), fill=(255, 255, 255, 255))
    for sgn in (-1, 1):                                  # shuffle arrows either side of the dice
        ax = cx + sgn * 78
        d.polygon([(ax + sgn * 18, cy), (ax, cy - 16), (ax, cy + 16)], fill=(255, 255, 255, 230))
    text_block(x, top + tp + 18, w, "RANDOM MATCHMAKING", "FAIR SPLIT", (24, 170, 140, 255),
               ["Open a lobby with a Join button.", "The bot splits everyone equally."])
    # your format
    x, w3 = 740, 220
    cx, cy = card(x, top, w3, h, tp, (255, 110, 40, 255))
    big = f"{size}V{size}" if size else "?V?"
    d.text((cx, cy - 6), big, font=lb_font("bold", 52), fill=(255, 255, 255, 255), anchor="mm")
    d.text((cx, cy + 34), f"{size * 2} PLAYERS" if size else "PICK A SIZE", font=lb_font("bold", 16), fill=(255, 235, 220, 255), anchor="mm")
    ty = top + tp + 18
    d.text((x + w3 // 2, ty), "YOUR FORMAT", font=lb_font("bold", 21), fill=(16, 22, 38, 255), anchor="mt")
    d.text((x + w3 // 2, ty + 32), f"FIRST TO {rounds}", font=lb_font("bold", 16), fill=(230, 90, 20, 255), anchor="mt")
    d.text((x + w3 // 2, ty + 62), "Change it with the", font=lb_font("medium", 14), fill=(60, 70, 85, 255), anchor="mt")
    d.text((x + w3 // 2, ty + 82), "dropdowns below.", font=lb_font("medium", 14), fill=(60, 70, 85, 255), anchor="mt")

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


async def squad_new_state(user) -> dict:
    prof = roblox_of(user.id)
    thumb = user.display_avatar.url
    if prof:
        try:
            urls = await bot.roblox.avatars([prof["roblox_id"]])
            thumb = urls.get(prof["roblox_id"]) or thumb
        except Exception:
            pass
    return {"owner_id": user.id, "origin": None, "size": None, "rounds": 3, "n": 0, "prof": prof, "thumb": thumb,
            "cap_name": (prof or {}).get("roblox_name") or "Username not set", "cap_display": user.display_name}


# ======================================================================================================
#  VS PICTURE SETTINGS  (the image on top of "Teams Announced").  Edit the numbers below, then run
#  preview_vs.py to see the result without starting the bot.  All pixel numbers are on the ORIGINAL
#  1779 x 884 background, the final picture is cropped and shrunk automatically.
# ======================================================================================================
VS_FILE_NAMES = ("vs_clean.jpg", "vs.jpg", "vs.png", "vs.jpeg")   # vs_clean.jpg = the background without the Discord logo circles
VS_WIDTH = 900            # final width in pixels. The height follows the crop below, so nothing is stretched
VS_NATIVE = (1779, 884)

# Default look of every profile picture
VS_STYLE = {
    "size": 216,          # width/height of the profile picture in pixels (bigger number = bigger picture)
    "ring": 8,            # thickness of the coloured ring
    "rotate": 0,          # degrees, positive = counter-clockwise. A circle only turns the picture inside it,
                          # use shape "square" or "rounded" to see the whole frame tilt
    "shape": "circle",    # "circle", "rounded" or "square"
}
VS_RINGS = ((30, 144, 255), (255, 42, 26))     # ring colour: team A (blue), team B (red)

# Where the pictures go when a team has 2 players: [team A, team B] x [first player, second player] (x, y)
VS_SLOTS = (((205, 280), (544, 558)), ((1539, 280), (1255, 558)))

# Change ONE picture. Key = (team, player): team 0 = left/blue, team 1 = right/red, player 0 = first (the
# captain), 1 = second ... Any of x, y, size, ring, rotate, shape can be set, the rest stay as above. Examples:
#   (0, 0): {"size": 260},                                  -> bigger captain A
#   (1, 1): {"rotate": 15, "shape": "rounded", "x": 1300},  -> tilted rounded frame, moved to x = 1300
#   (0, 1): {"y": 650, "rotate": -10},
VS_OVERRIDES = {
    (0, 0): {"shape": "rounded", "size": 252, "ring": 12, "rotate": 16},
    (0, 1): {"shape": "rounded", "size": 252, "ring": 12, "rotate": -15},
    (1, 0): {"shape": "rounded", "size": 252, "ring": 12, "rotate": -16},
    (1, 1): {"shape": "rounded", "size": 252, "ring": 12, "rotate": 24},
}

# Empty space is cut away so the profiles and the VS logo sit close together. VS_CROP_PAD = how much background
# (in original pixels) stays around the outermost profile / the VS logo. Smaller = tighter picture.
# Or set VS_CROP = (left, top, right, bottom) to choose the crop yourself, e.g. (0, 60, 1779, 800).
VS_CROP_PAD = 14
VS_CROP = None
VS_LOGO_BOX = (660, 250, 1120, 620)      # where the VS logo sits in the background (never cropped)

# Only used if you want a different look for one whole team (same keys as VS_STYLE), e.g. {"rotate": 8}
VS_TEAM_STYLE = ({}, {})
# ======================================================================================================

# Where the Discord-logo circles are baked into vs.jpg (do not change unless you change vs.jpg). Any of them that
# your pictures do not fully cover is wiped automatically.
VS_PLACEHOLDERS = ((438, 129), (438, 714), (1342, 131), (1342, 717))
VS_PLACEHOLDER_R = 118


def find_vs_image() -> str:
    for folder in (BASE_DIR, os.path.join(BASE_DIR, "assets")):
        for name in VS_FILE_NAMES:
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                return p
    return ""


def _vs_tile(data, size: int, ring: int, ring_rgb, tint, rotate: float, shape: str):
    """One finished profile picture: the picture, a coloured ring, a shape and a rotation."""
    from PIL import Image, ImageDraw
    d = max(10, int(size))
    ring = max(0, int(ring))
    av = None
    if data:
        try:
            av = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            av = None
    if av is None:
        av = Image.new("RGBA", (256, 256), tint + (255,))
    if shape == "circle" and rotate:     # a circle looks the same turned, so the picture inside is what turns
        big = av.resize((int(d * 1.5), int(d * 1.5)), Image.LANCZOS).rotate(rotate, resample=Image.BICUBIC)
        left = (big.width - d) // 2
        av = big.crop((left, left, left + d, left + d))
    else:
        av = av.resize((d, d), Image.LANCZOS)
    total = d + ring * 2
    corner = {"circle": total // 2, "rounded": total // 5, "square": 0}.get(shape, total // 2)
    inner_corner = max(0, corner - ring)
    tile = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle((0, 0, total - 1, total - 1), radius=corner, fill=ring_rgb + (255,))
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, d - 1, d - 1), radius=inner_corner, fill=255)
    tile.paste(av, (ring, ring), mask)
    if shape != "circle" and rotate:
        tile = tile.rotate(rotate, resample=Image.BICUBIC, expand=True)
    return tile


def _vs_positions(n: int, side: int, W: int, H: int):
    """Where the n pictures of one team go: 2 players use VS_SLOTS, any other size is laid out in a grid."""
    if n == 2:
        return list(VS_SLOTS[side])
    if n == 1:
        return [(VS_SLOTS[side][0][0], H // 2)]
    cols = 1 if n <= 3 else 2
    rows = math.ceil(n / cols)
    cx0 = VS_SLOTS[side][0][0]
    pitch = 200 if cols == 2 else 0
    out = []
    for k in range(n):
        row, col = divmod(k, cols)
        in_row = cols if (row < rows - 1 or n % cols == 0) else n % cols
        out.append((int(cx0 + (col - (in_row - 1) / 2) * pitch), int(H * (row + 0.5) / rows)))
    return out


def render_vs_card(teams_avatars) -> bytes:
    """teams_avatars = [[bytes|None, ...] team A, [...] team B]. Draws every player's picture on their side of
    the VS background using the VS_* settings above, crops it tight and shrinks it to VS_WIDTH."""
    from PIL import Image, ImageDraw, ImageFilter
    path = find_vs_image()
    if path:
        base = Image.open(path).convert("RGBA")
        if base.size != VS_NATIVE:
            base = base.resize(VS_NATIVE, Image.LANCZOS)
    else:                                                # no vs.jpg next to main.py: a plain blue | red split
        base = Image.new("RGBA", VS_NATIVE, (190, 30, 20, 255))
        ImageDraw.Draw(base).rectangle((0, 0, VS_NATIVE[0] // 2, VS_NATIVE[1]), fill=(25, 85, 190, 255))
    W, H = base.size
    tiles = []                                           # (tile, cx, cy) for every player
    for side in (0, 1):
        pics = list(teams_avatars[side])
        n = len(pics)
        shrink = 1.0 if n <= 2 else 0.85 if n <= 3 else max(0.4, min(0.72, 330 / max(1, math.ceil(n / 2)) / (VS_STYLE["size"] / 2)))
        tint = (40, 70, 130) if side == 0 else (120, 30, 30)
        for k, ((cx, cy), data) in enumerate(zip(_vs_positions(n, side, W, H), pics)):
            st = dict(VS_STYLE)
            st.update(VS_TEAM_STYLE[side])
            st.update(VS_OVERRIDES.get((side, k), {}))
            size = st["size"] * shrink
            cx, cy = st.get("x", cx), st.get("y", cy)
            tile = _vs_tile(data, size, st["ring"], VS_RINGS[side], tint, st["rotate"], st["shape"])
            tiles.append((tile, cx, cy, size / 2 + st["ring"]))
    if path and os.path.basename(path) != "vs_clean.jpg":   # an old vs.jpg: wipe the logo circles no picture covers
        for px, py in VS_PLACEHOLDERS:
            if any(math.hypot(cx - px, cy - py) <= 6 and rad >= VS_PLACEHOLDER_R - 4 for _t, cx, cy, rad in tiles):
                continue
            R = VS_PLACEHOLDER_R
            x0, y0, x1, y1 = max(0, px - R - 30), max(0, py - R - 30), min(W, px + R + 30), min(H, py + R + 30)
            mean = base.crop((x0, y0, x1, y1)).resize((1, 1), Image.BOX).getpixel((0, 0))
            m = Image.new("L", (x1 - x0, y1 - y0), 0)
            ImageDraw.Draw(m).ellipse((px - x0 - R - 6, py - y0 - R - 6, px - x0 + R + 6, py - y0 + R + 6), fill=255)
            base.paste(Image.new("RGBA", m.size, mean), (x0, y0), m.filter(ImageFilter.GaussianBlur(10)))
    for tile, cx, cy, _r in tiles:
        base.alpha_composite(tile, (int(cx - tile.width / 2), int(cy - tile.height / 2)))
    if VS_CROP:
        box = tuple(VS_CROP)
    else:                                                # tight box around every picture and the VS logo
        l, t, r, b = VS_LOGO_BOX
        for tile, cx, cy, _r in tiles:
            l, t = min(l, cx - tile.width / 2), min(t, cy - tile.height / 2)
            r, b = max(r, cx + tile.width / 2), max(b, cy + tile.height / 2)
        box = (l - VS_CROP_PAD, t - VS_CROP_PAD, r + VS_CROP_PAD, b + VS_CROP_PAD)
    box = (max(0, int(box[0])), max(0, int(box[1])), min(W, int(box[2])), min(H, int(box[3])))
    base = base.convert("RGB").crop(box)
    base = base.resize((VS_WIDTH, max(1, round(VS_WIDTH * base.height / base.width))), Image.LANCZOS)
    out = io.BytesIO()
    base.save(out, "JPEG", quality=92)
    return out.getvalue()


async def squad_vs_file(guild, teams):
    """Fetches every player's profile picture and returns (discord.File, filename). Captain is first in each team."""
    async def one(uid):
        mem = guild.get_member(uid) if guild else None
        if mem is None and guild:
            try:
                mem = await guild.fetch_member(uid)
            except discord.HTTPException:
                mem = None
        url = mem.display_avatar.replace(size=256, format="png").url if mem else None
        return await download_bytes(url) if url else None

    sides = []
    for team in teams[:2]:
        sides.append(list(await asyncio.gather(*[one(p["discord_id"]) for p in team])))
    jpg = await asyncio.to_thread(render_vs_card, sides)
    return discord.File(io.BytesIO(jpg), filename="vs_card.jpg"), "vs_card.jpg"


async def squad_setup_payload(state: dict):
    png = await asyncio.to_thread(render_squad_card, state["size"], state["rounds"], state["cap_name"])
    state["n"] += 1
    name = f"squad_{state['n']}.png"
    return SquadSetupView(state, name), discord.File(io.BytesIO(png), filename=name)


async def squad_edit(interaction: discord.Interaction, state: dict):
    view, file = await squad_setup_payload(state)
    await interaction.response.edit_message(view=view, attachments=[file])


class SquadSizeSelect(discord.ui.Select):
    def __init__(self, state):
        size = state["size"]
        options = [discord.SelectOption(label=f"{n}v{n}", value=str(n), description=f"{n} players per team.", default=size == n)
                   for n in (2, 3, 5)]
        custom = size is not None and size not in (2, 3, 5)
        options.append(discord.SelectOption(label=f"Custom ({size}v{size})" if custom else "Custom", value="custom",
                                            description=f"Type your own team size ({SQUAD_MIN} to {SQUAD_MAX}).", default=custom))
        super().__init__(placeholder="Select the team size...", options=options)
        self.state = state

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            return await interaction.response.send_modal(SquadNumberModal(self.state, "size"))
        self.state["size"] = int(self.values[0])
        await squad_edit(interaction, self.state)


class SquadRoundsSelect(discord.ui.Select):
    def __init__(self, state):
        rounds = state["rounds"]
        options = [
            discord.SelectOption(label="First to 1", value="1", description="Single deciding round.", default=rounds == 1),
            discord.SelectOption(label="First to 3", value="3", description="First team to win 3 rounds (5 rounds at most).", default=rounds == 3),
            discord.SelectOption(label="First to 5", value="5", description="First team to win 5 rounds (9 rounds at most).", default=rounds == 5),
        ]
        custom = rounds not in (1, 3, 5)
        options.append(discord.SelectOption(label=f"Custom (first to {rounds})" if custom else "Custom", value="custom",
                                            description="Type your own number next.", default=custom))
        super().__init__(placeholder="Select first to how many rounds...", options=options)
        self.state = state

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "custom":
            return await interaction.response.send_modal(SquadNumberModal(self.state, "rounds"))
        self.state["rounds"] = int(self.values[0])
        await squad_edit(interaction, self.state)


class SquadNumberModal(discord.ui.Modal):
    """Custom team size or custom first-to number."""
    def __init__(self, state, kind: str):
        super().__init__(title="Custom Team Size" if kind == "size" else "Custom Rounds", timeout=600)
        self.state, self.kind = state, kind
        lo, hi = (SQUAD_MIN, SQUAD_MAX) if kind == "size" else (1, MAX_ROUNDS)
        self.lo, self.hi = lo, hi
        self.value = discord.ui.TextInput(placeholder=f"{lo} to {hi}", min_length=1, max_length=3)
        self.add_item(discord.ui.Label(
            text="Players Per Team" if kind == "size" else "First To",
            description="5 means a 5v5 match (10 players)." if kind == "size" else "Rounds a team must win first.",
            component=self.value))

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.value.value.strip()
        if not raw.isdigit() or not (self.lo <= int(raw) <= self.hi):
            return await notice(interaction, "Invalid Number", f"> Enter a whole number from `{self.lo}` to `{self.hi}`.")
        await interaction.response.defer(ephemeral=True)
        self.state[self.kind] = int(raw)
        view, file = await squad_setup_payload(self.state)
        try:
            await self.state["origin"].edit_original_response(view=view, attachments=[file])
            await interaction.delete_original_response()
        except (discord.HTTPException, AttributeError):
            await interaction.followup.send(view=view, file=file, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadNumberModal error: {error!r}")


class SquadSetupView(discord.ui.LayoutView):
    def __init__(self, state: dict, filename: str):
        super().__init__(timeout=600)
        self.state = state
        size, rounds = state["size"], state["rounds"]
        c = discord.ui.Container(accent_colour=SQUAD_COLOUR)
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=f"attachment://{filename}")
        c.add_item(gallery)
        prof = state.get("prof")
        head = (f"### [{md(prof['roblox_display'])}]({profile_url(prof)}) (`@{esc(prof['roblox_name'])}`)" if prof
                else f"### {md(state['cap_display'])}")
        details = (f"{head}\n"
                   f"> **Captain A:** <@{state['owner_id']}>\n"
                   f"> **Team size:** {f'`{squad_format(size)}` ({size * 2} players)' if size else '`not chosen yet`'}\n"
                   f"> **First to:** `{rounds}` (`{max_total_rounds(rounds)}` rounds at most)")
        c.add_item(discord.ui.Section(discord.ui.TextDisplay(details), accessory=discord.ui.Thumbnail(state["thumb"])))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("> Set the **team size** and **first to**, then pick **Manual selection** or **Random matchmaking**."))
        row = discord.ui.ActionRow()
        row.add_item(SquadSizeSelect(state))
        c.add_item(row)
        row_r = discord.ui.ActionRow()
        row_r.add_item(SquadRoundsSelect(state))
        c.add_item(row_r)
        row2 = discord.ui.ActionRow()
        for key, label, style in (("manual", "Manual selection", discord.ButtonStyle.primary),
                                  ("random", "Random matchmaking", discord.ButtonStyle.success)):
            b = discord.ui.Button(label=label, style=style)
            b.callback = self._picker(key)
            row2.add_item(b)
        c.add_item(row2)
        self.add_item(c)

    def _picker(self, pick: str):
        async def callback(interaction: discord.Interaction):
            st = self.state
            if not st["size"]:
                return await notice(interaction, "Team Size Needed", "> Select the team size first (2v2, 3v3, 5v5 or Custom).", accent=GREY)
            prof = store.d["profiles"].get(str(interaction.user.id), {})
            await interaction.response.send_modal(SquadModal(pick, st["size"], st["rounds"], prof))
        return callback


class SquadModal(discord.ui.Modal):
    def __init__(self, pick: str, size: int, rounds: int, prof: dict = None):
        super().__init__(title=f"Squad {squad_format(size)} | First to {rounds} | {'Manual' if pick == 'manual' else 'Random'}"[:45], timeout=600)
        self.pick, self.size, self.rounds = pick, size, rounds

        self.username = discord.ui.TextInput(placeholder="Your Roblox username", default=(prof or {}).get("roblox_name") or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", description="Saved so you only type it once.", component=self.username))

        if pick == "manual":
            self.mates = discord.ui.UserSelect(placeholder=f"Select {size - 1} teammate(s)", min_values=size - 1, max_values=size - 1)
            self.add_item(discord.ui.Label(text="Your Teammates", description=f"Team A: you plus {size - 1} more player(s).", component=self.mates))
            self.enemies = discord.ui.UserSelect(placeholder=f"Select {size} opponent(s)", min_values=size, max_values=size)
            self.add_item(discord.ui.Label(text="Enemy Team", description=f"Team B: exactly {size}. The first to accept is Captain B.", component=self.enemies))
        else:
            self.friends = discord.ui.UserSelect(placeholder="Pre-add friends (optional)", min_values=0,
                                                 max_values=min(25, size * 2 - 1), required=False)
            self.add_item(discord.ui.Label(text="Pre-add Friends (optional)",
                                           description="They are pinged to join. Everyone else can join the lobby too.", component=self.friends))

        self.server = discord.ui.TextInput(placeholder="Roseal / Roblox server link", min_length=10, max_length=400)
        self.add_item(discord.ui.Label(text="Server Link", description="Your server link. Players join it from the ticket.", component=self.server))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not is_authorized(interaction.guild_id) and not is_owner(interaction.user):
            return await notice(interaction, "Not Authorized", "> Rivalz is not authorized in this server yet.", accent=GREY)
        server = parse_server_link(self.server.value)
        if not server:
            return await notice(interaction, "Invalid Server Link", [SERVER_HELP, "> Copy the link again and press **Squad Up** again."])

        host = interaction.user
        guild = interaction.guild
        name = self.username.value.strip()
        saved = store.d["profiles"].get(str(host.id), {})
        if not (saved.get("roblox_id") and str(saved.get("roblox_name", "")).lower() == name.lower()):
            rb = await bot.roblox.user_by_name(name)
            if not rb:
                return await notice(interaction, "Not Found", "> That Roblox username was not found.")
            squad_save_profile(host.id, rb)

        async def real_member(u):
            if isinstance(u, discord.Member):
                return u
            try:
                return await guild.fetch_member(u.id)
            except discord.HTTPException:
                return None

        assigned, invited = None, []
        if self.pick == "manual":
            mates = list({u.id: u for u in self.mates.values}.values())
            enemies = list({u.id: u for u in self.enemies.values}.values())
            ids = [host.id] + [u.id for u in mates] + [u.id for u in enemies]
            if len(mates) != self.size - 1 or len(enemies) != self.size or len(set(ids)) != self.size * 2:
                return await notice(interaction, "Teams Not Valid",
                                    [f"> A **{squad_format(self.size)}** needs {self.size - 1} teammate(s) and {self.size} opponent(s).",
                                     "> Nobody can be picked twice, and you are already on your own team."])
            for u in mates + enemies:
                if u.bot:
                    return await notice(interaction, "Teams Not Valid", "> You cannot pick a bot.")
                if not await real_member(u):
                    return await notice(interaction, "Teams Not Valid", f"> {u.mention} is not in this server.")
            assigned = [[host.id] + [u.id for u in mates], [u.id for u in enemies]]
        else:
            for u in self.friends.values:
                if u.id != host.id and not u.bot and u.id not in invited:
                    invited.append(u.id)

        cfg = {"size": self.size, "pick": self.pick, "server": server, "assigned": assigned, "invited": invited}
        await create_lobby(interaction, cfg, self.rounds)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"SquadModal error: {error!r}")


async def create_lobby(interaction: discord.Interaction, cfg: dict, rounds: int):
    """interaction must already be deferred (ephemeral). Posts the lobby card with the host already joined."""
    host = interaction.user
    prof = roblox_of(host.id)
    if not prof:
        return await notice(interaction, "Username Needed", "> Enter your Roblox username and try again.")
    if not is_owner(host):
        for L in squad_lobbies().values():
            if L["status"] in ("open", "starting") and (L["host"] == host.id or host.id in lobby_ids(L)):
                return await notice(interaction, "Request Failed", "> You already have an open squad lobby. Finish or cancel it first.")
    chan = interaction.channel      # the lobby channel when it belongs to this server, otherwise where Squad Up was started
    if SQUAD_LOBBY_CHANNEL_ID:
        try:
            cand = bot.get_channel(SQUAD_LOBBY_CHANNEL_ID) or await bot.fetch_channel(SQUAD_LOBBY_CHANNEL_ID)
            if getattr(getattr(cand, "guild", None), "id", None) == interaction.guild_id:
                chan = cand
        except discord.HTTPException:
            pass
    lid = uuid.uuid4().hex[:8]
    L = {
        "id": lid, "guild_id": interaction.guild_id, "channel_id": chan.id, "message_id": None,
        "host": host.id, "size": cfg["size"], "pick": cfg["pick"], "rounds": rounds, "server": cfg["server"],
        "assigned": cfg.get("assigned"), "invited": cfg.get("invited") or [], "captain_b": None,
        "players": [{"discord_id": host.id, **prof}], "status": "open",
        "created": time.time(), "expires": time.time() + EXPIRY_MINUTES * 60,
    }
    manual = L["pick"] == "manual"
    pings = [] if manual else (cfg.get("invited") or [])          # manual rivals are invited in their DMs
    if manual:
        pings = [u for u in L["assigned"][0] if u != host.id]     # teammates are pinged in the lobby channel
    top = ("Squad Up: " + " ".join(f"<@{u}>" for u in pings) + " press **Join** to play.") if pings else None
    try:
        msg = await chan.send(view=lobby_view(L, top_text=top))
    except discord.HTTPException as exc:
        print(f"Could not post the squad lobby: {exc!r}")
        return await notice(interaction, "Request Failed", "> I could not post the lobby there. Check my Send Messages permission in the lobby channel.")
    L["message_id"] = msg.id
    squad_lobbies()[lid] = L
    store.save()

    undelivered = []
    if manual:       # first to accept: every rival gets a DM invite, the first to press Accept is Captain B
        for uid in L["assigned"][1]:
            sent = None
            try:
                sent = await try_dm(await bot.fetch_user(uid), invite_view(L, host))
            except discord.HTTPException:
                pass
            if not sent:
                undelivered.append(uid)

    lines = [f"> Your **{squad_format(L['size'])}** lobby is live in {chan.mention}.",
             "> Players press **Join**. When it is full the teams are announced and the ticket opens."]
    if manual:
        lines.append("> You are **Captain A**. The first rival to press **Accept** in their DMs becomes **Captain B**.")
    sections = [lines, [field("Teams", SQUAD_PICK[L["pick"]]), field("Rounds", rounds_text(rounds)),
                        f"> Expires <t:{int(L['expires'])}:R>."]]
    if undelivered:
        sections.append(["> I could not DM " + ", ".join(f"<@{u}>" for u in undelivered) +
                         " (DMs closed). They can press **Join** in the lobby instead, and the first rival in becomes Captain B."])
    await interaction.followup.send(view=build_card(
        "Lobby Opened", sections, accent=SQUAD_COLOUR, buttons=[discord.ui.Button(label="Open Lobby", url=msg.jump_url)]), ephemeral=True)


# ============================================================ bot
class GuardedTree(app_commands.CommandTree):
    """Every slash command is owner only, except /ping (it works for everyone, in any server)."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_owner(interaction.user):
            return True
        cmd = interaction.command
        if cmd is not None and cmd.qualified_name in ("ping", "member stats", "streak"):     # /ping, /member stats and /streak work for everyone
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
        for key in ("panel", "log", "results", "duel_lb", "streaks"):
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

    async def start_health_server(self):
        """Render (and cron-job.org) need a web page to talk to. Only starts when the host gives a PORT."""
        port = os.getenv("PORT", "").strip()
        if not port.isdigit():
            return
        from aiohttp import web

        async def alive(request):
            return web.Response(text="Rivalz is running")

        app = web.Application()
        app.router.add_get("/", alive)
        app.router.add_get("/health", alive)
        self._web = web.AppRunner(app)
        await self._web.setup()
        await web.TCPSite(self._web, "0.0.0.0", int(port)).start()
        print(f"Health page listening on port {port} (/health)")

    async def setup_hook(self):
        await self.start_health_server()   # first, so the host sees the open port right away
        self.session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.session)
        self.add_dynamic_items(ChallengeButton, TicketButton, ReopenButton, OpenGankButton, LbButton, LbPage, LbSelect, LbJump,
                               SpectateButton, LogSpectatorsButton, RemoveSpectatorButton,
                               SquadLobbyButton, SquadTicketButton, SquadInviteButton)
        await self.tree.sync()
        expiry_loop.start()

    async def close(self):
        if self.session:
            await self.session.close()
        if getattr(self, "_web", None):
            await self._web.cleanup()
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
        for t in list(store.d["tickets"].values()):  # an old ticket that was still waiting for a gank: finish it
            if t.get("status") == "ended":
                ch_ = self.get_channel(t["channel_id"])
                if ch_:
                    asyncio.create_task(finalize_match(ch_, t))
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
        try:  # .member stats / /member stats / ?member stats typed as a normal message
            if message.guild and not message.author.bot and message.content and MEMBER_STATS_RE.match(message.content):
                asyncio.create_task(member_stats_text(message))
        except Exception as exc:
            print(f"Member stats trigger error: {exc!r}")
        try:  # "R ping", "Rivalz open general" ... only known commands are answered
            if (message.guild and not message.author.bot and message.content
                    and (RIVALZ_WORD.search(message.content) or R_LEAD.match(message.content))):
                asyncio.create_task(rivalz_task(message))
        except Exception as exc:
            print(f"Rivalz trigger error: {exc!r}")
        await super().on_message(message)

    async def on_guild_channel_delete(self, channel):
        if squad_tickets().pop(str(getattr(channel, "id", 0)), None) is not None:   # a squad ticket removed by hand
            store.save()
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
    await squad_expiry()   # squad lobbies that never filled up


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
        hl = e.get("hl")   # the row of the member who pressed My Rank
        d.rounded_rectangle((50, y, LB_W - 50, y + row_h), radius=24, fill=(26, 66, 78) if hl else C_ROW,
                            outline=C_GOLD if hl else C_ROW_LINE, width=4 if hl else 2)
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
    try:
        before = streak_table(row["m"]) if row["m"] in STREAK_MODES else None   # streaks WITHOUT this match
    except Exception as exc:
        print(f"Streak table error: {exc!r}")
        before = None
    lbstore.d["matches"].append(row)
    lbstore.save()
    if before is not None:
        queue_streak_events(row, before)
    return True


# ============================================================ win streaks
# A streak = decided matches won in a row without a loss. Duels have their own streak (STREAK_MODES=duel).
# Nothing extra is stored: every streak is replayed from the saved matches, so the leaderboard data is never touched
# and streaks that already exist in your history count straight away.
#   * announcements at STREAK_MIN (10), then every STREAK_STEP (5): 10, 15, 20, 25 ...
#   * /set count changes count too: extra wins are added to the streak, extra losses reset it
#   * beating a member who is on a streak (STREAK_MIN+) ends it: they go back to 0 and the winner gets
#     STREAK_BREAK_BONUS (2) streak for that win, NOT the loser's whole streak.
STREAK_MIN = max(2, env_int("STREAK_MIN", 10))
STREAK_STEP = max(1, env_int("STREAK_STEP", 5))
STREAK_BREAK_BONUS = max(1, env_int("STREAK_BREAK_BONUS", 2))
STREAK_MODES = tuple(m for m in re.findall(r"[a-z]+", os.getenv("STREAK_MODES", "duel").lower()) if m in LB_MODES) or ("duel",)
STREAK_STYLES = {
    "line": ("STREAK ON THE LINE", 0xE74C3C),
    "one": ("ONE WIN AWAY", 0xF1C40F),
    "best": ("NEW PERSONAL BEST", 0x1ABC9C),
    "record": ("SERVER RECORD", 0x9B59B6),
    "milestone": ("STREAK MILESTONE", 0xE67E22),
    "ended": ("STREAK ENDED", 0x95A5A6),
}


def streak_table(mode, matches=None):
    """Replay every decided match of one mode, oldest first.
    -> {user_id: {cur, best, prev_best, start, victim}}  (best = longest ever, prev_best = best before the current run)"""
    timeline = [(x["ts"], x["n"], x) for x in (lbstore.d["matches"] if matches is None else matches) if x["m"] == mode]
    if matches is None:   # wins / losses added by hand with /set count
        timeline += [(m["ts"], -1, m) for m in (store.d.get("streak_manual") or []) if m.get("m") == mode]
    timeline.sort(key=lambda e: (e[0], e[1]))
    users = {}

    def get(uid):
        return users.setdefault(uid, {"cur": 0, "best": 0, "prev_best": 0, "start": 0, "victim": None})

    for _ts, _n, x in timeline:
        if "uid" in x:   # a manual /set count change
            u = get(x["uid"])
            if x["dl"] > 0:
                u["cur"] = 0
            if x["dw"] > 0:
                if u["cur"] == 0:
                    u["start"], u["prev_best"] = x["ts"], u["best"]
                u["cur"] += x["dw"]
                u["best"] = max(u["best"], u["cur"])
            elif x["dw"] < 0 and x["dl"] <= 0:
                u["cur"] = max(0, u["cur"] + x["dw"])
            continue
        w, l = get(x["w"]), get(x["l"])
        gain = STREAK_BREAK_BONUS if l["cur"] >= STREAK_MIN else 1
        l["cur"] = 0                                    # a loss always sends the streak back to 0
        if w["cur"] == 0:
            w["start"], w["prev_best"] = x["ts"], w["best"]
        w["cur"] += gain
        w["best"] = max(w["best"], w["cur"])
        w["victim"] = x["l"]
    return users


def next_milestone(n: int) -> int:
    if n < STREAK_MIN:
        return STREAK_MIN
    return STREAK_MIN + ((n - STREAK_MIN) // STREAK_STEP + 1) * STREAK_STEP


def streak_events(row, before, after):
    """What happened to the streaks because of this one match. before/after = streak_table() without/with it."""
    w, l = row["w"], row["l"]
    out = []
    bl = before.get(l) if l is not None else None
    if bl and bl["cur"] >= STREAK_MIN:
        out.append({"kind": "ended", "n": bl["cur"], "winner": w, "loser": l, "lasted": row["ts"] - bl["start"]})
    b = (before.get(w) or {}).get("cur", 0)
    aw = after.get(w)
    if aw:
        a = aw["cur"]
        crossed = [k for k in range(STREAK_MIN, a + 1, STREAK_STEP) if k > b]
        if crossed:
            n = max(crossed)
            rec_n, rec_u = max(((u["best"], uid) for uid, u in before.items()), default=(0, None))
            if n > rec_n:
                out.append({"kind": "record", "n": n, "user": w, "prev": rec_n, "holder": rec_u})
            elif n > aw["prev_best"]:
                out.append({"kind": "best", "n": n, "user": w, "prev": aw["prev_best"]})
            else:
                out.append({"kind": "milestone", "n": n, "user": w, "victim": l})
        else:
            nxt = next_milestone(a)
            if nxt - a == 1 and a > b:
                out.append({"kind": "one", "n": a, "next": nxt, "user": w, "victim": l})
    return out


def queue_streak_events(row, before):
    try:
        events = streak_events(row, before, streak_table(row["m"]))
        if events:
            asyncio.get_running_loop().create_task(post_streak_events(row["g"], row["m"], events))
    except Exception as exc:
        print(f"Streak events error: {exc!r}")


def fmt_span(seconds) -> str:
    seconds = max(0, int(seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    if d:
        return f"{d} day{'s' if d != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    m = max(1, r // 60)
    return f"{m} minute{'s' if m != 1 else ''}"


def render_badge(number, colour: int) -> bytes:
    """The hexagon with the streak number (like the mock-up), drawn big and shrunk for smooth edges."""
    from PIL import Image, ImageDraw
    rgb = ((colour >> 16) & 255, (colour >> 8) & 255, colour & 255)
    S, OUT = 640, 160
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx = cy = S / 2
    r = S * 0.46
    pts = [(cx + r * math.cos(math.radians(60 * i - 90)), cy + r * math.sin(math.radians(60 * i - 90))) for i in range(6)]
    d.polygon(pts, fill=tuple(int(c * 0.2) + 16 for c in rgb) + (255,))
    d.line(pts + [pts[0]], fill=rgb + (255,), width=26, joint="curve")
    txt = str(number)
    size = 300 if len(txt) <= 2 else 230
    d.text((cx, cy + 6), txt, font=lb_font("bold", size), fill=rgb + (255,), anchor="mm")
    out = io.BytesIO()
    im.resize((OUT, OUT), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()


async def send_streak_card(gid, kind, number, main, small, mode=None):
    title, colour = STREAK_STYLES[kind]
    if mode and len(STREAK_MODES) > 1:
        small += f" · {MODES[mode]['name']}"
    png = await asyncio.to_thread(render_badge, number, colour)
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=colour)
    flame = "" if kind == "ended" else "🔥 "
    c.add_item(discord.ui.Section(discord.ui.TextDisplay(f"### {flame}{title}\n{main}\n-# {small}"),
                                  accessory=discord.ui.Thumbnail("attachment://streak.png")))
    view.add_item(c)
    return await send_cfg(gid, "streaks", view, file=discord.File(io.BytesIO(png), filename="streak.png"))


def wins_text(n) -> str:
    return f"{n} win{'s' if n != 1 else ''}"


async def post_streak_events(gid, mode, events):
    for ev in events:
        try:
            k, n = ev["kind"], ev["n"]
            if k == "ended":
                await send_streak_card(
                    gid, k, n, f"<@{ev['winner']}> ended <@{ev['loser']}>'s {n}-win streak.",
                    f"Final streak: {wins_text(n)} · Lasted {fmt_span(ev['lasted'])} · Ended by <@{ev['winner']}> "
                    f"· +{STREAK_BREAK_BONUS} streak", mode)
            elif k == "record":
                prev = f"{wins_text(ev['prev'])}" + (f", held by <@{ev['holder']}>" if ev["holder"] and ev["holder"] != ev["user"] else "") \
                    if ev["prev"] else "none yet"
                await send_streak_card(gid, k, n, f"<@{ev['user']}> now holds the longest streak in Rivalz.",
                                       f"{wins_text(n)} · Previous record: {prev}", mode)
            elif k == "best":
                prev = wins_text(ev["prev"]) if ev["prev"] else "none"
                await send_streak_card(gid, k, n, f"<@{ev['user']}> just set a new personal best: {n} wins in a row.",
                                       f"Previous best: {prev} · The streak is still going", mode)
            elif k == "milestone":
                await send_streak_card(gid, k, n, f"<@{ev['user']}> is on a {n}-win streak.",
                                       "Keep it going" + (f" · Last victim: <@{ev['victim']}>" if ev.get("victim") else ""), mode)
            elif k == "one":
                await send_streak_card(gid, k, n, f"<@{ev['user']}> is on {n} wins. One more reaches a {ev['next']}-win streak.",
                                       "Their next match decides it" + (f" · Last victim: <@{ev['victim']}>" if ev.get("victim") else ""), mode)
        except Exception as exc:
            print(f"Could not post a streak card: {exc!r}")
        await asyncio.sleep(1)


async def announce_on_the_line(t):
    """A match was just accepted: if one of the two fighters is on a streak, say it is on the line."""
    try:
        mode = t["mode"]
        if mode not in STREAK_MODES:
            return
        table = streak_table(mode)
        p0, p1 = t["p"][0]["discord_id"], t["p"][1]["discord_id"]   # p0 challenged, p1 accepted
        for holder, other in ((p0, p1), (p1, p0)):
            n = (table.get(holder) or {}).get("cur", 0)
            if n < STREAK_MIN:
                continue
            if holder == p0:
                main = f"<@{p1}> has accepted a match against <@{p0}>."
            else:
                main = f"<@{p1}> accepted a challenge from <@{p0}>."
            await send_streak_card(t["guild_id"], "line", n, main,
                                   f"<@{holder}> is on a {n}-win streak. If <@{other}> wins, the run ends here.", mode)
    except Exception as exc:
        print(f"Streak on the line error: {exc!r}")


async def apply_manual_streak(gid, mode, uid, dw, dl):
    """/set count: extra wins are added to the member's streak, extra losses send it back to 0. Streak cards post
    like after a real match."""
    if not dw and not dl:
        return []
    now = int(time.time())
    before = streak_table(mode)
    store.d.setdefault("streak_manual", []).append({"m": mode, "uid": int(uid), "ts": now, "dw": int(dw), "dl": int(dl)})
    store.save()
    after = streak_table(mode)
    events = streak_events({"w": uid, "l": None, "ts": now}, before, after) if dw > 0 else []
    if events and gid and mode in STREAK_MODES:
        asyncio.create_task(post_streak_events(gid, mode, events))
    return events


def streak_card(uid: int, modes=None, mention: bool = True):
    """Streak Status card: the member, then Current / Best / Next milestone for Duels."""
    blocks = [f"> <@{uid}>"]
    for mode in (modes or STREAK_MODES):
        table = streak_table(mode)
        me = table.get(uid) or {"cur": 0, "best": 0, "victim": None, "start": 0}
        cur = me["cur"]
        nxt = next_milestone(cur)
        blocks.append("\n".join([
            f"**{MODES[mode]['name']}**",
            f"> **Current streak:** {wins_text(cur)}" + (" 🔥" if cur >= STREAK_MIN else ""),
            f"> **Best streak:** {wins_text(me['best'])}",
            f"> **Next milestone:** {nxt} wins ({nxt - cur} to go)"]))
    view = discord.ui.LayoutView(timeout=None)
    c = discord.ui.Container(accent_colour=0xE67E22)
    c.add_item(discord.ui.TextDisplay("## Streak Status\n" + "\n\n".join(blocks)))
    view.add_item(c)
    return view


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
    title = re.search(r"(Duel) #(\d+) Result", text)
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


async def board_message(guild, mode, period, page=1, highlight=None):
    """Builds the whole leaderboard message: the layout plus its pictures. The board is global (all servers)."""
    rows = standings(mode, period)
    page, pages, podium_rows, list_rows = page_slice(rows, page)
    ids = [r[0] for _, r in podium_rows + list_rows]
    found = await asyncio.gather(*(lookup_user(u) for u in ids))
    names = {u: f[0] for u, f in zip(ids, found)}
    avatars = {u: f[1] for u, f in zip(ids, found) if f[1]}

    def entry(rank, r):
        return {"rank": rank, "uid": r[0], "name": names.get(r[0], "unknown"), "w": r[1], "l": r[2],
                "hl": highlight is not None and r[0] == highlight}

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
class LbSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"rv:lbsel:(?P<mode>duel)(?::(?P<page>\d+))?"):
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


class LbButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:lbme:(?P<mode>duel)"):
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
        """My Rank now JUMPS the board to the page the member is on (their row is highlighted)."""
        if not interaction.guild or not await guard(interaction):
            return
        await interaction.response.defer()
        period = gcfg(interaction.guild.id)["lb_period"].get(self.mode, "all")
        info = rank_info(standings(self.mode, period), interaction.user.id)
        if not info:   # not on this board: the private card explains it, the board stays where it is
            return await send_my_rank(interaction, self.mode, period)
        page = (info[0] - 1) // LB_PAGE + 1
        view, files = await board_message(interaction.guild, self.mode, period, page, highlight=interaction.user.id)
        await interaction.edit_original_response(view=view, attachments=files)
        try:
            await interaction.followup.send(
                f"You are **#{info[0]}** on the {LB_PERIODS[period]} {MODES[self.mode]['name']} board "
                f"with **{info[1]}W {info[2]}L** (Top {info[3]:.2f}%). Showing page {page}.", ephemeral=True)
        except discord.HTTPException:
            pass


class LbPage(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:lbpg:(?P<mode>duel):(?P<page>\d+):(?P<slot>[a-z0-9]+)"):
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


def jump_pages(page, pages):
    """The page numbers offered in the 'Jump to page' dropdown (Discord allows 25 options), around the current page."""
    if pages <= 25:
        return list(range(1, pages + 1))
    first = min(max(1, page - 12), pages - 24)
    return list(range(first, first + 25))


def page_range_text(n: int) -> str:
    return f"Ranks 1-{LB_PAGE}" if n == 1 else f"Ranks {(n - 1) * LB_PAGE + 1}-{n * LB_PAGE}"


class LbJump(discord.ui.DynamicItem[discord.ui.Select], template=r"rv:lbjump:(?P<mode>duel):(?P<page>\d+)"):
    """Dropdown under the board: pick any page (1, 2, 3 ...) and go straight there."""
    def __init__(self, mode, page=1, pages=1):
        super().__init__(discord.ui.Select(
            custom_id=f"rv:lbjump:{mode}:{page}", placeholder=f"Jump to page ({page}/{max(pages, page)})",
            options=[discord.SelectOption(label=f"Page {n}", value=str(n), description=page_range_text(n))
                     for n in jump_pages(page, max(pages, page))]))
        self.mode = mode
        self.page = page

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["mode"], page=int(match["page"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LbJump error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await guard(interaction):
            return
        values = (interaction.data or {}).get("values") or []
        try:
            target = int(values[0])
        except (ValueError, IndexError):
            target = self.page
        await interaction.response.defer()
        period = gcfg(interaction.guild.id)["lb_period"].get(self.mode, "all")
        view, files = await board_message(interaction.guild, self.mode, period, target)
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
        jump = discord.ui.ActionRow()
        jump.add_item(LbJump(mode, page, pages))
        c.add_item(jump)
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
    """Posts a fresh Duel board in its channel and removes the previous one. A board that has not
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
    await text("panel", "matchmaking", main, read_only, "Start a Duel or Squad Up from here.")
    await text("results", "results", main, read_only, "Finished match results.")
    await text("log", "match-logs", main, private, "Rivalz ticket logs (staff only).")
    await text("duel_lb", "duel-leaderboard", main, read_only, "Duel leaderboard, ranked by wins.")
    # Ongoing-Ping: the channel that announces live matches + the role that gets pinged for them
    adopt_legacy_extras(guild)  # your original server keeps its existing channel and role
    await text("ongoing", "ongoing-ping", main, read_only, "Live Duels. Join as a spectator to watch.", move=False)
    # Streak announcements (10, 15, 20 ... wins in a row). Your original server keeps its own streaks channel.
    await text("streaks", "streaks", main, read_only, "Win streak announcements: 10, 15, 20 wins in a row and every 5 after.", move=False)
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
    # Category 2: tickets (every Duel ticket is created in here)
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
@app_commands.describe(mode="Duel", winner="Who won", loser="Who lost", days_ago="How many days ago it happened (default 0)")
@app_commands.choices(mode=[app_commands.Choice(name="Duel", value="duel")])
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
        [f"> Challenge rivals to a **Duel** or **Squad Up** from the matchmaking panel. First to the chosen rounds wins."],
        ["`/ping` - check that Rivalz is online (everyone)",
         "`/member stats [member]` - Member stats card with their avatar. Also `.member stats`, `?member stats` (everyone)",
         "`/streak [member]` - win streak status: current, best, next milestone (everyone)",
         "`/set count d` - set a player's leaderboard count (developer only)",
         "`/rank`, `/panel`, `/authorize`, `/build`, `/update`, `/backup`, `/deauthorize` ... - owner only"],
        ["> You can also start a message with **R** or **Rivalz**: `R ping`, `R leaderboard`, `R top 3`, `R rank`, `R open general`, "
         "`R open Divisions category`, `R channels`.",
         "> Developer and trusted users: `R update`, `R panel`, `R build`, `R authorize`, `R backup`, `R db`, `R deauthorize`. "
         "Developer only: `/trust add`, `/trust remove` (or `R ta @user`, `R tr @user`).",
         "> Watch a live match from the ongoing-ping channel with **Join as Spectator** (no limit, one match at a time). Spectators can only use Join, Profile and Join VC."],
    ], accent=GREY), ephemeral=True)


@bot.tree.command(name="rank", description="See your global Duel rank.")
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


@bot.tree.command(name="streak", description="Check a win streak (yours by default): current, best and the server record.")
@app_commands.describe(member="Whose streak to check (default: you)", mode="Duel")
@app_commands.choices(mode=[app_commands.Choice(name="Duel", value="duel")])
async def streak_cmd(interaction: discord.Interaction, member: discord.User = None, mode: app_commands.Choice[str] = None):
    target = member or interaction.user
    await interaction.response.defer()
    await interaction.followup.send(
        view=streak_card(target.id, [mode.value] if mode else None, mention=True),
        allowed_mentions=NO_PING)


# ============================================================ /member stats  (also  .member stats  ?member stats)
STATS_BLACK = 0x010101      # Discord treats pure 0 as "no colour", so this is the darkest real black
MEMBER_STATS_RE = re.compile(r"^\s*[./?]\s*member\s*stats?\b[\s,:;-]*(?P<who>.*)$", re.I | re.S)
_stats_cooldown = {}


def win_rate_value(w: int, l: int) -> float:
    """Win rate 0.00 - 100.00 (%): wins / (wins + losses) x 100."""
    return (w / (w + l) * 100) if (w + l) else 0.0


def win_rate_text(w: int, l: int) -> str:
    return f"{win_rate_value(w, l):05.2f}%"      # 00.00% ... 100.00%


def overall_rows():
    """Duel wins and losses (all-time, global), ranked like a leaderboard."""
    tally = {}
    for mode in LB_MODES:
        for uid, w, l in standings(mode, "all"):
            e = tally.setdefault(uid, [0, 0])
            e[0] += w
            e[1] += l
    return sorted(((uid, w, l) for uid, (w, l) in tally.items()), key=lambda r: (-r[1], r[2], r[0]))


def stats_for(uid: int, view: str):
    """(rank, wins, losses, top_percent) for one view, rank None when unranked."""
    rows = overall_rows() if view == "overall" else standings(view, "all")
    info = rank_info(rows, uid)
    return info if info else (None, 0, 0, None)


class StatsView(discord.ui.LayoutView):
    """Member stats card: the member's own avatar and Duel stats. No buttons."""

    def __init__(self, asker_id: int, user, current: str = "overall"):
        super().__init__(timeout=None)
        self.asker_id, self.user, self.message = asker_id, user, None
        name = getattr(user, "display_name", None) or getattr(user, "global_name", None) or user.name
        rank, w, l, top = stats_for(user.id, "overall")
        head = discord.ui.TextDisplay(f"# Member Statistics\nStats for **{md(name)}**")
        body = [
            f"**User:** <@{user.id}>",
            f"**Rank:** `#{rank}`" if rank else "**Rank:** `Unranked`",
            f"**Top:** `{top:.2f}%`" if top is not None else "**Top:** `--`",
            f"**Matches:** `{w + l}`",
            f"**Wins:** `{w}`",
            f"**Losses:** `{l}`",
            f"**Win Rate:** `{win_rate_text(w, l)}`",
        ]
        container = discord.ui.Container(accent_colour=STATS_BLACK)
        container.add_item(discord.ui.Section(head, discord.ui.TextDisplay("\n".join(body)),
                                              accessory=discord.ui.Thumbnail(user.display_avatar.url)))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay("-# Rivalz | Global all-time leaderboard"))
        self.add_item(container)


member_group = app_commands.Group(name="member", description="Member tools.")


@member_group.command(name="stats", description="See the stats of yourself or any member.")
@app_commands.describe(member="Whose stats to show (default: you)")
async def member_stats_cmd(interaction: discord.Interaction, member: discord.User = None):
    await interaction.response.defer()
    target = member or interaction.user
    view = StatsView(interaction.user.id, target)
    view.message = await interaction.followup.send(view=view, wait=True, allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(member_group)


async def member_stats_text(message: discord.Message):
    """.member stats  ?member stats  [@member | member id | reply to someone]"""
    m = MEMBER_STATS_RE.match(message.content or "")
    if not m:
        return
    now = time.time()
    if now - _stats_cooldown.get(message.author.id, 0) < 3:
        return
    _stats_cooldown[message.author.id] = now
    who = (m.group("who") or "").strip()
    target = None
    if who:
        ids = re.findall(r"\d{15,25}", who)
        if ids:
            uid = int(ids[0])
            target = message.guild.get_member(uid) if message.guild else None
            if not target:
                try:
                    target = await bot.fetch_user(uid)
                except discord.HTTPException:
                    target = None
        elif message.guild:
            target = message.guild.get_member_named(who.lstrip("@"))
        if not target:
            return await message.reply("I could not find that member. Mention them or give their ID.", mention_author=False)
    else:
        ref = message.reference.resolved if message.reference else None
        if isinstance(ref, discord.Message):
            target = (message.guild.get_member(ref.author.id) if message.guild else None) or ref.author
        else:
            target = message.author
    view = StatsView(message.author.id, target)
    view.message = await message.reply(view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())


set_group = app_commands.Group(name="set", description="Developer only: leaderboard tools.")
count_group = app_commands.Group(name="count", description="Developer only: set a player's leaderboard count.", parent=set_group)


async def set_count(interaction: discord.Interaction, mode: str, user: discord.User, wins, losses):
    if not is_real_owner(interaction.user):
        return await interaction.response.send_message("Only the developer can use this command.", ephemeral=True)
    if wins is None and losses is None:
        return await interaction.response.send_message("Give at least `wins` or `losses`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    prev_w, prev_l = next(((w, l) for u, w, l in standings(mode, "all") if u == user.id), (0, 0))   # what the board shows now
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
    try:   # extra wins count as wins in a row (streak cards post), extra losses reset the streak
        await apply_manual_streak(interaction.guild_id, mode, user.id, new_w - prev_w, new_l - prev_l)
    except Exception as exc:
        print(f"Manual streak update failed: {exc!r}")
    cur_streak = (streak_table(mode).get(user.id) or {}).get("cur", 0)
    await notice(interaction, "Leaderboard Count Set",
                 [f"> **{MODES[mode]['name']}:** {user.mention} is now `{new_w}` wins / `{new_l}` losses (all-time).",
                  f"> Win streak: `{cur_streak}`. Streak cards post in the streaks channel at {STREAK_MIN}, then every {STREAK_STEP}.",
                  "> The boards refresh in a few seconds. Matches already recorded are kept."], accent=GREEN)


@count_group.command(name="d", description="Developer only: set a player's DUEL wins / losses on the leaderboard.")
@app_commands.describe(user="Whose count to set", wins="Total duel wins", losses="Total duel losses")
async def set_count_d(interaction: discord.Interaction, user: discord.User,
                      wins: app_commands.Range[int, 0, 100000] = None, losses: app_commands.Range[int, 0, 100000] = None):
    await set_count(interaction, "duel", user, wins, losses)


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
