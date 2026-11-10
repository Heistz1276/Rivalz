import subprocess, sys
try:
    import discord
except ModuleNotFoundError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "discord.py>=2.6,<3.0", "python-dotenv>=1.0,<2.0"])
"""Piro: universal role-based division MVP tracker with in-ticket live tracking.

Needs: discord.py >= 2.6 (Components V2) and python-dotenv.

The bot is split into focused sections under `piro_modules/`. Upload that folder with this entry file.
The art data lives in `piro_modules/assets.py`.

URA-only configuration is read from .env. Piro creates no channels or voice rooms; it adds
one tracker card to an existing Crusher ticket after Crusher's first post.
"""
import ast
import asyncio
import base64
import io
import json
import os
import platform
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# Built-in URA defaults. Railway does not upload .env, so these keep the bot working with only
# DISCORD_TOKEN set. Anything you set as a Railway variable (or in .env) still wins.
URA_DEFAULTS = {
    "BOT_NAME": "URA | DIV",
    "GUILD_ID": "1507237602091208714",
    "OWNER_ID": "1455426573741330495",
    "MVP_CHANNEL_IDS": "1524379574585000006",
    "MVP_BOT_ID": "1563126014702395542",
    "RAID_UPDATE_CHANNEL_ID": "1565732391107170364",
    "LOG_CHANNEL_ID": "1565771359097716766",
    "LEADERBOARD_CHANNEL_ID": "1565732417271234791",
    "RAID_START_NUMBER": "125",
    "CRUSHER_TICKET_CATEGORY_ID": "1507716131551707258",
    "CRUSHER_BOT_ID": "1566412027906228274",
    "DIV_TICKET_CATEGORY_ID": "1507716131551707258",
    "DIV_VOICE_CATEGORY_ID": "1531673247618302043",
    "CRUSHER_VOICE_CHANNEL_ID": "1531672967249920041",
    "LIVE_CATEGORY_ID": "1507716131551707258",
    "LIVE_IGNORE_IDS": "1524379571946913862,1562782175927992340",
    "LIVE_BOT_IDS": "1566412027906228274",
    "LIVE_JOIN_BOT_IDS": "1566412027906228274",
    "DIVISION_1_ROLE_ID": "1517694951650103427",
    "DIVISION_2_ROLE_ID": "1517694955835883682",
    "DIVISION_3_ROLE_ID": "1517694959291994132",
    "DIVISION_4_ROLE_ID": "1517694962119213056",
    "DIVISION_5_ROLE_ID": "1517694969538805831",
}
for _k, _v in URA_DEFAULTS.items():
    if not (os.getenv(_k) or "").strip():
        os.environ[_k] = _v


def env_ids(name: str) -> set[int]:
    return {int(x) for x in re.split(r"[,\s]+", os.getenv(name, "")) if x.isdigit()}


def env_int(name: str, default: int = 0) -> int:
    try:
        value = int(os.getenv(name, "0") or 0)
    except ValueError:
        value = 0
    return value or default


# ----------------------------------------------------------------------------
# Fixed URA configuration from .env.
# ----------------------------------------------------------------------------
BOT_NAME = os.getenv("BOT_NAME", "URA | DIV")
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = env_int("GUILD_ID")
OWNER_ID = env_int("OWNER_ID")

LOG_ID = env_int("LOG_CHANNEL_ID")
LEADERBOARD_ID = env_int("LEADERBOARD_CHANNEL_ID")
MVP_RESULTS_ID = env_int("RAID_UPDATE_CHANNEL_ID", env_int("DIVISION_UPDATES_CHANNEL_ID"))
LIVE_POST_ID = 0  # Live tracker cards are posted inside Crusher tickets.
MVP_BOT_ID = env_int("MVP_BOT_ID")
SOURCE_IDS = env_ids("MVP_CHANNEL_IDS")
MVP_CHANNEL_ID = sorted(SOURCE_IDS)[0] if SOURCE_IDS else 0

LIVE_CATEGORY_IDS = env_ids("LIVE_CATEGORY_ID")
DIV_TICKET_CATEGORY_ID = env_int("DIV_TICKET_CATEGORY_ID")
DIV_VOICE_CATEGORY_ID = env_int("DIV_VOICE_CATEGORY_ID")
CRUSHER_VOICE_CHANNEL_ID = env_int("CRUSHER_VOICE_CHANNEL_ID")
CRUSHER_TICKET_CATEGORY_ID = env_int("CRUSHER_TICKET_CATEGORY_ID", env_int("LIVE_CATEGORY_ID", 0))
CRUSHER_BOT_ID = env_int("CRUSHER_BOT_ID")
LIVE_IGNORE_IDS = env_ids("LIVE_IGNORE_IDS")
LIVE_BOT_IDS = env_ids("LIVE_BOT_IDS")
# Bots whose messages are "join" messages: every human they ping in a ticket counts as a helper.
LIVE_JOIN_BOT_IDS = env_ids("LIVE_JOIN_BOT_IDS")
if CRUSHER_BOT_ID:
    LIVE_BOT_IDS.add(CRUSHER_BOT_ID)
    LIVE_JOIN_BOT_IDS.add(CRUSHER_BOT_ID)
RAID_START_NUMBER = env_int("RAID_START_NUMBER", 1)
VERSION = "5.3-ura-in-ticket"
LIVE_SYNC_HOURS = env_int("LIVE_SYNC_HOURS", 12)
SETUP_CATEGORY_ID = env_int("SETUP_CATEGORY_ID", 1565732366595657769)  # /setup auto build puts channels here
ENV_DEFAULTS = {"log": LOG_ID, "leaderboard": LEADERBOARD_ID, "live": LIVE_POST_ID, "results": MVP_RESULTS_ID,
                "source": MVP_CHANNEL_ID, "category": CRUSHER_TICKET_CATEGORY_ID}
_script_dir = Path(__file__).resolve().parent
# Railway sets RAILWAY_VOLUME_MOUNT_PATH when a volume is attached. Data there survives redeploys.
_data_dir = Path(os.getenv("PIRO_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or _script_dir)
_db = Path(os.getenv("PIRO_DB_PATH") or "piro_main.sqlite3")
DB_PATH = str(_db if _db.is_absolute() else _data_dir / _db)
BACKUP_DIR = Path(os.getenv("PIRO_BACKUP_DIR") or str(_data_dir / "backups"))
KEEP_BACKUPS = 48
BADGE_DIR = Path(os.getenv("PIRO_BADGE_DIR", str(_script_dir / "badges")))

DEFAULT_DIVISION_ROLES = {d: 0 for d in range(1, 6)}
DIVISION_ROLES = {d: env_int(f"DIVISION_{d}_ROLE_ID", DEFAULT_DIVISION_ROLES[d]) for d in range(1, 6)}
DIVISION_ROLE_IDS = {role_id: d for d, role_id in DIVISION_ROLES.items()}
DIVISION_NAME_RE = re.compile(r"(?:\bdivision\s*[-_ ]?0?([1-5])\b|\bD\s*[-_ ]?([1-5])\b)", re.I)


def clean_image_url(d: int) -> str:
    value = os.getenv(f"DIVISION_{d}_IMAGE_URL", "").strip().strip('"').strip("'")
    if value and not re.match(r"https?://", value, re.I):
        print(f"DIVISION_{d}_IMAGE_URL is not a real https link ({value[:40]}...); ignoring it.")
        return ""
    return value


DIVISION_IMAGE_URLS = {d: clean_image_url(d) for d in range(1, 6)}

BRAND_COLOR = 0x5865F2
DIVISION_COLORS = {1: 0xF5C542, 2: 0x3B82F6, 3: 0x22C55E, 4: 0xA855F7, 5: 0xEF4444}
DIVISION_EMOJI = {1: "\U0001F7E8", 2: "\U0001F7E6", 3: "\U0001F7E9", 4: "\U0001F7EA", 5: "\U0001F7E5"}
RAID_RE = re.compile(r"\bRaid\b[\s`*_:\-]*#\s*0*(\d+)", re.I)
MENTION_RE = re.compile(r"<@!?(\d+)>")
CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
OPENED_RE = re.compile(r"\b(?:ticket\s+)?opened\s+by\s*:?\s*<@!?(\d+)>", re.I)
LIVE_TOTAL_RE = re.compile(r"(?:Live Helpers|Helpers Joined)\D{0,12}(\d+)", re.I)
HELP_RE = re.compile(r"Help Registered|confirmed for help|has joined|joined (?:the )?(?:backup|raid|ticket|help)|"
                     r"is (?:now )?helping|will (?:be )?help", re.I)
JOIN_NOTICE_RE = re.compile(r"help registered|confirmed for help|has joined|joined (?:the )?(?:backup|raid|ticket|help)|"
                            r"is (?:now )?helping|will (?:be )?help", re.I)
HELP_MENTION_RE = re.compile(r"<@!?(\d+)>\s*(?:has confirmed for help|has joined|joined|is (?:now )?helping|will (?:be )?help)", re.I)
NO_PING = discord.AllowedMentions.none()
START_TIME = time.time()

# ----------------------------------------------------------------------------
# DATABASE
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA synchronous=FULL")
db.execute("PRAGMA busy_timeout=5000")


def ensure_schema() -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS raid_results (
        message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
        raid_number INTEGER NOT NULL, raiders_json TEXT NOT NULL, tallies_json TEXT NOT NULL,
        winners_json TEXT NOT NULL, created_at TEXT NOT NULL, update_message_id INTEGER
    )""")
    for _col in ("update_ref", "log_ref", "skipped_json"):
        try:
            db.execute(f"ALTER TABLE raid_results ADD COLUMN {_col} TEXT")
        except sqlite3.OperationalError:
            pass
    try:
        db.execute("ALTER TABLE raid_results ADD COLUMN counted INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS trusted (user_id INTEGER PRIMARY KEY)")
    db.execute("""CREATE TABLE IF NOT EXISTS live_tickets (
        channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
        opener_id INTEGER, opener_division INTEGER, reported_total INTEGER,
        helpers_json TEXT NOT NULL DEFAULT '[]', post_ref TEXT, status TEXT NOT NULL DEFAULT 'live',
        created_at TEXT NOT NULL, ended_at TEXT, archive_ref TEXT
    )""")
    try:
        db.execute("ALTER TABLE live_tickets ADD COLUMN archive_ref TEXT")
    except sqlite3.OperationalError:
        pass
    db.execute("""CREATE TABLE IF NOT EXISTS ticket_mirrors (
        source_channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
        division_channel_id INTEGER NOT NULL UNIQUE, voice_channel_id INTEGER,
        ticket_number INTEGER NOT NULL, created_at TEXT NOT NULL,
        support_message_id INTEGER, message_count INTEGER NOT NULL DEFAULT -1,
        last_counted_message_id INTEGER
    )""")
    for column, declaration in (("support_message_id", "INTEGER"),
                                ("message_count", "INTEGER NOT NULL DEFAULT -1"),
                                ("last_counted_message_id", "INTEGER")):
        try:
            db.execute(f"ALTER TABLE ticket_mirrors ADD COLUMN {column} {declaration}")
        except sqlite3.OperationalError:
            pass
    db.commit()


ensure_schema()


def is_trusted(user_id: int) -> bool:
    return db.execute("SELECT 1 FROM trusted WHERE user_id=?", (user_id,)).fetchone() is not None


def get_setting(key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    db.commit()


def cfg_get(guild_id: int, key: str) -> int | None:
    value = get_setting(f"cfg:{guild_id}:{key}")
    return int(value) if value and value.isdigit() else None


def cfg_set(guild_id: int, key: str, value: int) -> None:
    set_setting(f"cfg:{guild_id}:{key}", str(value))


def apply_config(guild_id: int) -> None:
    """Use the fixed URA routing from .env; stale setup overrides are intentionally ignored."""
    global LOG_ID, LEADERBOARD_ID, LIVE_POST_ID, MVP_RESULTS_ID, SOURCE_IDS, MVP_CHANNEL_ID, LIVE_CATEGORY_IDS, CRUSHER_TICKET_CATEGORY_ID, DIV_VOICE_CATEGORY_ID, CRUSHER_VOICE_CHANNEL_ID
    if not GUILD_ID or guild_id != GUILD_ID:
        return
    LOG_ID = env_int("LOG_CHANNEL_ID")
    LEADERBOARD_ID = env_int("LEADERBOARD_CHANNEL_ID")
    LIVE_POST_ID = 0
    MVP_RESULTS_ID = env_int("RAID_UPDATE_CHANNEL_ID", env_int("DIVISION_UPDATES_CHANNEL_ID"))
    SOURCE_IDS = env_ids("MVP_CHANNEL_IDS")
    MVP_CHANNEL_ID = sorted(SOURCE_IDS)[0] if SOURCE_IDS else 0
    category = env_int("CRUSHER_TICKET_CATEGORY_ID", env_int("LIVE_CATEGORY_ID"))
    LIVE_CATEGORY_IDS = {category} if category else set()
    CRUSHER_TICKET_CATEGORY_ID = category
    DIV_VOICE_CATEGORY_ID = env_int("DIV_VOICE_CATEGORY_ID")
    CRUSHER_VOICE_CHANNEL_ID = env_int("CRUSHER_VOICE_CHANNEL_ID")


# ----------------------------------------------------------------------------
# BOT
# ----------------------------------------------------------------------------
DIV_LB_COMMANDS = {f"leaderboarddivision{d}" for d in range(1, 6)}
OWNER_ONLY_COMMANDS = {"trust", "backup", "restore"}
PUBLIC_COMMANDS = {"div-assists", "my-rank", "ping"}


class GuardedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        _cmd = interaction.command
        _root = _cmd.root_parent.name if _cmd is not None and _cmd.root_parent else getattr(_cmd, "name", "")
        if _root in DIV_LB_COMMANDS:  # the 5 division leaderboards: anyone, in any server or DM
            return True
        if not GUILD_ID or interaction.guild_id != GUILD_ID:
            raise app_commands.CheckFailure("wrong_server")
        if interaction.guild_id:
            apply_config(interaction.guild_id)
        uid = interaction.user.id
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild:
            return True
        if uid == OWNER_ID:
            return True
        command = interaction.command
        root = command.root_parent.name if command is not None and command.root_parent else getattr(command, "name", "")
        if root in PUBLIC_COMMANDS:
            return True
        if root not in OWNER_ONLY_COMMANDS and is_trusted(uid):
            return True
        raise app_commands.CheckFailure("not allowed")


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None,
                   tree_cls=GuardedTree)

PROCESS_LOCK = asyncio.Lock()
LIVE_LOCKS: dict[int, asyncio.Lock] = {}
LB_LOCK = asyncio.Lock()
LB_LAST_PRESS = 0.0
LIVE_SEEN: dict[int, str] = {}
TICKET_PROMPT_LOCKS: dict[int, asyncio.Lock] = {}
IMAGE_PROBLEM: str | None = None
STARTED = False


# ----------------------------------------------------------------------------



# Feature code is kept in focused files under piro_modules/. Loading into this shared
# runtime namespace preserves the existing bot state, slash-command decorators, and DB helpers.
_MODULE_DIR = Path(__file__).resolve().parent / "piro_modules"

def load_module(filename: str) -> None:
    module_path = _MODULE_DIR / filename
    code = module_path.read_text(encoding="utf-8")
    exec(compile(code, str(module_path), "exec"), globals())


for _module in (
    "assets.py",
    "shared.py",
    "ui.py",
    "leaderboard.py",
    "live_tracking.py",
    "backups.py",
    "ticket_setup.py",
    "events.py",
    "commands_admin.py",
    "commands_live.py",
    "commands_mvp.py",
    "commands_division_lb.py",
):
    load_module(_module)

# The 5 division leaderboards are GLOBAL commands (anyone, any server or DM). Every other command is
# registered for the URA server only, so they never show up in other servers.
if GUILD_ID:
    _home = discord.Object(id=GUILD_ID)
    for _cmd in list(bot.tree.get_commands()):
        if _cmd.name in DIV_LB_COMMANDS:
            continue
        bot.tree.remove_command(_cmd.name)
        bot.tree.add_command(_cmd, guild=_home)

# START
# ----------------------------------------------------------------------------
if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    raise RuntimeError("Set your Discord bot token in .env before starting the bot.")
bot.run(TOKEN)



