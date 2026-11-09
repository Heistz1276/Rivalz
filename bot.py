"""
Cruxer Discord Bot - discord.py 2.6+ (Components V2 layouts are used everywhere)

Install:
  pip install -U "discord.py>=2.6" aiohttp python-dotenv Pillow psutil

Turn ON "Message Content Intent" and "Server Members Intent" in the Developer Portal (Bot tab).

Bot permissions: Manage Channels, Manage Roles, Manage Webhooks, Manage Guild, Kick Members,
Ban Members, View Audit Log, Embed Links, Attach Files, Read Message History, Create Instant Invite,
Change Nickname. The bot's role must sit ABOVE the Tier-1..5 roles and the ping roles.

.env values:
  DISCORD_TOKEN, ROBLOX_COOKIE (optional Roblox presence/region lookup), CRUXER_DATA_DIR (persistent folder),
  CRUXER_BACKUP_CHANNEL_ID (optional)

DATA SAFETY: everything lives in cruxer_data.json (atomic saves, .bak, 48 snapshots, hourly DM backups,
automatic restore from the newest Discord backup). Server templates (/store) are part of the backup.
"""

import asyncio
import base64
import copy
import gzip
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import uuid
import warnings
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ======================= CONFIG =======================

BOT_NAME = "Cruxer"
BOT_VERSION = os.getenv("BOT_VERSION", "2.37")
LEGACY_BOT_NAME = "Imperium"  # only used to find/rename old categories, nicknames and read-me posts

TOKEN = os.getenv("DISCORD_TOKEN")
DEVELOPER_ID = 1455426573741330495

GUILD_ID = 1554169015662936114
CRUXER_CATEGORY_ID = 1564252456454590584
TICKET_CATEGORY_ID = 1564679069554188389
BATTLE_PANEL_CHANNEL_ID = 1554169017592451175
RAID_RESULTS_CHANNEL_ID = 1554468736440729650
LEADERBOARD_CHANNEL_ID = 1554469313782743050
LOGS_CHANNEL_ID = 1554469729769365555
MVPS_CHANNEL_ID = 1554551411214131351
SNIPE_CHANNEL_ID = 1555445538566967296

VIEWER_ROLE_ID = 0
PING_ROLE_ID = 0

EMBED_COLOR = 0x2B2D31
GREEN, RED, YELLOW, BLUE = 0x57F287, 0xED4245, 0xFEE75C, 0x5865F2

AUTHORIZE_CATEGORY_NAME = "Cruxer tickets"
CRUXER_CATEGORY_NAME = "Cruxer"
CATEGORY_NAMES = {"cruxer tickets", "crusher tickets"}
BATTLE_PANEL_CHANNEL_NAME = "battle-panel"
RAID_RESULTS_CHANNEL_NAME = "raid-results"
LEADERBOARD_CHANNEL_NAME = "leaderboard"
LOGS_CHANNEL_NAME = "logs"
MVPS_CHANNEL_NAME = "mvps"
SNIPE_CHANNEL_NAME = "snipe"
README_CHANNEL_NAME = "read-me"
WARNINGS_CHANNEL_NAME = "warnings"
WARNINGS_CHANNEL_ID = 1564682942809247854
PROMOTION_CHANNEL_ID = 1565009390258618398

SUPPORT_SERVER_INVITE = None
PREFIXES = (".", "!", "?")     # default prefixes; slash commands remain available as usual
OWN_PREFIX_COMMANDS = {"sync"}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = (os.getenv("CRUXER_DATA_DIR")
            or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
            or _BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
_NEW_DATA = os.path.join(DATA_DIR, "cruxer_data.json")
_OLD_DATA = os.path.join(DATA_DIR, "imperium_data.json")
DATA_FILE = (
    os.getenv("CRUXER_DATA_FILE")
    or os.getenv("IMPERIUM_DATA_FILE")
    or (_OLD_DATA if os.path.exists(_OLD_DATA) and not os.path.exists(_NEW_DATA) else _NEW_DATA)
)
BACKUP_FILE = DATA_FILE + ".bak"
_DATA_FOLDER = os.path.dirname(os.path.abspath(DATA_FILE))
SNAPSHOT_DIR = os.path.join(_DATA_FOLDER, "snapshots")
GANK_DIR = os.path.join(_DATA_FOLDER, "gank")       # gank images are kept here until the result is posted
GLOBAL_FILE = os.getenv("CRUXER_GLOBAL_FILE") or os.path.join(_DATA_FOLDER, "cruxer_global.json")
SNAPSHOT_KEEP = 48
SNAPSHOT_EVERY_SECONDS = 1800
BACKUP_CHANNEL_ID = int(os.getenv("CRUXER_BACKUP_CHANNEL_ID") or 0)
DATA_IS_PERSISTENT = bool(os.getenv("CRUXER_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
                          or os.getenv("CRUXER_DATA_FILE") or os.getenv("IMPERIUM_DATA_FILE"))

LEADERBOARD_PAGE_SIZE = 10
DAILY_REQUEST_LIMIT = 2
RATE_WINDOW_SECONDS = 24 * 60 * 60
ANTINUKE_WINDOW_SECONDS = 600

ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
TRACKER_POLL_SECONDS = 10
MAX_TRACKED_PER_GUILD = 25
JOIN_WARNING_SECONDS = 180
JOIN_FINAL_WARNING_SECONDS = 600
JOIN_CHECK_POLL_SECONDS = 20
HELPER_WARNING_LIMIT = 15

DEFAULT_PLACE_ID = 10418864827      # The Strongest Battlegrounds (used by the server scan when Roblox hides the place)
SCAN_MAX_SERVERS = 800
SCAN_TIME_BUDGET = 50               # seconds a /snipe server scan may run

TICKET_MESS_THRESHOLD = 25
MESS_PREFIX = "**Ticket is getting busy.**"
MESS_MESSAGES = [
    "**Ticket activity reminder**\nThis ticket is active. Use the button below to return to the request details.",
]
NO_ASSIST_WARNING_SECONDS = 300
GANK_IMAGE_SIZE = (1200, 675)
BACKUP_INTERVAL_HOURS = 1
DURATION_UPDATE_SECONDS = 15

SEARCH_DEFAULT_MINUTES = 60
SEARCH_PER_CHANNEL_LIMIT = 500
SEARCH_MAX_SHOWN = 20

RAID_PING_ROLE_ID = 1558114795536908418
BACKUP_PING_ROLE_ID = 1558114863723716790
REGION_PING_ROLES = {
    "asia": (0, "Asia"),
    "europe": (0, "Europe"),
    "africa": (0, "Africa"),
    "middle_east": (0, "Middle East"),
    "oceania": (0, "Oceania"),
    "north_america": (0, "North America"),
    "south_america": (0, "South America"),
}
GLOBAL_SUPPORT_ROLE_ID = 1565042400374554785
SPECIFIC_REGION_SUPPORT_ROLES = {
    "india": (1565042500253646948, "India-ASIA Teamers Support"),
    "singapore": (1565042605782212628, "Singapore-ASIA Teamers Support"),
    "japan": (1565042686787059933, "Japan-ASIA Teamers Support"),
    "uk": (1565042765002182737, "UK-EUROPE Teamers Support"),
    "north_america": (1565042817129119794, "NA-Countries Teamers Support"),
}
GLOBAL_SUPPORT_ROLE_NAME = "Global Teamers Support"
SUPPORT_ALERT_ROLES = {
    "global": (GLOBAL_SUPPORT_ROLE_ID, GLOBAL_SUPPORT_ROLE_NAME),
    **{f"region_{key}": value for key, value in SPECIFIC_REGION_SUPPORT_ROLES.items()},
    **REGION_PING_ROLES,
}
RAID_PING_ROLE_NAME = "|Raid Ping|"
BACKUP_PING_ROLE_NAME = "|Backup Ping|"
TIER_ROLE_IDS = {
    1: 1558116206849032242,
    2: 1558116306555772968,
    3: 1558116358791888956,
    4: 1558116403872145460,
    5: 1558116452618346557,
}
TIER_RAIDS_NEEDED = {5: 5, 4: 25, 3: 50, 2: 75, 1: 100}
TIER_DAILY_LIMITS = {1: 10, 2: 7, 3: 5, 4: 3, 5: 3}

# ======================================================
# DATA STORE
# ======================================================

def normalize_data(data: dict) -> dict:
    data.setdefault("raid_counters", {})
    data.setdefault("tickets", {})
    data.setdefault("stats", {"guilds": {}, "global": {}})
    data["stats"].setdefault("guilds", {})
    data["stats"].setdefault("global", {})
    data["stats"].setdefault("daily", {})
    data["stats"].setdefault("raid_assists", {})
    for key in ("panel_messages", "leaderboard_messages", "request_limits", "win_streaks", "configs",
                "trackers", "blacklist", "links", "pending_links", "panel_channels", "leaderboard_channels",
                "ticket_archive", "nuke_snapshots", "trusted", "type_counters",
                "left_members", "archived_trackers", "recruit_queue", "templates", "guild_settings",
                "channel_map", "category_map", "helper_warnings", "usage_metrics"):
        data.setdefault(key, {})
    for key in ("authorized_guilds", "audit", "global_history", "setup_guilds"):
        data.setdefault(key, [])
    return data


def _list_snapshots():
    try:
        return sorted(
            (os.path.join(SNAPSHOT_DIR, f) for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json.gz")),
            reverse=True,
        )
    except OSError:
        return []


def _read_data_file(path: str) -> dict:
    with open(path, "rb") as f:
        raw = f.read()
    if not raw.strip():
        raise ValueError("empty file")
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


_LOADED_FROM = None


def load_data():
    global _LOADED_FROM
    data = None
    for path in [DATA_FILE, BACKUP_FILE] + _list_snapshots():
        if not os.path.exists(path):
            continue
        try:
            data = _read_data_file(path)
            _LOADED_FROM = path
            if path != DATA_FILE:
                print(f"[{BOT_NAME}] Main data file was missing/unreadable - recovered from {path}")
            break
        except OSError as exc:
            raise SystemExit(f"[{BOT_NAME}] Cannot read {path}: {exc!r}. Refusing to start so no data is lost.")
        except Exception as exc:
            print(f"[{BOT_NAME}] {path} is corrupt ({exc!r}).")
            if path == DATA_FILE:
                try:
                    os.replace(path, f"{path}.corrupt-{int(time.time())}")
                except OSError:
                    pass
    return normalize_data(data or {})


_save_lock = threading.Lock()
_last_snapshot_at = 0.0


def _write_snapshot():
    global _last_snapshot_at
    now = time.time()
    if now - _last_snapshot_at < SNAPSHOT_EVERY_SECONDS:
        return
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        name = time.strftime("cruxer_%Y%m%d_%H%M%S", time.gmtime()) + ".json.gz"
        with open(DATA_FILE, "rb") as src, gzip.open(os.path.join(SNAPSHOT_DIR, name), "wb") as dst:
            shutil.copyfileobj(src, dst)
        _last_snapshot_at = now
        for old in _list_snapshots()[SNAPSHOT_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError as exc:
        print(f"[{BOT_NAME}] Could not write a snapshot: {exc!r}")


def _atomic_write_json(path: str, payload) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_data():
    with _save_lock:
        tmp = DATA_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(DATA, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(DATA_FILE) and os.path.getsize(DATA_FILE) > 0:
                try:
                    shutil.copyfile(DATA_FILE, BACKUP_FILE)
                except OSError:
                    pass
            os.replace(tmp, DATA_FILE)
        except (OSError, TypeError, ValueError) as exc:
            print(f"[{BOT_NAME}] !!! SAVE FAILED: {exc!r}")
            return
        _write_snapshot()


def save_global_file():
    try:
        _atomic_write_json(GLOBAL_FILE, {
            "saved_at": int(time.time()),
            "global": DATA["stats"]["global"],
            "history": DATA["global_history"],
        })
    except OSError as exc:
        print(f"[{BOT_NAME}] Could not write the global history file: {exc!r}")


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


DATA = load_data()
DATA_WAS_FRESH = _LOADED_FROM is None
if _LOADED_FROM not in (None, DATA_FILE):
    save_data()
if not DATA_IS_PERSISTENT:
    print(f"[{BOT_NAME}] WARNING: CRUXER_DATA_DIR is not set. Data is stored in {DATA_FILE}. "
          "If your host wipes files on redeploy, point CRUXER_DATA_DIR at a persistent volume.")
BOT_START_TIME = int(time.time())


def merge_global_sources():
    changed = False
    g = DATA["stats"]["global"]
    gf = _read_json(GLOBAL_FILE) or {}
    for uid, count in (gf.get("global") or {}).items():
        if isinstance(count, int) and count > g.get(uid, 0):
            g[uid] = count
            changed = True

    known = {(h.get("ts"), h.get("guild_id"), h.get("raid_number")) for h in DATA["global_history"]}
    for h in gf.get("history") or []:
        key = (h.get("ts"), h.get("guild_id"), h.get("raid_number"))
        if key not in known:
            DATA["global_history"].append(h)
            known.add(key)
            changed = True

    tally: dict[str, int] = {}
    for t in DATA["ticket_archive"].values():
        if t.get("ended_at") and t.get("result"):
            for uid in t.get("helper_order", []):
                tally[uid] = tally.get(uid, 0) + 1
    for uid, count in tally.items():
        if count > g.get(uid, 0):
            g[uid] = count
            changed = True

    DATA["global_history"].sort(key=lambda h: h.get("ts", 0))
    if changed:
        save_data()
    save_global_file()


merge_global_sources()


def next_raid_number(guild_id: int) -> int:
    gid = str(guild_id)
    DATA["raid_counters"][gid] = DATA["raid_counters"].get(gid, 0) + 1
    save_data()
    return DATA["raid_counters"][gid]


def next_type_number(guild_id: int, request_type: str) -> int:
    counters = DATA["type_counters"].setdefault(str(guild_id), {})
    counters[request_type] = counters.get(request_type, 0) + 1
    save_data()
    return counters[request_type]


def is_setup(guild_id: int) -> bool:
    # Authorization is stored separately from channel/category placement. Moving a channel
    # or renaming its parent must not disable Cruxer for the server.
    return guild_id in DATA["authorized_guilds"]


def ticket_number(ticket: dict) -> int:
    return ticket.get("type_number") or ticket["raid_number"]


def ticket_label(ticket: dict) -> str:
    if ticket.get("type_number"):
        return f"{ticket['type']}-ticket #{ticket['type_number']}"
    return f"{ticket['type']} #{ticket['raid_number']}"


def get_ticket(channel_id: int):
    return DATA["tickets"].get(str(channel_id))


def save_ticket(channel_id: int, ticket: dict):
    DATA["tickets"][str(channel_id)] = ticket
    snapshot = copy.deepcopy(ticket)
    snapshot["channel_id"] = channel_id
    snapshot["archived_at"] = int(time.time())
    DATA["ticket_archive"][f"{ticket['guild_id']}:{ticket['raid_number']}"] = snapshot
    save_data()


def today_str() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def add_raid_credit(guild_id: int, user_ids, raid_number=None, result=None, guild_name=None, is_raid: bool = False):
    gid = str(guild_id)
    guild_stats = DATA["stats"]["guilds"].setdefault(gid, {})
    global_stats = DATA["stats"]["global"]
    raid_only = DATA["stats"]["raid_assists"].setdefault(gid, {})
    daily_stats = DATA["stats"]["daily"].setdefault(gid, {}).setdefault(today_str(), {})
    for uid in user_ids:
        uid = str(uid)
        guild_stats[uid] = guild_stats.get(uid, 0) + 1
        global_stats[uid] = global_stats.get(uid, 0) + 1
        daily_stats[uid] = daily_stats.get(uid, 0) + 1
        if is_raid:
            raid_only[uid] = raid_only.get(uid, 0) + 1
    DATA["global_history"].append({
        "ts": int(time.time()), "guild_id": guild_id, "guild_name": guild_name,
        "raid_number": raid_number, "result": result, "helpers": [str(u) for u in user_ids],
    })
    save_data()
    save_global_file()


def get_raid_count(guild_id: int, user_id: int, scope: str) -> int:
    if scope == "global":
        return DATA["stats"]["global"].get(str(user_id), 0)
    return DATA["stats"]["guilds"].get(str(guild_id), {}).get(str(user_id), 0)


def get_raid_only_count(guild_id: int, user_id: int) -> int:
    return DATA["stats"]["raid_assists"].get(str(guild_id), {}).get(str(user_id), 0)


def leaderboard_entries(guild_id: int, scope: str):
    if scope == "global":
        source = DATA["stats"]["global"]
    elif scope == "daily":
        source = DATA["stats"]["daily"].get(str(guild_id), {}).get(today_str(), {})
    else:
        source = DATA["stats"]["guilds"].get(str(guild_id), {})
    entries = [(int(uid), count) for uid, count in source.items() if count > 0]
    entries.sort(key=lambda pair: pair[1], reverse=True)
    return entries


def get_rank(guild_id: int, user_id: int, scope: str):
    entries = leaderboard_entries(guild_id, scope)
    for i, (uid, count) in enumerate(entries):
        if uid == user_id:
            return i + 1, count, len(entries)
    return None, 0, len(entries)


def is_trusted(user_id: int) -> bool:
    return str(user_id) in DATA["trusted"]


def can_use_own(user_id: int) -> bool:
    return user_id == DEVELOPER_ID or is_trusted(user_id)


def is_linked(user_id: int) -> bool:
    return str(user_id) in DATA["links"]


# ---- request limit: a 24h window that starts at the member's FIRST request ----

def _active_window(guild_id: int, user_id: int):
    entry = DATA["request_limits"].get(str(guild_id), {}).get(str(user_id))
    if not entry or "window_start" not in entry:
        return None
    if time.time() >= entry["window_start"] + RATE_WINDOW_SECONDS:
        return None
    return entry


def rate_limit_status(guild_id: int, user_id: int, limit: int = DAILY_REQUEST_LIMIT):
    if user_id == DEVELOPER_ID:
        return True, 0, None
    entry = _active_window(guild_id, user_id)
    if entry is None:
        return True, 0, None
    return entry["count"] < limit, entry["count"], entry["window_start"] + RATE_WINDOW_SECONDS


def check_and_increment_rate_limit(guild_id: int, user_id: int, limit: int = DAILY_REQUEST_LIMIT):
    if user_id == DEVELOPER_ID:
        return True, 0, None
    entry = _active_window(guild_id, user_id)
    if entry is None:
        entry = {"window_start": int(time.time()), "count": 0}
    resets = entry["window_start"] + RATE_WINDOW_SECONDS
    guild_limits = DATA["request_limits"].setdefault(str(guild_id), {})
    if entry["count"] >= limit:
        guild_limits[str(user_id)] = entry
        save_data()
        return False, entry["count"], resets
    entry["count"] += 1
    guild_limits[str(user_id)] = entry
    save_data()
    return True, entry["count"], resets


def rate_limit_text(count: int, resets, limit: int = DAILY_REQUEST_LIMIT) -> str:
    when = f" Your requests reset <t:{resets}:R>." if resets else ""
    return f"Rate limit reached ({count}/{limit}).{when}"


# ---- Tier roles + typed ping roles ----

def find_named_role(guild: discord.Guild, role_id: int, name: str):
    role = guild.get_role(role_id) if role_id else None
    if role is None:
        wanted = name.lower()
        role = discord.utils.find(lambda r: r.name.lower() == wanted, guild.roles)
    return role


def ping_role_for(guild: discord.Guild, request_type: str):
    if request_type == "Raid":
        return find_named_role(guild, RAID_PING_ROLE_ID, RAID_PING_ROLE_NAME)
    return find_named_role(guild, BACKUP_PING_ROLE_ID, BACKUP_PING_ROLE_NAME)


def regional_ping_key(region: str | None):
    value = (region or "").casefold()
    middle_east_codes = {"ae", "bh", "iq", "ir", "jo", "kw", "lb", "om", "ps", "qa", "sa", "sy", "ye", "il"}
    asia_codes = {"af", "am", "az", "bd", "bt", "bn", "kh", "cn", "ge", "hk", "in", "id", "jp", "kz", "kp", "kr", "kg", "la", "mo", "my", "mv", "mn", "mm", "np", "pk", "ph", "sg", "lk", "tw", "tj", "th", "tl", "tm", "uz", "vn"}
    is_middle_east = "middle east" in value or any(re.search(rf"\b{code}\b", value) for code in middle_east_codes)
    if is_middle_east:
        key = "middle_east"
    elif value.startswith(("af", "africa")) or "africa" in value:
        key = "africa"
    elif value.startswith(("eu", "europe")) or "europe" in value:
        key = "europe"
    elif value.startswith(("oce", "oc ", "oceania")) or "oceania" in value or any(x in value for x in ("australia", "new zealand")):
        key = "oceania"
    elif value.startswith(("na", "north america")) or "north america" in value or any(x in value for x in ("united states", "canada", "mexico")):
        key = "north_america"
    elif value.startswith(("sa", "south america")) or "south america" in value or any(x in value for x in ("brazil", "argentina", "chile", "colombia", "peru")):
        key = "south_america"
    elif value.startswith(("as", "asia")) or "asia" in value or any(re.search(rf"\b{code}\b", value) for code in asia_codes):
        key = "asia"
    else:
        return None
    return key


def regional_ping_role_for(guild: discord.Guild, region: str | None):
    key = regional_ping_key(region)
    if not key:
        return None
    rid, name = REGION_PING_ROLES[key]
    role = find_named_role(guild, rid, name)
    if role:
        return role
    return discord.utils.find(
        lambda candidate: name.casefold() in candidate.name.casefold(), guild.roles)


def specific_region_support_role_for(guild: discord.Guild, region: str | None):
    value = (region or "").casefold()
    specific = None
    if "india" in value or re.search(r"\bin\b", value):
        specific = "india"
    elif "singapore" in value or re.search(r"\bsg\b", value):
        specific = "singapore"
    elif "japan" in value or re.search(r"\bjp\b", value):
        specific = "japan"
    elif "united kingdom" in value or "uk" in value or "london" in value:
        specific = "uk"
    elif any(term in value for term in ("north america", "united states", "canada", "mexico", "ashburn", "us-east", "us-west")):
        specific = "north_america"
    if specific:
        role_id, name = SPECIFIC_REGION_SUPPORT_ROLES[specific]
        role = find_named_role(guild, role_id, name)
        if role:
            return role
        return discord.utils.find(lambda candidate: name.casefold() in candidate.name.casefold(), guild.roles)
    return regional_ping_role_for(guild, region)


def tier_role(guild: discord.Guild, tier: int):
    return find_named_role(guild, TIER_ROLE_IDS[tier], f"Tier-{tier}")


def member_tier(member):
    guild = getattr(member, "guild", None)
    roles = getattr(member, "roles", None)
    if guild is None or roles is None:
        return None
    for tier in sorted(TIER_ROLE_IDS):
        role = tier_role(guild, tier)
        if role is not None and role in roles:
            return tier
    return None


def tier_for_raids(raids: int):
    for tier in sorted(TIER_RAIDS_NEEDED):
        if raids >= TIER_RAIDS_NEEDED[tier]:
            return tier
    return None


def request_limit_for(member) -> int:
    guild = getattr(member, "guild", None)
    if guild is None or not is_setup(guild.id):
        return DAILY_REQUEST_LIMIT
    return TIER_DAILY_LIMITS.get(member_tier(member), DAILY_REQUEST_LIMIT)


async def sync_tier_role(guild: discord.Guild, member: discord.Member):
    if not is_setup(guild.id):
        return None
    if (member.id not in (DEVELOPER_ID, guild.owner_id)
            and DATA.get("helper_warnings", {}).get(str(guild.id), {}).get(str(member.id), 0) > HELPER_WARNING_LIMIT):
        return None
    earned = tier_for_raids(get_raid_only_count(guild.id, member.id))
    if earned is None:
        return None
    current = member_tier(member)
    if current is not None and current <= earned:
        return None
    target = tier_role(guild, earned)
    if target is None:
        return None
    others = [r for r in (tier_role(guild, t) for t in TIER_ROLE_IDS) if r is not None and r != target and r in member.roles]
    try:
        if others:
            await member.remove_roles(*others, reason=f"{BOT_NAME} tier update")
        await member.add_roles(target, reason=f"{BOT_NAME} tier update - {get_raid_only_count(guild.id, member.id)} raid assists")
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"[{BOT_NAME}] Could not give {target.name} to {member} in {guild.name}: {exc}")
        return None
    return earned


async def award_tier_roles(guild: discord.Guild, user_ids):
    promoted = []
    for uid in user_ids:
        member = guild.get_member(int(uid))
        if member is None:
            continue
        new_tier = await sync_tier_role(guild, member)
        if new_tier:
            promoted.append((member, new_tier))
    if not promoted:
        return
    promotion_channel = await get_promotion_channel(guild)
    if not promotion_channel:
        return
    for member, tier in promoted:
        rescues = get_raid_only_count(guild.id, member.id)
        role = tier_role(guild, tier)
        next_tier = tier - 1 if tier > 1 else None
        next_needed = max(0, TIER_RAIDS_NEEDED[next_tier] - rescues) if next_tier else None
        try:
            await promotion_channel.send(view=card(
                "## \U0001f4e2 Raider Rank Update",
                "### Promoted\n- Reached the required rescue count for this rank.", SEP,
                f"### Member\n- <@{member.id}> - **{member.display_name}**\n- Current rescues: `{rescues}`\n"
                f"- Current rank: **Tier-{tier}**\n- New rank: {role.mention if role else f'**Tier-{tier}**'}", SEP,
                (f"### Next Rank: **Tier-{next_tier}**\n- Rescues needed: `{next_needed}`" if next_tier else "### Highest rank reached"),
                footer=BOT_NAME), allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not post promotion for {member.id}: {exc!r}")


def update_win_streak(guild_id: int, result: str) -> int:
    gid = str(guild_id)
    if result == "Won":
        DATA["win_streaks"][gid] = DATA["win_streaks"].get(gid, 0) + 1
    else:
        DATA["win_streaks"][gid] = 0
    save_data()
    return DATA["win_streaks"][gid]


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def ticket_channel_name(ticket: dict) -> str:
    if ticket.get("type_number"):
        return f"{ticket['type'].lower()}-ticket-{ticket['type_number']}"
    return f"ticket-no-{ticket['raid_number']}"


def _clean(text, limit: int) -> str:
    return (text or "").replace("`", "'")[:limit]


def box(text) -> str:
    text = (text or "None").replace("```", "'''")
    return f"```{text}```"


# ======================================================
# CARD HELPERS (every message the bot makes uses this look)
# ======================================================

SEP = "\x00sep"


def link_btn(label: str, url):
    if not url:
        return None
    return discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url)


def gallery(*urls):
    return discord.ui.MediaGallery(*[discord.MediaGalleryItem(u) for u in urls])


def card(*blocks, buttons=None, thumb=None, color=EMBED_COLOR, footer=None, header=None, timeout=None):
    """A Components V2 card. Strings become text, SEP a divider, other objects are added as they are.
    thumb = image shown beside the first text; header = a Button shown beside the first text."""
    blocks = list(blocks)
    budget = 3800 - (len(footer) + 4 if footer else 0)
    for _ in range(30):
        texts = [k for k, b in enumerate(blocks) if isinstance(b, str) and b != SEP]
        total = sum(len(blocks[k]) for k in texts)
        if total <= budget or not texts:
            break
        i = max(texts, key=lambda k: len(blocks[k]))
        blocks[i] = blocks[i][:max(60, len(blocks[i]) - (total - budget) - 3)] + "..."

    children, first = [], True
    for b in blocks:
        if b is None:
            continue
        if isinstance(b, str):
            if b == SEP:
                children.append(discord.ui.Separator())
                continue
            if first and (thumb or header is not None):
                accessory = discord.ui.Thumbnail(thumb) if thumb else header
                children.append(discord.ui.Section(discord.ui.TextDisplay(b), accessory=accessory))
            else:
                children.append(discord.ui.TextDisplay(b))
            first = False
        else:
            children.append(b)
    if footer:
        children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay(f"-# {footer}"))
    row = [b for b in (buttons or []) if b is not None][:5]
    if row:
        children.append(discord.ui.ActionRow(*row))
    view = discord.ui.LayoutView(timeout=timeout)
    view.add_item(discord.ui.Container(*children, accent_colour=color))
    return view


async def reply_card(interaction: discord.Interaction, *blocks, ephemeral=True, **kw):
    """Answer an interaction with a card (works before and after defer)."""
    view = card(*blocks, **kw)
    if interaction.response.is_done():
        return await interaction.followup.send(view=view, ephemeral=ephemeral)
    return await interaction.response.send_message(view=view, ephemeral=ephemeral)


# ======================================================
# SMALL SHARED HELPERS
# ======================================================

async def get_roblox_profile(session: aiohttp.ClientSession, username: str):
    try:
        async with session.post(
            "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": True},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("data"):
                user = data["data"][0]
                return user["id"], user["name"]
    except Exception:
        pass
    return None


def roblox_profile_url(roblox_id):
    return f"https://www.roblox.com/users/{roblox_id}/profile" if roblox_id else None


def roblox_display(username: str, roblox_id) -> str:
    url = roblox_profile_url(roblox_id)
    return f"[{username}]({url})" if url else f"{username} (not found on Roblox)"


def helper_line(ticket: dict, uid: str) -> str:
    info = ticket["helpers"].get(uid, {})
    url = roblox_profile_url(info.get("roblox_id"))
    if url:
        return f"<@{uid}> - [Roblox Profile]({url})"
    return f"<@{uid}> - Profile not found"


def saved_roblox_name(user_id: int):
    link = DATA["links"].get(str(user_id))
    if link:
        return link["roblox_username"]
    return DATA["configs"].get(str(user_id), {}).get("roblox_username")


def find_category(guild: discord.Guild):
    saved_map = DATA.get("category_map", {}).get(str(guild.id), 0)
    saved_id = saved_map.get("tickets", 0) if isinstance(saved_map, dict) else saved_map
    saved = guild.get_channel(saved_id) if saved_id else None
    if isinstance(saved, discord.CategoryChannel):
        return saved
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        return category
    return discord.utils.find(
        lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() in CATEGORY_NAMES,
        guild.categories,
    )


def find_cruxer_category(guild: discord.Guild):
    saved_map = DATA.get("category_map", {}).get(str(guild.id), {})
    saved_id = saved_map.get("cruxer", 0) if isinstance(saved_map, dict) else 0
    saved = guild.get_channel(saved_id) if saved_id else None
    if isinstance(saved, discord.CategoryChannel):
        return saved
    category = guild.get_channel(CRUXER_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        return category
    return discord.utils.find(
        lambda c: isinstance(c, discord.CategoryChannel)
        and c.name.casefold() in {CRUXER_CATEGORY_NAME.casefold(), LEGACY_BOT_NAME.casefold()},
        guild.categories,
    )


def configured_channel(guild: discord.Guild, key: str, default_id: int = 0, name: str | None = None):
    saved_id = DATA.get("channel_map", {}).get(str(guild.id), {}).get(key, 0)
    channel = guild.get_channel(saved_id) if saved_id else None
    if isinstance(channel, discord.TextChannel):
        return channel
    channel = guild.get_channel(default_id) if default_id else None
    if isinstance(channel, discord.TextChannel):
        return channel
    return discord.utils.get(guild.text_channels, name=name) if name else None


async def delete_previous(channel: discord.TextChannel, msg_key: str, chan_key: str):
    gid = str(channel.guild.id)
    old_id = DATA[msg_key].get(gid)
    if not old_id:
        return
    old_channel_id = DATA[chan_key].get(gid)
    old_channel = channel.guild.get_channel(old_channel_id) if old_channel_id else channel
    if old_channel is None:
        old_channel = channel
    try:
        old_msg = await old_channel.fetch_message(old_id)
        await old_msg.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, AttributeError):
        pass


_refresh_locks: dict[str, asyncio.Lock] = {}
_readme_locks: dict[int, asyncio.Lock] = {}

PURGE_KEYS = {
    "panel": ("panel_messages", "panel_channels"),
    "leaderboard": ("leaderboard_messages", "leaderboard_channels"),
}
PURGE_MARKERS = {"panel": "tsb:panel:request", "leaderboard": "lb:scope"}
PANEL_HINTS = (
    "choose raid or backup", "backup panel", "rescue ticket", "request for raids",
    "what is the benefit of using this", "create rescue ticket",
)


def _walk_components(components):
    stack = list(components or [])
    while stack:
        c = stack.pop()
        yield c
        for attr in ("children", "components"):
            kids = getattr(c, attr, None)
            if kids:
                try:
                    stack.extend(kids)
                except TypeError:
                    pass
        accessory = getattr(c, "accessory", None)
        if accessory:
            stack.append(accessory)


def _component_ids(components) -> set:
    found = set()
    for c in _walk_components(components):
        cid = getattr(c, "custom_id", None)
        if cid:
            found.add(cid)
    return found


def message_blob(msg: discord.Message) -> str:
    parts = [msg.content or ""]
    for e in msg.embeds:
        parts += [e.title or "", e.description or ""]
        for f in e.fields:
            parts += [f.name or "", f.value or ""]
        if e.footer and e.footer.text:
            parts.append(e.footer.text)
    for c in _walk_components(msg.components):
        content = getattr(c, "content", None)
        if isinstance(content, str):
            parts.append(content)
        placeholder = getattr(c, "placeholder", None)
        if isinstance(placeholder, str):
            parts.append(placeholder)
        for opt in getattr(c, "options", None) or []:
            parts += [str(getattr(opt, "label", "")), str(getattr(opt, "value", ""))]
    return "\n".join(p for p in parts if p).lower()


def msg_is_functional(msg: discord.Message, kind: str) -> bool:
    if PURGE_MARKERS[kind] not in _component_ids(msg.components):
        return False
    return bool(getattr(msg.flags, "components_v2", False))


def msg_looks_like(msg: discord.Message, kind: str) -> bool:
    ids = _component_ids(msg.components)
    blob = message_blob(msg)
    if kind == "panel":
        if any(i.startswith("tsb:panel") for i in ids):
            return True
        return any(h in blob for h in PANEL_HINTS)
    if any(i.startswith("lb:") for i in ids):
        return True
    return "leaderboard" in blob


def _is_dedicated(channel: discord.TextChannel, kind: str) -> bool:
    if kind == "panel":
        return channel.id == BATTLE_PANEL_CHANNEL_ID or channel.name.lower() == BATTLE_PANEL_CHANNEL_NAME
    return channel.id == LEADERBOARD_CHANNEL_ID or channel.name.lower() == LEADERBOARD_CHANNEL_NAME


async def purge_old_messages(channel: discord.TextChannel, kind: str):
    msg_key, chan_key = PURGE_KEYS[kind]
    await delete_previous(channel, msg_key, chan_key)
    wipe_all = _is_dedicated(channel, kind)
    try:
        async for msg in channel.history(limit=200):
            if msg.author.id != bot.user.id:
                continue
            if wipe_all or msg_looks_like(msg, kind):
                try:
                    await msg.delete()
                    leaderboard_state.pop(msg.id, None)
                except discord.HTTPException:
                    pass
    except (discord.Forbidden, discord.HTTPException):
        pass


async def blacklist_gate(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DEVELOPER_ID:
        return True
    entry = DATA["blacklist"].get(str(interaction.user.id))
    if not entry:
        return True
    text = f"You are blacklisted from using {BOT_NAME}.\n**Reason:** {entry.get('reason', 'No reason given')}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass
    return False


class BlacklistGate:
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blacklist_gate(interaction)


_bg_tasks: set = set()
_processed_prefix_messages: dict[int, float] = {}
_PREFIX_DEDUPE_SECONDS = 10


def mark_prefix_message_seen(message_id: int) -> bool:
    now = time.time()
    for mid in [m for m, ts in _processed_prefix_messages.items() if now - ts > _PREFIX_DEDUPE_SECONDS]:
        _processed_prefix_messages.pop(mid, None)
    if message_id in _processed_prefix_messages:
        return False
    _processed_prefix_messages[message_id] = now
    return True


def spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


def _truncate_field(lines: list[str], limit: int = 1000) -> str:
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            out.append(f"...and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) if out else "None"


# ======================================================
# READ ME  (one channel, one card, always named Cruxer)
# ======================================================

def developer_profile_url() -> str:
    return f"https://discord.com/users/{DEVELOPER_ID}"


class ReadmeView(discord.ui.LayoutView):
    def __init__(self, guild_name: str = "your server", authorized: bool = True):
        super().__init__(timeout=None)
        developer_profile = discord.ui.Button(
            label="Developer Profile", style=discord.ButtonStyle.link,
            url=developer_profile_url())
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f"# {BOT_NAME}\n"
                "A custom-built support bot designed for Roblox Battleground experiences. "
                "Cruxer helps players handle unfair battles, report issues, and get the support they need.\n\n"
                "`Developer: @heistz1 | Heistz`"),
            discord.ui.ActionRow(developer_profile)))


def build_readme_view(guild: discord.Guild, authorized: bool):
    return ReadmeView(guild.name, authorized)


async def create_or_update_readme(guild: discord.Guild, authorized: bool, target_channel: discord.TextChannel | None = None,
                                 category: discord.CategoryChannel | None = None):
    """Maintains exactly one read-me channel with exactly one Cruxer message."""
    lock = _readme_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        try:
            channels = [c for c in guild.text_channels if c.name.casefold() == README_CHANNEL_NAME.casefold()]
            mapped_readme_id = DATA.get("channel_map", {}).get(str(guild.id), {}).get("readme", 0)
            mapped_readme = guild.get_channel(mapped_readme_id) if mapped_readme_id else None
            existing = target_channel or (mapped_readme if isinstance(mapped_readme, discord.TextChannel) else None) or (channels[0] if channels else None)
            for duplicate in channels:
                if existing is not None and duplicate.id == existing.id:
                    continue
                try:
                    await duplicate.delete(reason=f"{BOT_NAME}: remove duplicate read-me channel")
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if existing is None:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_channels=True),
                }
                existing = await guild.create_text_channel(
                    name=README_CHANNEL_NAME, overwrites=overwrites,
                    topic=f"Information about {BOT_NAME}", category=category,
                    reason=f"{BOT_NAME} Read-me channel",
                )
            elif category is not None and existing.category_id != category.id:
                try:
                    await existing.edit(category=category, reason=f"{BOT_NAME} channel layout update")
                except discord.HTTPException:
                    pass
            elif LEGACY_BOT_NAME.lower() in (existing.topic or "").lower():
                try:
                    await existing.edit(topic=f"Information about {BOT_NAME}")
                except discord.HTTPException:
                    pass

            # Remove every old bot post (old Imperium-titled ones included) and keep one fresh card.
            view = build_readme_view(guild, authorized)
            keeper = None
            async for msg in existing.history(limit=100):
                if msg.author != bot.user:
                    continue
                if keeper is None and getattr(msg.flags, "components_v2", False):
                    keeper = msg
                else:
                    try:
                        await msg.delete()
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            if keeper:
                try:
                    await keeper.edit(view=view)
                    return existing
                except discord.HTTPException:
                    try:
                        await keeper.delete()
                    except discord.HTTPException:
                        pass
            await existing.send(view=view, allowed_mentions=discord.AllowedMentions.none())
            return existing
        except discord.Forbidden:
            return None
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not create/update Read-me in {guild.name}: {exc}")
            return None


# ======================================================
# ROBLOX LOW-LEVEL HELPERS (cookie requests, presence, region, server scan)
# ======================================================

_roblox_csrf: str | None = None
_presence_warned = False
GAME_NAME_CACHE: dict[int, str] = {}
REGION_CACHE: dict[str, str] = {}
PRESENCE_NAMES = {0: "Offline", 1: "Online (not in a game)", 2: "In a game", 3: "In Roblox Studio"}


def roblox_join_link(place_id, job_id) -> str:
    return f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"


def roblox_game_page_url(place_id) -> str:
    """Open the official Roblox experience page when a specific server ID is unavailable."""
    return f"https://www.roblox.com/games/{place_id}"


async def roblox_json(session: aiohttp.ClientSession, url: str, **kwargs):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), **kwargs) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception:
        pass
    return None


async def roblox_authed_post(session, url: str, payload: dict, extra_headers: dict | None = None):
    global _roblox_csrf
    if not ROBLOX_COOKIE:
        return None, None, {}
    headers = {"Content-Type": "application/json", "Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(3):
        if _roblox_csrf:
            headers["X-CSRF-TOKEN"] = _roblox_csrf
        try:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 403 and resp.headers.get("x-csrf-token") and attempt < 2:
                    _roblox_csrf = resp.headers["x-csrf-token"]
                    continue
                if resp.status == 429 and attempt < 2:
                    await asyncio.sleep(1.5)
                    continue
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = None
                return resp.status, data, dict(resp.headers)
        except Exception as exc:
            print(f"[{BOT_NAME}] Roblox POST {url} failed: {exc}")
            return None, None, {}
    return None, None, {}


def _region_tag(continent: str, lon, country_code: str = ""):
    continent = (continent or "").upper()
    if (country_code or "").upper() in {"AE", "BH", "IQ", "IR", "JO", "KW", "LB", "OM", "PS", "QA", "SA", "SY", "YE", "IL"}:
        return "ME"
    if continent == "NA":
        if lon is None:
            return "NA"
        return "NA @ East" if lon > -100 else "NA @ West"
    return {"EU": "EU", "AS": "AS", "OC": "OCE", "SA": "SA", "AF": "AF"}.get(continent)


def _format_region(data: dict):
    parts = []
    for key in ("city", "region"):
        value = str(data.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    cc = str(data.get("country_code") or "").strip()
    if cc and cc not in parts:
        parts.append(cc)
    place = ", ".join(parts)
    try:
        lon = float(data.get("longitude"))
    except (TypeError, ValueError):
        lon = None
    tag = _region_tag(data.get("continent_code") or "", lon, cc)
    if tag and place:
        return f"{tag} - {place}"
    return tag or place or None


async def geolocate_ip(session: aiohttp.ClientSession, ip: str):
    data = await roblox_json(session, f"https://get.geojs.io/v1/ip/geo/{ip}.json")
    if data:
        region = _format_region(data)
        if region:
            return region
    data = await roblox_json(session, f"https://ipwho.is/{ip}")   # second opinion if the first service fails
    if isinstance(data, dict) and data.get("success", True):
        return _format_region(data)
    return None


async def detect_server_region(session: aiohttp.ClientSession, place_id, job_id):
    """Region of a live Roblox server, e.g. "NA @ East - Atlanta, Georgia, US". None if it cannot be found."""
    if not place_id or not job_id:
        return None
    job_id = str(job_id).lower()
    cache_key = f"{place_id}:{job_id}"
    if cache_key in REGION_CACHE:
        return REGION_CACHE[cache_key]
    for lookup in (_try_gamejoin_region,):
        try:
            region = await lookup(session, place_id, job_id)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            print(f"[{BOT_NAME}] Region lookup via {lookup.__name__} failed: {exc!r}")
            region = None
        if region:
            REGION_CACHE[cache_key] = region
            if len(REGION_CACHE) > 500:
                REGION_CACHE.pop(next(iter(REGION_CACHE)))
            return region
    return None


async def _try_gamejoin_region(session: aiohttp.ClientSession, place_id, job_id):
    if not ROBLOX_COOKIE:
        return None
    try:
        place_int = int(place_id)
    except (TypeError, ValueError):
        return None
    for attempt in range(2):
        status, data, _ = await roblox_authed_post(
            session,
            "https://gamejoin.roblox.com/v1/join-game-instance",
            {
                "placeId": place_int, "isTeleport": False, "gameId": str(job_id),
                "gameJoinAttemptId": str(uuid.uuid4()), "joinOrigin": "PlayButton",
            },
            extra_headers={"User-Agent": "Roblox/WinInet", "Referer": f"https://www.roblox.com/games/{place_id}"},
        )
        if status == 200 and isinstance(data, dict):
            script = data.get("joinScript")
            if isinstance(script, str):
                try:
                    script = json.loads(script)
                except ValueError:
                    script = None
            script = script or {}
            ip = None
            for ep in script.get("UdmuxEndpoints") or []:
                if isinstance(ep, dict) and ep.get("Address"):
                    ip = ep["Address"]
                    break
            ip = ip or script.get("MachineAddress")
            if ip:
                return await geolocate_ip(session, ip)
        if attempt == 0:
            await asyncio.sleep(1.0)
    return None


def presence_in_game(p) -> bool:
    return bool(p) and p.get("userPresenceType") == 2


def presence_sig(p) -> str:
    return f"{p.get('gameId') or 'private'}:{p.get('placeId') or p.get('rootPlaceId') or p.get('lastLocation') or 'unknown'}"


async def roblox_presence(session: aiohttp.ClientSession, user_ids: list[int]):
    global _roblox_csrf, _presence_warned
    if session is None:
        return None
    headers = {"Content-Type": "application/json"}
    if ROBLOX_COOKIE:
        headers["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"
    for attempt in range(3):
        if _roblox_csrf:
            headers["X-CSRF-TOKEN"] = _roblox_csrf
        try:
            async with session.post(
                "https://presence.roblox.com/v1/presence/users",
                json={"userIds": user_ids}, headers=headers, timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 403 and resp.headers.get("x-csrf-token"):
                    _roblox_csrf = resp.headers["x-csrf-token"]
                    continue
                if resp.status == 200:
                    _presence_warned = False
                    data = await resp.json(content_type=None)
                    return {p["userId"]: p for p in data.get("userPresences", []) if "userId" in p}
                if resp.status == 429 or 500 <= resp.status < 600:
                    if attempt < 2:
                        retry_after = resp.headers.get("Retry-After")
                        try:
                            delay = min(float(retry_after), 3.0) if retry_after else 0.8 * (attempt + 1)
                        except (TypeError, ValueError):
                            delay = 0.8 * (attempt + 1)
                        await asyncio.sleep(delay)
                        continue
                if not _presence_warned:
                    _presence_warned = True
                    print(f"[{BOT_NAME}] Roblox presence returned HTTP {resp.status}. Set a valid ROBLOX_COOKIE in your .env.")
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if attempt < 2:
                await asyncio.sleep(0.8 * (attempt + 1))
                continue
            print(f"[{BOT_NAME}] Roblox presence request failed: {exc}")
            return None
        except Exception as exc:
            print(f"[{BOT_NAME}] Roblox presence request failed: {exc}")
            return None
    return None


async def roblox_user_details(session: aiohttp.ClientSession, roblox_id: int):
    data = await roblox_json(session, f"https://users.roblox.com/v1/users/{roblox_id}")
    if data:
        return data.get("displayName"), data.get("name")
    return None, None


async def roblox_avatar_url(session: aiohttp.ClientSession, roblox_id: int):
    data = await roblox_json(
        session, "https://thumbnails.roblox.com/v1/users/avatar-bust",
        params={"userIds": roblox_id, "size": "150x150", "format": "Png", "isCircular": "false"},
    )
    item = ((data or {}).get("data") or [None])[0]
    if item and item.get("state") == "Completed":
        return item.get("imageUrl")
    return None


async def roblox_game_name(session: aiohttp.ClientSession, universe_id, fallback: str) -> str:
    if not universe_id:
        return fallback or "Unknown Game"
    if universe_id in GAME_NAME_CACHE:
        return GAME_NAME_CACHE[universe_id]
    data = await roblox_json(session, "https://games.roblox.com/v1/games", params={"universeIds": universe_id})
    items = (data or {}).get("data") or []
    if items and items[0].get("name"):
        GAME_NAME_CACHE[universe_id] = items[0]["name"]
        return items[0]["name"]
    return fallback or "Unknown Game"


def _thumb_hash(url):
    m = re.search(r"rbxcdn\.com/([0-9a-fA-F]+)/", url or "")
    return m.group(1).lower() if m else None


async def scan_servers_for_player(session: aiohttp.ClientSession, roblox_id: int, place_id, budget: float = SCAN_TIME_BUDGET):
    """Finds a player who is in a PUBLIC server of a game by matching avatar headshots of the players
    in every public server (works even when Roblox hides the player's presence / joins are off).
    Returns (server_dict_or_None, servers_scanned)."""
    data = await roblox_json(
        session, "https://thumbnails.roblox.com/v1/users/avatar-headshot",
        params={"userIds": roblox_id, "size": "150x150", "format": "Png", "isCircular": "false"},
    )
    item = ((data or {}).get("data") or [None])[0] or {}
    target = _thumb_hash(item.get("imageUrl"))
    if not target:
        return None, 0

    start, scanned, cursor, retries = time.time(), 0, None, 0
    while time.time() - start < budget and scanned < SCAN_MAX_SERVERS:
        params = {"limit": 100, "sortOrder": "Desc", "excludeFullGames": "false"}
        if cursor:
            params["cursor"] = cursor
        page = await roblox_json(session, f"https://games.roblox.com/v1/games/{place_id}/servers/Public", params=params)
        if page is None:                      # rate limited or hiccup: wait and retry a few times
            retries += 1
            if retries > 4:
                break
            await asyncio.sleep(2.5)
            continue
        servers = page.get("data") or []
        requests_, owners = [], {}
        for s in servers:
            for tok in s.get("playerTokens") or []:
                rid = str(len(requests_))
                owners[rid] = s
                requests_.append({"requestId": rid, "token": tok, "type": "AvatarHeadshot", "size": "150x150", "format": "png", "isCircular": False})
        scanned += len(servers)
        for i in range(0, len(requests_), 100):
            if time.time() - start > budget:
                break
            try:
                async with session.post("https://thumbnails.roblox.com/v1/batch", json=requests_[i:i + 100],
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(1.5)
                        continue
                    batch = await resp.json(content_type=None)
            except Exception:
                continue
            for entry in (batch or {}).get("data", []):
                if _thumb_hash(entry.get("imageUrl")) == target:
                    s = owners.get(str(entry.get("requestId")))
                    if s:
                        return s, scanned
        cursor = page.get("nextPageCursor")
        if not cursor:
            break
    return None, scanned


# ======================================================
# BATTLE PANEL (Components V2 + Raid / Backup / Help select)
# ======================================================

class ConfigModal(discord.ui.Modal, title="Configuration"):
    def __init__(self, user_id: int):
        super().__init__()
        self.username = discord.ui.TextInput(
            label="Your Roblox username", default=saved_roblox_name(user_id),
            placeholder="Saved and autofilled on future requests", max_length=40,
        )
        self.add_item(self.username)
        self.region = discord.ui.TextInput(
            label="Default server region (optional)",
            default=DATA["configs"].get(str(user_id), {}).get("region"),
            placeholder="e.g. NA, EU, AS, OCE", required=False, max_length=30,
        )
        self.add_item(self.region)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        typed = self.username.value.strip()
        roblox = await get_roblox_profile(interaction.client.session, typed)
        name = roblox[1] if roblox else typed
        DATA["configs"][str(interaction.user.id)] = {"roblox_username": name, "region": self.region.value.strip()}
        save_data()
        if roblox:
            await reply_card(interaction, "## Configuration Saved", f"Roblox username: **{name}**", footer=BOT_NAME)
        else:
            await reply_card(interaction, "## Configuration Saved",
                             f"Saved **{name}**, but I could not find that account on Roblox. Press Configuration again to edit it.",
                             color=YELLOW, footer=BOT_NAME)


def help_view():
    return card(
        "## How to use the panel",
        "**1. Link your Roblox account**\n"
        "- Use `/link` once. Opening a ticket needs a linked account.\n"
        "**2. Fill in the request form**\n"
        "- Choose Raid or Backup, enter your Roblox username, and complete the request details.\n"
        "**3. Make the request**\n"
        "- Choose **Raid** (you need people to raid with you) or **Backup** (you are getting teamed on) from the menu.\n"
        "- After submitting, Cruxer checks whether the entered account is in a joinable server when Roblox verification is enabled.\n"
        "**4. Wait for helpers**\n"
        "- Helpers press Join Raid in your private ticket. Their Roblox profiles are shown in the ticket.\n"
        "**5. End the ticket**\n"
        "- Press End, pick Won / Lost / Voided, add your experience and the gank image, then Finish.",
        SEP,
        "**Good to know**\n"
        f"- You can make {DAILY_REQUEST_LIMIT} requests every 24 hours. Tier roles (earned by assisting raids) give more.\n"
        "- Fake alerts get you blacklisted.\n"
        "- Use `/pings` to choose which alerts you get.",
        footer=f"{BOT_NAME} - this message is only for you",
    )


async def start_request_flow(interaction: discord.Interaction, request_type: str):
    """rate limit -> linked check -> joins/in-game check -> request form."""
    if interaction.guild is None:
        return await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
    if not is_setup(interaction.guild.id):
        return await interaction.response.send_message(
            f"{BOT_NAME} is not authorized in this server yet. Ask the developer to run /authorize.", ephemeral=True)

    if not is_linked(interaction.user.id) and interaction.user.id != DEVELOPER_ID:
        return await interaction.response.send_message(view=LinkRequiredView(request_type), ephemeral=True)

    limit = request_limit_for(interaction.user)
    allowed, count, resets = rate_limit_status(interaction.guild.id, interaction.user.id, limit)
    if not allowed:
        return await interaction.response.send_message(rate_limit_text(count, resets, limit), ephemeral=True)

    # Let the requester complete the form first. The Roblox presence/joinability check
    # runs after they submit it, against the username they entered.
    await interaction.response.send_modal(RequestModal(request_type, interaction.user.id))


class LinkRequiredView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, request_type: str):
        super().__init__(timeout=600)
        self.request_type = request_type
        link = discord.ui.Button(label="Link Account", style=discord.ButtonStyle.success)
        link.callback = self.link_callback
        retry = discord.ui.Button(label="I Have Linked - Continue", style=discord.ButtonStyle.secondary)
        retry.callback = self.retry_callback
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "## Link Your Roblox Account\n"
                "- Only **linked** members can open a ticket or make a request.\n"
                "- Tap **Link Account**, put the code in your Roblox About section and press Verify.\n"
                "- Then tap **I Have Linked - Continue**. I never ask for your password or cookie."),
            discord.ui.ActionRow(link, retry),
            accent_colour=YELLOW,
        ))

    async def link_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LinkModal())

    async def retry_callback(self, interaction: discord.Interaction):
        await start_request_flow(interaction, self.request_type)


class PanelView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)

        config_btn = discord.ui.Button(label="Configuration", style=discord.ButtonStyle.secondary, custom_id="tsb:panel:config")
        config_btn.callback = self.config_callback
        help_btn = discord.ui.Button(label="Help", style=discord.ButtonStyle.primary, custom_id="tsb:panel:help")
        help_btn.callback = self.help_callback

        request_select = discord.ui.Select(
            placeholder="Choose Raid or Backup",
            custom_id="tsb:panel:request",
            options=[
                discord.SelectOption(label="Raid", value="Raid", description="Need people to raid with you"),
                discord.SelectOption(label="Backup", value="Backup", description="Got teamed on, need support"),
            ],
        )

        async def select_callback(interaction: discord.Interaction):
            choice = interaction.data["values"][0]
            await start_request_flow(interaction, choice)
            try:   # put the select back to its placeholder so the same option can be picked again
                await interaction.message.edit(view=PanelView())
            except discord.HTTPException:
                pass

        request_select.callback = select_callback

        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "## What is this\n"
                "- This is a backup panel. This is where you can **request** for **raids**, simply asking "
                "for help when getting teamed on especially in "
                "[**The Strongest Battlegrounds**](https://www.roblox.com/games/10418864827/The-Strongest-Battlegrounds) for now.\n"
                "- Simply choose **Raid** or **Backup** from the menu below and move further."
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## What is the benefit of using this?\n"
                "- Every request gets its own **private ticket**, helpers join with **one click**, and "
                "every result is **recorded** for the leaderboard.\n"
                "- Assisting raids earns **Tier roles** with higher request limits."
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## Rules:\n"
                "- **Link First**: You must `/link` your Roblox account before you can request.\n"
                "- **No Fake Alerts**: Fake alerts will result in a blacklist.\n"
                "- **Active Profile**: Request from the Roblox account you are currently using.\n"
                "- **Assist Others**: Earn rescue ranks by helping other players.\n"
                f"- **Limit**: {DAILY_REQUEST_LIMIT} requests per member every 24 hours. Higher Tier roles get more."
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## Configuration:\n"
                "- Save your details before making a ticket so they are autofilled. "
                "Tap the Configuration button again to edit them."
            ),
            discord.ui.ActionRow(config_btn, help_btn),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## Create rescue ticket now:\n"
                "- Choose **Raid** or **Backup** from the menu below. Not sure how it works? Choose **Help**."
            ),
            discord.ui.ActionRow(request_select),
        ))

    async def config_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfigModal(interaction.user.id))

    async def help_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=help_view(), ephemeral=True)


async def refresh_panel(channel: discord.TextChannel):
    gid = str(channel.guild.id)
    lock = _refresh_locks.setdefault(f"panel:{gid}", asyncio.Lock())
    async with lock:
        await purge_old_messages(channel, "panel")
        new_msg = await channel.send(view=PanelView())
        DATA["panel_messages"][gid] = new_msg.id
        DATA["panel_channels"][gid] = channel.id
        save_data()
        return new_msg


# ======================================================
# REQUEST FORM
# ======================================================

def is_roblox_url(link: str) -> bool:
    return bool(re.match(r"^https?://([a-z0-9-]+\.)?roblox\.com/", link, re.I))


def parse_roblox_link(link: str):
    """(place_id, job_id) found inside a Roblox link; either may be None."""
    link = link or ""
    place = (re.search(r"placeId=(\d+)", link, re.I)
             or re.search(r"roblox\.com/(?:[a-z\-]+/)?games/(?:start/)?(\d+)", link, re.I)
             or re.search(r"experiences/start\?.*?placeId=(\d+)", link, re.I))
    job = (re.search(r"(?:gameInstanceId|serverInstanceId|gameId|jobId)=([0-9a-fA-F\-]{8,})", link, re.I)
           or re.search(r"privateServerLinkCode=([0-9]+)", link, re.I))
    return (place.group(1) if place else None, job.group(1) if job else None)


async def verify_requester(session: aiohttp.ClientSession, roblox_id: int, typed_link: str):
    """(ok, error_message, verified_join_link). With a working ROBLOX_COOKIE the member must be in a joinable
    game and the link is rebuilt from the LIVE server. Without a cookie the pasted roblox.com link is accepted."""
    typed_link = (typed_link or "").strip()

    def fallback(why: str):
        if typed_link and is_roblox_url(typed_link):
            return True, None, typed_link
        return False, f"{why} Paste your Roblox server link in the form and try again.", None

    if not ROBLOX_COOKIE:
        return fallback("Live server verification is not enabled, so I need your server link.")

    presences = await roblox_presence(session, [roblox_id])
    if presences is None:
        return fallback("I could not reach Roblox to check that you are in a game.")

    p = presences.get(roblox_id)
    if not presence_in_game(p):
        return False, "You are not in a Roblox game right now. Join your game first, then make the request.", None

    place_id = p.get("placeId") or p.get("rootPlaceId")
    job_id = p.get("gameId")
    if not place_id or not job_id:
        return False, (
            "You are in a game, but your joins are off (or you are in a private server), so helpers cannot join you. "
            "Set your experience joins to **Everyone** in Roblox privacy settings, then try again."), None

    link_place, link_job = parse_roblox_link(typed_link)
    if link_place and str(link_place) != str(place_id):
        return False, "That server link is for a different game than the one you are in.", None
    if link_job and link_job.lower() != str(job_id).lower():
        return False, "That server link is not the server you are currently in. Copy the link from your current server.", None
    return True, None, roblox_join_link(place_id, job_id)


class DispatchView(discord.ui.LayoutView):
    def __init__(self, ticket: dict, channel: discord.TextChannel, used: int, resets, limit: int = DAILY_REQUEST_LIMIT):
        super().__init__(timeout=None)
        if resets is None:
            usage_text, resets_text = "`Unlimited`", "Never"
        else:
            usage_text = f"`{used}/{limit} used ({max(0, limit - used)} remaining)`"
            resets_text = f"<t:{resets}:R>"
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                f"**{ticket['type']} Request [#{ticket_number(ticket)}] Dispatched Successfully!**\n\n"
                f"- **Ticket Channel:** {channel.mention}\n"
                f"- **Usage Status:** {usage_text}\n"
                f"- **Usage Resets:** {resets_text}\n\n"
                "-# All helpers have been notified. Tap the channel link above to view your live ticket."),
            accent_colour=GREEN,
        ))


class RequestModal(discord.ui.Modal):
    def __init__(self, request_type: str, user_id: int = 0, server_link_default: str | None = None):
        super().__init__(title=f"{request_type} Request")
        self.request_type = request_type
        self.linked = DATA["links"].get(str(user_id))
        self.auto_link = bool(ROBLOX_COOKIE)

        self.region = None
        if not ROBLOX_COOKIE:
            self.region = discord.ui.TextInput(
                label="Server region (empty = auto-detect)",
                default=DATA["configs"].get(str(user_id), {}).get("region") or None,
                placeholder="e.g. NA, EU, AS, OCE - or leave empty", required=False, max_length=30)
            self.add_item(self.region)

        self.reported_players = discord.ui.TextInput(
            label="Reported players / details", style=discord.TextStyle.paragraph,
            placeholder="Names of teamers, what happened, etc.", max_length=500)
        self.add_item(self.reported_players)

        self.clan = None
        if request_type == "Raid":
            self.clan = discord.ui.TextInput(label="Enemy guild / clan", required=False, max_length=60)
            self.add_item(self.clan)

        self.server_link = None
        if not self.auto_link:
            self.server_link = discord.ui.TextInput(
                label="Server link", default=server_link_default,
                placeholder="https://www.roblox.com/games/...", max_length=300)
            self.add_item(self.server_link)

        # Always show the Roblox username so a requester can correct it for this request.
        self.username = discord.ui.TextInput(
            label="Your Roblox username", default=saved_roblox_name(user_id),
            placeholder="Enter the Roblox account you are currently using", max_length=40)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        link = self.server_link.value.strip() if self.server_link else ""
        if self.server_link and not is_roblox_url(link):
            return await interaction.response.send_message("Server link must be a valid roblox.com link. Try again.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        if guild is None:
            return await interaction.followup.send("This can only be used in a server.", ephemeral=True)
        if not is_linked(interaction.user.id) and interaction.user.id != DEVELOPER_ID:
            return await interaction.followup.send("Link your Roblox account with /link first.", ephemeral=True)

        category = find_category(guild)
        if not is_setup(guild.id):
            return await interaction.followup.send(f"{BOT_NAME} is not authorized in this server yet. Run /authorize first.", ephemeral=True)

        for existing in category.text_channels:
            ticket = get_ticket(existing.id)
            if ticket and ticket["status"] == "open" and ticket["requester_id"] == interaction.user.id:
                return await interaction.followup.send(f"You already have an open request: {existing.mention}", ephemeral=True)

        limit = request_limit_for(interaction.user)
        allowed, count, resets = rate_limit_status(guild.id, interaction.user.id, limit)
        if not allowed:
            return await interaction.followup.send(rate_limit_text(count, resets, limit), ephemeral=True)

        session = interaction.client.session
        typed_name = self.username.value.strip().lstrip("@")
        roblox = await get_roblox_profile(session, typed_name)
        if not roblox:
            return await interaction.followup.send(
                f"I could not find a Roblox account named **{typed_name}**. Check the spelling and try again.", ephemeral=True)
        roblox_id, roblox_name = roblox

        ok, error, verified_link = await verify_requester(session, roblox_id, link)
        if not ok:
            return await interaction.followup.send(error, ephemeral=True)
        link = verified_link

        region = self.region.value.strip() if self.region is not None else ""
        if not region:
            place_id, job_id = parse_roblox_link(link)
            region = await detect_server_region(session, place_id, job_id) or "Unknown"

        allowed, count, resets = check_and_increment_rate_limit(guild.id, interaction.user.id, limit)
        if not allowed:
            return await interaction.followup.send(rate_limit_text(count, resets, limit), ephemeral=True)

        (display_name, _), avatar = await asyncio.gather(
            roblox_user_details(session, roblox_id), roblox_avatar_url(session, roblox_id))

        raid_number = next_raid_number(guild.id)
        setup_on = is_setup(guild.id)
        type_number = next_type_number(guild.id, self.request_type) if setup_on else None

        ping_roles = []
        if setup_on:
            global_support = find_named_role(guild, GLOBAL_SUPPORT_ROLE_ID, GLOBAL_SUPPORT_ROLE_NAME)
            if global_support is None:
                global_support = await ensure_role(
                    guild, GLOBAL_SUPPORT_ROLE_ID, GLOBAL_SUPPORT_ROLE_NAME, mentionable=True, lines=[])
            region_role = specific_region_support_role_for(guild, region)
            region_key = regional_ping_key(region)
            if region_role is None and region_key:
                _, region_role_name = REGION_PING_ROLES[region_key]
                region_role = await ensure_role(
                    guild, 0, region_role_name, mentionable=True, lines=[])
            for role in (region_role, global_support):
                if role is not None and role not in ping_roles:
                    ping_roles.append(role)
        ping = " ".join(role.mention for role in ping_roles) or None

        ticket = {
            "guild_id": guild.id, "raid_number": raid_number, "type_number": type_number,
            "type": self.request_type, "requester_id": interaction.user.id,
            "requester_name": interaction.user.name,
            "roblox_username": roblox_name, "roblox_id": roblox_id,
            "roblox_display": display_name, "avatar_url": avatar,
            "region": region, "clan": self.clan.value if self.clan else None,
            "reported_players": self.reported_players.value, "server_link": link,
            "started_at": int(time.time()), "helpers": {}, "helper_order": [],
            "status": "open", "result": None, "experience": None, "gank_url": None, "gank_file": None,
            "ended_at": None, "ended_by": None, "deleted_by": None, "results_url": None,
            "info_message_id": None, "message_count": 0, "edit_btn": True, "v2": True, "ping": ping,
            "no_assist_warned": False, "busy_message_id": None,
        }

        viewer = guild.get_role(VIEWER_ROLE_ID) or guild.default_role
        overwrites = {
            # Tickets are visible to the server, but only the requester and confirmed helpers can chat.
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_channels=True, attach_files=True),
        }
        if VIEWER_ROLE_ID and viewer != guild.default_role:
            overwrites[viewer] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
        try:
            channel = await guild.create_text_channel(
                name=ticket_channel_name(ticket), category=category, overwrites=overwrites,
                topic=f"requester:{interaction.user.id}|raid:{raid_number}")
        except discord.Forbidden as exc:
            print(f"[{BOT_NAME}] Ticket channel creation denied in {guild.name}: HTTP {exc.status}, Discord code {exc.code}: {exc}")
            return await interaction.followup.send(
                f"Discord denied ticket creation (HTTP {exc.status}, code {exc.code}). Give me **Manage Channels** and check the category's channel permissions.",
                ephemeral=True)

        save_ticket(channel.id, ticket)
        ticket_msg = await channel.send(
            view=TicketView(channel.id, ticket),
            allowed_mentions=discord.AllowedMentions(roles=True, users=True))
        ticket["info_message_id"] = ticket_msg.id
        save_ticket(channel.id, ticket)

        await log_ticket_created(guild, ticket, channel, ticket_msg.jump_url)
        await interaction.followup.send(view=DispatchView(ticket, channel, count, resets, limit), ephemeral=True)


# ======================================================
# TICKET INFORMATION CARD (live: the duration keeps counting)
# ======================================================

async def locate_info_message_id(channel: discord.TextChannel, ticket: dict):
    mid = ticket.get("info_message_id")
    if mid:
        return mid
    try:
        async for msg in channel.history(limit=15, oldest_first=True):
            if msg.author.id == bot.user.id and (msg.embeds or msg.components):
                ticket["info_message_id"] = msg.id
                save_ticket(channel.id, ticket)
                return msg.id
    except discord.HTTPException:
        pass
    return None


async def refresh_ticket_message(channel: discord.TextChannel, ticket: dict) -> bool:
    mid = await locate_info_message_id(channel, ticket)
    if not mid:
        return False
    kwargs = {"view": TicketView(channel.id, ticket)}
    legacy = not ticket.get("v2")
    if legacy:   # an old embed-style ticket is converted to the new card once
        kwargs.update(content=None, embeds=[])
    try:
        await channel.get_partial_message(mid).edit(**kwargs)
    except (discord.NotFound, discord.HTTPException):
        return False
    if legacy:
        ticket["v2"] = True
        save_ticket(channel.id, ticket)
    return True


class TicketView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, channel_id: int, ticket: dict | None = None):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        t = ticket or get_ticket(channel_id)
        if not t:
            return
        is_open = t.get("status") == "open"
        now = int(time.time())
        end = t.get("ended_at") or now
        url = roblox_profile_url(t.get("roblox_id"))
        label = ticket_label(t)

        children = []
        if is_open:
            edit_btn = discord.ui.Button(label="Edit", style=discord.ButtonStyle.secondary, custom_id=f"tsb:edit:{channel_id}")
            edit_btn.callback = self.edit_callback
            children.append(discord.ui.Section(discord.ui.TextDisplay(f"# {label}"), accessory=edit_btn))
        else:
            children.append(discord.ui.TextDisplay(f"# {label} - Ended"))
        children.append(discord.ui.Separator())

        name, display = t["roblox_username"], t.get("roblox_display")
        shown = f"{display} (@{name})" if display and display.lower() != name.lower() else f"@{name}"
        who = (f"## [{shown}]({url})" if url else f"## {shown}") + \
              f"\n{t.get('ping') or '**Global Teamers Support**'} will assist. Double-check all details and, if needed, use **Edit** to correct details such as the server region. " \
              "Accurate information helps the backup crew avoid targeting innocent players and handle your request faster."
        if t.get("avatar_url"):
            children.append(discord.ui.Section(discord.ui.TextDisplay(who), accessory=discord.ui.Thumbnail(t["avatar_url"])))
        else:
            children.append(discord.ui.TextDisplay(who))
        children.append(discord.ui.Separator())

        top = f"## Server Region\n> `{_clean(t.get('region'), 80) or 'Unknown'}`"
        if t["type"] == "Raid":
            top += f"\n## Enemy Guild\n> `{_clean(t.get('clan'), 60) or 'None'}`"
        children.append(discord.ui.TextDisplay(top))
        children.append(discord.ui.Separator())
        children.append(discord.ui.TextDisplay(f"## Reported Players\n{box(t.get('reported_players'))}"))
        children.append(discord.ui.Separator())

        session_text = (
            "## Request Info\n"
            f"- **Opened:** <t:{t['started_at']}:R>\n"
            f"- **Duration:** `{format_duration(end - t['started_at'])}`"
        )
        if not is_open and t.get("ended_at"):
            session_text += f"\n- **Ended:** <t:{t['ended_at']}:R>"
        children.append(discord.ui.TextDisplay(session_text))
        children.append(discord.ui.Separator())

        helpers = "\n".join(helper_line(t, uid) for uid in t.get("helper_order", [])) or "No helpers yet."
        children.append(discord.ui.TextDisplay(f"## Helpers ({len(t.get('helper_order', []))})\n{helpers}"))
        children.append(discord.ui.Separator())
        foot = f"Ticket opened by <@{t['requester_id']}> | {name} | {'Open' if is_open else 'Ended'}"
        if t.get("edited_at"):
            foot += " | edited by the requester"
        children.append(discord.ui.TextDisplay(f"-# {foot}"))

        buttons = []
        if is_open:
            join_btn = discord.ui.Button(label="Join Raid", style=discord.ButtonStyle.success, custom_id=f"tsb:join:{channel_id}")
            join_btn.callback = self.join_callback
            buttons.append(join_btn)
        buttons.append(link_btn("Profile", url))
        if is_open:
            end_btn = discord.ui.Button(label="End", style=discord.ButtonStyle.danger, custom_id=f"tsb:end:{channel_id}")
            end_btn.callback = self.end_callback
            buttons.append(end_btn)
        guide_btn = discord.ui.Button(label="Guide", style=discord.ButtonStyle.secondary, custom_id=f"tsb:guide:{channel_id}")
        guide_btn.callback = self.guide_callback
        buttons.append(guide_btn)
        row = [b for b in buttons if b is not None]
        if row:
            children.append(discord.ui.ActionRow(*row))
        self.add_item(discord.ui.Container(*children, accent_colour=GREEN if is_open else EMBED_COLOR))

    async def join_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is not open anymore.", ephemeral=True)
        if interaction.user.id == ticket["requester_id"]:
            return await interaction.response.send_message("This is your own request.", ephemeral=True)
        if str(interaction.user.id) in ticket["helpers"]:
            return await interaction.response.send_message("You already joined this raid.", ephemeral=True)
        await interaction.response.send_modal(JoinRobloxModal(self.channel_id, interaction.user.id))

    async def guide_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=card(
            "## Quick Guide",
            "1. Check the server region and reported player names.\n"
            "2. Press **Join Raid** to register as an assistant, then join the requester in Roblox.\n"
            "3. Use the ticket chat to coordinate and tell the requester when you are ready.\n"
            "4. The requester or staff can end the ticket and record the result. Helpers do not need to link Roblox.",
            footer=BOT_NAME), ephemeral=True)

    async def end_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is not open anymore.", ephemeral=True)
        if interaction.user.id != ticket["requester_id"] and not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only the requester or staff can end this.", ephemeral=True)
        await interaction.response.send_message("Choose the raid result.", view=RaidResultView(self.channel_id))

    async def edit_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is not open anymore.", ephemeral=True)
        if interaction.user.id not in (ticket["requester_id"], DEVELOPER_ID):
            return await interaction.response.send_message("Only the person who made this request can edit it.", ephemeral=True)
        await interaction.response.send_modal(EditRequestModal(self.channel_id, ticket))


class EditRequestModal(discord.ui.Modal, title="Edit Request"):
    def __init__(self, channel_id: int, ticket: dict):
        super().__init__()
        self.channel_id = channel_id
        region_default = ticket["region"] if ticket.get("region") not in (None, "Unknown") else None
        self.region = discord.ui.TextInput(
            label="Server region (leave empty to auto-detect)",
            default=region_default[:30] if region_default else None, required=False, max_length=30)
        self.reported = discord.ui.TextInput(
            label="Reported players / details", style=discord.TextStyle.paragraph,
            default=(ticket.get("reported_players") or "")[:500] or None, max_length=500)
        self.add_item(self.region)
        self.add_item(self.reported)
        self.clan = None
        if ticket["type"] == "Raid":
            self.clan = discord.ui.TextInput(label="Enemy guild / clan", default=(ticket.get("clan") or "")[:60] or None,
                                             required=False, max_length=60)
            self.add_item(self.clan)
        self.server_link = discord.ui.TextInput(
            label="Server link (change it only if you moved)",
            default=(ticket.get("server_link") or "")[:300] or None, max_length=300)
        self.add_item(self.server_link)

    async def on_submit(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is not open anymore.", ephemeral=True)
        if interaction.user.id not in (ticket["requester_id"], DEVELOPER_ID):
            return await interaction.response.send_message("Only the requester can edit this.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        session = interaction.client.session
        changes = []

        link = self.server_link.value.strip()
        if link != ticket["server_link"]:
            if not is_roblox_url(link):
                return await interaction.followup.send("Server link must be a valid roblox.com link.", ephemeral=True)
            if ROBLOX_COOKIE:
                ok, error, verified = await verify_requester(session, ticket["roblox_id"], link)
                if not ok:
                    return await interaction.followup.send(error, ephemeral=True)
                link = verified
            if link != ticket["server_link"]:
                ticket["server_link"] = link
                changes.append("Server link")

        region = self.region.value.strip()
        if not region:
            place_id, job_id = parse_roblox_link(ticket["server_link"])
            region = await detect_server_region(session, place_id, job_id) or ticket.get("region") or "Unknown"
        if region != ticket.get("region"):
            ticket["region"] = region
            changes.append("Region")

        reported = self.reported.value.strip()
        if reported != ticket.get("reported_players"):
            ticket["reported_players"] = reported
            changes.append("Reported players")

        if self.clan is not None:
            clan = self.clan.value.strip() or None
            if clan != ticket.get("clan"):
                ticket["clan"] = clan
                changes.append("Enemy guild")

        if not changes:
            return await interaction.followup.send("Nothing was changed.", ephemeral=True)

        ticket["edited_at"] = int(time.time())
        ticket.setdefault("edit_history", []).append({"ts": ticket["edited_at"], "by": interaction.user.id, "fields": changes})
        save_ticket(self.channel_id, ticket)
        await refresh_ticket_message(interaction.channel, ticket)
        try:
            await interaction.channel.send(
                f"<@{interaction.user.id}> updated the request: **{', '.join(changes)}**.",
                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await interaction.followup.send("Request updated.", ephemeral=True)


class JoinRobloxModal(discord.ui.Modal, title="Join Raid"):
    def __init__(self, channel_id: int, user_id: int = 0):
        super().__init__()
        self.channel_id = channel_id
        self.username = discord.ui.TextInput(label="Your Roblox username", default=saved_roblox_name(user_id), max_length=40)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket or ticket["status"] != "open":
            return await interaction.response.send_message("This ticket is not open anymore.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        typed_name = self.username.value.strip()
        roblox = await get_roblox_profile(interaction.client.session, typed_name)
        roblox_id = roblox[0] if roblox else None
        roblox_name = roblox[1] if roblox else typed_name

        uid = str(interaction.user.id)
        ticket["helpers"][uid] = {
            "username": roblox_name, "roblox_id": roblox_id, "joined_at": int(time.time()),
            "seen_online": False, "warned": False, "final_warned": False,
        }
        if uid not in ticket["helper_order"]:
            ticket["helper_order"].append(uid)
        save_ticket(self.channel_id, ticket)

        channel = interaction.channel
        try:
            await channel.set_permissions(interaction.user, view_channel=True, send_messages=True, read_message_history=True)
        except discord.Forbidden:
            pass
        await refresh_ticket_message(channel, ticket)

        join_view = discord.ui.View(timeout=None)
        place_id, job_id = parse_roblox_link(ticket.get("server_link") or "")
        if place_id:
            join_view.add_item(discord.ui.Button(
                label="Join Server", style=discord.ButtonStyle.link,
                url=roblox_join_link(place_id, job_id) if job_id else roblox_game_page_url(place_id)))
        profile_url = roblox_profile_url(roblox_id)
        if profile_url:
            join_view.add_item(discord.ui.Button(label="Roblox Profile", style=discord.ButtonStyle.link, url=profile_url))
        await channel.send(f"<@{interaction.user.id}> joined the raid.", view=join_view)

        view = discord.ui.View(timeout=None)
        if place_id:
            view.add_item(discord.ui.Button(
                label="Join Server", style=discord.ButtonStyle.link,
                url=roblox_join_link(place_id, job_id) if job_id else roblox_game_page_url(place_id)))
        else:
            view.add_item(discord.ui.Button(label="Open Server", style=discord.ButtonStyle.link, url=ticket["server_link"]))
        await interaction.followup.send("You're in. Choose your Roblox client/account in the join prompt.", view=view, ephemeral=True)


# ======================================================
# END FLOW: RESULT -> EXPERIENCE -> GANK -> FINISH -> DELETE
# ======================================================

class RaidResultView(BlacklistGate, discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        for label, style in (("Won", discord.ButtonStyle.success), ("Lost", discord.ButtonStyle.danger), ("Voided", discord.ButtonStyle.secondary)):
            btn = discord.ui.Button(label=label, style=style)
            btn.callback = self._make_callback(label)
            self.add_item(btn)

    def _make_callback(self, result: str):
        async def callback(interaction: discord.Interaction):
            ticket = get_ticket(self.channel_id)
            if not ticket:
                return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
            ticket["result"] = result
            ticket["ended_by"] = interaction.user.id
            save_ticket(self.channel_id, ticket)
            await interaction.response.send_modal(RaidExperienceModal(self.channel_id))
        return callback


class RaidExperienceModal(discord.ui.Modal, title="Raid Experience"):
    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.experience = discord.ui.TextInput(label="How was the raid experience?", style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.experience)

    async def on_submit(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
        ticket["experience"] = self.experience.value
        save_ticket(self.channel_id, ticket)
        await interaction.response.send_message(
            f"Result recorded: {ticket['result']}. Upload the gank image, then press Finish.",
            view=PostEndView(self.channel_id))


class PostEndView(BlacklistGate, discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        upload_btn = discord.ui.Button(label="Upload Gank", style=discord.ButtonStyle.primary)
        upload_btn.callback = self.upload_callback
        self.add_item(upload_btn)
        finish_btn = discord.ui.Button(label="Finish", style=discord.ButtonStyle.success)
        finish_btn.callback = self.finish_callback
        self.add_item(finish_btn)

    async def upload_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Send the raid image as an attachment in this channel within 2 minutes.", ephemeral=True)

        def check(m: discord.Message):
            return (m.channel.id == interaction.channel.id and m.author.id == interaction.user.id and m.attachments
                    and (m.attachments[0].content_type or "").startswith("image"))

        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            return await interaction.followup.send("No image received. Press Upload Gank to try again.", ephemeral=True)

        ticket = get_ticket(self.channel_id)
        if ticket:
            att = msg.attachments[0]
            # Download it NOW: Discord deletes attachments when the ticket channel is deleted, which is why the
            # image used to show up blank in raid results.
            try:
                raw = await att.read()
                ext = os.path.splitext(att.filename)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    ext = ".png"
                os.makedirs(GANK_DIR, exist_ok=True)
                path = os.path.join(GANK_DIR, f"{ticket['guild_id']}_{ticket['raid_number']}{ext}")
                with open(path, "wb") as f:
                    f.write(raw)
                ticket["gank_file"] = path
                ticket["gank_filename"] = os.path.basename(att.filename) or f"gank{ext}"
            except (discord.HTTPException, OSError, ValueError) as exc:
                print(f"[{BOT_NAME}] Could not keep the gank image: {exc!r}")
            ticket["gank_url"] = att.url
            save_ticket(self.channel_id, ticket)
        await interaction.followup.send("Gank image saved.", ephemeral=True)

    async def finish_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
        if interaction.user.id != ticket["requester_id"] and not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only the requester or staff can finish this.", ephemeral=True)
        if not ticket["result"]:
            return await interaction.response.send_message("Choose a raid result first.", ephemeral=True)
        await interaction.response.defer()
        await finalize_raid(interaction.client, interaction.guild, interaction.channel, ticket)


class ResultView(BlacklistGate, discord.ui.LayoutView):
    """The raid result card (Raiders button in the header, gank image inside the card)."""

    def __init__(self, channel_id: int, ticket: dict, gank_name: str | None = None,
                 preview_url: str | None = None, full_size_url: str | None = None):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        raiders = discord.ui.Button(label="Raiders", style=discord.ButtonStyle.secondary, custom_id=f"tsb:raiders:{channel_id}")
        raiders.callback = self.raiders_callback

        duration = format_duration((ticket.get("ended_at") or int(time.time())) - ticket["started_at"])
        assisted = ", ".join(f"<@{u}>" for u in ticket.get("helper_order", [])) or "None"
        ended_by = f"<@{ticket['ended_by']}>" if ticket.get("ended_by") else "Unknown"
        lines = [
            f"- **Assisted** - {assisted}",
            f"- **Duration** - `{duration}`",
            f"- **Ended At** - <t:{ticket.get('ended_at') or int(time.time())}:F>",
            f"- **Ended By** - {ended_by}",
        ]
        if ticket["type"] == "Raid":
            lines.append(f"- **Enemy Clan** - {_clean(ticket.get('clan'), 60) or 'None'}")
        result = ticket.get("result") or "Voided"
        color = {"Won": GREEN, "Lost": RED}.get(result, EMBED_COLOR)

        children = [
            discord.ui.Section(discord.ui.TextDisplay(f"# {ticket['type']} - `#{ticket_number(ticket)}` Result |"), accessory=raiders),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"> ### `{result}`"),
            discord.ui.Separator(),
            discord.ui.TextDisplay("\n".join(lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"> ## Win Streak - `#{ticket.get('streak', 0)}`"),
        ]
        if ticket.get("experience"):
            children += [discord.ui.Separator(), discord.ui.TextDisplay(f"## Experience\n> {_clean(ticket['experience'], 500)}")]
        preview = preview_url or (f"attachment://{gank_name}" if gank_name else None)
        if preview:
            children += [discord.ui.Separator(), gallery(preview)]
        children += [
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## Check Raiders:\n"
                "-# You can now check raider who was in this raid\n"
                "-# Just by tapping Raiders button above.\n"
                "-# The raid is ended. Those who are still in the server, you can leave."),
        ]
        self.add_item(discord.ui.Container(*children, accent_colour=color))
        if full_size_url:
            self.add_item(discord.ui.ActionRow(link_btn("Open full-size image", full_size_url)))

    async def raiders_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
        if not ticket["helper_order"]:
            return await interaction.response.send_message("No helpers confirmed for this raid.", ephemeral=True)
        lines = [f"- {helper_line(ticket, uid)}" for uid in ticket["helper_order"]]
        await interaction.response.send_message(
            view=card(f"## {ticket_label(ticket)} Raiders - {len(lines)}", "\n".join(lines), footer=BOT_NAME),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def load_gank(ticket: dict, session):
    """(bytes, filename) of the gank image, from the saved copy first, then the old Discord link."""
    path = ticket.get("gank_file")
    try:
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read(), ticket.get("gank_filename") or ("gank" + (os.path.splitext(path)[1] or ".png"))
    except OSError:
        pass
    url = ticket.get("gank_url")
    if url and session is not None:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.read(), ticket.get("gank_filename") or "gank.png"
        except Exception:
            pass
    return None, None


def make_gank_preview(raw: bytes) -> bytes:
    """Create a fixed-size cropped display copy while leaving the original attachment untouched."""
    if Image is None or ImageOps is None:
        print(f"[{BOT_NAME}] Pillow is missing; install requirements.txt to create uniform gank previews.")
        return raw
    try:
        with Image.open(io.BytesIO(raw)) as source:
            preview = ImageOps.fit(source.convert("RGB"), GANK_IMAGE_SIZE,
                                   method=getattr(getattr(Image, "Resampling", Image), "LANCZOS"),
                                   centering=(0.5, 0.5))
            output = io.BytesIO()
            preview.save(output, format="JPEG", quality=90, optimize=True)
            return output.getvalue()
    except Exception as exc:
        print(f"[{BOT_NAME}] Could not create fixed-size gank preview: {exc!r}")
        return raw


async def finalize_raid(bot_client: commands.Bot, guild: discord.Guild, channel: discord.TextChannel, ticket: dict):
    ticket["status"] = "ended"
    ticket["ended_at"] = int(time.time())
    save_ticket(channel.id, ticket)

    helper_ids = [int(uid) for uid in ticket["helper_order"]]
    if helper_ids:
        is_raid = ticket["type"] == "Raid"
        add_raid_credit(guild.id, helper_ids, raid_number=ticket["raid_number"], result=ticket["result"],
                        guild_name=guild.name, is_raid=is_raid)
        if is_raid:
            try:
                await award_tier_roles(guild, helper_ids)
            except Exception as exc:
                print(f"[{BOT_NAME}] Could not update Tier roles: {exc!r}")

    ticket["streak"] = update_win_streak(guild.id, ticket["result"])
    save_ticket(channel.id, ticket)
    await refresh_ticket_message(channel, ticket)   # the ticket card switches to its ended state

    results_channel = configured_channel(guild, "raid_results", RAID_RESULTS_CHANNEL_ID, RAID_RESULTS_CHANNEL_NAME)
    if results_channel:
        data, gname = await load_gank(ticket, bot_client.session)
        mentions = discord.AllowedMentions(users=True)
        results_msg = None
        if data:
            try:
                preview_data = make_gank_preview(data)
                preview_name = "gank-preview.jpg"
                results_msg = await results_channel.send(
                    view=ResultView(channel.id, ticket, preview_name),
                    files=[
                        discord.File(io.BytesIO(preview_data), filename=preview_name),
                        # Keep the original available at full resolution; spoiler hides its inline preview.
                        discord.File(io.BytesIO(data), filename=gname, spoiler=True),
                    ], allowed_mentions=mentions)
                if results_msg.attachments:
                    ticket["gank_preview_url"] = results_msg.attachments[0].url
                    ticket["gank_full_url"] = (results_msg.attachments[1].url
                                               if len(results_msg.attachments) > 1 else results_msg.attachments[0].url)
                    ticket["gank_url"] = ticket["gank_full_url"]
                    await results_msg.edit(view=ResultView(
                        channel.id, ticket, gname,
                        preview_url=ticket["gank_preview_url"], full_size_url=ticket["gank_full_url"]))
                if ticket.get("gank_file"):
                    try:
                        os.remove(ticket["gank_file"])
                    except OSError:
                        pass
                    ticket["gank_file"] = None
            except discord.HTTPException as exc:
                print(f"[{BOT_NAME}] Result with image failed ({exc}); posting it without the image.")
        if results_msg is None:
            results_msg = await results_channel.send(view=ResultView(
                channel.id, ticket, None,
                preview_url=ticket.get("gank_preview_url"),
                full_size_url=ticket.get("gank_full_url") or ticket.get("gank_url")),
                allowed_mentions=mentions)
        ticket["results_url"] = results_msg.jump_url
        save_ticket(channel.id, ticket)

    await log_ticket_closed(guild, ticket)
    await warn_offline_helpers(guild, ticket)
    save_ticket(channel.id, ticket)
    await post_mvps(guild, ticket)

    await channel.send(view=DeleteTicketView(channel.id, ticket))
    spawn(send_backup_to_dev(reason=f"{ticket_label(ticket)} finished"))


async def post_mvps(guild: discord.Guild, ticket: dict):
    mvps_channel = configured_channel(guild, "mvps", MVPS_CHANNEL_ID, MVPS_CHANNEL_NAME)
    if not mvps_channel or not ticket["helper_order"]:
        return
    ended = ticket.get("ended_at") or int(time.time())
    lines, buttons = [], []
    for i, uid in enumerate(ticket["helper_order"][:3], start=1):
        h = ticket["helpers"].get(uid, {})
        spent = format_duration(ended - h.get("joined_at", ended))
        lines.append(f"- **#{i}.** <@{uid}> - `{spent}`")
        buttons.append(link_btn(f"#{i} Roblox Profile", roblox_profile_url(h.get("roblox_id"))))
    notes = _clean(ticket.get("experience"), 300) or "No notes"
    by = f" ~ <@{ticket['ended_by']}>" if ticket.get("ended_by") else ""
    view = card(
        f"# {ticket['type']} `#{ticket_number(ticket)}` MVPs",
        SEP,
        "> ## Top Raiders:\n" + "\n".join(lines),
        SEP,
        f"> ## Extra Notes\n- {notes}{by}",
        buttons=[link_btn("More Info", ticket.get("results_url"))] + buttons,
        footer="First to join this raid - " + BOT_NAME,
    )
    await mvps_channel.send(view=view, allowed_mentions=discord.AllowedMentions(users=True))


# ======================================================
# LOGS (created / closed / deleted / re-opened)
# ======================================================

async def get_logs_channel(guild: discord.Guild):
    return configured_channel(guild, "logs", LOGS_CHANNEL_ID, LOGS_CHANNEL_NAME)


async def _send_log(guild: discord.Guild, view):
    ch = await get_logs_channel(guild)
    if ch:
        try:
            await ch.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not post a log: {exc}")


def _requester_profile_btn(ticket: dict):
    return link_btn("Roblox Profile", roblox_profile_url(ticket.get("roblox_id")))


async def log_ticket_created(guild: discord.Guild, ticket: dict, channel, jump_url: str):
    display = ticket.get("roblox_display") or ticket["roblox_username"]
    lines = [
        f"**Requester Roblox:** `{ticket['roblox_username']} (Display: {display})`",
        f"**Region:** `{_clean(ticket.get('region'), 80)}`",
    ]
    if ticket["type"] == "Raid":
        lines.append(f"**Enemy Guild:** `{_clean(ticket.get('clan'), 60) or 'Not provided'}`")
    view = card(
        f"## {ticket['type']} `#{ticket_number(ticket)}` Ticket Created",
        f"**Ticket Channel:** {channel.mention}\n"
        f"**Opened By:** <@{ticket['requester_id']}> (`@{ticket.get('requester_name', 'unknown')}`)\n"
        f"**Created:** <t:{ticket['started_at']}:F>",
        SEP, "\n".join(lines),
        f"**Description:**\n{box(ticket.get('reported_players'))}",
        buttons=[link_btn("More Info", jump_url), _requester_profile_btn(ticket)],
        footer=BOT_NAME,
    )
    await _send_log(guild, view)


async def log_ticket_closed(guild: discord.Guild, ticket: dict):
    duration = format_duration((ticket.get("ended_at") or int(time.time())) - ticket["started_at"])
    ended_by = f"<@{ticket['ended_by']}>" if ticket.get("ended_by") else "Unknown"
    view = card(
        f"## {ticket_label(ticket)} Closed",
        f"**Result:** `{ticket.get('result') or 'None'}`\n"
        f"**Duration:** `{duration}`\n"
        f"**Helpers:** `{len(ticket.get('helper_order', []))}`\n"
        f"**Ended By:** {ended_by}",
        "-# Check the full result in the raid results channel.",
        buttons=[link_btn("More Info", ticket.get("results_url")), _requester_profile_btn(ticket)],
        footer=BOT_NAME,
    )
    await _send_log(guild, view)


async def log_ticket_deleted(guild: discord.Guild, ticket: dict, channel_id: int):
    await _send_log(guild, ReopenTicketView(channel_id, ticket))


async def log_ticket_reopened(guild: discord.Guild, ticket: dict, by_id: int, new_channel, jump_url: str):
    view = card(
        f"## {ticket_label(ticket)} Re-Opened",
        f"**Ticket Channel:** {new_channel.mention}\n**Re-Opened By:** <@{by_id}>\n**Time:** <t:{int(time.time())}:F>",
        buttons=[link_btn("More Info", jump_url), _requester_profile_btn(ticket)],
        footer=BOT_NAME,
    )
    await _send_log(guild, view)


# ======================================================
# DELETE / REOPEN
# ======================================================

class DeleteTicketView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, channel_id: int, ticket: dict | None = None):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        btn = discord.ui.Button(label="Delete Ticket", style=discord.ButtonStyle.danger, custom_id=f"tsb:delete:{channel_id}")
        btn.callback = self.delete_callback
        text = "## This raid has been recorded\n-# You can delete this ticket now. The developer can re-open it later."
        if ticket and ticket.get("status") == "open":
            text = "## This ticket was re-opened\n-# You can delete it below if needed."
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(text), discord.ui.ActionRow(btn)))

    async def delete_callback(self, interaction: discord.Interaction):
        ticket = get_ticket(self.channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
        if interaction.user.id != ticket["requester_id"] and not interaction.user.guild_permissions.manage_channels:
            return await interaction.response.send_message("Only the requester or staff can delete this.", ephemeral=True)
        ticket["status"] = "deleted"
        ticket["deleted_by"] = interaction.user.id
        save_ticket(self.channel_id, ticket)
        await log_ticket_deleted(interaction.guild, ticket, self.channel_id)
        await interaction.response.send_message("Deleting this ticket in 5 seconds.")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except discord.NotFound:
            pass


class ReopenTicketView(discord.ui.LayoutView):
    """The 'ticket deleted' log card with the developer-only Re-Open button."""

    def __init__(self, channel_id: int, ticket: dict | None = None):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        t = ticket or get_ticket(channel_id) or {}
        btn = discord.ui.Button(label="Re-Open Ticket", style=discord.ButtonStyle.primary, custom_id=f"tsb:reopen:{channel_id}")
        btn.callback = self.reopen_callback
        label = ticket_label(t) if t else "Ticket"
        by = f"<@{t['deleted_by']}>" if t.get("deleted_by") else "Unknown"
        row = [btn, link_btn("More Info", t.get("results_url")), link_btn("Roblox Profile", roblox_profile_url(t.get("roblox_id")))]
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## {label} Deleted\n**Deleted By:** {by}\n**Deleted:** <t:{int(time.time())}:F>"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# {BOT_NAME}"),
            discord.ui.ActionRow(*[b for b in row if b is not None]),
        ))

    async def reopen_callback(self, interaction: discord.Interaction):
        if interaction.user.id != DEVELOPER_ID:
            return await interaction.response.send_message("Only the developer can re-open a ticket.", ephemeral=True)
        ticket = get_ticket(self.channel_id)
        if not ticket:
            return await interaction.response.send_message("Ticket data not found.", ephemeral=True)
        if ticket["status"] != "deleted":
            return await interaction.response.send_message("This ticket is not deleted.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category = find_category(guild)
        viewer = guild.get_role(VIEWER_ROLE_ID) or guild.default_role
        requester = guild.get_member(ticket["requester_id"])
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True, manage_channels=True, attach_files=True),
        }
        if VIEWER_ROLE_ID and viewer != guild.default_role:
            overwrites[viewer] = discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
        if requester:
            overwrites[requester] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        new_channel = await guild.create_text_channel(
            name=ticket_channel_name(ticket), category=category, overwrites=overwrites,
            topic=f"requester:{ticket['requester_id']}|raid:{ticket['raid_number']}")

        ticket["status"] = "open"
        ticket["deleted_by"] = None
        ticket["message_count"] = 0
        ticket["edit_btn"] = True
        ticket["v2"] = True
        ticket["ended_at"] = None       # the live duration counts again
        ticket["started_at"] = ticket["started_at"]
        ticket["result"] = None
        DATA["tickets"].pop(str(self.channel_id), None)
        save_ticket(new_channel.id, ticket)

        info_msg = await new_channel.send(view=TicketView(new_channel.id, ticket), allowed_mentions=discord.AllowedMentions.none())
        ticket["info_message_id"] = info_msg.id
        save_ticket(new_channel.id, ticket)
        await new_channel.send(view=DeleteTicketView(new_channel.id, ticket))
        await log_ticket_reopened(guild, ticket, interaction.user.id, new_channel, info_msg.jump_url)
        await interaction.followup.send(f"Ticket re-opened: {new_channel.mention}", ephemeral=True)


# ======================================================
# AUTHORIZE / DEAUTHORIZE SETUP
# ======================================================

CHANNEL_TYPES = [
    ("battle_panel", "Battle Panel", BATTLE_PANEL_CHANNEL_ID, BATTLE_PANEL_CHANNEL_NAME),
    ("raid_results", "Raid Results", RAID_RESULTS_CHANNEL_ID, RAID_RESULTS_CHANNEL_NAME),
    ("leaderboard", "Leaderboard", LEADERBOARD_CHANNEL_ID, LEADERBOARD_CHANNEL_NAME),
    ("logs", "Logs", LOGS_CHANNEL_ID, LOGS_CHANNEL_NAME),
    ("mvps", "MVPs", MVPS_CHANNEL_ID, MVPS_CHANNEL_NAME),
    ("snipe", "Snipe", SNIPE_CHANNEL_ID, SNIPE_CHANNEL_NAME),
    ("warnings", "Warnings", WARNINGS_CHANNEL_ID, WARNINGS_CHANNEL_NAME),
    ("promotion", "Promotions", PROMOTION_CHANNEL_ID, "promotions"),
    ("readme", "Read-me", 0, README_CHANNEL_NAME),
]


async def get_or_create_categories(guild: discord.Guild) -> dict[str, discord.CategoryChannel]:
    saved_map = DATA.get("category_map", {}).get(str(guild.id), {})
    saved_ticket_id = saved_map.get("tickets", 0) if isinstance(saved_map, dict) else 0
    categories = {
        "cruxer": find_cruxer_category(guild),
        "tickets": find_category(guild),
    }
    for key, name in (("cruxer", CRUXER_CATEGORY_NAME), ("tickets", AUTHORIZE_CATEGORY_NAME)):
        category = categories[key]
        if category is None:
            configured_id = CRUXER_CATEGORY_ID if key == "cruxer" else TICKET_CATEGORY_ID
            category = guild.get_channel(configured_id)
            if not isinstance(category, discord.CategoryChannel):
                category = await guild.create_category(name, reason=f"{BOT_NAME} /build")
        is_custom_ticket_category = (
            key == "tickets" and saved_ticket_id == category.id
            and category.name.casefold() not in {AUTHORIZE_CATEGORY_NAME.casefold(), "crusher tickets"}
        )
        if category.name != name and not is_custom_ticket_category:
            try:
                await category.edit(name=name, reason=f"{BOT_NAME} category layout")
            except discord.HTTPException:
                pass
        categories[key] = category
    return categories


async def get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Compatibility helper: ticket channels belong in Cruxer tickets."""
    return (await get_or_create_categories(guild))["tickets"]


async def run_setup(guild: discord.Guild, selected_keys: set):
    # Channel creation is explicitly requested through /build, after /authorize.
    selected_keys = set(selected_keys) | {"snipe", "readme", "warnings", "promotion"}
    await ensure_guild_roles(guild)
    categories = await get_or_create_categories(guild)
    category = categories["cruxer"]
    DATA.setdefault("category_map", {})[str(guild.id)] = {
        key: value.id for key, value in categories.items()
    }
    created = {}

    for key, label, configured_id, name in CHANNEL_TYPES:
        if key not in selected_keys:
            continue
        if key == "readme":
            created[key] = await create_or_update_readme(guild, authorized=True, category=category)
            continue
        channel = guild.get_channel(configured_id) if configured_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == name, guild.text_channels)
        if channel is None:
            channel = await guild.create_text_channel(name, category=category, reason=f"{BOT_NAME} /build")
        elif channel.category_id != category.id:
            try:
                await channel.edit(category=category, reason=f"{BOT_NAME} /build channel layout")
            except discord.HTTPException:
                pass
        created[key] = channel

    DATA.setdefault("channel_map", {}).setdefault(str(guild.id), {}).update(
        {key: channel.id for key, channel in created.items() if isinstance(channel, discord.TextChannel)})
    save_data()

    if created.get("battle_panel"):
        await refresh_panel(created["battle_panel"])
    if created.get("leaderboard"):
        await refresh_leaderboard(created["leaderboard"])

    try:    # after authorizing, the server layout is stored as a template (see /store and /manage)
        store_template(guild)
    except Exception as exc:
        print(f"[{BOT_NAME}] Could not store the server template: {exc!r}")
    return categories, created


async def run_update(guild: discord.Guild):
    """Create only missing Cruxer layout pieces and place utility channels in Cruxer."""
    await ensure_guild_roles(guild)
    categories = await get_or_create_categories(guild)
    utility_category = categories["cruxer"]
    ticket_category = categories["tickets"]
    DATA.setdefault("category_map", {})[str(guild.id)] = {
        key: value.id for key, value in categories.items()
    }
    created = {}
    mapping = DATA.setdefault("channel_map", {}).setdefault(str(guild.id), {})
    for key, label, configured_id, name in CHANNEL_TYPES:
        if key == "readme":
            existing_id = mapping.get("readme", 0)
            existing = guild.get_channel(existing_id) if existing_id else None
            if not isinstance(existing, discord.TextChannel):
                existing = discord.utils.get(guild.text_channels, name=name)
            channel = await create_or_update_readme(guild, authorized=True, target_channel=existing,
                                                    category=utility_category)
            if channel:
                created[key] = channel
                mapping[key] = channel.id
            continue
        channel = guild.get_channel(mapping.get(key, 0)) if mapping.get(key) else None
        if not isinstance(channel, discord.TextChannel) and configured_id:
            channel = guild.get_channel(configured_id)
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.get(guild.text_channels, name=name)
        target_category = ticket_category if key == "tickets" else utility_category
        if channel is None:
            channel = await guild.create_text_channel(name, category=target_category,
                                                     reason=f"{BOT_NAME} /update")
        elif channel.category_id != target_category.id:
            try:
                await channel.edit(category=target_category, reason=f"{BOT_NAME} /update layout")
            except discord.HTTPException:
                pass
        created[key] = channel
        mapping[key] = channel.id

    save_data()
    if created.get("battle_panel"):
        await refresh_panel(created["battle_panel"])
    if created.get("leaderboard"):
        await refresh_leaderboard(created["leaderboard"])
    try:
        store_template(guild)
    except Exception as exc:
        print(f"[{BOT_NAME}] Could not update the server template: {exc!r}")
    return categories, created


async def find_bot_channels(guild: discord.Guild):
    found = []
    categories = [find_cruxer_category(guild), find_category(guild)]
    for category in categories:
        if category:
            found.extend(category.channels)
    for key, label, configured_id, name in CHANNEL_TYPES:
        channel = guild.get_channel(configured_id) if configured_id else None
        if not isinstance(channel, discord.TextChannel):
            channel = discord.utils.find(
                lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == name, guild.text_channels)
        if channel and channel not in found:
            found.append(channel)
    found.extend(category for category in categories if category)
    return found


def _setup_result_view(guild, categories, created, mode: str):
    if isinstance(categories, dict):
        lines = [f"- **Cruxer category:** {categories['cruxer'].mention}",
                 f"- **Cruxer tickets category:** {categories['tickets'].mention}"]
    else:  # legacy setup view
        lines = [f"- **Cruxer category:** {categories.mention}"]
    for key, label, _, _ in CHANNEL_TYPES:
        if created.get(key):
            lines.append(f"- **{label}:** {created[key].mention}")
    return card(f"## {BOT_NAME} Authorized", f"{mode} setup finished in **{guild.name}**.", "\n".join(lines),
                "-# A server template was stored. Use /manage to get it by DM or restore it.", color=GREEN, footer=BOT_NAME)


class ManualSetupSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=label, value=key) for key, label, _, _ in CHANNEL_TYPES]
        super().__init__(placeholder="Choose which channels to create", min_values=1, max_values=len(options), options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            categories, created = await run_setup(interaction.guild, set(self.values))
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Manual build failed: HTTP {exc.status}, code {exc.code}: {exc}")
            return await interaction.followup.send(
                f"Build failed (HTTP {exc.status}, Discord code {exc.code}). Check Manage Channels, Manage Roles, and channel overwrites.", ephemeral=True)
        await interaction.followup.send(view=_setup_result_view(interaction.guild, categories, created, "Manual"), ephemeral=True)


class ManualSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(ManualSetupSelect())


class AuthorizeConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Auto Setup", style=discord.ButtonStyle.success)
    async def auto_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        categories, created = await run_setup(interaction.guild, {key for key, _, _, _ in CHANNEL_TYPES})
        await interaction.followup.send(view=_setup_result_view(interaction.guild, categories, created, "Auto"), ephemeral=True)

    @discord.ui.button(label="Manual Setup", style=discord.ButtonStyle.secondary)
    async def manual_setup(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Choose which channels to create.", view=ManualSetupView(), ephemeral=True)


# ======================================================
# LEADERBOARD (Components V2)
# ======================================================

leaderboard_state: dict[int, dict] = {}
SCOPE_LABELS = {
    "global": "Global Leaderboard",
    "server": "Server Total Leaderboard",
    "daily": "Server Daily Leaderboard",
}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


async def resolve_username(client: commands.Bot, guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member:
        return member.name
    user = client.get_user(user_id)
    if user:
        return user.name
    try:
        return (await client.fetch_user(user_id)).name
    except discord.HTTPException:
        return "unknown-user"


class LeaderboardView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, *, guild_name: str = "-", scope: str = "global", page: int = 0, total_pages: int = 1,
                 stats_text: str = "-", rankings_text: str = "-", total_members: int = 0, updated_ts: int | None = None):
        super().__init__(timeout=None)
        self.page = page
        updated_ts = updated_ts or int(time.time())

        scope_select = discord.ui.Select(
            custom_id="lb:scope",
            options=[discord.SelectOption(label=label, value=value, default=(value == scope)) for value, label in SCOPE_LABELS.items()],
        )
        scope_select.callback = self.scope_callback

        my_rank_btn = discord.ui.Button(label="My Rank", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="lb:myrank")
        my_rank_btn.callback = self.my_rank_callback
        top_btn = discord.ui.Button(label="Top 10", emoji="📶", style=discord.ButtonStyle.secondary, custom_id="lb:top10")
        top_btn.callback = self.top_callback
        first_btn = discord.ui.Button(label="⏪", style=discord.ButtonStyle.secondary, custom_id="lb:first")
        first_btn.callback = self.first_callback
        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, custom_id="lb:prev")
        prev_btn.callback = self.prev_callback
        page_btn = discord.ui.Button(label=f"{page + 1}/{total_pages}", style=discord.ButtonStyle.secondary, custom_id="lb:page", disabled=True)
        next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary, custom_id="lb:next")
        next_btn.callback = self.next_callback
        last_btn = discord.ui.Button(label="⏩", style=discord.ButtonStyle.secondary, custom_id="lb:last")
        last_btn.callback = self.last_callback

        scope_line = ("**Scope:** *every server* - permanent history, never resets" if scope == "global"
                      else f"**Server:** *{guild_name}*'s server")
        self.add_item(discord.ui.Container(
            discord.ui.ActionRow(scope_select),
            discord.ui.TextDisplay(f"Leaderboard for the number of times you helped.\n\n{scope_line}"),
            discord.ui.Separator(),
            discord.ui.Section(discord.ui.TextDisplay("## 📊 Your Current Stats"), accessory=my_rank_btn),
            discord.ui.TextDisplay(stats_text),
            discord.ui.Separator(),
            discord.ui.Section(discord.ui.TextDisplay("## 🏆 Rankings"), accessory=top_btn),
            discord.ui.TextDisplay(rankings_text),
            discord.ui.Separator(),
            discord.ui.TextDisplay(f"-# Last updated: <t:{updated_ts}:R> • Total members: {total_members:,}"),
            discord.ui.ActionRow(first_btn, prev_btn, page_btn, next_btn, last_btn),
        ))

    async def _update(self, interaction: discord.Interaction, *, scope=None, page=None, delta=0):
        state = leaderboard_state.setdefault(interaction.message.id, {"scope": "global", "page": 0})
        if scope is not None:
            state["scope"] = scope
            state["page"] = 0
        if page is not None:
            state["page"] = page
        state["page"] += delta
        await interaction.response.defer()
        view = await make_leaderboard_view(interaction.client, interaction.guild, state["scope"], state["page"], interaction.user)
        state["page"] = view.page
        await interaction.edit_original_response(view=view)

    async def scope_callback(self, interaction: discord.Interaction):
        await self._update(interaction, scope=interaction.data["values"][0])

    async def my_rank_callback(self, interaction: discord.Interaction):
        await self._update(interaction)

    async def top_callback(self, interaction: discord.Interaction):
        await self._update(interaction, page=0)

    async def first_callback(self, interaction: discord.Interaction):
        await self._update(interaction, page=0)

    async def prev_callback(self, interaction: discord.Interaction):
        await self._update(interaction, delta=-1)

    async def next_callback(self, interaction: discord.Interaction):
        await self._update(interaction, delta=1)

    async def last_callback(self, interaction: discord.Interaction):
        await self._update(interaction, page=10 ** 9)


async def make_leaderboard_view(client: commands.Bot, guild: discord.Guild, scope: str, page: int, viewer=None):
    entries = leaderboard_entries(guild.id, scope)
    total_pages = max(1, math.ceil(len(entries) / LEADERBOARD_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * LEADERBOARD_PAGE_SIZE
    page_entries = entries[start:start + LEADERBOARD_PAGE_SIZE]

    if page_entries:
        rows = []
        for i, (uid, count) in enumerate(page_entries):
            rank = start + i + 1
            name = await resolve_username(client, guild, uid)
            rows.append(f"{MEDALS.get(rank, f'**{rank}.**')} `@{name}`\n-# Assists: `{count:,}`")
        rankings_text = "\n".join(rows)
    else:
        rankings_text = "No raids recorded yet."

    if viewer is None:
        stats_text = "> Press **My Rank** to see your stats."
    else:
        rank, count, total = get_rank(guild.id, viewer.id, scope)
        rank_text = "Unranked" if rank is None else f"#{rank} (Top {(rank / total * 100) if total else 0:.2f}%)"
        stats_text = f"> **User:** `@{viewer.name}`\n> **Rank:** {rank_text}\n> **Assist(s):** {count:,}"

    return LeaderboardView(guild_name=guild.name, scope=scope, page=page, total_pages=total_pages,
                           stats_text=stats_text, rankings_text=rankings_text, total_members=len(entries))


async def refresh_leaderboard(channel: discord.TextChannel, initial_scope: str = "global"):
    gid = str(channel.guild.id)
    lock = _refresh_locks.setdefault(f"lb:{gid}", asyncio.Lock())
    async with lock:
        await purge_old_messages(channel, "leaderboard")
        view = await make_leaderboard_view(bot, channel.guild, initial_scope, 0, None)
        new_msg = await channel.send(view=view)
        leaderboard_state[new_msg.id] = {"scope": initial_scope, "page": 0}
        DATA["leaderboard_messages"][gid] = new_msg.id
        DATA["leaderboard_channels"][gid] = channel.id
        save_data()
        return new_msg


# ======================================================
# BOT
# ======================================================

class CruxerTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await blacklist_gate(interaction)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        await super().on_error(interaction, error)


def register_ticket_views():
    """Re-attach the buttons of every stored ticket (start-up and after an automatic restore)."""
    for channel_id_str, ticket in DATA["tickets"].items():
        channel_id = int(channel_id_str)
        try:
            if ticket["status"] == "open":
                bot.add_view(TicketView(channel_id, ticket))
            elif ticket["status"] == "ended":
                bot.add_view(DeleteTicketView(channel_id, ticket))
            elif ticket["status"] == "deleted":
                bot.add_view(ReopenTicketView(channel_id, ticket))
            if ticket.get("results_url"):   # the Raiders button of the posted result must keep working after a restart
                bot.add_view(ResultView(channel_id, ticket, None))
        except Exception as exc:
            print(f"[{BOT_NAME}] Could not re-attach buttons of ticket {channel_id}: {exc!r}")


async def get_prefix(bot_, message: discord.Message):
    """Mention, then this server's custom prefix (if /customize set one), then the default prefixes."""
    prefixes = list(commands.when_mentioned(bot_, message))
    if message.guild:
        custom = DATA["guild_settings"].get(str(message.guild.id), {}).get("prefix")
        if custom:
            prefixes.append(custom)
    prefixes.extend(PREFIXES)
    return prefixes


class CruxerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=get_prefix, intents=intents, tree_cls=CruxerTree, help_command=None)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.add_view(PanelView())
        self.add_view(ReadmeView())
        self.add_view(LeaderboardView())
        self.add_view(PingsView())
        register_ticket_views()
        duration_updater.start()
        tracker_loop.start()
        join_watch_loop.start()
        no_assist_watch_loop.start()
        backup_loop.start()

    async def close(self):
        try:
            save_data()
            save_global_file()
        except Exception as exc:
            print(f"[{BOT_NAME}] Final save failed: {exc!r}")
        if self.session:
            await self.session.close()
        await super().close()


bot = CruxerBot()


def guild_prefix(guild) -> str | None:
    return DATA["guild_settings"].get(str(guild.id), {}).get("prefix") if guild else None


@bot.check
async def prefix_rules(ctx: commands.Context) -> bool:
    """Blacklist + per-server prefix rules.
    [OWN] prefix commands ONLY answer to the default prefixes (or a mention); everything else answers to the
    server's custom prefix when /customize set one."""
    if str(ctx.author.id) in DATA["blacklist"] and ctx.author.id != DEVELOPER_ID:
        return False
    mention = bool(ctx.prefix) and ctx.prefix.startswith("<@")
    if ctx.command is not None and ctx.command.name in OWN_PREFIX_COMMANDS:
        return mention or ctx.prefix in PREFIXES
    custom = guild_prefix(ctx.guild)
    if custom and ctx.prefix in PREFIXES:
        return False
    return True


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure, commands.CommandOnCooldown)):
        return
    print(f"[{BOT_NAME}] Prefix command error in {getattr(ctx.command, 'name', '?')}: {error!r}")


@tasks.loop(seconds=DURATION_UPDATE_SECONDS)
async def duration_updater():
    """Keeps the live duration of every open ticket counting (the card also shows a Discord live timestamp)."""
    for channel_id_str, ticket in list(DATA["tickets"].items()):
        if ticket["status"] != "open":
            continue
        channel = bot.get_channel(int(channel_id_str))
        if channel is None:
            continue
        try:
            await refresh_ticket_message(channel, ticket)
        except Exception as exc:
            print(f"[{BOT_NAME}] Duration update failed for {channel_id_str}: {exc!r}")
        await asyncio.sleep(0.4)


@duration_updater.before_loop
async def before_duration_updater():
    await bot.wait_until_ready()


@bot.listen("on_message")
async def ticket_busy_watcher(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    ticket = get_ticket(message.channel.id)
    if not ticket or ticket["status"] == "deleted":
        return
    ticket["message_count"] = ticket.get("message_count", 0) + 1
    n = ticket["message_count"]
    if n % TICKET_MESS_THRESHOLD == 0:
        previous_id = ticket.get("busy_message_id")
        if previous_id:
            try:
                old_notice = await message.channel.fetch_message(previous_id)
                await old_notice.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        info_id = await locate_info_message_id(message.channel, ticket)
        view = None
        if info_id:
            url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{info_id}"
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Jump to Information", style=discord.ButtonStyle.link, url=url))
        busy_view = card(
            "## \U0001f4a1 Ah! I see a lot of messages in here.",
            "> Unsatisfied? **More members = Better support.** Invite friends to help.", SEP,
            "- Lost? The main team alert got buried by chat.\n"
            "- Use the button below to jump back to the request information.\n"
            "- Check the request details before joining.",
            buttons=[discord.ui.Button(label="Session Info", style=discord.ButtonStyle.link, url=(
                f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{info_id}" if info_id else message.channel.jump_url))],
            color=0x8B8794, footer=f"{n} messages in this ticket")
        try:
            notice = await message.channel.send(view=busy_view)
            ticket["busy_message_id"] = notice.id
        except discord.HTTPException:
            pass
        save_ticket(message.channel.id, ticket)
    elif n % 10 == 0:
        save_ticket(message.channel.id, ticket)


async def upgrade_open_tickets():
    """Tickets opened before this update are converted to the new card once."""
    for cid, ticket in list(DATA["tickets"].items()):
        if ticket["status"] != "open" or ticket.get("v2"):
            continue
        channel = bot.get_channel(int(cid))
        if channel is not None:
            await refresh_ticket_message(channel, ticket)


# ======================================================
# SHARED HELPERS FOR OWNER / ADMIN COMMANDS
# ======================================================

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id == DEVELOPER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def require_admin(interaction: discord.Interaction) -> bool:
    if is_admin(interaction):
        return True
    await interaction.response.send_message("Administrator permission required.", ephemeral=True)
    return False


async def require_owner(interaction: discord.Interaction, dev_only: bool = False) -> bool:
    allowed = interaction.user.id == DEVELOPER_ID if dev_only else can_use_own(interaction.user.id)
    if allowed:
        return True
    await interaction.response.send_message(f"Only the {BOT_NAME} developer can use this command.", ephemeral=True)
    return False


# ======================================================
# BASIC COMMANDS
# ======================================================

@bot.tree.command(name="setup_panel", description="Post/refresh the raid request panel in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def setup_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_panel(interaction.channel)
    await interaction.followup.send("Panel posted. Old panels were removed.", ephemeral=True)


@bot.tree.command(name="leaderboard", description="Post/refresh the raid leaderboard in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await refresh_leaderboard(interaction.channel)
    await interaction.followup.send("Leaderboard posted.", ephemeral=True)


@bot.tree.command(name="say", description="[OWN] Make Cruxer send a message (optionally as a reply, optionally with a file or GIF)")
@app_commands.describe(message="The message Cruxer should send",
                       reply_to="Reply to a message: paste its message ID or its message link",
                       attachment="Optional image / GIF / file to send with it")
@app_commands.guild_only()
async def say_command(interaction: discord.Interaction, message: str = None, reply_to: str = None,
                      attachment: discord.Attachment = None):
    if not can_use_own(interaction.user.id):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    if not message and not attachment:
        return await interaction.response.send_message("Type a message or add a file / GIF.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    channel = interaction.channel
    target = None
    if reply_to:
        nums = re.findall(r"\d{15,25}", reply_to)
        if not nums:
            return await interaction.followup.send("I could not read that message ID or link.", ephemeral=True)
        if len(nums) >= 3:   # a full message link: .../channels/<server>/<channel>/<message>
            if int(nums[-3]) != interaction.guild_id:
                return await interaction.followup.send("That message link is from another server.", ephemeral=True)
            channel = interaction.guild.get_channel_or_thread(int(nums[-2])) or channel
        try:
            target = await channel.fetch_message(int(nums[-1]))
        except discord.NotFound:
            return await interaction.followup.send("I could not find that message in that channel.", ephemeral=True)
        except discord.Forbidden:
            return await interaction.followup.send("I can't read that channel's messages.", ephemeral=True)

    try:
        file = await attachment.to_file() if attachment else None
        if target:   # a normal Discord reply (it pings the author like any reply)
            await target.reply(content=message, file=file, mention_author=True)
        else:
            await channel.send(content=message, file=file)
        await interaction.followup.send("Reply sent." if target else "Message sent.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to send messages (or attach files) there.", ephemeral=True)
    except discord.HTTPException as exc:
        await interaction.followup.send(f"Discord refused it: `{exc.text}`", ephemeral=True)


@bot.tree.command(name="ping", description="Check Cruxer's latency and connection")
@app_commands.guild_only()
async def ping_command(interaction: discord.Interaction):
    uptime = format_duration(int(time.time()) - BOT_START_TIME)
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        ram_mb = proc.memory_info().rss / (1024 * 1024)
        cpu_text = f"{proc.cpu_percent(interval=None):.1f}%"
    except Exception:
        ram_mb, cpu_text = 0, "Unavailable"
    members = sum(g.member_count or 0 for g in bot.guilds)
    size_mb = os.path.getsize(DATA_FILE) / (1024 * 1024) if os.path.exists(DATA_FILE) else 0
    DATA["usage_metrics"] = {
        "updated_at": int(time.time()),
        "server_count": len(bot.guilds),
        "member_count_across_servers": members,
        "servers": {str(g.id): {"name": g.name, "members": g.member_count or 0} for g in bot.guilds},
    }
    save_data()
    await interaction.response.send_message(view=card(
        f"## 🟢 {BOT_NAME}",
        f"📊 **Latency:** {round(bot.latency * 1000)} ms\n"
        f"⏱️ **Uptime:** {uptime}\n"
        f"🖥️ **Resources**\n　▣ RAM: {ram_mb:.0f} MB\n　⚙️ CPU: {cpu_text}\n"
        f"📈 **Size:** {size_mb:.2f} MB data\n"
        f"🌐 **Servers:** {len(bot.guilds)}\n👥 **Members:** {members:,}",
        footer=BOT_NAME, color=GREEN))


@bot.tree.command(name="nuke", description="Go on. Press it.")
async def nuke_command(interaction: discord.Interaction):
    await interaction.response.send_message("This guy thought he's him lol.")


# ======================================================
# Roles (created as part of /build)
# ======================================================

async def ensure_role(guild: discord.Guild, role_id: int, name: str, *, mentionable: bool, lines: list):
    role = find_named_role(guild, role_id, name)
    if role is not None:
        note = "already exists"
        if role_id and role.id == role_id and role.name != name:
            try:
                await role.edit(name=name, reason=f"{BOT_NAME} role setup")
                note = "renamed to match Cruxer"
            except (discord.Forbidden, discord.HTTPException):
                note = "already exists (could NOT rename - give me Manage Roles)"
        if mentionable and not role.mentionable:
            try:
                await role.edit(mentionable=True, reason=f"{BOT_NAME} /build role setup")
                note = "already exists (made mentionable)"
            except (discord.Forbidden, discord.HTTPException):
                note = "already exists (could NOT make it mentionable - give me Manage Roles)"
        lines.append(f"- {role.mention} - {note}")
        return role
    try:
        role = await guild.create_role(name=name, mentionable=mentionable, reason=f"{BOT_NAME} /build role setup")
        lines.append(f"- {role.mention} - created")
        return role
    except (discord.Forbidden, discord.HTTPException):
        lines.append(f"- **{name}** - could not be created (give me Manage Roles)")
        return None


async def ensure_guild_roles(guild: discord.Guild):
    """Create the ping and Tier roles as part of /build or /update."""
    ping_lines: list = []
    await ensure_role(guild, RAID_PING_ROLE_ID, RAID_PING_ROLE_NAME, mentionable=True, lines=ping_lines)
    await ensure_role(guild, BACKUP_PING_ROLE_ID, BACKUP_PING_ROLE_NAME, mentionable=True, lines=ping_lines)
    await ensure_role(guild, GLOBAL_SUPPORT_ROLE_ID, GLOBAL_SUPPORT_ROLE_NAME, mentionable=True, lines=ping_lines)
    for rid, name in REGION_PING_ROLES.values():
        await ensure_role(guild, rid, name, mentionable=True, lines=ping_lines)
    for rid, name in SPECIFIC_REGION_SUPPORT_ROLES.values():
        await ensure_role(guild, rid, name, mentionable=True, lines=ping_lines)
    tier_lines: list = []
    for tier in sorted(TIER_ROLE_IDS):
        await ensure_role(guild, TIER_ROLE_IDS[tier], f"Tier-{tier}", mentionable=False, lines=tier_lines)

    if guild.id not in DATA["setup_guilds"]:
        DATA["setup_guilds"].append(guild.id)
        save_data()

    promoted = 0
    for uid in list(DATA["stats"]["raid_assists"].get(str(guild.id), {})):
        member = guild.get_member(int(uid))
        if member is not None and await sync_tier_role(guild, member):
            promoted += 1

    save_data()
    return {"ping_roles": ping_lines, "tier_roles": tier_lines, "promoted": promoted}


raid_group = app_commands.Group(name="raid", description="Raid requests", guild_only=True)
backup_group = app_commands.Group(name="backup", description="Backup requests", guild_only=True)


@raid_group.command(name="panel", description="Open the Raid ticket-making form")
async def raid_panel_command(interaction: discord.Interaction):
    await start_request_flow(interaction, "Raid")


@backup_group.command(name="panel", description="Open the Backup ticket-making form")
async def backup_panel_command(interaction: discord.Interaction):
    await start_request_flow(interaction, "Backup")


bot.tree.add_command(raid_group)
bot.tree.add_command(backup_group)


# ======================================================
# .av (prefix command)
# ======================================================

async def resolve_prefix_target(ctx: commands.Context, text: str | None):
    if not text:
        ref = ctx.message.reference
        if ref and isinstance(ref.resolved, discord.Message):
            author = ref.resolved.author
            return (ctx.guild.get_member(author.id) if ctx.guild else None) or author
        return ctx.author
    text = text.strip()
    for converter in (commands.MemberConverter, commands.UserConverter):
        try:
            return await converter().convert(ctx, text)
        except commands.CommandError:
            continue
    if text.isdigit():
        try:
            return await bot.fetch_user(int(text))
        except discord.HTTPException:
            pass
    return None


@bot.command(name="av", aliases=["avatar", "pfp"])
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def av_command(ctx: commands.Context, *, target: str = None):
    """av [user] - avatar (PNG / JPG / WEBP, plus GIF when animated), banner and username of a user."""
    if not mark_prefix_message_seen(ctx.message.id):
        return
    user = await resolve_prefix_target(ctx, target)
    if user is None:
        return await ctx.reply("I could not find that user. Mention them or give their ID.", mention_author=False)
    try:
        full = await bot.fetch_user(user.id)
    except discord.HTTPException:
        full = user
    member = user if isinstance(user, discord.Member) else (ctx.guild.get_member(user.id) if ctx.guild else None)

    shown = (member or full).display_avatar
    formats = ["png", "jpg", "webp"] + (["gif"] if shown.is_animated() else [])
    links = []
    for fmt in formats:
        try:
            links.append(f"[{fmt.upper()}]({shown.replace(size=1024, format=fmt).url})")
        except (ValueError, TypeError):
            continue
    name = (member or full).display_name
    color = (full.accent_colour.value if full.accent_colour else EMBED_COLOR)

    desc = " | ".join(links)
    if member and member.guild_avatar and full.avatar and member.guild_avatar.key != full.avatar.key:
        desc += f"\n**Global avatar:** [Open]({full.avatar.replace(size=1024).url})"

    blocks = [f"## Avatar - {name}\n{desc}", gallery(shown.replace(size=1024).url), SEP,
              f"## Profile - {name}\n**Username:** `{full.name}`"]
    if full.banner:
        blocks.append(gallery(full.banner.replace(size=1024).url))
    buttons = [link_btn("Open Avatar", shown.replace(size=1024).url)]
    if full.banner:
        buttons.append(link_btn("Open Banner", full.banner.replace(size=1024).url))
    await ctx.reply(view=card(*blocks, buttons=buttons, color=color, footer=f"Requested by {ctx.author.display_name}"),
                    mention_author=False, allowed_mentions=discord.AllowedMentions.none())


# ======================================================
# /purge, /member-stats, /add
# ======================================================

@bot.tree.command(name="purge", description="Keep only the latest panel and delete all the older ones")
@app_commands.describe(item="What to clean up (default: Backup Panel)", server_wide="Also clean the other channels of this server")
@app_commands.choices(item=[
    app_commands.Choice(name="Backup Panel", value="panel"),
    app_commands.Choice(name="Leaderboard", value="leaderboard"),
])
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def purge_command(interaction: discord.Interaction, item: app_commands.Choice[str] = None, server_wide: bool = False):
    if not await require_admin(interaction):
        return
    kind = item.value if item else "panel"
    guild = interaction.guild
    if server_wide:
        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history]
        depth = 150
    else:
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Use this in a normal text channel.", ephemeral=True)
        channels, depth = [interaction.channel], 500

    await interaction.response.defer(ephemeral=True)
    candidates = []
    for ch in channels:
        try:
            async for msg in ch.history(limit=depth):
                if msg.author.id == bot.user.id and msg_looks_like(msg, kind):
                    candidates.append(msg)
        except (discord.Forbidden, discord.HTTPException):
            continue

    functional = [m for m in candidates if msg_is_functional(m, kind)]
    keep = max(functional, key=lambda m: m.id) if functional else None
    removed = 0
    for m in candidates:
        if keep is not None and m.id == keep.id:
            continue
        try:
            await m.delete()
            leaderboard_state.pop(m.id, None)
            removed += 1
        except discord.HTTPException:
            continue

    msg_key, chan_key = PURGE_KEYS[kind]
    label = "panel" if kind == "panel" else "leaderboard"
    if keep is None:
        if isinstance(interaction.channel, discord.TextChannel):
            await (refresh_panel if kind == "panel" else refresh_leaderboard)(interaction.channel)
        return await interaction.followup.send(
            f"No working {label} was found, so I removed {removed} old/legacy one(s) and posted a fresh one here.", ephemeral=True)

    DATA[msg_key][str(guild.id)] = keep.id
    DATA[chan_key][str(guild.id)] = keep.channel.id
    save_data()
    await interaction.followup.send(f"Kept the latest {label}: {keep.jump_url}\nRemoved {removed} older one(s).", ephemeral=True)


@bot.tree.command(name="member-stats", description="Check how many raids a member has assisted in")
@app_commands.describe(member="Leave empty to check yourself")
@app_commands.guild_only()
async def member_stats_command(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    count = get_raid_count(interaction.guild.id, target.id, "server")
    rank, _, total = get_rank(interaction.guild.id, target.id, "server")
    rank_text = f"#{rank} of {total}" if rank else "Unranked"
    tier = member_tier(target) if is_setup(interaction.guild.id) else None
    body = f"**Assists (server):** `{count}`\n**Rank:** {rank_text}"
    if tier:
        body += f"\n**Tier:** Tier-{tier} (`{get_raid_only_count(interaction.guild.id, target.id)}` raid assists)"
    await interaction.response.send_message(view=card(f"## {target.display_name}", body, thumb=target.display_avatar.url, footer=BOT_NAME),
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="add", description="Post a fresh backup panel or leaderboard here (deletes the old one)")
@app_commands.describe(item="What should be posted in this channel")
@app_commands.choices(item=[
    app_commands.Choice(name="Backup Panel", value="panel"),
    app_commands.Choice(name="Leaderboard", value="leaderboard"),
])
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def add_command(interaction: discord.Interaction, item: app_commands.Choice[str]):
    if not await require_admin(interaction):
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a normal text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        if item.value == "panel":
            await refresh_panel(interaction.channel)
            await interaction.followup.send("Old backup panels removed. New panel posted here.", ephemeral=True)
        else:
            await refresh_leaderboard(interaction.channel)
            await interaction.followup.send("Old leaderboards removed. New leaderboard posted here.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("I do not have permission to send messages in this channel.", ephemeral=True)


# ======================================================
# /authorize, /deauthorize, /overview
# ======================================================

@bot.tree.command(name="authorize", description="[OWN] Authorize Cruxer in this server; use /build for channels")
@app_commands.guild_only()
async def authorize_command(interaction: discord.Interaction):
    if not can_use_own(interaction.user.id):
        return await interaction.response.send_message(f"Only the {BOT_NAME} developer can use /authorize.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild.id not in DATA["authorized_guilds"]:
        DATA["authorized_guilds"].append(guild.id)
        save_data()
    existing_readme = configured_channel(guild, "readme", 0, README_CHANNEL_NAME)
    if existing_readme:
        await create_or_update_readme(guild, authorized=True, target_channel=existing_readme)
    await interaction.followup.send(
        view=card(f"## {BOT_NAME} Authorized", f"{guild.name} is authorized. Run `/build` as the server owner to create channels or map existing ones.",
                  color=GREEN, footer=BOT_NAME), ephemeral=True)


set_group = app_commands.Group(name="set", description="[OWN] Set a member's raid assist count")


@set_group.command(name="count", description="[OWN] Set a member's assist count and synchronize their tier")
@app_commands.guild_only()
@app_commands.describe(member="Member whose assist count will change", count="New number of raid assists")
async def set_count_command(interaction: discord.Interaction, member: discord.Member,
                            count: app_commands.Range[int, 0, 100000]):
    if not can_use_own(interaction.user.id):
        return await interaction.response.send_message("You are not authorized to use this [OWN] command.", ephemeral=True)
    guild = interaction.guild
    gid, uid = str(guild.id), str(member.id)
    previous = get_raid_only_count(guild.id, member.id)
    previous_tier = member_tier(member)
    DATA["stats"]["raid_assists"].setdefault(gid, {})[uid] = int(count)
    DATA["stats"]["guilds"].setdefault(gid, {})[uid] = int(count)
    save_data()
    earned = tier_for_raids(int(count))
    current_roles = [role for role in (tier_role(guild, t) for t in TIER_ROLE_IDS) if role and role in member.roles]
    target = tier_role(guild, earned) if earned else None
    try:
        if current_roles:
            await member.remove_roles(*[r for r in current_roles if r != target], reason=f"{BOT_NAME} /set count")
        if target and target not in member.roles:
            await member.add_roles(target, reason=f"{BOT_NAME} /set count")
    except (discord.Forbidden, discord.HTTPException) as exc:
        return await interaction.response.send_message(
            f"Count saved as `{count}`, but I could not update the tier role. Check Manage Roles and role order. Error: `{exc}`",
            ephemeral=True)
    promotion_channel = await get_promotion_channel(guild)
    if previous_tier is not None and (earned is None or earned > previous_tier):
        if promotion_channel:
            new_rank = f"Tier-{earned}" if earned is not None else "No Tier"
            await promotion_channel.send(view=card(
                "## Raider Rank Update", "### Demoted\n- Assist count changed by the Cruxer owner.", SEP,
                f"### Member\n- <@{member.id}> - **{member.display_name}**\n"
                f"- Previous rank: **Tier-{previous_tier}**\n- New rank: **{new_rank}**\n"
                f"- Previous raid assists: `{previous}`\n- Current raid assists: `{count}`",
                color=RED, footer=BOT_NAME), allowed_mentions=discord.AllowedMentions(users=True))
    elif earned and (previous_tier is None or earned < previous_tier):
        if promotion_channel:
            nxt = earned - 1 if earned > 1 else None
            next_needed = max(0, TIER_RAIDS_NEEDED[nxt] - count) if nxt else None
            await promotion_channel.send(view=card(
                "## \U0001f4e2 Raider Rank Update", "### Promoted\n- Assist count set by Cruxer owner.", SEP,
                f"### Member\n- <@{member.id}>\n- Previous rescues: `{previous}`\n- Current rescues: `{count}`\n"
                f"- New Rank: {target.mention if target else f'**Tier-{earned}**'}", SEP,
                f"### Next Rank: **Tier-{nxt}**\n- Rescues Needed: `{next_needed}`" if nxt else "### Highest rank reached",
                footer=BOT_NAME), allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    leaderboard_channel = configured_channel(guild, "leaderboard", LEADERBOARD_CHANNEL_ID, LEADERBOARD_CHANNEL_NAME)
    if leaderboard_channel:
        try:
            await refresh_leaderboard(leaderboard_channel, initial_scope="server")
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not refresh leaderboard after /set count: {exc!r}")
    await interaction.response.send_message(f"Set {member.mention}'s raid assist count to `{count}`.", ephemeral=True)


bot.tree.add_command(set_group)


@bot.tree.command(name="build", description="Build Cruxer channels or map existing channels in this server")
@app_commands.guild_only()
@app_commands.choices(mode=[
    app_commands.Choice(name="Auto create channels and category", value="auto"),
    app_commands.Choice(name="Manually choose channels to create", value="manual"),
    app_commands.Choice(name="Choose existing channels", value="choose"),
])
@app_commands.describe(
    mode="Auto creates the Cruxer layout; Manual creates selected channels; Choose maps channels you already made",
    battle_panel="Where members open Raid and Backup requests",
    raid_results="Where completed raid results are posted",
    leaderboard="Where the leaderboard is posted",
    logs="Ticket, setup, and moderation logs",
    mvps="Top helper and MVP announcements",
    snipe="Roblox player server search results",
    warnings="Helper attendance and no-assist warnings",
    promotions="Tier promotion announcements",
    read_me="Cruxer information and developer profile",
    ticket_category="Existing category where new Raid and Backup tickets should be created",
)
async def build_command(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    battle_panel: discord.TextChannel = None,
    raid_results: discord.TextChannel = None,
    leaderboard: discord.TextChannel = None,
    logs: discord.TextChannel = None,
    mvps: discord.TextChannel = None,
    snipe: discord.TextChannel = None,
    warnings: discord.TextChannel = None,
    promotions: discord.TextChannel = None,
    read_me: discord.TextChannel = None,
    ticket_category: discord.CategoryChannel = None,
):
    guild = interaction.guild
    if interaction.user.id not in (guild.owner_id, DEVELOPER_ID):
        return await interaction.response.send_message("Only this server's owner or the Cruxer developer can use `/build`.", ephemeral=True)
    if guild.id not in DATA["authorized_guilds"]:
        return await interaction.response.send_message("This server is not authorized yet. Ask the Cruxer owner to run `/authorize`.", ephemeral=True)
    if mode.value == "manual":
        return await interaction.response.send_message(
            "**Build Cruxer**\nChoose which channels Cruxer should create. Warnings and Promotions are included automatically.\n"
            "Warnings are for helper attendance and no-assist notices. Promotions are for tier upgrades.",
            view=ManualSetupView(), ephemeral=True)
    if mode.value == "auto":
        await interaction.response.defer(ephemeral=True)
        try:
            category, created = await run_setup(guild, {key for key, _, _, _ in CHANNEL_TYPES})
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Automatic build failed: HTTP {exc.status}, code {exc.code}: {exc}")
            return await interaction.followup.send(
                f"Build failed (HTTP {exc.status}, Discord code {exc.code}). Check Manage Channels, Manage Roles, and channel overwrites.", ephemeral=True)
        return await interaction.followup.send(view=_setup_result_view(guild, category, created, "Automatic build"), ephemeral=True)

    await ensure_guild_roles(guild)
    chosen = {
        "battle_panel": battle_panel, "raid_results": raid_results, "leaderboard": leaderboard,
        "logs": logs, "mvps": mvps, "snipe": snipe, "warnings": warnings,
        "promotion": promotions, "readme": read_me,
    }
    mapping = DATA.setdefault("channel_map", {}).setdefault(str(guild.id), {})
    mapping.update({key: channel.id for key, channel in chosen.items() if channel is not None})
    category_map = DATA.setdefault("category_map", {}).setdefault(str(guild.id), {})
    if ticket_category is not None:
        if not isinstance(category_map, dict):
            category_map = {"tickets": int(category_map)} if category_map else {}
            DATA["category_map"][str(guild.id)] = category_map
        category_map["tickets"] = ticket_category.id
    if not any(chosen.values()) and ticket_category is None:
        return await interaction.response.send_message(
            view=card("## Choose existing channels", "Pick at least one channel.\n\n"
                      "You can also select **Ticket Category** in the command options to choose where new tickets are created.\n\n"
                      "**What each channel is for**\n"
                      "Battle Panel: requests. Raid Results: completed raids. Leaderboard: rankings. Logs: ticket events.\n"
                      "MVPs: helper highlights. Snipe: Roblox search results. Warnings: helper attendance. Promotions: tier upgrades. Read-me: Cruxer and developer profile.",
                      footer=BOT_NAME), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    save_data()
    if battle_panel:
        await refresh_panel(battle_panel)
    if leaderboard:
        await refresh_leaderboard(leaderboard)
    if read_me:
        await create_or_update_readme(guild, authorized=True, target_channel=read_me)
    await interaction.followup.send(
        view=card("## Channel mapping saved",
                  "Cruxer will use the selected channels" +
                  (f" and create new tickets under **{ticket_category.name}**." if ticket_category else ".") +
                  " You can move or remap channels later with `/build mode:Choose existing channels`.",
                  color=GREEN, footer=BOT_NAME), ephemeral=True)


@bot.tree.command(name="update", description="Create missing Cruxer categories and channels without duplicating existing ones")
@app_commands.guild_only()
async def update_layout_command(interaction: discord.Interaction):
    guild = interaction.guild
    if interaction.user.id not in (guild.owner_id, DEVELOPER_ID):
        return await interaction.response.send_message(
            "Only this server's owner or the Cruxer developer can use `/update`.", ephemeral=True)
    if guild.id not in DATA["authorized_guilds"]:
        return await interaction.response.send_message(
            "This server is not authorized yet. Ask the Cruxer developer to run `/authorize` first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        categories, channels = await run_update(guild)
    except discord.HTTPException as exc:
        print(f"[{BOT_NAME}] Update failed: HTTP {exc.status}, code {exc.code}: {exc}")
        return await interaction.followup.send(
            f"Update failed (HTTP {exc.status}, Discord code {exc.code}). Check Manage Channels and channel permissions.",
            ephemeral=True)
    await interaction.followup.send(
        view=card("## Cruxer layout updated",
                  "Missing channels were created, existing channels were reused, and utility channels were placed under Cruxer. "
                  "No duplicate channels were intentionally created.", SEP,
                  f"**Cruxer:** {categories['cruxer'].mention}\n"
                  f"**Cruxer tickets:** {categories['tickets'].mention}\n"
                  f"**Channels checked:** `{len(channels)}`",
                  color=GREEN, footer=BOT_NAME), ephemeral=True)


@bot.tree.command(name="deauthorize", description="[OWN] Remove Cruxer's setup from this server and leave")
@app_commands.guild_only()
async def deauthorize_command(interaction: discord.Interaction):
    if interaction.user.id != DEVELOPER_ID:
        return await interaction.response.send_message(f"Only the {BOT_NAME} developer can use /deauthorize.", ephemeral=True)
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    try:
        store_template(guild)   # keep the layout so /manage can rebuild it later
    except Exception:
        pass

    for ch in await find_bot_channels(guild):
        try:
            await ch.delete(reason=f"{BOT_NAME} deauthorized")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

    gid = str(guild.id)
    DATA["authorized_guilds"] = [g for g in DATA["authorized_guilds"] if g != guild.id]
    for key in ("panel_messages", "leaderboard_messages", "panel_channels", "leaderboard_channels"):
        DATA[key].pop(gid, None)
    moved = {k: t for k, t in DATA["trackers"].items() if t.get("guild_id") == guild.id}
    for k in moved:
        DATA["trackers"].pop(k, None)
    if moved:
        DATA["archived_trackers"].setdefault(gid, {}).update(moved)
    for cid, t in list(DATA["tickets"].items()):
        if t.get("guild_id") == guild.id and t["status"] == "open":
            t["status"] = "deleted"
            t["deleted_by"] = interaction.user.id
            save_ticket(int(cid), t)
    save_data()
    save_global_file()
    try:
        await interaction.followup.send(f"{BOT_NAME} has been removed from this server. Leaving now.", ephemeral=True)
    except discord.HTTPException:
        pass
    await guild.leave()


@bot.tree.command(name="overview", description="View and manage Cruxer's setup in this server")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def overview_command(interaction: discord.Interaction):
    guild = interaction.guild
    gid = str(guild.id)
    open_tickets = sum(1 for t in DATA["tickets"].values() if t.get("guild_id") == guild.id and t["status"] == "open")
    body = (
        f"**Authorized:** {'Yes' if guild.id in DATA['authorized_guilds'] else 'No'}\n"
        f"**Bot uptime:** `{format_duration(int(time.time()) - BOT_START_TIME)}`\n"
        f"**Open tickets:** `{open_tickets}`\n"
        f"**Total raids logged:** `{DATA['raid_counters'].get(gid, 0)}`\n"
        f"**Total assists (server):** `{sum(DATA['stats']['guilds'].get(gid, {}).values())}`\n"
        f"**Authorized:** {'Yes' if is_setup(guild.id) else 'No'}\n"
        f"**Template stored:** {'Yes' if gid in DATA['templates'] else 'No (use /store)'}"
    )
    view = discord.ui.LayoutView(timeout=120)

    async def refresh_panel_cb(inter: discord.Interaction):
        ch = guild.get_channel(BATTLE_PANEL_CHANNEL_ID) or discord.utils.get(guild.text_channels, name=BATTLE_PANEL_CHANNEL_NAME)
        if not ch:
            return await inter.response.send_message("Battle panel channel not found.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await refresh_panel(ch)
        await inter.followup.send("Panel refreshed.", ephemeral=True)

    async def refresh_lb_cb(inter: discord.Interaction):
        ch = guild.get_channel(LEADERBOARD_CHANNEL_ID) or discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL_NAME)
        if not ch:
            return await inter.response.send_message("Leaderboard channel not found.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await refresh_leaderboard(ch)
        await inter.followup.send("Leaderboard refreshed.", ephemeral=True)

    b1 = discord.ui.Button(label="Refresh Panel", style=discord.ButtonStyle.secondary)
    b1.callback = refresh_panel_cb
    b2 = discord.ui.Button(label="Refresh Leaderboard", style=discord.ButtonStyle.secondary)
    b2.callback = refresh_lb_cb
    logs_channel = await get_logs_channel(guild)
    row = [b1, b2] + ([link_btn("Open Logs", logs_channel.jump_url)] if logs_channel else [])
    view.add_item(discord.ui.Container(
        discord.ui.TextDisplay(f"## {BOT_NAME} Overview - {guild.name}"), discord.ui.Separator(),
        discord.ui.TextDisplay(body), discord.ui.Separator(), discord.ui.TextDisplay(f"-# {BOT_NAME}"),
        discord.ui.ActionRow(*row)))
    await interaction.response.send_message(view=view, ephemeral=True)


# ======================================================
# /bot stats (owner only, public) - view ANY server + remove the bot from any server
# ======================================================

bot_group = app_commands.Group(name="bot", description="[OWN] Cruxer bot management", guild_only=True)

REQUIRED_PERMS = [
    ("manage_channels", "Manage Channels"), ("manage_roles", "Manage Roles"), ("manage_webhooks", "Manage Webhooks"),
    ("manage_guild", "Manage Server"), ("kick_members", "Kick Members"), ("view_audit_log", "View Audit Log"),
    ("embed_links", "Embed Links"), ("attach_files", "Attach Files"), ("read_message_history", "Read Message History"),
    ("create_instant_invite", "Create Invite"),
]


def build_bot_stats_embed(guild: discord.Guild) -> discord.Embed:
    gid = str(guild.id)
    authorized = guild.id in DATA["authorized_guilds"]
    open_tickets = sum(1 for t in DATA["tickets"].values() if t.get("guild_id") == guild.id and t["status"] == "open")
    perms = guild.me.guild_permissions

    embed = discord.Embed(title=f"{BOT_NAME} - Bot Stats", description=f"Viewing **{discord.utils.escape_markdown(guild.name)}** (`{guild.id}`)", color=EMBED_COLOR)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Authorized", value="Yes" if authorized else "No", inline=True)
    embed.add_field(name="Administrator", value="Yes" if perms.administrator else "No", inline=True)
    embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    missing = [label for attr, label in REQUIRED_PERMS if not (perms.administrator or getattr(perms, attr, False))]
    embed.add_field(name="Bot Permissions", value="All required permissions are granted." if not missing else "**Missing:** " + ", ".join(missing), inline=False)
    role_lines = [role.mention for role in reversed(guild.me.roles) if not role.is_default()]
    embed.add_field(name=f"Bot Roles ({len(role_lines)})", value=_truncate_field(role_lines), inline=False)
    created = int(guild.created_at.timestamp())
    bot_joined = int(guild.me.joined_at.timestamp()) if guild.me.joined_at else None
    embed.add_field(name="Server Info", value=(
        f"Owner: <@{guild.owner_id}> (`{guild.owner_id}`)\nCreated: <t:{created}:R>\n"
        f"Bot joined: {f'<t:{bot_joined}:R>' if bot_joined else 'Unknown'}\nBoost level: `{guild.premium_tier}`"), inline=False)
    embed.add_field(name=f"Server Stats - {guild.name}", value=(
        f"Members: `{guild.member_count or 0:,}`\nText channels: `{len(guild.text_channels)}`\n"
        f"Voice channels: `{len(guild.voice_channels)}`\nCategories: `{len(guild.categories)}`\nRoles: `{len(guild.roles)}`\n"
        f"Open tickets: `{open_tickets}`\nRaids logged: `{DATA['raid_counters'].get(gid, 0)}`\n"
        f"Total assists: `{sum(DATA['stats']['guilds'].get(gid, {}).values())}`"), inline=False)
    embed.add_field(name="Global (all servers)", value=(
        f"Leaderboard members: `{len(DATA['stats']['global']):,}`\nTotal assists ever: `{sum(DATA['stats']['global'].values()):,}`\n"
        f"Raids in history: `{len(DATA['global_history']):,}`"), inline=False)
    server_lines = []
    for g in sorted(bot.guilds, key=lambda x: (x.member_count or 0), reverse=True):
        flag = "Authorized" if g.id in DATA["authorized_guilds"] else "Unauthorized"
        server_lines.append(f"**{discord.utils.escape_markdown(g.name)}** - {g.member_count or 0:,} members - {flag}")
    embed.add_field(name=f"Servers ({len(bot.guilds)})", value=_truncate_field(server_lines), inline=False)
    embed.set_footer(text=f"{BOT_NAME} - Uptime {format_duration(int(time.time()) - BOT_START_TIME)}")
    return embed


class ConfirmLeaveView(discord.ui.View):
    def __init__(self, target_guild_id: int):
        super().__init__(timeout=60)
        self.target_guild_id = target_guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != DEVELOPER_ID:
            await interaction.response.send_message(f"Only the {BOT_NAME} developer can do this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        target = bot.get_guild(self.target_guild_id)
        if target is None:
            return await interaction.response.edit_message(content=f"{BOT_NAME} is not in that server anymore.", view=None)
        name = target.name
        DATA["authorized_guilds"] = [g for g in DATA["authorized_guilds"] if g != target.id]
        save_data()
        await interaction.response.edit_message(content=f"{BOT_NAME} has been removed from **{name}**.", view=None)
        await target.leave()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class BotStatsView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, current_guild_id: int, page: int = 0):
        super().__init__(timeout=900)
        self.current_guild_id = current_guild_id
        self.page = page
        self._build()

    def _build(self):
        self.clear_items()
        guilds = sorted(bot.guilds, key=lambda g: g.name.lower())
        pages = max(1, math.ceil(len(guilds) / self.PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        chunk = guilds[self.page * self.PAGE_SIZE:(self.page + 1) * self.PAGE_SIZE]

        remove_here = discord.ui.Button(label="Remove Bot From Viewed Server", style=discord.ButtonStyle.danger)
        remove_here.callback = self.remove_here_callback
        self.add_item(remove_here)

        if pages > 1:
            prev_btn = discord.ui.Button(label="◀ Servers", style=discord.ButtonStyle.secondary, disabled=self.page <= 0)
            next_btn = discord.ui.Button(label="Servers ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= pages - 1)

            async def prev_cb(interaction: discord.Interaction):
                self.page -= 1
                self._build()
                await interaction.response.edit_message(view=self)

            async def next_cb(interaction: discord.Interaction):
                self.page += 1
                self._build()
                await interaction.response.edit_message(view=self)

            prev_btn.callback, next_btn.callback = prev_cb, next_cb
            self.add_item(prev_btn)
            self.add_item(next_btn)
        if not chunk:
            return

        def options(mark_current: bool):
            return [discord.SelectOption(label=(g.name[:100] or str(g.id)), value=str(g.id),
                                         description=f"{g.member_count or 0:,} members"[:100],
                                         default=bool(mark_current and g.id == self.current_guild_id)) for g in chunk]

        view_select = discord.ui.Select(placeholder=f"View stats of a server (page {self.page + 1}/{pages})", options=options(True))

        async def view_cb(interaction: discord.Interaction):
            target = bot.get_guild(int(view_select.values[0]))
            if target is None:
                return await interaction.response.send_message("The bot is not in that server anymore.", ephemeral=True)
            self.current_guild_id = target.id
            self._build()
            await interaction.response.edit_message(embed=build_bot_stats_embed(target), view=self)

        view_select.callback = view_cb
        self.add_item(view_select)

        remove_select = discord.ui.Select(placeholder="Remove the bot from a server", options=options(False))

        async def remove_cb(interaction: discord.Interaction):
            await self._ask_confirm(interaction, int(remove_select.values[0]))

        remove_select.callback = remove_cb
        self.add_item(remove_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_use_own(interaction.user.id):
            await interaction.response.send_message(f"Only the {BOT_NAME} developer can use these controls.", ephemeral=True)
            return False
        return True

    async def _ask_confirm(self, interaction: discord.Interaction, guild_id: int):
        target = bot.get_guild(guild_id)
        name = target.name if target else str(guild_id)
        await interaction.response.send_message(f"Are you sure you want to remove {BOT_NAME} from **{name}**?",
                                                view=ConfirmLeaveView(guild_id), ephemeral=True)

    async def remove_here_callback(self, interaction: discord.Interaction):
        await self._ask_confirm(interaction, self.current_guild_id)


@bot_group.command(name="stats", description="[OWN] Bot roles, servers, server stats and authorization (any server)")
async def bot_stats_command(interaction: discord.Interaction):
    if not can_use_own(interaction.user.id):
        return await interaction.response.send_message(f"Only the {BOT_NAME} developer can use /bot stats.", ephemeral=True)
    await interaction.response.defer()
    await interaction.followup.send(embed=build_bot_stats_embed(interaction.guild), view=BotStatsView(interaction.guild.id))


bot.tree.add_command(bot_group)


# ======================================================
# /anti-nuke + /nuke-restore
# ======================================================

def serialize_overwrites(channel: discord.abc.GuildChannel):
    out = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        out.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": allow.value, "deny": deny.value})
    return out


def snapshot_channel(ch: discord.abc.GuildChannel):
    snap = {"id": ch.id, "name": ch.name, "position": ch.position,
            "category_id": getattr(ch, "category_id", None), "overwrites": serialize_overwrites(ch)}
    if isinstance(ch, discord.CategoryChannel):
        snap["kind"] = "category"
    elif isinstance(ch, discord.TextChannel):
        snap.update(kind="text", topic=ch.topic, nsfw=ch.nsfw, slowmode_delay=ch.slowmode_delay)
    elif isinstance(ch, discord.StageChannel):
        snap.update(kind="stage")
    elif isinstance(ch, discord.VoiceChannel):
        snap.update(kind="voice", bitrate=ch.bitrate, user_limit=ch.user_limit)
    elif isinstance(ch, discord.ForumChannel):
        snap.update(kind="forum", topic=getattr(ch, "topic", None), nsfw=ch.nsfw, slowmode_delay=ch.slowmode_delay)
    else:
        return None
    return snap


def fix_locked_snapshot(snap_ch: dict, previous, everyone_id: int):
    items = snap_ch["overwrites"]
    entry = next((i for i in items if i["type"] == "role" and i["id"] == everyone_id), None)
    ow = discord.PermissionOverwrite.from_pair(
        discord.Permissions(entry["allow"]) if entry else discord.Permissions.none(),
        discord.Permissions(entry["deny"]) if entry else discord.Permissions.none())
    ow.send_messages = previous
    if entry:
        items.remove(entry)
    if not ow.is_empty():
        allow, deny = ow.pair()
        items.append({"id": everyone_id, "type": "role", "allow": allow.value, "deny": deny.value})


def rebuild_overwrites(guild: discord.Guild, items: list):
    result = {}
    for it in items:
        target = guild.get_role(it["id"]) if it["type"] == "role" else guild.get_member(it["id"])
        if target is None:
            continue
        result[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(it["allow"]), discord.Permissions(it["deny"]))
    return result


async def recreate_channel(guild: discord.Guild, s: dict, id_map: dict):
    overwrites = rebuild_overwrites(guild, s.get("overwrites", []))
    reason = f"{BOT_NAME} /nuke-restore"
    kind = s["kind"]
    if kind == "category":
        return await guild.create_category(s["name"], overwrites=overwrites, reason=reason)
    kwargs = {"overwrites": overwrites, "reason": reason}
    parent = None
    if s.get("category_id"):
        parent = id_map.get(s["category_id"]) or guild.get_channel(s["category_id"])
    if isinstance(parent, discord.CategoryChannel):
        kwargs["category"] = parent
    if kind == "text":
        return await guild.create_text_channel(s["name"], topic=s.get("topic"), nsfw=bool(s.get("nsfw")),
                                               slowmode_delay=s.get("slowmode_delay") or 0, **kwargs)
    if kind == "voice":
        bitrate = min(s.get("bitrate") or 64000, int(guild.bitrate_limit))
        return await guild.create_voice_channel(s["name"], bitrate=bitrate, user_limit=s.get("user_limit") or 0, **kwargs)
    if kind == "stage":
        return await guild.create_stage_channel(s["name"], **kwargs)
    if kind == "forum":
        return await guild.create_forum(s["name"], topic=s.get("topic"), nsfw=bool(s.get("nsfw")),
                                        slowmode_delay=s.get("slowmode_delay") or 0, **kwargs)
    return None


@bot.tree.command(name="anti-nuke", description="Lock the server, remove recent bots/webhooks, revoke invites, and undo recent channel creations")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def antinuke_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("Administrator permission required.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    gid = str(guild.id)
    cutoff = discord.utils.utcnow().timestamp() - ANTINUKE_WINDOW_SECONDS
    protected_channels = {ch.id for ch in await find_bot_channels(guild)}
    snap = DATA["nuke_snapshots"].get(gid) or {
        "started_at": int(time.time()), "by": interaction.user.id,
        "locked": {}, "deleted": [], "invites": [], "kicked_bots": [], "webhooks": []}
    results = []

    locked = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            if overwrite.send_messages is not False:
                previous = overwrite.send_messages
                overwrite.send_messages = False
                await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=f"{BOT_NAME} /anti-nuke")
                snap["locked"].setdefault(str(channel.id), previous)
                locked += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    results.append(f"- Locked {locked} text channel(s).")

    kicked = 0
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=25):
            if entry.created_at.timestamp() < cutoff:
                continue
            member = guild.get_member(entry.target.id) if entry.target else None
            if member:
                try:
                    await member.kick(reason=f"{BOT_NAME} /anti-nuke - recently added bot")
                    snap["kicked_bots"].append({"id": member.id, "name": member.name})
                    kicked += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
    except discord.Forbidden:
        results.append("- Missing View Audit Log permission - could not check for recently added bots.")
    else:
        results.append(f"- Removed {kicked} recently added bot(s).")

    removed_webhooks = 0
    try:
        for wh in await guild.webhooks():
            if wh.created_at and wh.created_at.timestamp() >= cutoff:
                try:
                    info = {"name": wh.name, "channel_id": wh.channel_id}
                    await wh.delete(reason=f"{BOT_NAME} /anti-nuke")
                    snap["webhooks"].append(info)
                    removed_webhooks += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
    except discord.Forbidden:
        results.append("- Missing Manage Webhooks permission - could not check webhooks.")
    else:
        results.append(f"- Removed {removed_webhooks} recently created webhook(s).")

    revoked_invites = 0
    try:
        for invite in await guild.invites():
            try:
                info = {"channel_id": invite.channel.id if invite.channel else None, "max_age": invite.max_age or 0,
                        "max_uses": invite.max_uses or 0, "temporary": bool(invite.temporary), "code": invite.code}
                await invite.delete(reason=f"{BOT_NAME} /anti-nuke")
                snap["invites"].append(info)
                revoked_invites += 1
            except (discord.Forbidden, discord.HTTPException):
                continue
    except discord.Forbidden:
        results.append("- Missing Manage Guild permission - could not revoke invites.")
    else:
        results.append(f"- Revoked {revoked_invites} invite(s).")

    deleted_channels = 0
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_create, limit=25):
            if entry.created_at.timestamp() < cutoff:
                continue
            channel = guild.get_channel(entry.target.id) if entry.target else None
            if channel and channel.id not in protected_channels:
                try:
                    snap_ch = snapshot_channel(channel)
                    if snap_ch is not None:
                        prev = snap["locked"].get(str(channel.id), "missing")
                        if prev != "missing":
                            fix_locked_snapshot(snap_ch, prev, guild.default_role.id)
                    await channel.delete(reason=f"{BOT_NAME} /anti-nuke - recently created channel")
                    if snap_ch is not None:
                        snap["deleted"].append(snap_ch)
                    deleted_channels += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
    except discord.Forbidden:
        results.append("- Missing View Audit Log permission - could not check for recently created channels.")
    else:
        results.append(f"- Deleted {deleted_channels} recently created channel(s).")

    if snap["locked"] or snap["deleted"] or snap["invites"] or snap["kicked_bots"] or snap["webhooks"]:
        DATA["nuke_snapshots"][gid] = snap
        save_data()
        results.append("\nEverything was recorded. Run **/nuke-restore** to put it back.")
    await interaction.followup.send(view=card("## Anti-Nuke Sweep Complete", "\n".join(results), footer=BOT_NAME), ephemeral=True)


@bot.tree.command(name="nuke-restore", description="Undo the last /anti-nuke: unlock channels, recreate deleted channels and invites")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def nuke_restore_command(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    guild = interaction.guild
    gid = str(guild.id)
    snap = DATA["nuke_snapshots"].get(gid)
    if not snap:
        return await interaction.response.send_message("There is nothing to restore. Nothing was recorded by /anti-nuke in this server.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    lines = []

    unlocked = failed_unlock = 0
    for cid, previous in snap["locked"].items():
        channel = guild.get_channel(int(cid))
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = previous
            await channel.set_permissions(guild.default_role, overwrite=None if overwrite.is_empty() else overwrite,
                                          reason=f"{BOT_NAME} /nuke-restore")
            unlocked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed_unlock += 1
    lines.append(f"**Unlocked:** {unlocked} channel(s)" + (f" ({failed_unlock} failed)" if failed_unlock else "") + ".")

    id_map: dict[int, discord.abc.GuildChannel] = {}
    created, failed = [], []
    ordered = sorted(snap["deleted"], key=lambda s: (0 if s["kind"] == "category" else 1, s.get("position", 0)))
    for s in ordered:
        try:
            new = await recreate_channel(guild, s, id_map)
        except (discord.Forbidden, discord.HTTPException):
            new = None
        if new is None:
            failed.append(s["name"])
            continue
        id_map[s["id"]] = new
        created.append(new)
    for s in ordered:
        new = id_map.get(s["id"])
        if new is not None:
            try:
                await new.edit(position=s.get("position", 0), reason=f"{BOT_NAME} /nuke-restore")
            except (discord.Forbidden, discord.HTTPException):
                pass
    if snap["deleted"]:
        lines.append(f"**Recreated channels ({len(created)}/{len(snap['deleted'])}):** " + (_truncate_field([c.mention for c in created], 600) if created else "none"))
        if failed:
            lines.append("**Could not recreate:** " + ", ".join(f"`{n}`" for n in failed))
        lines.append("-# Channel settings, permissions and positions are restored. Messages cannot be - Discord does not keep them.")

    new_links = []
    for inv in snap["invites"]:
        channel = guild.get_channel(inv["channel_id"]) if inv.get("channel_id") else None
        channel = channel or id_map.get(inv.get("channel_id"))
        if channel is None or not hasattr(channel, "create_invite"):
            continue
        try:
            new_inv = await channel.create_invite(max_age=inv.get("max_age", 0), max_uses=inv.get("max_uses", 0),
                                                  temporary=inv.get("temporary", False), reason=f"{BOT_NAME} /nuke-restore")
            new_links.append(new_inv.url)
        except (discord.Forbidden, discord.HTTPException):
            continue
    if snap["invites"]:
        lines.append(f"**Recreated invites:** {len(new_links)}/{len(snap['invites'])} (new codes)")
        if new_links:
            lines.append(_truncate_field(new_links, 500))
    if snap["kicked_bots"]:
        lines.append("**Kicked bots (re-invite them yourself):** " + ", ".join(f"{b['name']} (`{b['id']}`)" for b in snap["kicked_bots"]))
    if snap["webhooks"]:
        lines.append("**Deleted webhooks (their URLs cannot come back):** " + ", ".join(f"`{w['name']}`" for w in snap["webhooks"]))

    DATA["nuke_snapshots"].pop(gid, None)
    save_data()
    await interaction.followup.send(view=card("## Nuke Restore Complete", "\n".join(lines), footer=BOT_NAME, color=GREEN), ephemeral=True)


# ======================================================
# SERVER RECOVERY: /bulk unban
# ======================================================

bulk_group = app_commands.Group(name="bulk", description="Server recovery tools")


@bulk_group.command(name="unban", description="Unban members banned by a specified bot during a time window")
@app_commands.describe(bot_id="Discord user ID of the bot that performed the bans",
                       hours="How far back to check (1-720 hours; Discord audit logs have limited retention)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def bulk_unban_command(interaction: discord.Interaction, bot_id: str, hours: app_commands.Range[int, 1, 720] = 24):
    if not await require_admin(interaction):
        return
    guild = interaction.guild
    try:
        executor_id = int(bot_id.strip())
        if executor_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return await interaction.response.send_message("Enter the numeric Discord user ID of the bot that made the bans.", ephemeral=True)
    me = guild.me
    if me is None or not me.guild_permissions.ban_members or not me.guild_permissions.view_audit_log:
        return await interaction.response.send_message("I need **Ban Members** and **View Audit Log** permissions to do this.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(hours))
    targets = set()
    try:
        async for entry in guild.audit_logs(limit=None, after=cutoff, action=discord.AuditLogAction.ban):
            executor, target = getattr(entry, "user", None), getattr(entry, "target", None)
            if executor is not None and executor.id == executor_id and target is not None and getattr(target, "id", None) is not None:
                targets.add(int(target.id))
    except discord.Forbidden:
        return await interaction.followup.send("Discord denied access to the audit log. Check my **View Audit Log** permission.", ephemeral=True)
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Could not read the audit log: {exc}", ephemeral=True)
    if not targets:
        return await interaction.followup.send(
            f"No ban audit entries by bot ID `{executor_id}` were found in the last **{hours} hour(s)**. "
            "Check the ID and time window; Discord may no longer retain older audit entries.", ephemeral=True)

    currently_banned = {}
    try:
        async for ban_entry in guild.bans(limit=None):
            if ban_entry.user.id in targets:
                currently_banned[ban_entry.user.id] = ban_entry.user
    except discord.Forbidden:
        return await interaction.followup.send("Discord denied access to the ban list. Check my **Ban Members** permission.", ephemeral=True)
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Could not read the ban list: {exc}", ephemeral=True)

    unbanned, failed = [], []
    for user_id, user in currently_banned.items():
        try:
            await guild.unban(user, reason=f"Server recovery: ban was made by bot {executor_id} within {hours} hour(s)")
            unbanned.append(user_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            failed.append((user_id, str(exc)[:120]))

    body = (f"**Banned by bot:** `{executor_id}`\n**Time window:** last {hours} hour(s)\n"
            f"**Matching ban records:** {len(targets)}\n**Unbanned:** {len(unbanned)}\n**Failed:** {len(failed)}")
    if failed:
        body += "\n**Failed user IDs:** " + ", ".join(f"`{uid}`" for uid, _ in failed[:15])
    await interaction.followup.send(view=card("## Bulk Unban - Server Recovery", body,
                                              footer="Only bans attributed to the specified bot in Discord's audit log were considered."),
                                    ephemeral=True)


bot.tree.add_command(bulk_group)


# ======================================================
# ROBLOX PROFILE CARD BLOCKS (used by /whois, /snipe, /hitlist add)
# ======================================================

def iso_to_ts(value: str):
    try:
        value = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def linked_discord_for_roblox(roblox_id: int, exclude: int | None = None):
    for uid, link in DATA["links"].items():
        if link.get("roblox_id") == roblox_id and (exclude is None or int(uid) != exclude):
            return int(uid)
    return None


async def presence_to_info(presence: dict, with_region: bool = False) -> dict:
    place_id = presence.get("placeId") or presence.get("rootPlaceId")
    job_id = presence.get("gameId")
    game = await roblox_game_name(bot.session, presence.get("universeId"), presence.get("lastLocation"))
    info = {"game": game, "place_id": place_id, "job_id": job_id, "private": not (job_id and place_id), "since": int(time.time())}
    if with_region and place_id and job_id:
        info["region"] = await detect_server_region(bot.session, place_id, job_id)
    return info


async def resolve_roblox_for_member(session: aiohttp.ClientSession, member: discord.abc.User):
    link = DATA["links"].get(str(member.id))
    if link:
        return link["roblox_id"], link["roblox_username"]
    name = saved_roblox_name(member.id)
    if not name:
        mine = [t for t in DATA["ticket_archive"].values() if t.get("requester_id") == member.id and t.get("roblox_username")]
        mine.sort(key=lambda t: t.get("started_at", 0), reverse=True)
        name = mine[0]["roblox_username"] if mine else None
    found = await get_roblox_profile(session, name) if name else None
    return found if found else (None, None)


async def roblox_profile_blocks(session: aiohttp.ClientSession, roblox_id: int, roblox_name: str):
    """The whole Roblox profile as card blocks. Returns (blocks, avatar_url, profile_url)."""
    user_json, friends, followers, followings, history, groups, avatar, presences = await asyncio.gather(
        roblox_json(session, f"https://users.roblox.com/v1/users/{roblox_id}"),
        roblox_json(session, f"https://friends.roblox.com/v1/users/{roblox_id}/friends/count"),
        roblox_json(session, f"https://friends.roblox.com/v1/users/{roblox_id}/followers/count"),
        roblox_json(session, f"https://friends.roblox.com/v1/users/{roblox_id}/followings/count"),
        roblox_json(session, f"https://users.roblox.com/v1/users/{roblox_id}/username-history", params={"limit": 10}),
        roblox_json(session, f"https://groups.roblox.com/v2/users/{roblox_id}/groups/roles"),
        roblox_avatar_url(session, roblox_id),
        roblox_presence(session, [roblox_id]),
    )
    user_json = user_json or {}
    created = iso_to_ts(user_json.get("created") or "")
    url = roblox_profile_url(roblox_id)
    created_text = f"<t:{created}:F> (<t:{created}:R>)" if created else "Unknown"
    name = user_json.get("name", roblox_name)

    profile = (
        f"## Roblox - {name}\n"
        f"**Username:** `{name}`\n**Display name:** `{user_json.get('displayName', 'Unknown')}`\n"
        f"**User ID:** `{roblox_id}`\n**Created:** {created_text}\n"
        f"**Verified badge:** {'Yes' if user_json.get('hasVerifiedBadge') else 'No'}\n"
        f"**Banned:** {'Yes' if user_json.get('isBanned') else 'No'}\n**Profile:** [Open Profile]({url})")
    social = (
        f"**Friends:** {(friends or {}).get('count', '?')} | **Followers:** {(followers or {}).get('count', '?')} | "
        f"**Following:** {(followings or {}).get('count', '?')} | **Groups:** {len((groups or {}).get('data', [])) if groups else '?'}")

    p = (presences or {}).get(roblox_id)
    if presences is None:
        status_text, joins_text = "Unknown (set a valid ROBLOX_COOKIE to see this)", "Unknown"
    else:
        p = p or {}
        ptype = p.get("userPresenceType", 0)
        status_text = PRESENCE_NAMES.get(ptype, "Offline")
        if ptype == 2:
            game = await roblox_game_name(session, p.get("universeId"), p.get("lastLocation"))
            status_text += f" - {game}"
            joins_text = "Everyone / allowed (joinable server)" if p.get("gameId") else "Off or private (no join link available)"
        else:
            joins_text = "Only visible while the player is in a game"
    last = iso_to_ts((p or {}).get("lastOnline") or "")
    status = f"**Now:** {status_text}\n**Last online:** {f'<t:{last}:R>' if last else 'Unknown'}\n**Joins:** {joins_text}"

    extra = []
    old_names = [h["name"] for h in (history or {}).get("data", [])]
    if old_names:
        extra.append(f"**Previous usernames:** {_clean(', '.join(old_names), 250)}")
    about = _clean(user_json.get("description", ""), 300)
    extra.append(f"**About:**\n{box(about or 'No description')}")
    linked_to = linked_discord_for_roblox(roblox_id)
    extra.append(f"**Linked Discord:** {f'<@{linked_to}>' if linked_to else 'Not linked to any Discord account'}")
    return [profile, social, status, "\n".join(extra)], avatar, url


# ======================================================
# TRACKING CARDS  (hitlist alerts and /snipe results)
# ======================================================

def tracker_label(t: dict) -> str:
    display, username = t.get("display_name"), t["username"]
    if display and display.lower() != username.lower():
        return f"{display} (@{username})"
    return f"@{username}"


def track_buttons(t: dict, info: dict, state: str):
    if state != "found":
        return [discord.ui.Button(label="Target Left Server", style=discord.ButtonStyle.secondary, disabled=True, custom_id="tsb:left")] \
            if t["kind"] == "hitlist" else []
    place_id = info.get("place_id")
    row = []
    if place_id and info.get("job_id") and not info.get("private"):
        row.append(link_btn("Join Server", roblox_join_link(place_id, info["job_id"])))
    elif place_id:
        row.append(link_btn("Open Game Page", f"https://www.roblox.com/games/{place_id}"))
    row.append(link_btn("Open Profile", roblox_profile_url(t["roblox_id"])))
    return row


def build_track_view(t: dict, info: dict, state: str, mention: str | None = None):
    hit = t["kind"] == "hitlist"
    found = state == "found"
    private = info.get("private", False)
    if not found:
        color, title = RED, ("TARGET DISCONNECTED" if hit else "PLAYER LEFT THE GAME")
    elif private:
        color, title = YELLOW, ("TARGET FOUND [Private / Join-Off]" if hit else "PLAYER IS PLAYING")
    else:
        color, title = GREEN, ("TARGET FOUND" if hit else "PLAYER IS PLAYING")

    head = f"## {title}\n"
    if mention:
        head += f"{mention}\n"
    head += (f"**User:** `{tracker_label(t)}`\n**Profile:** [Open Profile]({roblox_profile_url(t['roblox_id'])})\n"
             f"**Detected:** <t:{info.get('since', int(time.time()))}:R>")

    if hit:
        server_type = "Private / Join-Off" if private else "Public"
        game = f"**Game:** {info.get('game', 'Unknown Game')}\n**Server:** {server_type}"
        if info.get("region"):
            game += f"\n**Region:** {info['region']}"
        if info.get("scanned"):
            game += "\n**Found by:** server scan"
        job = info.get("job_id")
        blocks = [head, SEP, game, f"**Server Job ID**\n{box(job if job else 'Hidden (private or join-off server)')}"]
        if found and private:
            blocks.append("-# This player appears to be in a private server or has joins turned off. A direct join link is not available.")
    else:
        blocks = [head, SEP, f"**Playing:** {info.get('game', 'Unknown Game')}"]
    return card(*blocks, thumb=t.get("avatar_url"), color=color, footer=f"{BOT_NAME} Hitlist" if hit else f"{BOT_NAME} Snipe",
                buttons=track_buttons(t, info, state))


def find_snipe_channel(guild: discord.Guild):
    channel = configured_channel(guild, "snipe", SNIPE_CHANNEL_ID, SNIPE_CHANNEL_NAME)
    if not isinstance(channel, discord.TextChannel):
        channel = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and c.name.lower() == SNIPE_CHANNEL_NAME, guild.text_channels)
    return channel


async def ensure_snipe_channel(guild: discord.Guild):
    # Channel creation belongs to /build; runtime lookups must never silently rebuild a layout.
    return find_snipe_channel(guild)


async def get_tracker_channel(t: dict):
    channel_id = t.get("alert_channel_id")
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


async def resolve_snipe_channel(t: dict):
    guild = bot.get_guild(t["guild_id"])
    channel = await ensure_snipe_channel(guild) if guild else None
    if channel is not None:
        t["alert_channel_id"] = channel.id
    return channel


async def deep_find_later(t: dict, info: dict, msg: discord.Message):
    """A joins-off target: scan the public servers in the background and add the server to the alert when found."""
    place = info.get("place_id") or DEFAULT_PLACE_ID
    server, _ = await scan_servers_for_player(bot.session, t["roblox_id"], place)
    if not server:
        return
    info.update(job_id=server["id"], place_id=place, private=False, scanned=True)
    info["region"] = await detect_server_region(bot.session, place, server["id"])
    if t.get("message_id") != msg.id or not t.get("sig"):
        return
    t["info"] = info
    try:
        await msg.edit(view=build_track_view(t, info, "found", f"<@{t['added_by']}>"))
    except discord.HTTPException:
        pass


async def fill_region_later(t: dict, info: dict, msg: discord.Message):
    if not msg or info.get("region") or not info.get("place_id") or not info.get("job_id") or info.get("private"):
        return
    region = await detect_server_region(bot.session, info["place_id"], info["job_id"])
    if not region:
        return
    info["region"] = region
    if t.get("message_id") != msg.id or not t.get("sig"):
        return
    try:
        await msg.edit(view=build_track_view(t, info, "found", f"<@{t['added_by']}>"))
    except discord.HTTPException:
        pass


async def announce_found(t: dict, info: dict):
    channel = await resolve_snipe_channel(t)
    if channel is None:
        return None
    mention = f"<@{t['added_by']}>"
    try:
        msg = await channel.send(view=build_track_view(t, info, "found", mention),
                                 allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException as exc:
        print(f"[{BOT_NAME}] Could not send tracker alert: {exc}")
        return None
    if t["kind"] == "hitlist":
        if info.get("private"):
            spawn(deep_find_later(t, info, msg))
        elif not info.get("region"):
            spawn(fill_region_later(t, info, msg))
    return msg


async def announce_left(t: dict):
    if not t.get("message_id"):
        return
    channel = await get_tracker_channel(t)
    if channel is None:
        return
    info = t.get("info") or {}
    try:
        await channel.get_partial_message(t["message_id"]).edit(view=build_track_view(t, info, "left", f"<@{t['added_by']}>"))
    except discord.HTTPException:
        pass


async def process_tracker(t: dict, presence: dict | None):
    in_game = presence_in_game(presence)
    if in_game:
        t["miss"] = 0
        sig = presence_sig(presence)
        if t.get("sig") == sig:
            return
        if t.get("sig"):
            await announce_left(t)
        info = await presence_to_info(presence)
        t["sig"] = sig
        t["info"] = info
        msg = await announce_found(t, info)
        t["message_id"] = msg.id if msg else None
        save_data()
        return
    if t.get("sig"):
        t["miss"] = t.get("miss", 0) + 1
        needed = 1 if str(t["sig"]).startswith("private:") else 2
        if t["miss"] >= needed:
            await announce_left(t)
            t["sig"] = None
            t["miss"] = 0
            save_data()


@tasks.loop(seconds=TRACKER_POLL_SECONDS)
async def tracker_loop():
    trackers = [t for t in DATA["trackers"].values() if t["kind"] == "hitlist"]
    if not trackers or bot.session is None:
        return
    ids = list({t["roblox_id"] for t in trackers})
    presences: dict = {}
    for i in range(0, len(ids), 50):
        result = await roblox_presence(bot.session, ids[i:i + 50])
        if result is None:
            return
        presences.update(result)
    await asyncio.gather(*(process_tracker(t, presences.get(t["roblox_id"])) for t in trackers), return_exceptions=True)


@tracker_loop.before_loop
async def before_tracker_loop():
    await bot.wait_until_ready()


class TrackerAddModal(discord.ui.Modal):
    def __init__(self, kind: str):
        super().__init__(title="Hitlist - Add Target")
        self.kind = kind
        self.username = discord.ui.TextInput(label="Roblox username", max_length=40)
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild, session = interaction.guild, interaction.client.session
        typed = self.username.value.strip().lstrip("@")
        roblox = await get_roblox_profile(session, typed)
        if not roblox:
            return await interaction.followup.send(f"I could not find a Roblox account named **{typed}**.", ephemeral=True)
        roblox_id, roblox_name = roblox

        in_guild = [t for t in DATA["trackers"].values() if t["guild_id"] == guild.id and t["kind"] == self.kind]
        if len(in_guild) >= MAX_TRACKED_PER_GUILD:
            return await interaction.followup.send(f"This server is already tracking {MAX_TRACKED_PER_GUILD} players. Remove one first.", ephemeral=True)
        key = f"{self.kind}:{guild.id}:{roblox_id}"
        if key in DATA["trackers"]:
            return await interaction.followup.send(f"**{roblox_name}** is already being tracked.", ephemeral=True)

        (display_name, _), avatar, snap_presences = await asyncio.gather(
            roblox_user_details(session, roblox_id), roblox_avatar_url(session, roblox_id), roblox_presence(session, [roblox_id]))
        t = {"kind": self.kind, "guild_id": guild.id, "channel_id": interaction.channel_id, "roblox_id": roblox_id,
             "username": roblox_name, "display_name": display_name, "avatar_url": avatar,
             "added_by": interaction.user.id, "sig": None, "info": None, "message_id": None, "miss": 0}
        DATA["trackers"][key] = t

        found_now = False
        snap_p = (snap_presences or {}).get(roblox_id)
        if presence_in_game(snap_p):
            info = await presence_to_info(snap_p)
            t["sig"] = presence_sig(snap_p)
            t["info"] = info
            msg = await announce_found(t, info)
            t["message_id"] = msg.id if msg else None
            found_now = msg is not None
        save_data()
        log_audit(f"{self.kind}_add", guild.id, interaction.user.id, roblox_id=roblox_id, roblox_username=roblox_name)

        snipe = await ensure_snipe_channel(guild)
        where = snipe.mention if snipe else "#snipe (I could not create it - give me Manage Channels)"
        text = f"Now tracking **{tracker_label(t)}**. Alerts are posted only in {where}."
        if found_now:
            text += f"\n**{roblox_name}** is already in a game, so the alert was posted right now."
        elif snap_presences is not None:
            text += f"\n**{roblox_name}** is not in a game at the moment. I check every {TRACKER_POLL_SECONDS}s and alert you the second they join."
        if not ROBLOX_COOKIE:
            text += "\n\n**Warning:** ROBLOX_COOKIE is not set in the bot's .env, so Roblox will refuse presence checks."
        elif snap_presences is None:
            text += "\n\n**Warning:** Roblox refused the presence check just now (bad/expired ROBLOX_COOKIE?). Try `/hitlist debug`."
        await interaction.followup.send(text, ephemeral=True)
        try:
            blocks, av, url = await roblox_profile_blocks(session, roblox_id, roblox_name)
            await interaction.followup.send(view=card(*blocks, thumb=av, footer=BOT_NAME), ephemeral=True)
        except discord.HTTPException:
            pass


def find_tracker_key(guild_id: int, kind: str, username: str):
    wanted = username.strip().lstrip("@").lower()
    for key, t in DATA["trackers"].items():
        if t["guild_id"] == guild_id and t["kind"] == kind and t["username"].lower() == wanted:
            return key
    return None


def make_tracker_autocomplete(kind: str):
    async def autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=t["username"], value=t["username"]) for t in DATA["trackers"].values()
                if t["guild_id"] == interaction.guild_id and t["kind"] == kind and current in t["username"].lower()][:25]
    return autocomplete


async def remove_tracker(interaction: discord.Interaction, kind: str, username: str):
    key = find_tracker_key(interaction.guild.id, kind, username)
    if key is None:
        return await interaction.response.send_message(f"**{username}** is not being tracked.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    t = DATA["trackers"][key]
    if t.get("sig"):
        await announce_left(t)
    DATA["trackers"].pop(key, None)
    save_data()
    log_audit(f"{kind}_remove", interaction.guild.id, interaction.user.id, roblox_id=t["roblox_id"], roblox_username=t["username"])
    await interaction.followup.send(f"Stopped tracking **{t['username']}**.", ephemeral=True)


hitlist_group = app_commands.Group(name="hitlist", description="Get alerts with a join button when a Roblox player joins a game",
                                   guild_only=True, default_permissions=discord.Permissions(administrator=True))


@hitlist_group.command(name="add", description="Start tracking a Roblox player (asks for their username)")
async def hitlist_add(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.send_modal(TrackerAddModal("hitlist"))


@hitlist_group.command(name="remove", description="Stop tracking a Roblox player")
@app_commands.describe(username="Roblox username to stop tracking")
@app_commands.autocomplete(username=make_tracker_autocomplete("hitlist"))
async def hitlist_remove(interaction: discord.Interaction, username: str):
    if not await require_admin(interaction):
        return
    await remove_tracker(interaction, "hitlist", username)


@hitlist_group.command(name="list", description="Show everyone this server is tracking")
async def hitlist_list(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    targets = [t for t in DATA["trackers"].values() if t["guild_id"] == interaction.guild.id and t["kind"] == "hitlist"]
    lines = []
    for t in targets:
        state = "in a server" if t.get("sig") else "waiting"
        lines.append(f"- [{tracker_label(t)}]({roblox_profile_url(t['roblox_id'])}) - {state} - added by <@{t['added_by']}>")
    await interaction.response.send_message(
        view=card(f"## Hitlist ({len(targets)}/{MAX_TRACKED_PER_GUILD})", "\n".join(lines) or "Nobody is being tracked. Use `/hitlist add`.", footer=BOT_NAME),
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def roblox_cookie_status(session: aiohttp.ClientSession):
    if not ROBLOX_COOKIE:
        return False, "ROBLOX_COOKIE is not set"
    try:
        async with session.get("https://users.roblox.com/v1/users/authenticated",
                               headers={"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return True, f"logged in as {data.get('name')}"
            return False, f"Roblox rejected the cookie (HTTP {resp.status}) - it is expired or invalid"
    except Exception as exc:
        return False, f"could not reach Roblox ({exc!r})"


@hitlist_group.command(name="debug", description="Show exactly what Roblox reports for every tracked target")
async def hitlist_debug(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    session = interaction.client.session
    ok, cookie_text = await roblox_cookie_status(session)
    targets = [t for t in DATA["trackers"].values() if t["guild_id"] == interaction.guild.id and t["kind"] == "hitlist"]
    lines = [f"**Bot Roblox account:** {'OK' if ok else 'PROBLEM'} - {cookie_text}",
             f"**Tracked targets:** {len(targets)}", f"**Poll interval:** every {TRACKER_POLL_SECONDS}s", ""]
    if targets:
        presences = await roblox_presence(session, [t["roblox_id"] for t in targets][:50])
        if presences is None:
            lines.append("**Presence check FAILED.** Roblox refused the request - usually a bad or expired ROBLOX_COOKIE.")
        else:
            for t in targets:
                p = presences.get(t["roblox_id"]) or {}
                ptype = p.get("userPresenceType", 0)
                place, job = p.get("placeId") or p.get("rootPlaceId"), p.get("gameId")
                if ptype == 2 and job and place:
                    verdict = "In a game, joinable. An alert with a join button should fire."
                elif ptype == 2:
                    verdict = "In a game, but joins are off. The alert fires and a server scan looks for the public server."
                elif ptype == 1:
                    verdict = "Online, not in a game."
                else:
                    verdict = "Offline - or hiding their status. Use /snipe to scan the servers."
                lines.append(f"**{t['username']}** - {PRESENCE_NAMES.get(ptype, 'Offline')}\n"
                             f"-# lastLocation: `{p.get('lastLocation') or 'none'}` | placeId: `{place or 'hidden'}` | gameId: `{job or 'hidden'}` | "
                             f"state: {'alert already sent' if t.get('sig') else 'waiting for him to join'}\n-# {verdict}")
    await interaction.followup.send(view=card("## Hitlist Debug", "\n".join(lines), footer=BOT_NAME), ephemeral=True)


bot.tree.add_command(hitlist_group)


# ======================================================
# /snipe - find a player RIGHT NOW (presence first, then a scan of the public servers)
# ======================================================

@bot.tree.command(name="snipe", description="Find a Roblox player's server right now (scans the public servers if Roblox hides it)")
@app_commands.describe(username="Roblox username to look up",
                       place_id="Game place ID to scan (default: The Strongest Battlegrounds)",
                       deep="Scan the public servers when the player's server is hidden (default: yes)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def snipe_command(interaction: discord.Interaction, username: str, place_id: str = None, deep: bool = True):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    session = interaction.client.session
    if session is None:
        return await interaction.followup.send("Roblox lookup is still starting up. Try /snipe again in a few seconds.", ephemeral=True)
    typed = username.strip().lstrip("@")
    scan_place = place_id.strip() if place_id and place_id.strip().isdigit() else None

    try:
        roblox = await get_roblox_profile(session, typed)
        if not roblox:
            return await interaction.followup.send(f"I could not find a Roblox account named **{username}**.", ephemeral=True)
        roblox_id, roblox_name = roblox
        presences = await roblox_presence(session, [roblox_id])
        if presences is None:
            return await interaction.followup.send(
                "Roblox did not return a presence result after a few attempts. Try `/snipe` again in a moment. If this keeps happening, check ROBLOX_COOKIE.",
                ephemeral=True)
        p = presences.get(roblox_id) or {}
        display_name, _ = await roblox_user_details(session, roblox_id)
        avatar = await roblox_avatar_url(session, roblox_id)
        t = {"kind": "hitlist", "guild_id": interaction.guild.id, "channel_id": interaction.channel_id, "roblox_id": roblox_id,
             "username": roblox_name, "display_name": display_name, "avatar_url": avatar, "added_by": interaction.user.id}
        profile_url = roblox_profile_url(roblox_id)
        joins_on = presence_in_game(p) and bool(p.get("gameId") and (p.get("placeId") or p.get("rootPlaceId")))

        # 1) in a game and joins on: everything straight away
        if joins_on:
            info = await presence_to_info(p, with_region=True)
            blocks, av, _ = await roblox_profile_blocks(session, roblox_id, roblox_name)
            found = build_track_view(t, info, "found")
            await interaction.followup.send(view=found, ephemeral=True)
            return await interaction.followup.send(view=card(*blocks, thumb=av, footer=BOT_NAME), ephemeral=True)

        # 2) hidden / joins off / offline: scan the public servers of the game
        status = PRESENCE_NAMES.get(p.get("userPresenceType", 0), "Offline")
        place = scan_place or p.get("placeId") or p.get("rootPlaceId") or DEFAULT_PLACE_ID
        if deep:
            await interaction.followup.send(view=card(
                f"## Scanning for {roblox_name}",
                f"Roblox status: **{status}**. Joins are off or hidden, so I am checking the public servers of place `{place}`.\n"
                f"-# This takes up to {SCAN_TIME_BUDGET} seconds.", color=YELLOW, footer=BOT_NAME), ephemeral=True)
            server, scanned = await scan_servers_for_player(session, roblox_id, place)
            if server:
                info = {"game": await roblox_game_name(session, p.get("universeId"), p.get("lastLocation") or "Unknown Game"),
                        "place_id": place, "job_id": server["id"], "private": False, "since": int(time.time()), "scanned": True}
                info["region"] = await detect_server_region(session, place, server["id"])
                extra = f"{server.get('playing', '?')}/{server.get('maxPlayers', '?')} players in that server."
                await interaction.followup.send(view=card(
                    f"## {roblox_name} found",
                    f"Found in a public server after checking **{scanned}** server(s). {extra}", color=GREEN, footer=BOT_NAME,
                    thumb=avatar), ephemeral=True)
                return await interaction.followup.send(view=build_track_view(t, info, "found"), ephemeral=True)
            note = (f"Checked **{scanned}** public server(s) of place `{place}` and did not find them. They may be offline, in a private/VIP "
                    "server, in a different place, or in a server I did not reach in time. Try again, or give the right `place_id`.")
        else:
            note = "Server scan was turned off. Run it again with `deep` on to scan the public servers."

        last = iso_to_ts(p.get("lastOnline") or "")
        details = await roblox_json(session, f"https://users.roblox.com/v1/users/{roblox_id}") or {}
        created = iso_to_ts(details.get("created") or "")
        body = (f"**Username:** `{roblox_name}`\n**Display name:** `{display_name or 'Unknown'}`\n**User ID:** `{roblox_id}`\n"
                f"**Created:** {f'<t:{created}:R>' if created else 'Unknown'}\n**Status:** {status}"
                + (f"\n**Last online:** <t:{last}:R>" if last else ""))
        return await interaction.followup.send(view=card(
            f"## {roblox_name} - not found", body, SEP, note + "\nUse `/hitlist add` to be alerted the moment they join.",
            thumb=avatar, color=YELLOW, footer=BOT_NAME, buttons=[link_btn("Open Profile", profile_url)]), ephemeral=True)

    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        print(f"[{BOT_NAME}] /snipe Roblox request failed for {typed}: {exc}")
        return await interaction.followup.send("The Roblox lookup timed out. Please try `/snipe` again in a moment.", ephemeral=True)
    except discord.HTTPException as exc:
        print(f"[{BOT_NAME}] /snipe Discord response failed: {exc}")
        return await interaction.followup.send("I found the player, but Discord could not send the result. Please try `/snipe` again.", ephemeral=True)
    except Exception as exc:
        print(f"[{BOT_NAME}] /snipe unexpected error for {typed}: {exc!r}")
        return await interaction.followup.send("Something went wrong while checking that player. Please try `/snipe` again.", ephemeral=True)


# ======================================================
# /blacklist  /trust
# ======================================================

class BlacklistAddModal(discord.ui.Modal, title="Blacklist Member"):
    user_id = discord.ui.TextInput(label="Discord user ID", min_length=15, max_length=22)
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.user_id.value.strip()
        if not raw.isdigit():
            return await interaction.response.send_message("That is not a valid Discord user ID.", ephemeral=True)
        uid = int(raw)
        if uid == DEVELOPER_ID:
            return await interaction.response.send_message("You cannot blacklist the developer.", ephemeral=True)
        if bot.user and uid == bot.user.id:
            return await interaction.response.send_message("You cannot blacklist the bot itself.", ephemeral=True)
        DATA["blacklist"][str(uid)] = {"reason": self.reason.value.strip(), "by": interaction.user.id, "at": int(time.time())}
        save_data()
        log_audit("blacklist_add", interaction.guild.id, interaction.user.id, discord_id=uid, reason=self.reason.value.strip())
        await interaction.response.send_message(
            view=card("## Member Blacklisted", f"**User:** <@{uid}> (`{uid}`)", f"**Reason**\n{box(self.reason.value.strip())}", footer=BOT_NAME, color=RED),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


blacklist_group = app_commands.Group(name="blacklist", description=f"[OWN] Block members from using {BOT_NAME}", guild_only=True)


@blacklist_group.command(name="add", description="[OWN] Blacklist a member (asks for their user ID and a reason)")
async def blacklist_add(interaction: discord.Interaction):
    if not await require_owner(interaction):
        return
    await interaction.response.send_modal(BlacklistAddModal())


async def blacklist_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"{uid} - {e.get('reason', '')}"[:100], value=uid)
            for uid, e in DATA["blacklist"].items() if current in uid][:25]


@blacklist_group.command(name="remove", description="[OWN] Remove a member from the blacklist")
@app_commands.describe(user_id="Discord user ID to unblacklist")
@app_commands.autocomplete(user_id=blacklist_autocomplete)
async def blacklist_remove(interaction: discord.Interaction, user_id: str):
    if not await require_owner(interaction):
        return
    uid = user_id.strip()
    if uid not in DATA["blacklist"]:
        return await interaction.response.send_message("That user is not blacklisted.", ephemeral=True)
    old_entry = DATA["blacklist"].pop(uid, None) or {}
    save_data()
    log_audit("blacklist_remove", interaction.guild.id, interaction.user.id, discord_id=uid, reason=old_entry.get("reason", ""))
    await interaction.response.send_message(f"<@{uid}> (`{uid}`) has been removed from the blacklist.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(blacklist_group)

trust_group = app_commands.Group(name="trust", description=f"[OWN] Let a member use {BOT_NAME}'s [OWN] commands", guild_only=True)


@trust_group.command(name="add", description="[OWN] Trust a member (by user ID) so they can use the [OWN] commands")
@app_commands.describe(user_id="Discord user ID of the member to trust")
async def trust_add(interaction: discord.Interaction, user_id: str):
    if not await require_owner(interaction, dev_only=True):
        return
    raw = user_id.strip()
    if not raw.isdigit() or not (15 <= len(raw) <= 22):
        return await interaction.response.send_message("That is not a valid Discord user ID.", ephemeral=True)
    uid = int(raw)
    if uid == DEVELOPER_ID:
        return await interaction.response.send_message("You are the developer, you already have full access.", ephemeral=True)
    if bot.user and uid == bot.user.id:
        return await interaction.response.send_message("You cannot trust the bot itself.", ephemeral=True)
    if raw in DATA["trusted"]:
        return await interaction.response.send_message(f"<@{uid}> (`{uid}`) is already trusted.", ephemeral=True,
                                                       allowed_mentions=discord.AllowedMentions.none())
    await interaction.response.defer(ephemeral=True)
    try:
        user = await interaction.client.fetch_user(uid)
    except discord.NotFound:
        return await interaction.followup.send("I could not find a Discord user with that ID.", ephemeral=True)
    except discord.HTTPException:
        user = None
    if user is not None and user.bot:
        return await interaction.followup.send("Bots cannot be trusted.", ephemeral=True)
    DATA["trusted"][raw] = {"added_by": interaction.user.id, "at": int(time.time())}
    save_data()
    log_audit("trust_add", interaction.guild.id, interaction.user.id, discord_id=uid)
    await interaction.followup.send(f"<@{uid}>{f' (`{user.name}`)' if user else ''} is now trusted and can use the [OWN] commands.",
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def trust_autocomplete(interaction: discord.Interaction, current: str):
    choices = []
    for uid in DATA["trusted"]:
        cached = bot.get_user(int(uid))
        label = f"{cached.name} ({uid})" if cached else uid
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=uid))
    return choices[:25]


@trust_group.command(name="remove", description="[OWN] Stop trusting a member")
@app_commands.describe(user_id="Discord user ID to stop trusting")
@app_commands.autocomplete(user_id=trust_autocomplete)
async def trust_remove(interaction: discord.Interaction, user_id: str):
    if not await require_owner(interaction, dev_only=True):
        return
    raw = user_id.strip()
    if raw not in DATA["trusted"]:
        return await interaction.response.send_message("That user is not trusted.", ephemeral=True)
    DATA["trusted"].pop(raw, None)
    save_data()
    log_audit("trust_remove", interaction.guild.id, interaction.user.id, discord_id=int(raw) if raw.isdigit() else raw)
    await interaction.response.send_message(f"<@{raw}> (`{raw}`) is no longer trusted.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


bot.tree.add_command(trust_group)


# ======================================================
# MEMBERS LEAVING / REJOINING (nothing is deleted)
# ======================================================

@bot.event
async def on_member_remove(member: discord.Member):
    gid, uid = str(member.guild.id), str(member.id)
    server = DATA["stats"]["guilds"].get(gid, {}).pop(uid, 0)
    raids = DATA["stats"]["raid_assists"].get(gid, {}).pop(uid, 0)
    daily = {}
    for day, day_stats in DATA["stats"]["daily"].get(gid, {}).items():
        if uid in day_stats:
            daily[day] = day_stats.pop(uid)
    if server or raids or daily:
        DATA["left_members"].setdefault(gid, {})[uid] = {"server": server, "raids": raids, "daily": daily, "left_at": int(time.time())}
    save_data()


@bot.event
async def on_member_join(member: discord.Member):
    gid, uid = str(member.guild.id), str(member.id)
    saved = DATA["left_members"].get(gid, {}).pop(uid, None)
    if not saved:
        return
    g = DATA["stats"]["guilds"].setdefault(gid, {})
    g[uid] = g.get(uid, 0) + saved.get("server", 0)
    r = DATA["stats"]["raid_assists"].setdefault(gid, {})
    r[uid] = r.get(uid, 0) + saved.get("raids", 0)
    for day, count in (saved.get("daily") or {}).items():
        d = DATA["stats"]["daily"].setdefault(gid, {}).setdefault(day, {})
        d[uid] = d.get(uid, 0) + count
    save_data()
    try:
        await sync_tier_role(member.guild, member)
    except Exception:
        pass


# ======================================================
# COMMAND SYNC
# ======================================================

_commands_synced = False


async def sync_guild_commands(guild: discord.Guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"[{BOT_NAME}] Synced {len(synced)} commands to {guild.name}: " + ", ".join(sorted(c.name for c in synced)))
        return synced
    except discord.Forbidden:
        print(f"[{BOT_NAME}] Cannot sync commands in {guild.name} ({guild.id}). Re-invite the bot with the applications.commands scope.")
    except Exception as exc:
        print(f"[{BOT_NAME}] Command sync FAILED in {guild.name}: {exc!r}")
    return None


async def sync_all_commands():
    try:
        await bot.http.bulk_upsert_global_commands(bot.application_id, [])
    except Exception as exc:
        print(f"[{BOT_NAME}] Could not clear global commands: {exc!r}")
    for guild in bot.guilds:
        await sync_guild_commands(guild)


@bot.command(name="sync")
async def sync_prefix_command(ctx: commands.Context):
    if not can_use_own(ctx.author.id):
        return
    await sync_all_commands()
    names = ", ".join(sorted(c.name for c in bot.tree.get_commands()))
    await ctx.reply(f"Commands re-synced in {len(bot.guilds)} server(s).\nTop-level commands: {names}\nRestart/refresh Discord (Ctrl+R) if they do not show up.")


@bot.tree.command(name="sync", description=f"[OWN] Re-register {BOT_NAME}'s slash commands in every server")
@app_commands.guild_only()
async def sync_command(interaction: discord.Interaction):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    await sync_all_commands()
    names = ", ".join(sorted(f"/{c.name}" for c in bot.tree.get_commands()))
    await interaction.followup.send(f"Commands re-synced in {len(bot.guilds)} server(s).\n{names}\nIf something is missing, restart/refresh Discord (Ctrl+R).", ephemeral=True)


# ======================================================
# AUDIT LOG (kept forever)
# ======================================================

def log_audit(action: str, guild_id: int, by: int, **fields):
    entry = {"ts": int(time.time()), "action": action, "guild_id": guild_id, "by": by}
    entry.update(fields)
    DATA["audit"].append(entry)
    save_data()


AUDIT_LABELS = {
    "hitlist_add": "Hitlist - Added", "hitlist_remove": "Hitlist - Removed", "see_add": "See - Added", "see_remove": "See - Removed",
    "blacklist_add": "Blacklist - Added", "blacklist_remove": "Blacklist - Removed", "trust_add": "Trust - Added", "trust_remove": "Trust - Removed",
}
AUDIT_FILTERS = {
    "all": None, "hitlist": ("hitlist_add", "hitlist_remove"), "see": ("see_add", "see_remove"),
    "blacklist": ("blacklist_add", "blacklist_remove"), "trust": ("trust_add", "trust_remove"),
}
AUDIT_PAGE_SIZE = 6


class AuditView(discord.ui.View):
    def __init__(self, guild_id: int, filt: str, page: int = 0):
        super().__init__(timeout=300)
        self.guild_id, self.filt, self.page = guild_id, filt, page
        self.prev_btn = discord.ui.Button(label="Prev", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self.prev_cb
        self.next_btn = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary)
        self.next_btn.callback = self.next_cb
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)

    def entries(self):
        wanted = AUDIT_FILTERS[self.filt]
        rows = [e for e in DATA["audit"] if e.get("guild_id") == self.guild_id and (wanted is None or e["action"] in wanted)]
        rows.reverse()
        return rows

    def build(self) -> discord.Embed:
        rows = self.entries()
        pages = max(1, math.ceil(len(rows) / AUDIT_PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1
        chunk = rows[self.page * AUDIT_PAGE_SIZE:(self.page + 1) * AUDIT_PAGE_SIZE]
        embed = discord.Embed(title=f"{BOT_NAME} Audit Log - {self.filt.title()}", color=EMBED_COLOR)
        if not chunk:
            embed.description = "Nothing logged yet."
        for e in chunk:
            lines = [f"**By:** <@{e['by']}>"]
            if e["action"].startswith(("blacklist", "trust")):
                lines.append(f"**User:** <@{e['discord_id']}> (`{e['discord_id']}`)")
                if e.get("reason"):
                    lines.append(f"**Reason:** {e['reason'][:200]}")
                link = DATA["links"].get(str(e["discord_id"]))
                if link:
                    lines.append(f"**Roblox:** [{link['roblox_username']}]({roblox_profile_url(link['roblox_id'])})")
            else:
                url = roblox_profile_url(e.get("roblox_id"))
                lines.append(f"**Roblox:** [{e.get('roblox_username', 'Unknown')}]({url})" if url else f"**Roblox:** {e.get('roblox_username', 'Unknown')}")
            embed.add_field(name=f"{AUDIT_LABELS.get(e['action'], e['action'])}  -  <t:{e['ts']}:f>", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"Page {self.page + 1} of {pages} - {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not can_use_own(interaction.user.id):
            await interaction.response.send_message(f"Only the {BOT_NAME} developer can use this.", ephemeral=True)
            return False
        return True

    async def prev_cb(self, interaction: discord.Interaction):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build(), view=self)

    async def next_cb(self, interaction: discord.Interaction):
        self.page += 1
        await interaction.response.edit_message(embed=self.build(), view=self)


@bot.tree.command(name="audit", description="[OWN] See hitlist / blacklist / trust additions and removals")
@app_commands.describe(filter="Which entries to show")
@app_commands.choices(filter=[
    app_commands.Choice(name="Everything", value="all"), app_commands.Choice(name="Hitlist", value="hitlist"),
    app_commands.Choice(name="See (old entries)", value="see"), app_commands.Choice(name="Blacklist", value="blacklist"),
    app_commands.Choice(name="Trust", value="trust"),
])
@app_commands.guild_only()
async def audit_command(interaction: discord.Interaction, filter: app_commands.Choice[str] = None):
    if not await require_owner(interaction):
        return
    view = AuditView(interaction.guild.id, filter.value if filter else "all")
    await interaction.response.send_message(embed=view.build(), view=view, ephemeral=True)


# ======================================================
# /view
# ======================================================

@bot.tree.command(name="view", description="See how many tickets a member created (raid vs backup)")
@app_commands.describe(member="The member to look up")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def view_command(interaction: discord.Interaction, member: discord.Member):
    if not await require_admin(interaction):
        return
    tickets = [t for t in DATA["ticket_archive"].values() if t.get("guild_id") == interaction.guild.id and t.get("requester_id") == member.id]
    tickets.sort(key=lambda t: t.get("started_at", 0), reverse=True)
    raids = sum(1 for t in tickets if t["type"] == "Raid")
    backups = sum(1 for t in tickets if t["type"] == "Backup")
    won, lost = sum(1 for t in tickets if t.get("result") == "Won"), sum(1 for t in tickets if t.get("result") == "Lost")
    voided = sum(1 for t in tickets if t.get("result") == "Voided")
    open_now = sum(1 for t in tickets if t["status"] == "open")

    summary = (f"**Tickets created:** `{len(tickets)}` (Raid `{raids}` / Backup `{backups}`)\n"
               f"**Results:** Won `{won}` / Lost `{lost}` / Voided `{voided}` / Open now `{open_now}`\n"
               f"**Assists (this server):** `{get_raid_count(interaction.guild.id, member.id, 'server')}`")
    tier = member_tier(member) if is_setup(interaction.guild.id) else None
    if tier:
        summary += f"\n**Tier:** Tier-{tier} (`{get_raid_only_count(interaction.guild.id, member.id)}` raid assists)"
    link = DATA["links"].get(str(member.id))
    if link:
        summary += f"\n**Linked Roblox:** [{link['roblox_username']}]({roblox_profile_url(link['roblox_id'])})"
    elif tickets:
        t = tickets[0]
        url = roblox_profile_url(t.get("roblox_id"))
        shown_name = f"[{t['roblox_username']}]({url})" if url else t["roblox_username"]
        summary += f"\n**Last Roblox used:** {shown_name}"
    blocks = [f"## Ticket History - {member.display_name}", summary]
    if tickets:
        lines = [f"- `{ticket_label(t)}` {t['type']} - {t.get('result') or ('Open' if t['status'] == 'open' else 'No result')} - <t:{t['started_at']}:d>"
                 for t in tickets[:10]]
        blocks += [SEP, "**Latest tickets**\n" + "\n".join(lines)]
    else:
        blocks.append("This member has not created any tickets here.")
    await interaction.response.send_message(view=card(*blocks, thumb=member.display_avatar.url, footer=BOT_NAME),
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# ======================================================
# /search - who used ANOTHER bot's command recently (admin)
# ======================================================

def message_preview(msg: discord.Message, limit: int = 160) -> str:
    text = (msg.content or "").strip()
    if not text and msg.embeds:
        e = msg.embeds[0]
        text = (e.description or e.title or "").strip()
    if not text:
        text = " ".join(c.content for c in _walk_components(msg.components) if isinstance(getattr(c, "content", None), str)).strip()
    if not text and msg.attachments:
        text = f"[{len(msg.attachments)} attachment(s)]"
    text = discord.utils.escape_mentions(text).replace("\n", " ").replace("`", "'")
    return (text[:limit - 3] + "...") if len(text) > limit else (text or "[no text]")


def find_invoker(msg: discord.Message):
    meta = getattr(msg, "interaction_metadata", None)
    user = getattr(meta, "user", None) if meta is not None else None
    if user is not None:
        name = getattr(meta, "name", None)
        if not name:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    legacy = msg.interaction
                name = getattr(legacy, "name", None) if legacy else None
            except Exception:
                name = None
        kind = getattr(getattr(meta, "type", None), "name", "") or ""
        label = f"`/{name}`" if name else {"application_command": "a slash command", "component": "a button or menu",
                                          "modal_submit": "a form"}.get(kind, "an interaction")
        return user, label, "slash"
    ref = msg.reference
    resolved = ref.resolved if ref else None
    if isinstance(resolved, discord.Message) and not resolved.author.bot:
        typed = discord.utils.escape_mentions((resolved.content or "").strip().replace("`", "'"))[:60] or "a message"
        return resolved.author, f"`{typed}` (probably a prefix command)", "reply"
    return None


@bot.tree.command(name="search", description="Find who used another bot's commands recently, what they ran, and ping them")
@app_commands.describe(application="Only check messages posted by this bot (default: every bot)",
                       channel="Only search this channel (default: every channel I can read)",
                       minutes=f"How far back to look in minutes (default {SEARCH_DEFAULT_MINUTES})",
                       ping="Ping the people found (default: yes)")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def search_command(interaction: discord.Interaction, application: discord.User = None, channel: discord.TextChannel = None,
                         minutes: app_commands.Range[int, 1, 1440] = SEARCH_DEFAULT_MINUTES, ping: bool = True):
    if not await require_admin(interaction):
        return
    guild = interaction.guild
    await interaction.response.defer()
    after = discord.utils.utcnow() - timedelta(minutes=minutes)
    candidates = [channel] if channel is not None else list(guild.text_channels) + list(guild.threads)
    channels = [ch for ch in candidates if ch.permissions_for(guild.me).view_channel and ch.permissions_for(guild.me).read_message_history]

    hits, scanned = [], 0
    for ch in channels:
        try:
            async for msg in ch.history(limit=SEARCH_PER_CHANNEL_LIMIT, after=after, oldest_first=False):
                scanned += 1
                if not msg.author.bot or msg.author.id == bot.user.id:
                    continue
                if application is not None and msg.author.id != application.id:
                    continue
                found = find_invoker(msg)
                if found is None or found[0].bot:
                    continue
                user, label, how = found
                hits.append({"ts": int(msg.created_at.timestamp()), "user": user, "label": label, "app": msg.author.display_name,
                             "channel": ch, "url": msg.jump_url, "preview": message_preview(msg)})
        except (discord.Forbidden, discord.HTTPException):
            continue

    hits.sort(key=lambda h: h["ts"], reverse=True)
    window = f"the last {minutes} minute(s)"
    scope = f"in {channel.mention}" if channel else "across the server"
    who = f" from **{discord.utils.escape_markdown(application.display_name)}**" if application else " from bots"
    if not hits:
        return await interaction.followup.send(view=card(
            "## Command Search",
            f"No command replies{who} {scope} in {window}.",
            "-# I can only trace a message the other bot posted as the reply to a slash command (Discord stamps those with the person who ran it). "
            "Messages a bot posts on its own, ephemeral replies and older messages cannot be traced - try a bigger `minutes` value.",
            footer=f"{BOT_NAME} - {scanned:,} message(s) checked"))

    shown, blocks, size = [], [], 0
    for h in hits:
        if len(shown) >= SEARCH_MAX_SHOWN:
            break
        u = h["user"]
        block = (f"**{len(shown) + 1}.** <@{u.id}> (`{u.name}` / `{u.id}`) ran {h['label']} on **{discord.utils.escape_markdown(h['app'])}**\n"
                 f"-# <t:{h['ts']}:R> in {h['channel'].mention} - [Jump to message]({h['url']})\n> {h['preview']}")
        if size + len(block) > 3000:
            break
        shown.append(h)
        blocks.append(block)
        size += len(block)
    footer = f"{BOT_NAME} - {scanned:,} message(s) checked"
    if len(hits) > len(shown):
        footer += f" - showing {len(shown)} of {len(hits)}"
    unique = []
    for h in shown:
        if h["user"].id not in [u.id for u in unique]:
            unique.append(h["user"])
    view = card("## Command Search", f"Found **{len(hits)}** command reply(ies){who} {scope} in {window}.", SEP, "\n\n".join(blocks), footer=footer)
    if ping:
        allowed = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=u.id) for u in unique])
        await interaction.followup.send(view=view, allowed_mentions=allowed)
        # V2 messages cannot carry a plain content line, so the ping goes out as its own short message
        await interaction.followup.send("Pinging: " + " ".join(f"<@{u.id}>" for u in unique), allowed_mentions=allowed)
    else:
        await interaction.followup.send(view=view, allowed_mentions=discord.AllowedMentions.none())


# ======================================================
# /link /unlink
# ======================================================

async def roblox_description(session: aiohttp.ClientSession, roblox_id: int):
    data = await roblox_json(session, f"https://users.roblox.com/v1/users/{roblox_id}")
    return None if data is None else (data.get("description") or "")


async def live_server_link(user_id: int):
    link = DATA["links"].get(str(user_id))
    if not link or not ROBLOX_COOKIE or bot.session is None:
        return None
    result = await roblox_presence(bot.session, [link["roblox_id"]])
    p = (result or {}).get(link["roblox_id"])
    if not presence_in_game(p):
        return None
    place_id, job_id = p.get("placeId") or p.get("rootPlaceId"), p.get("gameId")
    return roblox_join_link(place_id, job_id) if place_id and job_id else None


class LinkVerifyView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is not your link request.", ephemeral=True)
            return False
        return await blacklist_gate(interaction)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = DATA["pending_links"].get(str(interaction.user.id))
        if not pending or pending["expires"] < time.time():
            return await interaction.response.send_message("This request expired. Run /link again.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        description = await roblox_description(interaction.client.session, pending["roblox_id"])
        if description is None:
            return await interaction.followup.send("Could not reach Roblox. Try again in a moment.", ephemeral=True)
        if pending["code"].lower() not in description.lower():
            return await interaction.followup.send(
                f"I could not find `{pending['code']}` in the About section of **{pending['username']}**. "
                "Save your profile, wait a few seconds, and press Verify again.", ephemeral=True)
        uid = str(interaction.user.id)
        if linked_discord_for_roblox(pending["roblox_id"], exclude=interaction.user.id):
            DATA["pending_links"].pop(uid, None)
            save_data()
            return await interaction.followup.send(f"**{pending['username']}** is already linked to another Discord account. Ask the developer if that is a mistake.", ephemeral=True)
        DATA["links"][uid] = {"roblox_id": pending["roblox_id"], "roblox_username": pending["username"], "linked_at": int(time.time())}
        DATA["configs"].setdefault(uid, {})["roblox_username"] = pending["username"]
        DATA["pending_links"].pop(uid, None)
        save_data()
        await interaction.followup.send(view=card(
            "## Account Linked",
            f"Linked to **{pending['username']}**. You can remove the code from your Roblox profile now.\n"
            "You can now open tickets and use `/whois`. Your server link and region are taken from the server you are in.",
            color=GREEN, footer=BOT_NAME), ephemeral=True)


class LinkModal(discord.ui.Modal, title="Link Roblox Account"):
    username = discord.ui.TextInput(label="Your Roblox username", max_length=40)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        roblox = await get_roblox_profile(interaction.client.session, self.username.value.strip().lstrip("@"))
        if not roblox:
            return await interaction.followup.send("I could not find that Roblox account.", ephemeral=True)
        roblox_id, roblox_name = roblox
        if linked_discord_for_roblox(roblox_id, exclude=interaction.user.id):
            return await interaction.followup.send(f"**{roblox_name}** is already linked to another Discord account.", ephemeral=True)
        code = "CRUXER-" + secrets.token_hex(3).upper()
        DATA["pending_links"][str(interaction.user.id)] = {"roblox_id": roblox_id, "username": roblox_name, "code": code,
                                                           "expires": int(time.time()) + 900}
        save_data()
        await interaction.followup.send(
            f"To prove **{roblox_name}** is yours:\n1. Open your Roblox profile and edit the **About** section.\n"
            f"2. Paste this code anywhere in it: `{code}`\n3. Save, then press **Verify** below (valid for 15 minutes).\n\n"
            "I never ask for your password or cookie.", view=LinkVerifyView(interaction.user.id), ephemeral=True)


@bot.tree.command(name="link", description="Safely link your Roblox account (needed to open tickets)")
@app_commands.guild_only()
async def link_command(interaction: discord.Interaction):
    await interaction.response.send_modal(LinkModal())


@bot.tree.command(name="unlink", description=f"Unlink your Roblox account from {BOT_NAME}")
@app_commands.guild_only()
async def unlink_command(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if uid not in DATA["links"]:
        return await interaction.response.send_message("You do not have a linked Roblox account.", ephemeral=True)
    DATA["links"].pop(uid, None)
    save_data()
    await interaction.response.send_message("Your Roblox account has been unlinked.", ephemeral=True)


# ======================================================
# JOINS OFF / NOT IN-GAME CHECK
# ======================================================

async def join_check(user_id: int):
    """("skip" | "ok" | "blocked", server_link_or_None)."""
    if not ROBLOX_COOKIE or bot.session is None:
        return "skip", None
    link = DATA["links"].get(str(user_id))
    if not link:
        return "skip", None
    result = await roblox_presence(bot.session, [link["roblox_id"]])
    if result is None:
        return "skip", None
    p = result.get(link["roblox_id"])
    if presence_in_game(p):
        place_id, job_id = p.get("placeId") or p.get("rootPlaceId"), p.get("gameId")
        if place_id and job_id:
            return "ok", roblox_join_link(place_id, job_id)
    return "blocked", None


class JoinCheckView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, request_type: str, user_id: int):
        super().__init__(timeout=600)
        self.request_type = request_type
        self.user_id = user_id
        privacy_btn = discord.ui.Button(label="Privacy Settings", style=discord.ButtonStyle.link, url="https://www.roblox.com/my/account#!/privacy")
        retry_btn = discord.ui.Button(label="Retry Join Check", style=discord.ButtonStyle.success)
        retry_btn.callback = self.retry_callback
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "## Joins Off Or Not In-Game\n"
                "- You have either joins off for everyone, or you are not in-game right now or both.\n"
                "- To let helpers join you, set your experience joins to **Everyone** in "
                "[Roblox settings](https://www.roblox.com/my/account#!/privacy).\n"
                "- Join your game, then press **Retry Join Check**."),
            discord.ui.ActionRow(privacy_btn, retry_btn),
            accent_colour=YELLOW))

    async def retry_callback(self, interaction: discord.Interaction):
        try:
            state, link = await asyncio.wait_for(join_check(interaction.user.id), timeout=2.2)
        except Exception:
            state, link = "blocked", None
        if state == "blocked":
            return await interaction.response.send_message(
                "Still not detected in a joinable game. Join your game, make sure joins are set to **Everyone**, then retry.", ephemeral=True)
        await interaction.response.send_modal(RequestModal(self.request_type, interaction.user.id, link))


# ======================================================
# HELPER WATCHER (offline 3 min = first warning, 10 min = final warning)
# ======================================================

@tasks.loop(seconds=30)
async def no_assist_watch_loop():
    now = int(time.time())
    for cid, ticket in list(DATA["tickets"].items()):
        if (ticket.get("status") != "open" or ticket.get("no_assist_warned")
                or ticket.get("helper_order") or now - ticket.get("started_at", now) < NO_ASSIST_WARNING_SECONDS):
            continue
        channel = bot.get_channel(int(cid))
        guild = bot.get_guild(ticket.get("guild_id"))
        if channel is None or guild is None:
            continue
        role = ping_role_for(guild, ticket.get("type", "Backup"))
        mention = ticket.get("ping") or (role.mention if role else None)
        prefix = f"{mention} " if mention else ""
        try:
            await channel.send(
                f"{prefix}This {ticket.get('type', 'request').lower()} ticket has been open for **5 minutes** "
                "and no helpers have joined yet. If you can assist, please check the request and use **Join Raid**.",
                allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not send no-assist reminder for {cid}: {exc!r}")
            continue
        ticket["no_assist_warned"] = True
        save_ticket(int(cid), ticket)
        try:
            warning_channel = await get_warnings_channel(guild)
            if warning_channel and warning_channel.id != channel.id:
                await warning_channel.send(
                    f"{prefix}No assistance yet: {ticket_label(ticket)} in {channel.mention} has been open for five minutes.",
                    allowed_mentions=discord.AllowedMentions(roles=True))
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not mirror no-assist warning for {cid}: {exc!r}")


@no_assist_watch_loop.before_loop
async def before_no_assist_watch_loop():
    await bot.wait_until_ready()


async def get_warnings_channel(guild: discord.Guild):
    return configured_channel(guild, "warnings", WARNINGS_CHANNEL_ID, WARNINGS_CHANNEL_NAME)


async def get_promotion_channel(guild: discord.Guild):
    return configured_channel(guild, "promotion", PROMOTION_CHANNEL_ID, "promotions")


def _join_btn(ticket: dict):
    b = link_btn("Join Server", ticket.get("server_link"))
    if b is None:
        return None
    v = discord.ui.View(timeout=None)
    v.add_item(b)
    return v


def helper_in_ticket_server(presence: dict | None, ticket: dict) -> bool:
    if not presence or presence.get("userPresenceType") != 2:
        return False
    wanted_place, wanted_job = parse_roblox_link(ticket.get("server_link") or "")
    current_place = presence.get("placeId")
    current_job = presence.get("gameId")
    if not wanted_place or not wanted_job or not current_place or not current_job:
        return False
    return str(current_place) == str(wanted_place) and str(current_job).casefold() == str(wanted_job).casefold()


async def record_helper_warning(guild: discord.Guild, ticket: dict, user_id: int, reason: str) -> tuple[int, bool]:
    if user_id in (DEVELOPER_ID, guild.owner_id):
        return int(DATA.get("helper_warnings", {}).get(str(guild.id), {}).get(str(user_id), 0)), False
    gid, uid = str(guild.id), str(user_id)
    by_guild = DATA.setdefault("helper_warnings", {}).setdefault(gid, {})
    count = int(by_guild.get(uid, 0)) + 1
    by_guild[uid] = count
    demoted = count > HELPER_WARNING_LIMIT
    member = guild.get_member(user_id)
    if demoted and member:
        tier_roles = [role for role in (tier_role(guild, t) for t in TIER_ROLE_IDS) if role and role in member.roles]
        if tier_roles:
            try:
                await member.remove_roles(*tier_roles, reason=f"{BOT_NAME}: exceeded {HELPER_WARNING_LIMIT} helper warnings")
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[{BOT_NAME}] Could not demote warned helper {user_id}: {exc!r}")
    save_data()
    if demoted:
        promotion_channel = await get_promotion_channel(guild)
        if promotion_channel:
            rank_line = "Tier role removed after exceeding the 15-warning limit."
            try:
                await promotion_channel.send(view=card(
                    "## Helper Demotion", f"Member: <@{user_id}>\nTicket: {ticket_label(ticket)}\n"
                    f"Reason: {reason}\nWarnings: `{count}/{HELPER_WARNING_LIMIT}`.\n{rank_line}",
                    color=RED, footer=BOT_NAME),
                    allowed_mentions=discord.AllowedMentions(users=True))
            except discord.HTTPException as exc:
                print(f"[{BOT_NAME}] Could not post helper demotion for {user_id}: {exc!r}")
    return count, demoted


@tasks.loop(seconds=JOIN_CHECK_POLL_SECONDS)
async def join_watch_loop():
    if not ROBLOX_COOKIE or bot.session is None:
        return
    now = int(time.time())
    watch = []
    for cid, ticket in list(DATA["tickets"].items()):
        if ticket["status"] != "open":
            continue
        for uid, h in ticket["helpers"].items():
            if h.get("roblox_id") and not h.get("seen_online"):
                watch.append((cid, ticket, uid))
    if not watch:
        return
    ids = list({ticket["helpers"][uid]["roblox_id"] for _, ticket, uid in watch})
    presences: dict = {}
    for i in range(0, len(ids), 50):
        result = await roblox_presence(bot.session, ids[i:i + 50])
        if result is None:
            return
        presences.update(result)

    changed = set()
    for cid, ticket, uid in watch:
        h = ticket["helpers"][uid]
        p = presences.get(h["roblox_id"])
        if not p:
            continue
        if helper_in_ticket_server(p, ticket):
            h["seen_online"] = True
            changed.add(cid)
            continue
        # Record whether they ever reached the requested server, but do not warn mid-ticket.
        # Attendance warnings are evaluated once, after the raid ends.
    for cid in changed:
        save_ticket(int(cid), DATA["tickets"][cid])


@join_watch_loop.before_loop
async def before_join_watch_loop():
    await bot.wait_until_ready()


async def warn_offline_helpers(guild: discord.Guild, ticket: dict):
    if not ROBLOX_COOKIE or bot.session is None or not ticket["helper_order"]:
        return
    check_ids = [h["roblox_id"] for h in ticket["helpers"].values() if h.get("roblox_id") and not h.get("seen_online")]
    presences = await roblox_presence(bot.session, check_ids[:50]) if check_ids else {}
    if presences is None:
        return
    problems = []
    for uid in ticket["helper_order"]:
        user_id = int(uid)
        if user_id in (DEVELOPER_ID, guild.owner_id):
            continue
        h = ticket["helpers"].get(uid, {})
        if not h.get("roblox_id"):
            problems.append(f"- <@{uid}> - could not verify Roblox attendance; no warning counted")
            continue
        if h.get("seen_online"):
            continue
        p = presences.get(h["roblox_id"])
        if helper_in_ticket_server(p, ticket):
            h["seen_online"] = True
            continue
        if p is not None:
            state = "online in a different server" if p.get("userPresenceType", 0) != 0 else "offline"
            count, demoted = await record_helper_warning(
                guild, ticket, user_id, f"Ticket ended before joining the requested Roblox server ({state})")
            demotion_text = " - Tier removed" if demoted else ""
            problems.append(
                f"- <@{uid}> - [{h['username']}]({roblox_profile_url(h['roblox_id'])}) was {state}, "
                f"not in the requested server. Warnings: `{count}/{HELPER_WARNING_LIMIT}`{demotion_text}.")
    if not problems:
        return
    target = await get_warnings_channel(guild) or await get_logs_channel(guild)
    if target:
        await target.send(view=card(f"## Helper Attendance Warnings - {ticket_label(ticket)}",
                                    "These helpers were checked once after the ticket ended:\n" + "\n".join(problems),
                                    color=RED, footer=BOT_NAME), allowed_mentions=discord.AllowedMentions.none())


# ======================================================
# /whois (linked members and [OWN] users, PUBLIC reply)
# ======================================================

@bot.tree.command(name="whois", description="Full Roblox profile and Discord info for a member or Roblox user (linked members only)")
@app_commands.describe(member="Discord member to look up", roblox="Roblox username to look up")
@app_commands.guild_only()
async def whois_command(interaction: discord.Interaction, member: discord.Member = None, roblox: str = None):
    if not can_use_own(interaction.user.id) and not is_linked(interaction.user.id):
        return await interaction.response.send_message(
            view=card("## Link Your Roblox Account", "Only **linked** members can use `/whois`. Use `/link` first, then try again.",
                      color=YELLOW, footer=BOT_NAME), ephemeral=True)
    if member is None and not roblox:
        return await interaction.response.send_message("Give a Discord member, a Roblox username, or both.", ephemeral=True)

    await interaction.response.defer()
    session, guild = interaction.client.session, interaction.guild
    roblox_id = roblox_name = None
    if roblox:
        found = await get_roblox_profile(session, roblox.strip().lstrip("@"))
        if not found:
            return await interaction.followup.send(f"I could not find a Roblox account named **{roblox}**.")
        roblox_id, roblox_name = found
        if member is None:
            did = linked_discord_for_roblox(roblox_id)
            if did:
                member = guild.get_member(did)
    else:
        roblox_id, roblox_name = await resolve_roblox_for_member(session, member)

    blocks, thumb, buttons = [], None, []
    if member is not None:
        banner = None
        try:
            fetched = await interaction.client.fetch_user(member.id)
            banner = fetched.banner.url if fetched.banner else None
        except discord.HTTPException:
            pass
        created = int(member.created_at.timestamp())
        joined = int(member.joined_at.timestamp()) if member.joined_at else None
        notable = [n.replace("_", " ").title() for n in ("administrator", "manage_guild", "manage_channels", "manage_roles", "kick_members", "ban_members")
                   if getattr(member.guild_permissions, n)]
        flags = [f.name.replace("_", " ").title() for f in member.public_flags.all()]
        tickets = [t for t in DATA["ticket_archive"].values() if t.get("guild_id") == guild.id and t.get("requester_id") == member.id]
        bl = DATA["blacklist"].get(str(member.id))
        tier = member_tier(member) if is_setup(guild.id) else None
        blocks.append(
            f"## Discord - {member.display_name}\n"
            f"**Username:** `{member.name}` | **ID:** `{member.id}` | **Bot:** {'Yes' if member.bot else 'No'}\n"
            f"**Created:** <t:{created}:F> (<t:{created}:R>)\n"
            f"**Joined:** {f'<t:{joined}:R>' if joined else 'Unknown'} | **Top role:** {member.top_role.mention}\n"
            f"**Key permissions:** {', '.join(notable) or 'None'}"
            + (f"\n**Badges:** {', '.join(flags)}" if flags else ""))
        blocks.append(
            f"**{BOT_NAME}**\n"
            f"**Assists (server / global):** {get_raid_count(guild.id, member.id, 'server')} / {get_raid_count(guild.id, member.id, 'global')}"
            + (f"\n**Tier:** Tier-{tier} ({get_raid_only_count(guild.id, member.id)} raid assists)" if tier else "")
            + f"\n**Tickets created:** {len(tickets)} ({sum(1 for t in tickets if t['type'] == 'Raid')} raid, {sum(1 for t in tickets if t['type'] == 'Backup')} backup)"
            + f"\n**Blacklisted:** {('Yes - ' + _clean(bl.get('reason', ''), 120)) if bl else 'No'}")
        if banner:
            blocks.append(gallery(banner))
        thumb = member.display_avatar.url
        blocks.append(SEP)

    if roblox_id:
        rblocks, avatar, url = await roblox_profile_blocks(session, roblox_id, roblox_name)
        blocks += rblocks
        buttons.append(link_btn("Open Roblox Profile", url))
        thumb = thumb or avatar
    elif member is not None:
        blocks.append(f"No Roblox account found for {member.mention}. They have not linked one or made a request yet.")

    await interaction.followup.send(view=card(*blocks, thumb=thumb if isinstance(blocks[0], str) else None, buttons=buttons, footer=BOT_NAME),
                                    allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="tickets-export", description="[OWN] Download every stored ticket as a JSON file")
@app_commands.describe(all_servers="Include tickets from every server (default: this server only)")
@app_commands.guild_only()
async def tickets_export_command(interaction: discord.Interaction, all_servers: bool = False):
    if not await require_owner(interaction):
        return
    rows = {k: v for k, v in DATA["ticket_archive"].items() if all_servers or v.get("guild_id") == interaction.guild.id}
    await interaction.response.send_message(f"{len(rows)} ticket(s) in the archive.",
                                            file=discord.File(io.BytesIO(json.dumps(rows, indent=2).encode("utf-8")), filename="cruxer_tickets.json"),
                                            ephemeral=True)


# ======================================================
# /pings - members pick their Raid / Backup ping roles
# ======================================================

async def get_or_make_ping_role(guild: discord.Guild, request_type: str):
    role = ping_role_for(guild, request_type)
    if role is not None:
        return role
    rid, name = (RAID_PING_ROLE_ID, RAID_PING_ROLE_NAME) if request_type == "Raid" else (BACKUP_PING_ROLE_ID, BACKUP_PING_ROLE_NAME)
    return await ensure_role(guild, rid, name, mentionable=True, lines=[])


class PingsView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, member: discord.Member | None = None):
        super().__init__(timeout=None)
        have_raid = have_backup = False
        have_regions = set()
        if member is not None and getattr(member, "guild", None):
            r, b = ping_role_for(member.guild, "Raid"), ping_role_for(member.guild, "Backup")
            have_raid, have_backup = bool(r and r in member.roles), bool(b and b in member.roles)
            for key, (rid, name) in SUPPORT_ALERT_ROLES.items():
                role = find_named_role(member.guild, rid, name)
                if role and role in member.roles:
                    have_regions.add(key)
        request_select = discord.ui.Select(
            placeholder="Choose Raid and Backup alerts", custom_id="tsb:pings:requests", min_values=0, max_values=2,
            options=[
                discord.SelectOption(label="Raid Ping", value="raid", description="Get pinged for new Raid requests", default=have_raid),
                discord.SelectOption(label="Backup Ping", value="backup", description="Get pinged for new Backup requests", default=have_backup),
            ])
        region_select = discord.ui.Select(
            placeholder="Choose regional support alerts", custom_id="tsb:pings:regions", min_values=0, max_values=len(SUPPORT_ALERT_ROLES),
            options=[discord.SelectOption(label=name, value=key, default=key in have_regions)
                     for key, (_, name) in SUPPORT_ALERT_ROLES.items()])

        async def request_callback(interaction: discord.Interaction):
            await self.apply_roles(interaction, "requests", set(interaction.data.get("values", [])))

        async def region_callback(interaction: discord.Interaction):
            await self.apply_roles(interaction, "regions", set(interaction.data.get("values", [])))

        request_select.callback = request_callback
        region_select.callback = region_callback
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(
                "## Ping Roles\n"
                "Choose Raid/Backup alerts and regional support alerts separately. Clear a menu to remove that group."),
            discord.ui.TextDisplay("### Request alerts"), discord.ui.ActionRow(request_select),
            discord.ui.TextDisplay("### Regional alerts"), discord.ui.ActionRow(region_select)))

    async def apply_roles(self, interaction: discord.Interaction, group: str, chosen: set[str]):
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This only works in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        add, remove, missing = [], [], []
        if group == "requests":
            for key, kind in (("raid", "Raid"), ("backup", "Backup")):
                role = await get_or_make_ping_role(guild, kind)
                if role is None:
                    missing.append(kind)
                    continue
                if key in chosen and role not in member.roles:
                    add.append(role)
                elif key not in chosen and role in member.roles:
                    remove.append(role)
        else:
            for key, (rid, name) in SUPPORT_ALERT_ROLES.items():
                role = find_named_role(guild, rid, name)
                if role is None and key in chosen:
                    role = await ensure_role(guild, rid, name, mentionable=True, lines=[])
                if role is None:
                    continue
                if key in chosen and role not in member.roles:
                    add.append(role)
                elif key not in chosen and role in member.roles:
                    remove.append(role)
        try:
            if add:
                await member.add_roles(*add, reason=f"{BOT_NAME} /pings")
            if remove:
                await member.remove_roles(*remove, reason=f"{BOT_NAME} /pings")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.followup.send(view=card(
                "## Could not change your roles", "My role must be **above** the ping roles and I need **Manage Roles**. Tell an admin.",
                color=RED, footer=BOT_NAME), ephemeral=True)
        lines = [f"- Added {r.mention}" for r in add] + [f"- Removed {r.mention}" for r in remove]
        if missing:
            lines.append(f"- Could not find or create the {', '.join(missing)} ping role.")
        await interaction.followup.send(view=card("## Ping Roles Updated", "\n".join(lines) or "Nothing changed.", color=GREEN, footer=BOT_NAME),
                                        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        if not (interaction.message and interaction.message.flags.ephemeral):
            try:    # reset the public panel's menu
                await interaction.message.edit(view=PingsView())
            except discord.HTTPException:
                pass


@bot.tree.command(name="pings", description="Choose which ping roles you get (Raid and Backup)")
@app_commands.describe(post_panel="Admins: post a public ping-role panel in this channel")
@app_commands.guild_only()
async def pings_command(interaction: discord.Interaction, post_panel: bool = False):
    if post_panel:
        if not await require_admin(interaction):
            return
        await interaction.response.send_message("Ping panel posted.", ephemeral=True)
        return await interaction.channel.send(view=PingsView())
    await interaction.response.send_message(view=PingsView(interaction.user), ephemeral=True)


# ======================================================
# /customize - per-server bot name, profile and prefix (never touches other servers)
# ======================================================

def gsettings(guild_id: int) -> dict:
    return DATA["guild_settings"].setdefault(str(guild_id), {})


async def patch_bot_member(guild: discord.Guild, payload: dict):
    """Edits the bot's profile INSIDE this server only (nickname, avatar, banner, bio)."""
    route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild.id)
    await bot.http.request(route, json=payload, reason=f"{BOT_NAME} /customize")


async def fetch_image_data_uri(session: aiohttp.ClientSession, url: str):
    """(data_uri, error)"""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None, f"Could not download the image (HTTP {resp.status})."
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
                return None, "That link is not a PNG, JPG, GIF or WEBP image."
            raw = await resp.read()
    except Exception:
        return None, "Could not download that image."
    if len(raw) > 8 * 1024 * 1024:
        return None, "That image is bigger than 8 MB."
    return f"data:{ctype};base64,{base64.b64encode(raw).decode()}", None


def customize_summary(guild: discord.Guild) -> str:
    s = gsettings(guild.id)
    return (f"**Bot name here:** `{guild.me.display_name}`\n"
            f"**Command prefix here:** `{s.get('prefix') or 'default (. and !)'}`\n"
            f"**Custom profile:** {'Yes' if s.get('profile') else 'No'}\n"
            f"-# Changes only apply to **{guild.name}**. [OWN] commands always keep the default prefixes.")


class NameModal(discord.ui.Modal, title="Change Bot Name"):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.name = discord.ui.TextInput(label="Bot name in this server", default=guild.me.display_name[:32], min_length=1, max_length=32)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await patch_bot_member(interaction.guild, {"nick": self.name.value.strip()})
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await reply_card(interaction, "## Could not change the name", f"Discord refused: `{exc}`", color=RED, footer=BOT_NAME)
        gsettings(interaction.guild.id)["nick"] = self.name.value.strip()
        save_data()
        await reply_card(interaction, "## Name Changed", f"I am now called **{self.name.value.strip()}** in this server only.", color=GREEN, footer=BOT_NAME)


class ProfileModal(discord.ui.Modal, title="Change Bot Profile"):
    def __init__(self):
        super().__init__()
        self.avatar = discord.ui.TextInput(label="Avatar image link (optional)", required=False, max_length=400, placeholder="https://...png")
        self.banner = discord.ui.TextInput(label="Banner image link (optional)", required=False, max_length=400, placeholder="https://...png")
        self.bio = discord.ui.TextInput(label="Bio (optional)", required=False, style=discord.TextStyle.paragraph, max_length=190)
        for i in (self.avatar, self.banner, self.bio):
            self.add_item(i)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        payload, done = {}, []
        for field, value, label in (("avatar", self.avatar.value.strip(), "Avatar"), ("banner", self.banner.value.strip(), "Banner")):
            if value:
                uri, err = await fetch_image_data_uri(interaction.client.session, value)
                if err:
                    return await reply_card(interaction, f"## {label} problem", err, color=RED, footer=BOT_NAME)
                payload[field] = uri
                done.append(label)
        if self.bio.value.strip():
            payload["bio"] = self.bio.value.strip()
            done.append("Bio")
        if not payload:
            return await reply_card(interaction, "## Nothing to change", "Fill in at least one field.", color=YELLOW, footer=BOT_NAME)
        try:
            await patch_bot_member(interaction.guild, payload)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await reply_card(interaction, "## Could not change the profile", f"Discord refused: `{exc}`", color=RED, footer=BOT_NAME)
        gsettings(interaction.guild.id)["profile"] = True
        save_data()
        await reply_card(interaction, "## Profile Changed", f"Updated: **{', '.join(done)}** (this server only).", color=GREEN, footer=BOT_NAME)


class PrefixModal(discord.ui.Modal, title="Change Command Prefix"):
    def __init__(self, guild: discord.Guild):
        super().__init__()
        self.prefix = discord.ui.TextInput(label="New prefix (1-3 characters, no spaces)", default=gsettings(guild.id).get("prefix") or None,
                                           min_length=1, max_length=3, placeholder="e.g. ?")

    async def on_submit(self, interaction: discord.Interaction):
        value = self.prefix.value.strip()
        if not value or any(c.isspace() for c in value) or value.startswith(("/", "@")):
            return await interaction.response.send_message("A prefix cannot contain spaces or start with / or @.", ephemeral=True)
        gsettings(interaction.guild.id)["prefix"] = value
        save_data()
        await reply_card(interaction, "## Prefix Changed",
                         f"Commands in **{interaction.guild.name}** now use `{value}` (example: `{value}av`).\n"
                         "-# Other servers are not affected. [OWN] commands still use `.` and `!`.", color=GREEN, footer=BOT_NAME)


class CustomizeView(BlacklistGate, discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild, owner_id: int):
        super().__init__(timeout=300)
        self.guild, self.owner_id = guild, owner_id
        select = discord.ui.Select(placeholder="Choose what to change", options=[
            discord.SelectOption(label="Bot Name", value="name", description="Rename the bot in this server"),
            discord.SelectOption(label="Bot Profile", value="profile", description="Avatar, banner and bio in this server"),
            discord.SelectOption(label="Command Prefix", value="prefix", description="Prefix for commands in this server"),
            discord.SelectOption(label="Reset Everything", value="reset", description="Back to the defaults in this server"),
        ])
        select.callback = self.choose
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"## Customize {BOT_NAME}"), discord.ui.Separator(),
            discord.ui.TextDisplay(customize_summary(guild)), discord.ui.ActionRow(select)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This is not your menu.", ephemeral=True)
            return False
        return await blacklist_gate(interaction)

    async def choose(self, interaction: discord.Interaction):
        choice = interaction.data["values"][0]
        if choice == "name":
            return await interaction.response.send_modal(NameModal(self.guild))
        if choice == "profile":
            return await interaction.response.send_modal(ProfileModal())
        if choice == "prefix":
            return await interaction.response.send_modal(PrefixModal(self.guild))
        await interaction.response.defer(ephemeral=True)
        try:
            await patch_bot_member(self.guild, {"nick": None, "avatar": None, "banner": None, "bio": None})
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[{BOT_NAME}] Profile reset partly failed: {exc}")
        DATA["guild_settings"].pop(str(self.guild.id), None)
        save_data()
        await reply_card(interaction, "## Customization Reset", "Name, profile and prefix are back to the defaults in this server.", color=GREEN, footer=BOT_NAME)


@bot.tree.command(name="customize", description="Change the bot's name, profile and command prefix in THIS server only")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def customize_command(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.send_message(view=CustomizeView(interaction.guild, interaction.user.id), ephemeral=True)


# ======================================================
# /store and /manage - server templates (channels, roles, permissions)
# ======================================================

def _ow_items(ch) -> list:
    out = []
    for target, ow in ch.overwrites.items():
        allow, deny = ow.pair()
        if isinstance(target, discord.Role):
            out.append({"t": "role", "name": "@everyone" if target.is_default() else target.name, "allow": allow.value, "deny": deny.value})
        else:
            out.append({"t": "member", "id": target.id, "allow": allow.value, "deny": deny.value})
    return out


def snapshot_template(guild: discord.Guild) -> dict:
    roles = [{"name": r.name, "color": r.colour.value, "hoist": r.hoist, "mentionable": r.mentionable,
              "permissions": r.permissions.value, "position": r.position}
             for r in guild.roles if not r.is_default() and not r.managed]
    cats = [{"name": c.name, "position": c.position, "overwrites": _ow_items(c)} for c in guild.categories]
    chans = []
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        entry = {"name": ch.name, "position": ch.position, "category": ch.category.name if ch.category else None, "overwrites": _ow_items(ch)}
        if isinstance(ch, discord.TextChannel):
            entry.update(kind="text", topic=ch.topic, nsfw=ch.nsfw, slowmode=ch.slowmode_delay)
        elif isinstance(ch, discord.StageChannel):
            entry.update(kind="stage")
        elif isinstance(ch, discord.VoiceChannel):
            entry.update(kind="voice", bitrate=ch.bitrate, user_limit=ch.user_limit)
        elif isinstance(ch, discord.ForumChannel):
            entry.update(kind="forum", topic=getattr(ch, "topic", None), nsfw=ch.nsfw, slowmode=ch.slowmode_delay)
        else:
            continue
        chans.append(entry)
    return {"guild_id": guild.id, "guild_name": guild.name, "saved_at": int(time.time()),
            "roles": roles, "categories": cats, "channels": chans}


def store_template(guild: discord.Guild) -> dict:
    tpl = snapshot_template(guild)
    DATA["templates"][str(guild.id)] = tpl
    save_data()
    return tpl


PART_LABELS = {"roles": "Roles", "categories": "Categories", "text": "Text channels",
               "voice": "Voice & stage channels", "forum": "Forum channels"}
KIND_TO_PART = {"text": "text", "voice": "voice", "stage": "voice", "forum": "forum"}


def template_counts(tpl: dict) -> dict:
    counts = {"roles": len(tpl.get("roles", [])), "categories": len(tpl.get("categories", [])), "text": 0, "voice": 0, "forum": 0}
    for c in tpl.get("channels", []):
        counts[KIND_TO_PART.get(c["kind"], "text")] += 1
    return counts


def _existing_keys(guild: discord.Guild):
    roles = {r.name.lower() for r in guild.roles}
    cats = {c.name.lower() for c in guild.categories}
    chans = set()
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        chans.add((ch.name.lower(), (ch.category.name.lower() if ch.category else None)))
    return roles, cats, chans


def preview_text(guild: discord.Guild, tpl: dict, parts: set) -> str:
    roles, cats, chans = _existing_keys(guild)
    lines = []
    if "roles" in parts:
        new = [r["name"] for r in sorted(tpl["roles"], key=lambda r: -r["position"]) if r["name"].lower() not in roles]
        lines.append(f"**Roles:** `{len(new)}` new of `{len(tpl['roles'])}`" + (f"\n-# {_truncate_field(new, 350).replace(chr(10), ', ')}" if new else ""))
    if "categories" in parts:
        new = [c["name"] for c in tpl["categories"] if c["name"].lower() not in cats]
        lines.append(f"**Categories:** `{len(new)}` new of `{len(tpl['categories'])}`" + (f"\n-# {_truncate_field(new, 300).replace(chr(10), ', ')}" if new else ""))
    for part in ("text", "voice", "forum"):
        if part not in parts:
            continue
        group = [c for c in tpl["channels"] if KIND_TO_PART.get(c["kind"], "text") == part]
        new = [c for c in group if (c["name"].lower(), (c["category"].lower() if c["category"] else None)) not in chans]
        names = [f"{c['name']}" for c in new]
        lines.append(f"**{PART_LABELS[part]}:** `{len(new)}` new of `{len(group)}`" + (f"\n-# {_truncate_field(names, 350).replace(chr(10), ', ')}" if names else ""))
    if not lines:
        lines.append("Nothing selected.")
    return "\n".join(lines) + "\n-# Anything that already exists (same name) is skipped, so nothing is duplicated."


def _tpl_overwrites(guild: discord.Guild, items: list) -> dict:
    result = {}
    for it in items:
        if it["t"] == "role":
            target = guild.default_role if it["name"] == "@everyone" else discord.utils.find(lambda r: r.name == it["name"], guild.roles)
        else:
            target = guild.get_member(it["id"])
        if target is not None:
            result[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(it["allow"]), discord.Permissions(it["deny"]))
    return result


async def apply_template(guild: discord.Guild, tpl: dict, parts: set) -> dict:
    res = {"roles": 0, "categories": 0, "channels": 0, "skipped": 0, "failed": []}
    me, reason = guild.me, f"{BOT_NAME} /manage restore"
    roles, cats, chans = _existing_keys(guild)

    if "roles" in parts:      # highest first: Discord puts each new role at the bottom, so this keeps the order
        for r in sorted(tpl["roles"], key=lambda x: -x["position"]):
            if r["name"].lower() in roles:
                res["skipped"] += 1
                continue
            perms = r["permissions"] if me.guild_permissions.administrator else r["permissions"] & me.guild_permissions.value
            try:
                await guild.create_role(name=r["name"], permissions=discord.Permissions(perms), colour=discord.Colour(r["color"]),
                                        hoist=r["hoist"], mentionable=r["mentionable"], reason=reason)
                roles.add(r["name"].lower())
                res["roles"] += 1
            except (discord.Forbidden, discord.HTTPException):
                res["failed"].append(f"role {r['name']}")
            await asyncio.sleep(0.3)

    cat_objs = {c.name.lower(): c for c in guild.categories}
    need_cats = "categories" in parts or any(p in parts for p in ("text", "voice", "forum"))
    if need_cats:
        for c in sorted(tpl["categories"], key=lambda x: x["position"]):
            if c["name"].lower() in cat_objs:
                if "categories" in parts:
                    res["skipped"] += 1
                continue
            if "categories" not in parts:
                continue
            try:
                cat_objs[c["name"].lower()] = await guild.create_category(c["name"], overwrites=_tpl_overwrites(guild, c["overwrites"]), reason=reason)
                res["categories"] += 1
            except (discord.Forbidden, discord.HTTPException):
                res["failed"].append(f"category {c['name']}")
            await asyncio.sleep(0.3)

    for c in sorted(tpl["channels"], key=lambda x: x["position"]):
        part = KIND_TO_PART.get(c["kind"], "text")
        if part not in parts:
            continue
        cname = c["category"].lower() if c["category"] else None
        if (c["name"].lower(), cname) in chans:
            res["skipped"] += 1
            continue
        kwargs = {"overwrites": _tpl_overwrites(guild, c["overwrites"]), "reason": reason}
        parent = cat_objs.get(cname) if cname else None
        if parent is not None:
            kwargs["category"] = parent
        try:
            if c["kind"] == "text":
                await guild.create_text_channel(c["name"], topic=c.get("topic"), nsfw=bool(c.get("nsfw")), slowmode_delay=c.get("slowmode") or 0, **kwargs)
            elif c["kind"] == "voice":
                await guild.create_voice_channel(c["name"], bitrate=min(c.get("bitrate") or 64000, int(guild.bitrate_limit)),
                                                 user_limit=c.get("user_limit") or 0, **kwargs)
            elif c["kind"] == "stage":
                await guild.create_stage_channel(c["name"], **kwargs)
            elif c["kind"] == "forum":
                await guild.create_forum(c["name"], topic=c.get("topic"), nsfw=bool(c.get("nsfw")), slowmode_delay=c.get("slowmode") or 0, **kwargs)
            chans.add((c["name"].lower(), cname))
            res["channels"] += 1
        except (discord.Forbidden, discord.HTTPException):
            res["failed"].append(f"channel {c['name']}")
        await asyncio.sleep(0.3)
    return res


@bot.tree.command(name="store", description="Save this server's channels, roles and permissions as a template")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def store_command(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    tpl = store_template(interaction.guild)
    c = template_counts(tpl)
    await interaction.followup.send(view=card(
        "## Server Template Stored",
        f"**Server:** {interaction.guild.name}\n**Saved:** <t:{tpl['saved_at']}:F>\n"
        f"**Roles:** `{c['roles']}` | **Categories:** `{c['categories']}`\n"
        f"**Text:** `{c['text']}` | **Voice & stage:** `{c['voice']}` | **Forum:** `{c['forum']}`",
        "-# Channel and role permissions are included. The template is part of every data backup. The developer can use /manage to get it by DM or rebuild a server from it.",
        color=GREEN, footer=BOT_NAME), ephemeral=True)


class ManageView(discord.ui.LayoutView):
    def __init__(self, guild: discord.Guild, owner_id: int, uploaded: dict | None = None):
        super().__init__(timeout=600)
        self.guild, self.owner_id, self.uploaded = guild, owner_id, uploaded
        self.source = "upload" if uploaded else (str(guild.id) if str(guild.id) in DATA["templates"] else next(iter(DATA["templates"]), None))
        self.parts = set(PART_LABELS)
        self.confirming = False
        self.build()

    def templates(self) -> dict:
        t = dict(DATA["templates"])
        if self.uploaded:
            t["upload"] = self.uploaded
        return t

    def build(self):
        self.clear_items()
        templates = self.templates()
        tpl = templates.get(self.source)
        if tpl is None:
            self.add_item(discord.ui.Container(discord.ui.TextDisplay("## Manage Templates\nNo template is stored yet. Run **/store** in a server first, or attach a template file to /manage.")))
            return
        c = template_counts(tpl)
        head = (f"## Manage Template\n**Source:** {tpl.get('guild_name', 'Uploaded')} (saved <t:{tpl['saved_at']}:R>)\n"
                f"**Target:** {self.guild.name}\n"
                f"-# Template has {c['roles']} roles, {c['categories']} categories, {c['text']} text, {c['voice']} voice/stage, {c['forum']} forum.")
        src = discord.ui.Select(placeholder="Template to use", options=[
            discord.SelectOption(label=(t.get("guild_name") or k)[:100], value=k, default=(k == self.source),
                                 description=f"Saved {time.strftime('%Y-%m-%d', time.gmtime(t['saved_at']))}") for k, t in list(templates.items())[:25]])
        src.callback = self.on_source
        parts = discord.ui.Select(placeholder="What to restore", min_values=1, max_values=len(PART_LABELS), options=[
            discord.SelectOption(label=label, value=key, default=(key in self.parts)) for key, label in PART_LABELS.items()])
        parts.callback = self.on_parts
        dm = discord.ui.Button(label="DM Me The Template", style=discord.ButtonStyle.secondary)
        dm.callback = self.on_dm
        go = discord.ui.Button(label="Confirm - Create Now" if self.confirming else "Restore Here",
                               style=discord.ButtonStyle.danger if self.confirming else discord.ButtonStyle.success)
        go.callback = self.on_restore
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(head), discord.ui.Separator(),
            discord.ui.TextDisplay("**Preview**\n" + preview_text(self.guild, tpl, self.parts)),
            discord.ui.ActionRow(src), discord.ui.ActionRow(parts), discord.ui.ActionRow(dm, go)))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or not can_use_own(interaction.user.id):
            await interaction.response.send_message("This is not your menu.", ephemeral=True)
            return False
        return True

    async def on_source(self, interaction: discord.Interaction):
        self.source, self.confirming = interaction.data["values"][0], False
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_parts(self, interaction: discord.Interaction):
        self.parts, self.confirming = set(interaction.data["values"]), False
        self.build()
        await interaction.response.edit_message(view=self)

    async def on_dm(self, interaction: discord.Interaction):
        tpl = self.templates().get(self.source)
        if tpl is None:
            return await interaction.response.send_message("No template selected.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        c = template_counts(tpl)
        name = re.sub(r"[^a-zA-Z0-9_-]+", "_", tpl.get("guild_name") or "server")
        try:
            await interaction.user.send(
                f"**{BOT_NAME} server template - {tpl.get('guild_name')}**\nRoles: {c['roles']} | Categories: {c['categories']} | "
                f"Text: {c['text']} | Voice/stage: {c['voice']} | Forum: {c['forum']}\n"
                "Attach this file to `/manage` (the `template_file` option) in any server to preview and rebuild it.",
                file=discord.File(io.BytesIO(json.dumps(tpl, indent=2).encode("utf-8")), filename=f"cruxer_template_{name}.json"))
        except discord.HTTPException:
            return await interaction.followup.send("I could not DM you. Open your DMs for server members and try again.", ephemeral=True)
        await interaction.followup.send("Template sent to your DMs.", ephemeral=True)

    async def on_restore(self, interaction: discord.Interaction):
        tpl = self.templates().get(self.source)
        if tpl is None:
            return await interaction.response.send_message("No template selected.", ephemeral=True)
        if not self.confirming:
            self.confirming = True
            self.build()
            return await interaction.response.edit_message(view=self)
        await interaction.response.edit_message(view=card("## Restoring...", "Creating roles and channels. This can take a minute.", color=YELLOW, footer=BOT_NAME))
        res = await apply_template(self.guild, tpl, set(self.parts))
        body = (f"**Roles created:** `{res['roles']}`\n**Categories created:** `{res['categories']}`\n**Channels created:** `{res['channels']}`\n"
                f"**Skipped (already exist):** `{res['skipped']}`")
        if res["failed"]:
            body += "\n**Failed:** " + _truncate_field([f"`{f}`" for f in res["failed"]], 600).replace("\n", ", ")
        await interaction.edit_original_response(view=card("## Restore Complete", body, color=GREEN if not res["failed"] else YELLOW, footer=BOT_NAME))
        self.stop()


@bot.tree.command(name="manage", description="[OWN] Get a server template by DM, preview it and rebuild roles/channels from it")
@app_commands.describe(template_file="Optional: a template file from a previous /manage DM")
@app_commands.guild_only()
async def manage_command(interaction: discord.Interaction, template_file: discord.Attachment = None):
    if not await require_owner(interaction):
        return
    uploaded = None
    if template_file is not None:
        if template_file.size > 5 * 1024 * 1024:
            return await interaction.response.send_message("That file is too big.", ephemeral=True)
        try:
            uploaded = json.loads((await template_file.read()).decode("utf-8"))
            if not isinstance(uploaded, dict) or "roles" not in uploaded or "channels" not in uploaded or "categories" not in uploaded:
                raise ValueError("missing template fields")
            uploaded.setdefault("saved_at", int(time.time()))
        except Exception as exc:
            return await interaction.response.send_message(f"That is not a valid template file ({exc!r}).", ephemeral=True)
    await interaction.response.send_message(view=ManageView(interaction.guild, interaction.user.id, uploaded), ephemeral=True)


# ======================================================
# DATA BACKUP / RESTORE
# ======================================================

_last_backup_hash: str | None = None
_restore_checked = False


def decode_backup(raw: bytes) -> dict:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


async def backup_targets():
    targets = []
    try:
        dev = bot.get_user(DEVELOPER_ID) or await bot.fetch_user(DEVELOPER_ID)
        targets.append(dev.dm_channel or await dev.create_dm())
    except discord.HTTPException:
        pass
    if BACKUP_CHANNEL_ID:
        try:
            targets.append(bot.get_channel(BACKUP_CHANNEL_ID) or await bot.fetch_channel(BACKUP_CHANNEL_ID))
        except discord.HTTPException:
            pass
    return targets


async def send_backup_to_dev(force: bool = False, reason: str = "Scheduled backup") -> str:
    global _last_backup_hash
    if not _restore_checked:
        return "waiting"
    raw = json.dumps(DATA, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    if not force and digest == _last_backup_hash:
        return "unchanged"
    payload = gzip.compress(raw, 6)
    if len(payload) > 9_500_000:
        print(f"[{BOT_NAME}] Backup is {len(payload)} bytes - too big to send. Copy the data folder by hand.")
        return "too_big"
    stamp = time.strftime("%Y-%m-%d_%H%M", time.gmtime())
    text = (f"**{BOT_NAME} data backup** - {reason}\n"
            f"Global leaderboard members: `{len(DATA['stats']['global']):,}` | raids in history: `{len(DATA['global_history']):,}` | "
            f"archived tickets: `{len(DATA['ticket_archive']):,}` | server templates: `{len(DATA['templates'])}`\n"
            "-# If the bot ever starts with no data, it restores the newest of these by itself. Manual: /data-restore.")
    sent = False
    for target in await backup_targets():
        try:
            await target.send(text, file=discord.File(io.BytesIO(payload), filename=f"cruxer_backup_{stamp}.json.gz"))
            sent = True
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not send the backup to {target}: {exc}")
    if sent:
        _last_backup_hash = digest
        return "sent"
    return "failed"


def _backup_score(d: dict) -> int:
    try:
        return len(d.get("ticket_archive") or {}) * 1000 + sum((d.get("stats") or {}).get("global", {}).values()) + len(d.get("global_history") or [])
    except Exception:
        return 0


async def auto_restore_from_discord():
    best, best_score, best_name = None, -1, None
    for source in await backup_targets():
        checked = 0
        try:
            async for msg in source.history(limit=150):
                if msg.author.id != bot.user.id:
                    continue
                for att in msg.attachments:
                    if not att.filename.startswith("cruxer_backup_") or att.size > 25 * 1024 * 1024:
                        continue
                    try:
                        candidate = decode_backup(await att.read())
                    except Exception:
                        continue
                    checked += 1
                    score = _backup_score(candidate)
                    if score > best_score:
                        best, best_score, best_name = candidate, score, att.filename
                if checked >= 15:
                    break
        except (discord.Forbidden, discord.HTTPException):
            continue
    if best is None:
        return None
    DATA.clear()
    DATA.update(normalize_data(best))
    save_data()
    merge_global_sources()
    register_ticket_views()
    return best_name


@tasks.loop(hours=BACKUP_INTERVAL_HOURS)
async def backup_loop():
    await send_backup_to_dev(force=False)


@backup_loop.before_loop
async def before_backup_loop():
    await bot.wait_until_ready()


def merge_backup(incoming: dict) -> dict:
    """Merge a backup into the live data WITHOUT ever lowering a number or deleting anything."""
    added = {"leaderboard_users": 0, "archive": 0, "history": 0, "blacklist": 0, "links": 0}
    if "stats" not in incoming and incoming and all(isinstance(v, dict) and "raid_number" in v for v in incoming.values()):
        incoming = {"ticket_archive": incoming}

    g = DATA["stats"]["global"]
    for uid, count in ((incoming.get("stats") or {}).get("global") or {}).items():
        if isinstance(count, int) and count > g.get(uid, 0):
            g[uid] = count
            added["leaderboard_users"] += 1
    for gid, users in ((incoming.get("stats") or {}).get("guilds") or {}).items():
        dst = DATA["stats"]["guilds"].setdefault(gid, {})
        for uid, count in (users or {}).items():
            if isinstance(count, int) and count > dst.get(uid, 0):
                dst[uid] = count
    for gid, users in ((incoming.get("stats") or {}).get("raid_assists") or {}).items():
        dst = DATA["stats"]["raid_assists"].setdefault(gid, {})
        for uid, count in (users or {}).items():
            if isinstance(count, int) and count > dst.get(uid, 0):
                dst[uid] = count
    for gid, days in ((incoming.get("stats") or {}).get("daily") or {}).items():
        for day, users in (days or {}).items():
            dst = DATA["stats"]["daily"].setdefault(gid, {}).setdefault(day, {})
            for uid, count in (users or {}).items():
                if isinstance(count, int) and count > dst.get(uid, 0):
                    dst[uid] = count

    for key, ticket in (incoming.get("ticket_archive") or {}).items():
        if key not in DATA["ticket_archive"]:
            DATA["ticket_archive"][key] = ticket
            added["archive"] += 1
    known = {(h.get("ts"), h.get("guild_id"), h.get("raid_number")) for h in DATA["global_history"]}
    for h in incoming.get("global_history") or []:
        k = (h.get("ts"), h.get("guild_id"), h.get("raid_number"))
        if k not in known:
            DATA["global_history"].append(h)
            known.add(k)
            added["history"] += 1
    known_audit = {(a.get("ts"), a.get("action"), a.get("by")) for a in DATA["audit"]}
    for a in incoming.get("audit") or []:
        k = (a.get("ts"), a.get("action"), a.get("by"))
        if k not in known_audit:
            DATA["audit"].append(a)
            known_audit.add(k)
    DATA["audit"].sort(key=lambda a: a.get("ts", 0))

    for gid, count in (incoming.get("raid_counters") or {}).items():
        if isinstance(count, int) and count > DATA["raid_counters"].get(gid, 0):
            DATA["raid_counters"][gid] = count
    for gid, counters in (incoming.get("type_counters") or {}).items():
        dst = DATA["type_counters"].setdefault(gid, {})
        for kind, count in (counters or {}).items():
            if isinstance(count, int) and count > dst.get(kind, 0):
                dst[kind] = count
    for gid in incoming.get("setup_guilds") or []:
        if gid not in DATA["setup_guilds"]:
            DATA["setup_guilds"].append(gid)
    for gid, members in (incoming.get("left_members") or {}).items():
        dst = DATA["left_members"].setdefault(gid, {})
        for uid, saved in (members or {}).items():
            dst.setdefault(uid, saved)
    for uid, entry in (incoming.get("blacklist") or {}).items():
        if uid not in DATA["blacklist"]:
            DATA["blacklist"][uid] = entry
            added["blacklist"] += 1
    for uid, entry in (incoming.get("links") or {}).items():
        if uid not in DATA["links"]:
            DATA["links"][uid] = entry
            added["links"] += 1
    for uid, entry in (incoming.get("configs") or {}).items():
        DATA["configs"].setdefault(uid, entry)
    for gid, tpl in (incoming.get("templates") or {}).items():       # server templates (/store)
        if gid not in DATA["templates"] or tpl.get("saved_at", 0) > DATA["templates"][gid].get("saved_at", 0):
            DATA["templates"][gid] = tpl
    for gid, settings in (incoming.get("guild_settings") or {}).items():   # /customize
        DATA["guild_settings"].setdefault(gid, settings)
    for gid in incoming.get("authorized_guilds") or []:
        if gid not in DATA["authorized_guilds"]:
            DATA["authorized_guilds"].append(gid)
    # the trusted list is deliberately NOT merged from a backup (re-add people with /trust add)
    save_data()
    merge_global_sources()
    return added


@bot.tree.command(name="data-backup", description="[OWN] DM yourself a full backup of Cruxer's data right now")
@app_commands.guild_only()
async def data_backup_command(interaction: discord.Interaction):
    if not await require_owner(interaction, dev_only=True):
        return
    await interaction.response.defer(ephemeral=True)
    result = await send_backup_to_dev(force=True, reason="Manual backup")
    texts = {"sent": "Backup sent to your DMs.", "failed": "I could not DM you. Open your DMs for server members and try again.",
             "too_big": "The data file is too big to send. Copy the data folder from the host instead.",
             "waiting": "The bot is still starting up. Try again in a few seconds."}
    await interaction.followup.send(texts.get(result, "Done."), ephemeral=True)


@bot.tree.command(name="data-restore", description="[OWN] Merge a Cruxer backup file back in (never lowers any count)")
@app_commands.describe(file="A cruxer_backup_*.json(.gz) / cruxer_data.json / cruxer_tickets.json file")
@app_commands.guild_only()
async def data_restore_command(interaction: discord.Interaction, file: discord.Attachment):
    if not await require_owner(interaction, dev_only=True):
        return
    if file.size > 25 * 1024 * 1024:
        return await interaction.response.send_message("That file is too big.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        incoming = decode_backup(await file.read())
    except Exception as exc:
        return await interaction.followup.send(f"That is not a valid backup file ({exc!r}).", ephemeral=True)
    added = merge_backup(incoming)
    register_ticket_views()
    await interaction.followup.send(view=card(
        "## Backup Merged", "Nothing was deleted or lowered.",
        f"- Leaderboard members raised/added: `{added['leaderboard_users']}`\n- Archived tickets added: `{added['archive']}`\n"
        f"- Raid history entries added: `{added['history']}`\n- Blacklist entries added: `{added['blacklist']}`\n"
        f"- Linked accounts added: `{added['links']}`\n- Global leaderboard now has `{len(DATA['stats']['global']):,}` members.",
        color=GREEN, footer=BOT_NAME), ephemeral=True)


@bot.tree.command(name="data-status", description="[OWN] Where Cruxer saves its data and how safe it is")
@app_commands.guild_only()
async def data_status_command(interaction: discord.Interaction):
    if not await require_owner(interaction, dev_only=True):
        return
    size = os.path.getsize(DATA_FILE) if os.path.exists(DATA_FILE) else 0
    snaps = _list_snapshots()
    await interaction.response.send_message(view=card(
        f"## {BOT_NAME} Data Status",
        f"**Data file:** `{DATA_FILE}` ({size / 1024:,.1f} KB)\n"
        f"**Persistent folder set:** {'Yes' if DATA_IS_PERSISTENT else '**No** - set CRUXER_DATA_DIR'}\n"
        f"**Snapshots:** {len(snaps)} (newest: `{os.path.basename(snaps[0]) if snaps else 'none'}`)\n"
        f"**Backups to Discord:** DM{' + backup channel' if BACKUP_CHANNEL_ID else ''}, every {BACKUP_INTERVAL_HOURS}h and after each raid",
        f"**Stored:** `{len(DATA['stats']['global']):,}` leaderboard members, `{len(DATA['global_history']):,}` raids, "
        f"`{len(DATA['ticket_archive']):,}` archived tickets, `{len(DATA['audit']):,}` audit entries, `{len(DATA['templates'])}` server templates",
        footer=BOT_NAME), ephemeral=True)


# ======================================================
# NEW SERVER / REMOVED / READY
# ======================================================

@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"Joined new server: {guild.name} ({guild.id})")
    await sync_guild_commands(guild)
    readme = await create_or_update_readme(guild, authorized=guild.id in DATA["authorized_guilds"])
    print(f"{'Created/updated' if readme else 'Could not create'} Read-me channel in {guild.name}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    print(f"Removed from server: {guild.name} ({guild.id})")


_ready_done = False


@bot.event
async def on_ready():
    global _commands_synced, _ready_done, _restore_checked

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Connected to {len(bot.guilds)} server(s). Global leaderboard entries loaded: {len(DATA['stats']['global'])}")
    if _ready_done:
        return
    _ready_done = True

    cookie_ok, cookie_text = await roblox_cookie_status(bot.session)
    print(f"[{BOT_NAME}] Roblox account check: {'OK' if cookie_ok else 'PROBLEM'} - {cookie_text}")

    # The bot is called Cruxer everywhere: rename the application user and any old nickname that still says Imperium.
    if bot.user.name.lower() == LEGACY_BOT_NAME.lower():
        try:
            await bot.user.edit(username=BOT_NAME)
        except discord.HTTPException as exc:
            print(f"[{BOT_NAME}] Could not rename the bot account (Discord limits this): {exc}")

    try:
        if DATA_WAS_FRESH:
            restored = None
            try:
                restored = await auto_restore_from_discord()
            except Exception as exc:
                print(f"[{BOT_NAME}] Auto-restore failed: {exc!r}")
            try:
                dev = bot.get_user(DEVELOPER_ID) or await bot.fetch_user(DEVELOPER_ID)
                if restored:
                    await dev.send(f"**{BOT_NAME} started with no data file and restored everything automatically** from `{restored}`.")
                elif bot.guilds:
                    await dev.send(f"**{BOT_NAME} started with NO data file and found no backup.** If this is not the very first run, "
                                   "upload your latest backup with **/data-restore** and set CRUXER_DATA_DIR to a persistent folder.")
            except discord.HTTPException:
                pass
    finally:
        _restore_checked = True

    if not _commands_synced:
        _commands_synced = True
        await sync_all_commands()

    for guild in bot.guilds:
        gid = str(guild.id)
        nick = guild.me.nick if guild.me else None
        if nick and LEGACY_BOT_NAME.lower() in nick.lower() and not DATA["guild_settings"].get(gid, {}).get("nick"):
            try:
                await guild.me.edit(nick=None)
            except discord.HTTPException:
                pass

        if guild.id in DATA["authorized_guilds"]:
            await ensure_snipe_channel(guild)
            category = find_cruxer_category(guild)
            if category is not None and category.name.lower() == LEGACY_BOT_NAME.lower():
                await get_or_create_categories(guild)
            if configured_channel(guild, "readme", 0, README_CHANNEL_NAME):
                await create_or_update_readme(guild, authorized=True)
            if gid not in DATA["templates"]:
                try:
                    store_template(guild)
                except Exception as exc:
                    print(f"[{BOT_NAME}] Could not store a template for {guild.name}: {exc!r}")

        panel_channel = (guild.get_channel(DATA["panel_channels"].get(gid, 0)) or guild.get_channel(BATTLE_PANEL_CHANNEL_ID)
                         or discord.utils.get(guild.text_channels, name=BATTLE_PANEL_CHANNEL_NAME))
        if isinstance(panel_channel, discord.TextChannel):
            try:
                await refresh_panel(panel_channel)
            except discord.HTTPException:
                pass
        leaderboard_channel = (guild.get_channel(DATA["leaderboard_channels"].get(gid, 0)) or guild.get_channel(LEADERBOARD_CHANNEL_ID)
                               or discord.utils.get(guild.text_channels, name=LEADERBOARD_CHANNEL_NAME))
        if isinstance(leaderboard_channel, discord.TextChannel):
            try:
                await refresh_leaderboard(leaderboard_channel)
            except discord.HTTPException:
                pass

    try:
        await upgrade_open_tickets()
    except Exception as exc:
        print(f"[{BOT_NAME}] Could not upgrade open tickets: {exc!r}")


# ======================================================
# START
# ======================================================

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Put it in your .env file:\nDISCORD_TOKEN=your_token_here")
    bot.run(TOKEN)