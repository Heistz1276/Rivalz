"""
Rivalz - Matchmaking bot for Duels and Spars
Requires: discord.py >= 2.6 (Components V2 + modal selects), aiohttp, python-dotenv, Pillow
Run:      py main.py
Needs the MESSAGE CONTENT INTENT enabled in the Developer Portal (Bot tab) for the gank image step.
"""
import asyncio
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse


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
}

EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
GANK_SECONDS = 120
LB_MODES = ("duel", "spar")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def find_gif() -> str:
    for p in (os.path.join(BASE_DIR, "panel.gif"), os.path.join(BASE_DIR, "assets", "panel.gif")):
        if os.path.isfile(p):
            return p
    return ""


GIF_PATH = find_gif()
DATA_DIR = os.getenv("DATA_DIR", "").strip() or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
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
class Store:
    def __init__(self, path: str):
        self.path = path
        self.d = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.d = json.load(fh)
            except Exception as exc:
                print(f"Could not read {path}, starting fresh: {exc}")
                self.d = {}
        self.d.setdefault("counters", {})
        for mode in ("duel", "spar"):
            self.d["counters"].setdefault(mode, 0)
        for key in ("challenges", "profiles", "tickets", "archive", "guilds"):
            self.d.setdefault(key, {})
        self.d.setdefault("matches", [])

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=2)
        os.replace(tmp, self.path)


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
    for key in ("ch", "cat", "lb_msg", "readme"):
        g.setdefault(key, {})
    return g


def is_authorized(gid) -> bool:
    return bool(gid) and bool(store.d["guilds"].get(str(gid), {}).get("authorized"))


def cfg_channel_id(gid, key):
    return gcfg(gid)["ch"].get(key)


# ============================================================ manual server link (set while requesting)
LINK_HOSTS = {"roblox.com", "www.roblox.com", "web.roblox.com", "roseal.live", "www.roseal.live"}
JOB_RE = re.compile(r"^[0-9a-fA-F-]{32,40}$")
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SERVER_HELP = (
    "> Paste a Roblox server link. These work:\n"
    "> - a **Roseal** join link (`roseal.live/join?placeId=...&gameInstanceId=...`)\n"
    "> - a Roblox join link (`roblox.com/games/start?placeId=...&gameInstanceId=...`)\n"
    "> - a **private server** link (`roblox.com/games/ID/name?privateServerLinkCode=...`)\n"
    "> - a Roblox **share** link (`roblox.com/share?code=...&type=Server`)")


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

    if path.rstrip("/").lower() == "/share" or "code" in params and params.get("type", "").lower() == "server":
        code = params.get("code", "")
        if CODE_RE.match(code) and params.get("type", "server").lower() == "server":
            return {"kind": "share", "code": code}
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
        return None, f"https://www.roblox.com/share?code={srv['code']}&type=Server"
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
    return f"{n} Round" if n == 1 else f"{n} Rounds"


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
    """True when this server is authorized. Otherwise replies and returns False."""
    gid = gid or interaction.guild_id
    if is_authorized(gid):
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
def is_staff(user) -> bool:
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
    buttons = [discord.ui.Button(label="More Info", url=ticket_jump(t))]
    await send_cfg(t["guild_id"], "log", build_card(f"{m['name']}-ticket #{t['number']:03d} Closed", sections,
                                             accent=GREY, buttons=buttons))


def archive_of(t) -> dict:
    return {
        "mode": t["mode"], "number": t["number"], "rounds": t["rounds"], "p": t["p"], "scores": t["scores"],
        "guild_id": t["guild_id"], "created": t["created"], "result_url": t.get("result_url"), "server": t.get("server"),
        "reopened": False, "deleted_by": None, "deleted_at": 0,
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
    return build_card(f"{m['name']}-ticket #{a['number']:03d} Deleted", [fields, ["-# Rivalz"]],
                      accent=GREY, buttons=buttons)


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

    cat_id = gcfg(guild.id)["cat"].get("tickets")
    category = guild.get_channel(cat_id) if cat_id else None  # None = top of the channel list if /build was not run
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
    }
    msg = await channel.send(view=ticket_view(t, ping=True))
    t["message_id"] = msg.id
    store.d["tickets"][str(channel.id)] = t
    store.save()
    await log_opened(t)
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
            placeholder="Select number of rounds",
            options=[
                discord.SelectOption(label="1 Round", value="1", description="Single deciding round."),
                discord.SelectOption(label="3 Rounds", value="3", description="Best of three."),
                discord.SelectOption(label="5 Rounds", value="5", description="Best of five."),
                discord.SelectOption(label="Custom", value="custom", description="Type your own number next."),
            ],
        )
        self.add_item(discord.ui.Label(text="Number of Rounds", component=self.rounds))

        self.server = discord.ui.TextInput(placeholder="Roseal / Roblox server link", min_length=10, max_length=400)
        self.add_item(discord.ui.Label(text="Server Link", description="Your server link. Your rival joins it from the ticket.",
                                       component=self.server))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not is_authorized(interaction.guild_id):
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
            ["> Press **Enter Rounds** and type how many rounds you want.", f"> Allowed: `1` to `{MAX_ROUNDS}`."],
            accent=MODES[mode]["colour"], buttons=[button], timeout=600), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ChallengeModal error: {error!r}")


class CustomRoundsModal(discord.ui.Modal):
    def __init__(self, mode: str, rival_id: int, server: dict):
        super().__init__(title="Custom Rounds", timeout=600)
        self.mode, self.rival_id, self.server = mode, rival_id, server
        self.value = discord.ui.TextInput(placeholder=f"1 to {MAX_ROUNDS}", min_length=1, max_length=3)
        self.add_item(discord.ui.Label(text="Number of Rounds", component=self.value))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        raw = self.value.value.strip()
        if not raw.isdigit() or not (1 <= int(raw) <= MAX_ROUNDS):
            return await notice(interaction, "Request Failed", f"> Rounds must be a number from `1` to `{MAX_ROUNDS}`.")
        try:
            rival = await interaction.guild.fetch_member(self.rival_id)
        except discord.NotFound:
            return await notice(interaction, "Request Failed", "> That rival is not in this server.")
        await submit_challenge(interaction, self.mode, rival, int(raw), self.server)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"CustomRoundsModal error: {error!r}")


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
        await TICKET_HANDLERS[self.act](interaction, t)


# ---------- join (manual server link -> Roseal -> Roblox)
async def h_join(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can join this match.")
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
        lines += ["> Tap **Open Roseal**. Roseal opens first and then launches Roblox.",
                  "> If Roseal does not open, use **Open Roblox Directly**."]
    else:
        lines += ["> Tap **Open Roblox Directly** to launch Roblox.",
                  "> Roseal only supports public server links, so it is not shown for this one."]
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
        self.add_item(discord.ui.Label(text="Your Score", description=f"Rounds you won out of {t['rounds']}.",
                                       component=self.score))

    async def on_submit(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(self.cid))
        if not t or t["status"] != "open":
            return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
        raw = self.score.value.strip()
        rounds = t["rounds"]
        if not raw.isdigit() or int(raw) > rounds:
            return await notice(interaction, "Invalid Score", f"> Enter a whole number from `0` to `{rounds}`.")
        value = int(raw)
        other = t["scores"][1 - self.side]

        if other is not None and value + other > rounds:
            # the two scores cannot both be true, so wipe both and let the players add them again
            t["scores"] = [None, None]
            store.save()
            await notice(interaction, "Scores Reset",
                         [f"> Your score `{value}` and your rival's score `{other}` add up to `{value + other}`, "
                          f"but this match only has `{rounds}` rounds.",
                          "> Both scores were reset. Add them again honestly."], accent=RED)
            await refresh_ticket(t)
            await update_result(t)
            try:
                await interaction.channel.send(view=build_card(
                    "Score Conflict",
                    [f"> The two scores added up to more than `{rounds}` rounds, so both were reset.",
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
    if mine is None:
        return await notice(interaction, "Not Allowed", "> Only the two fighters can add a score.")
    if side is not None and mine != side:
        return await notice(interaction, "Not Your Score",
                            f"> This button is for **{md(t['p'][side]['roblox_name'])}**. You can only add your own score.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
    await interaction.response.send_modal(ScoreModal(t["channel_id"], mine, t))


async def h_s0(interaction, t):
    await add_score(interaction, t, 0)


async def h_s1(interaction, t):
    await add_score(interaction, t, 1)


async def h_score(interaction, t):  # older tickets only
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
        sections.append(["> Send your gank image in this channel within `2 minutes`.",
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
    store.save()


async def finalize_match(channel, t):
    """Gank received or skipped: record the match and offer Delete Ticket straight away."""
    if t["status"] != "ended":
        return
    t["status"] = "finished"
    store.save()
    GANK_TASKS.pop(t["channel_id"], None)
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


async def gank_wait(channel, cid: int):
    t = store.d["tickets"].get(str(cid))
    if not t:
        return
    ids = {p["discord_id"] for p in t["p"]}

    def check(m):
        return (m.channel.id == channel.id and not m.author.bot and m.author.id in ids
                and image_attachment(m) is not None)

    timeout = max(1.0, t["gank_deadline"] - time.time())
    try:
        msg = await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        t = store.d["tickets"].get(str(cid))
        if t and t["status"] == "ended":
            await finalize_match(channel, t)  # gank is optional, so just carry on
        return
    t = store.d["tickets"].get(str(cid))
    if not t or t["status"] != "ended":
        return
    t["gank_url"] = image_attachment(msg).url
    t["gank_by"] = msg.author.id
    store.save()
    await finalize_match(channel, t)


def start_gank(channel, t):
    old = GANK_TASKS.pop(channel.id, None)
    if old:
        old.cancel()
    GANK_TASKS[channel.id] = asyncio.create_task(gank_wait(channel, t["channel_id"]))


async def h_end(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can end this match.")
    if t["status"] != "open":
        return await notice(interaction, "Already Ended", "> This match has already ended.")
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
    await update_result(t, force=True)
    await log_closed(t, interaction.user.id)


async def h_skip(interaction, t):
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
        atts = list(interaction.message.attachments) if interaction.message else []
        att = (next((a for a in atts if a.filename.startswith("gank_full")), None)
               or next((a for a in atts if a.filename.startswith("gank_card")), None))
        if not att:
            return await notice(interaction, "No Gank", "> The gank image is no longer available.", accent=GREY)
        await interaction.response.send_message(view=build_card(
            "Gank", ["> Full-size gank image. Tap the image to enlarge it."],
            media=att.url, accent=GREY, timeout=600,
            buttons=[discord.ui.Button(label="Open Original", url=att.url)]), ephemeral=True)


async def h_delete(interaction, t):
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
    ["`1` Select a mode, enter your Roblox username (saved for next time) and paste your **server link**.",
     "`2` Your rival presses **Accept** and a private ticket with a voice channel opens.",
     "`3` Fight, then each player adds their own score. The winner is declared automatically.",
     "`4` Press **End**. Send the gank image if you have one (optional), then press **Delete Ticket**."],
    ["> **Settings** is optional. Fake challenges or scores lead to a blacklist."],
]

PANEL_INTRO = "> Challenge a rival to a **Duel** or train in a **Spar**. Private ticket, voice channel and tracked scores."
PANEL_MODES = (
    "**Duel** `Competitive` A match with a winner and a loser.\n"
    "**Spar** `Practice` Warm up and train together."
)
PANEL_HOW = (
    "### How it works\n"
    "> `1` Pick **Duel** or **Spar** below\n"
    "> `2` Enter your username, rival, rounds and server link\n"
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
    """Slash commands only work in authorized servers (the owner can always use them)."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID or is_authorized(interaction.guild_id):
            return True
        await safe_reply(interaction, "Rivalz is not authorized in this server yet. The bot owner needs to run /authorize.")
        return False


def migrate_legacy():
    """Your original server keeps working: it is marked authorized and keeps its existing channels and categories."""
    for guild in bot.guilds:
        if str(guild.id) in store.d["guilds"] or not guild.get_channel(LEGACY["panel"]):
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
        intents.message_content = True  # needed to read the gank image
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=GuardedTree)
        self.session: aiohttp.ClientSession = None
        self.roblox: RobloxClient = None
        self._ready_once = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.session)
        self.add_dynamic_items(ChallengeButton, TicketButton, ReopenButton, OpenGankButton, LbButton)
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
        migrate_legacy()
        backfill_matches()
        for guild in self.guilds:
            gcfg(guild.id)
            await ensure_readme(guild)
            if is_authorized(guild.id) and gcfg(guild.id)["ch"].get("panel"):
                await post_panel(guild)
        store.save()
        lb_loop.start()  # draws the leaderboards now and then every 10 minutes

    async def on_guild_join(self, guild: discord.Guild):
        gcfg(guild.id)
        store.save()
        await ensure_readme(guild)
        await tell_owner(f"Rivalz joined **{guild.name}** (`{guild.id}`). Run `/authorize` there, then `/build`.")


bot = Rivalz()


async def post_panel(guild) -> bool:
    channel_id = cfg_channel_id(guild.id, "panel")
    if not channel_id:
        print(f"{guild.name}: no panel channel yet (run /build).")
        return False
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
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
            await channel.send(view=PanelView(), file=discord.File(GIF_PATH, filename="panel.gif"))
        else:
            print("panel.gif not found next to main.py (or in assets/), posting the panel without the banner.")
            await channel.send(view=PanelView())
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


# ============================================================ leaderboard image (Pillow)
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
LB_ROWS_PER_PAGE = 7
LB_PERIODS = {"day": "Daily", "week": "Weekly", "month": "Monthly", "all": "All-time"}
C_BG1, C_BG2 = (9, 22, 29), (14, 44, 56)
C_CARD, C_CARD_LINE = (17, 38, 46), (31, 100, 112)
C_PANEL, C_ROW, C_ROW_LINE = (18, 53, 64), (22, 38, 46), (42, 68, 82)
C_TAB_BG, C_TAB_ON = (27, 43, 51), (14, 143, 168)
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
        f = lb_font("bold", int(size * 0.5))
        d.text((size / 2, size / 2), (fallback_text or "?")[:1].upper(), font=f, fill=C_TEXT, anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _fit_text(draw, text, font_weight, size, max_w, min_size=14):
    while size > min_size:
        f = lb_font(font_weight, size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 2
    f = lb_font(font_weight, min_size)
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
    import math
    pts = [(cx + r * math.cos(math.radians(60 * i - 30)), cy + r * math.sin(math.radians(60 * i - 30))) for i in range(6)]
    draw.polygon(pts, fill=colour)
    inner = [(cx + (r - 8) * math.cos(math.radians(60 * i - 30)), cy + (r - 8) * math.sin(math.radians(60 * i - 30)))
             for i in range(6)]
    draw.polygon(inner, fill=tuple(max(0, c - 70) for c in colour))


def render_leaderboard(mode_name, period, page, pages, entries, podium, viewer=None, avatars=None):
    """entries: list of dicts (rank, uid, name, w, l) for this page's rows.
    podium: list of up to 3 such dicts (page 1 only). viewer: dict(rank,name,w,l,uid,up) or None.
    avatars: {uid: bytes}."""
    from PIL import Image, ImageDraw, ImageFilter
    avatars = avatars or {}
    show_podium = page == 0
    row_h, gap = 92, 14

    # ---- layout height
    y = 225
    if show_podium:
        y = 700 + 30
    if viewer:
        y += 92 + gap + 8
    y += len(entries) * (row_h + gap)
    if not show_podium and not entries:
        y += 140
    height = y + 100

    im = Image.new("RGB", (LB_W, height), C_BG1)
    strip = Image.new("RGB", (1, height))
    for yy in range(height):  # vertical gradient
        t = yy / max(1, height - 1)
        strip.putpixel((0, yy), tuple(int(C_BG1[i] + (C_BG2[i] - C_BG1[i]) * t) for i in range(3)))
    im = strip.resize((LB_W, height))
    d = ImageDraw.Draw(im, "RGBA")

    # ---- card
    d.rounded_rectangle((40, 40, LB_W - 40, height - 40), radius=34, fill=C_CARD + (235,), outline=C_CARD_LINE, width=3)
    d.text((LB_W / 2, 92), "Leaderboard", font=lb_font("bold", 40), fill=C_TEXT, anchor="mm")

    # ---- tabs
    d.rounded_rectangle((168, 132, 832, 188), radius=18, fill=C_TAB_BG)
    tab_w = (832 - 168 - 20) / 4
    for i, (key, label) in enumerate(LB_PERIODS.items()):
        x0 = 178 + i * tab_w
        if key == period:
            d.rounded_rectangle((x0, 140, x0 + tab_w - 4, 180), radius=12, fill=C_TAB_ON)
        d.text((x0 + (tab_w - 4) / 2, 160), label, font=lb_font("medium", 22), fill=C_TEXT, anchor="mm")

    # ---- podium
    if show_podium:
        d.rounded_rectangle((62, 215, LB_W - 62, 700), radius=40, fill=C_PANEL, outline=(34, 150, 170), width=2,
                            corners=(False, False, True, True))
        slots = {1: (500, 400, 220, C_GOLD), 2: (240, 395, 156, C_SILVER), 3: (760, 395, 156, C_BRONZE)}
        by_rank = {e["rank"]: e for e in podium}
        for rank, (cx, cy, size, colour) in slots.items():
            e = by_rank.get(rank)
            top = cy - size // 2
            glow = Image.new("RGBA", (size + 80, size + 80), (0, 0, 0, 0))
            ImageDraw.Draw(glow).rounded_rectangle((40, 40, size + 40, size + 40), radius=36, fill=colour + (70,))
            glow = glow.filter(ImageFilter.GaussianBlur(14))
            im.paste(glow, (cx - size // 2 - 40, top - 40), glow)
            d = ImageDraw.Draw(im, "RGBA")
            border = 6 if rank == 1 else 4
            d.rounded_rectangle((cx - size // 2 - border, top - border, cx + size // 2 + border, top + size + border),
                                radius=int(size * 0.2) + border, fill=colour if e else (60, 80, 90))
            av = _rounded_avatar(avatars.get(e["uid"]) if e else None, size, int(size * 0.2),
                                 (e or {}).get("name", "?"), tint={1: (14, 130, 160), 2: (50, 90, 150), 3: (170, 110, 40)}[rank])
            im.paste(av, (cx - size // 2, top), av)
            d = ImageDraw.Draw(im, "RGBA")
            if rank == 1:
                _crown(d, cx, top - 6)
            else:
                _medal(d, cx, top - 28, colour)
            name_y = top + size + 26
            if e:
                f, txt = _fit_text(d, f"@{e['name']}", "medium", 26 if rank == 1 else 22, 230)
                d.text((cx, name_y), txt, font=f, fill=colour, anchor="mm")
                d.text((cx, name_y + 36), f"{e['w']} Wins", font=lb_font("bold", 30 if rank == 1 else 26), fill=colour, anchor="mm")
                d.text((cx, name_y + 70), f"W {e['w']}   L {e['l']}", font=lb_font("regular", 18), fill=C_MUTED, anchor="mm")
            else:
                d.text((cx, name_y + 10), "No one yet", font=lb_font("medium", 20), fill=C_MUTED, anchor="mm")
        d.text((LB_W / 2, 668), f"Ranked by wins  |  {mode_name}", font=lb_font("regular", 18), fill=C_MUTED, anchor="mm")
        y = 730
    else:
        y = 225

    def draw_row(y, e, label_name=None, sub=None, highlight=False):
        box = (60, y, LB_W - 60, y + row_h)
        if highlight:
            d.rounded_rectangle(box, radius=24, fill=(15, 58, 71), outline=C_CYAN, width=2)
        else:
            d.rounded_rectangle(box, radius=24, fill=C_ROW, outline=C_ROW_LINE, width=2)
        d.text((105, y + row_h / 2), str(e["rank"]) if e["rank"] else "-", font=lb_font("bold", 30), fill=C_TEXT, anchor="mm")
        av = _rounded_avatar(avatars.get(e["uid"]), 64, 14, e.get("name", "?"))
        im.paste(av, (152, y + 14), av)
        title = label_name or f"@{e['name']}"
        f, txt = _fit_text(d, title, "medium", 26, 520)
        d.text((235, y + 30), txt, font=f, fill=C_TEXT, anchor="lm")
        d.text((235, y + 62), sub or f"W {e['w']}  L {e['l']}", font=lb_font("regular", 19), fill=C_MUTED, anchor="lm")
        d.text((LB_W - 96, y + row_h / 2), f"{e['w']} Wins", font=lb_font("bold", 28), fill=C_CYAN, anchor="rm")

    if viewer:
        if viewer["rank"]:
            sub = f"W {viewer['w']}  L {viewer['l']}"
            if viewer.get("up"):
                sub += f"   |   Rank up in {viewer['up']} win{'s' if viewer['up'] != 1 else ''}"
            else:
                sub += "   |   You are #1"
        else:
            sub = "Win a match to get ranked"
        draw_row(y, {"rank": viewer["rank"], "uid": viewer["uid"], "name": viewer["name"], "w": viewer["w"], "l": viewer["l"]},
                 label_name="Your Current Rank", sub=sub, highlight=True)
        y += 92 + gap + 8

    if not entries and not (show_podium and podium):
        d.text((LB_W / 2, y + 60), "No matches recorded yet", font=lb_font("medium", 26), fill=C_MUTED, anchor="mm")
        y += 140
    for e in entries:
        draw_row(y, e)
        y += row_h + gap

    d.text((LB_W / 2, y + 22), f"Page {page + 1} of {pages}", font=lb_font("regular", 20), fill=C_MUTED, anchor="mm")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=False)
    return out.getvalue()


# ============================================================ match stats (feeds the leaderboards)
def period_start(period: str) -> float:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight.timestamp()
    if period == "week":
        return (midnight - timedelta(days=now.weekday())).timestamp()
    if period == "month":
        return midnight.replace(day=1).timestamp()
    return 0.0


def unrecord_match(gid, mode, number) -> int:
    before = len(store.d["matches"])
    store.d["matches"] = [x for x in store.d["matches"] if not (x["g"] == gid and x["m"] == mode and x["n"] == number)]
    removed = before - len(store.d["matches"])
    if removed:
        store.save()
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
    store.d["matches"].append(row)
    store.save()
    return True


def backfill_matches():
    """One time: turn matches that were already saved (deleted + still open tickets) into leaderboard records."""
    if store.d.get("matches_backfilled"):
        return
    seen = {(x["g"], x["m"], x["n"]) for x in store.d["matches"]}
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
            store.d["matches"].append(row)
    store.d["matches_backfilled"] = True
    store.save()


def standings(gid, mode, period):
    since = period_start(period)
    tally = {}
    for x in store.d["matches"]:
        if x["g"] != gid or x["m"] != mode or x["ts"] < since:
            continue
        tally.setdefault(x["w"], [0, 0])[0] += 1
        tally.setdefault(x["l"], [0, 0])[1] += 1
    return sorted(((uid, w, l) for uid, (w, l) in tally.items()), key=lambda r: (-r[1], r[2], r[0]))


def rank_info(rows, uid):
    """(rank, wins, losses, top_percent) for a user, or None when unranked."""
    for i, (u, w, l) in enumerate(rows):
        if u == uid:
            return i + 1, w, l, (i + 1) / len(rows) * 100
    return None


USER_CACHE = {}


async def lookup_user(uid):
    hit = USER_CACHE.get(uid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1], hit[2]
    name, data = f"user{str(uid)[-4:]}", None
    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        name = user.name
        data = await user.display_avatar.replace(size=128, format="png").read()
    except Exception:
        pass
    USER_CACHE[uid] = (time.time(), name, data)
    return name, data


async def render_board(guild, mode, period, page, viewer=None):
    """Returns (png bytes, total pages, page used)."""
    rows = standings(guild.id, mode, period)
    pages = max(1, math.ceil(max(0, len(rows) - 3) / LB_ROWS_PER_PAGE))
    page = min(max(page, 0), pages - 1)
    start = 3 + page * LB_ROWS_PER_PAGE
    podium_rows = rows[:3] if page == 0 else []
    list_rows = rows[start:start + LB_ROWS_PER_PAGE]
    ids = {r[0] for r in podium_rows + list_rows}
    if viewer:
        ids.add(viewer.id)
    ids = list(ids)
    found = await asyncio.gather(*(lookup_user(u) for u in ids))
    names = {u: f[0] for u, f in zip(ids, found)}
    avatars = {u: f[1] for u, f in zip(ids, found) if f[1]}

    def entry(rank, r):
        return {"rank": rank, "uid": r[0], "name": names.get(r[0], "unknown"), "w": r[1], "l": r[2]}

    podium = [entry(i + 1, r) for i, r in enumerate(podium_rows)]
    entries = [entry(start + i + 1, r) for i, r in enumerate(list_rows)]
    view_row = None
    if viewer:
        info = rank_info(rows, viewer.id)
        if info:
            above = rows[info[0] - 2][1] if info[0] > 1 else None
            view_row = {"rank": info[0], "uid": viewer.id, "name": names.get(viewer.id, viewer.name), "w": info[1],
                        "l": info[2], "up": max(1, above - info[1] + 1) if above is not None else 0}
        else:
            view_row = {"rank": 0, "uid": viewer.id, "name": names.get(viewer.id, viewer.name), "w": 0, "l": 0, "up": 0}
    png = await asyncio.to_thread(render_leaderboard, MODES[mode]["name"], period, page, pages,
                                  entries, podium, view_row, avatars)
    return png, pages, page


# ============================================================ leaderboard buttons
class LbButton(discord.ui.DynamicItem[discord.ui.Button],
               template=r"rv:lb:(?P<kind>per|prev|next|me):(?P<mode>duel|spar):(?P<period>day|week|month|all):(?P<page>\d+)"):
    def __init__(self, kind, mode, period, page=0, *, label=None, style=None, disabled=False, row=None):
        if label is None:
            label = {"per": LB_PERIODS[period], "prev": "◀", "next": "▶", "me": "My Rank"}[kind]
        if style is None:
            style = discord.ButtonStyle.success if kind == "me" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label, style=style, disabled=disabled, row=row,
                                           custom_id=f"rv:lb:{kind}:{mode}:{period}:{page}"))
        self.kind, self.mode, self.period, self.page = kind, mode, period, page

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["kind"], match["mode"], match["period"], int(match["page"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"LbButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not await guard(interaction):
            return
        if self.kind == "me":
            return await send_my_rank(interaction, self.mode)
        already_private = bool(interaction.message and interaction.message.flags.ephemeral)
        if already_private:
            await interaction.response.defer()  # edit this private board in place
        else:
            await interaction.response.defer(ephemeral=True)  # open a private board for this player only
        png, pages, page = await render_board(interaction.guild, self.mode, self.period, self.page, interaction.user)
        file = discord.File(io.BytesIO(png), filename="leaderboard.png")
        view = lb_view(self.mode, self.period, page, pages, public=False)
        if already_private:
            await interaction.edit_original_response(attachments=[file], view=view)
        else:
            await interaction.followup.send(file=file, view=view, ephemeral=True)


def lb_view(mode, period, page, pages, public):
    view = discord.ui.View(timeout=None if public else 600)
    for key in LB_PERIODS:
        view.add_item(LbButton("per", mode, key, 0, row=0,
                               style=discord.ButtonStyle.primary if key == period else discord.ButtonStyle.secondary))
    if public:
        view.add_item(LbButton("me", mode, period, 0, row=0))
        return view
    view.add_item(LbButton("prev", mode, period, max(0, page - 1), row=1, disabled=page <= 0))
    view.add_item(discord.ui.Button(label=f"Page {page + 1} / {pages}", disabled=True, row=1))
    view.add_item(LbButton("next", mode, period, min(pages - 1, page + 1), row=1, disabled=page >= pages - 1))
    view.add_item(LbButton("me", mode, period, 0, row=1))
    return view


async def send_my_rank(interaction: discord.Interaction, mode: str):
    await interaction.response.defer(ephemeral=True)
    gid, user = interaction.guild.id, interaction.user
    m = MODES[mode]
    overall = rank_info(standings(gid, mode, "all"), user.id)
    if overall:
        top = [f"> **User:** `@{esc(user.name)}`",
               f"> **Rank:** #{overall[0]} (Top {overall[3]:.2f}%)",
               f"> **Wins:** {overall[1]}",
               f"> **Losses:** {overall[2]}"]
    else:
        top = [f"> **User:** `@{esc(user.name)}`", "> **Rank:** Unranked", f"> **Wins:** 0",
               f"> Win a {m['name']} to get on the board."]
    lines = []
    for key in ("day", "week", "month"):
        info = rank_info(standings(gid, mode, key), user.id)
        label = LB_PERIODS[key]
        lines.append(f"> **{label}:** " + (f"#{info[0]} (Top {info[3]:.2f}%) | {info[1]}W {info[2]}L" if info else "Unranked"))
    await interaction.followup.send(view=build_card(
        f"📊 Your Current Stats ({m['name']})", [top, lines], accent=m["colour"]), ephemeral=True)


async def refresh_leaderboards(guild, repost=False):
    """Draw the Duel and Spar boards into their channels (edits the one message the bot keeps there)."""
    cfg = gcfg(guild.id)
    for mode in LB_MODES:
        cid = cfg["ch"].get(f"{mode}_lb")
        chan = (guild.get_channel(cid) if cid else None)
        if not chan:
            continue
        try:
            png, pages, _ = await render_board(guild, mode, "day", 0, None)
        except Exception as exc:
            print(f"Could not draw the {mode} leaderboard: {exc!r}")
            continue
        view = lb_view(mode, "day", 0, pages, public=True)
        msg_id = cfg["lb_msg"].get(mode)
        if repost:
            try:
                async for message in chan.history(limit=30):
                    if message.author.id == bot.user.id:
                        try:
                            await message.delete()
                        except discord.HTTPException:
                            pass
            except discord.HTTPException:
                pass
            msg_id = None
        if msg_id:
            try:
                await chan.get_partial_message(msg_id).edit(
                    attachments=[discord.File(io.BytesIO(png), filename="leaderboard.png")], view=view)
                continue
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                print(f"Could not update the {mode} leaderboard: {exc!r}")
                continue
        try:
            msg = await chan.send(file=discord.File(io.BytesIO(png), filename="leaderboard.png"), view=view)
            cfg["lb_msg"][mode] = msg.id
            store.save()
        except discord.HTTPException as exc:
            print(f"Could not post the {mode} leaderboard (needs View Channel, Send Messages, Attach Files): {exc!r}")


LB_TASKS = {}


def schedule_lb_refresh(gid):
    """Refresh a few seconds after a match is recorded (several matches in a row become one refresh)."""
    old = LB_TASKS.get(gid)
    if old and not old.done():
        return

    async def later():
        await asyncio.sleep(8)
        guild = bot.get_guild(gid)
        if guild and is_authorized(gid):
            await refresh_leaderboards(guild)

    LB_TASKS[gid] = asyncio.create_task(later())


@tasks.loop(minutes=10)
async def lb_loop():
    for guild in list(bot.guilds):
        if is_authorized(guild.id):
            await refresh_leaderboards(guild)  # also rolls Daily/Weekly/Monthly over when the period changes


# ============================================================ read-me channel + /authorize + /build
README_TEXT = ("Welcome to **Rivalz**. Rivalz coordinates Duel and Spar matchmaking, tracks every score, "
               "and ranks fighters on the Duel and Spar leaderboards.")


def readme_view(gid):
    ok = is_authorized(gid)
    sections = [[f"**Status:** {'Authorized' if ok else 'Not Authorized'}"], [README_TEXT]]
    if not ok:
        sections.append(["-# The bot owner must run `/authorize` before Rivalz can be used in this server."])
    elif not gcfg(gid)["ch"].get("panel"):
        sections.append(["-# The bot owner must run `/build` to create the Rivalz channels."])
    buttons = [discord.ui.Button(label="Developer Profile", url=DEVELOPER_URL)] if DEVELOPER_URL else None
    return build_card("Rivalz", sections, accent=GREEN if ok else GREY, buttons=buttons)


async def tell_owner(text):
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(text)
    except Exception:
        pass


async def ensure_readme(guild):
    """Create the read-me channel (once) and keep its status card up to date."""
    rd = gcfg(guild.id)["readme"]
    chan = guild.get_channel(rd["channel"]) if rd.get("channel") else None
    if chan is None:
        if not guild.me.guild_permissions.manage_channels:
            print(f"Cannot create read-me in {guild.name}: missing Manage Channels.")
            await tell_owner(f"Rivalz joined **{guild.name}** but needs **Manage Channels** to create the read-me channel.")
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False, add_reactions=False,
                create_public_threads=False, create_private_threads=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                  read_message_history=True, manage_channels=True),
        }
        try:
            chan = await guild.create_text_channel("read-me", overwrites=overwrites, topic="About Rivalz",
                                                   reason="Rivalz read-me")
        except discord.HTTPException as exc:
            print(f"Could not create read-me in {guild.name}: {exc!r}")
            return
        rd["channel"], rd["message"] = chan.id, None
    view = readme_view(guild.id)
    if rd.get("message"):
        try:
            await chan.get_partial_message(rd["message"]).edit(view=view)
            store.save()
            return
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            print(f"Could not update read-me in {guild.name}: {exc!r}")
            return
    try:
        msg = await chan.send(view=view)
        rd["message"] = msg.id
    except discord.HTTPException as exc:
        print(f"Could not post read-me in {guild.name}: {exc!r}")
    store.save()


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

    async def text(key, name, parent, overwrites, topic):
        c = guild.get_channel(chs[key]) if chs.get(key) else None
        if isinstance(c, discord.TextChannel):
            kept.append(c.mention)
            return c
        c = await guild.create_text_channel(name, category=parent, overwrites=overwrites, topic=topic, reason="Rivalz /build")
        chs[key] = c.id
        created.append(c.mention)
        return c

    main = await category("main", "RIVALZ", {})
    await text("panel", "matchmaking", main, read_only, "Start a Duel or Spar from here.")
    await text("results", "results", main, read_only, "Finished match results.")
    await text("log", "match-logs", main, private, "Rivalz ticket logs (staff only).")
    boards = await category("lb", "LEADERBOARDS", {})
    await text("duel_lb", "duel-leaderboard", boards, read_only, "Duel leaderboard, ranked by wins.")
    await text("spar_lb", "spar-leaderboard", boards, read_only, "Spar leaderboard, ranked by wins.")
    await category("tickets", "TICKETS", private)
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
    await interaction.response.defer(ephemeral=True)
    api_ms = round((time.perf_counter() - started) * 1000)
    pending = sum(1 for c in store.d["challenges"].values() if c["status"] == "pending")
    open_tickets = sum(1 for t in store.d["tickets"].values() if t["status"] != "finished")
    await interaction.followup.send(view=build_card(
        "Rivalz Online",
        [[field("Gateway", f"{round(bot.latency * 1000)} ms"),
          field("API Round Trip", f"{api_ms} ms"),
          field("Uptime", fmt_uptime(time.time() - START_TIME))],
         [field("Servers", len(bot.guilds)), field("Pending Challenges", pending), field("Active Tickets", open_tickets)]],
        accent=GREEN), ephemeral=True)


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
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ok = await post_panel(interaction.guild)
    await interaction.followup.send("Panel reposted." if ok else "Could not post the panel. Run /build first.", ephemeral=True)


@bot.tree.command(name="authorize", description="Owner only: authorize Rivalz to be used in this server.")
@app_commands.guild_only()
async def authorize(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    cfg = gcfg(interaction.guild.id)
    was = cfg["authorized"]
    cfg["authorized"] = True
    cfg["authorized_by"], cfg["authorized_at"] = interaction.user.id, int(time.time())
    store.save()
    await ensure_readme(interaction.guild)
    built = bool(cfg["ch"].get("panel"))
    lines = ["> Rivalz is now authorized in this server." if not was else "> This server was already authorized."]
    lines.append("> Everything is set up. Use `/build` any time to repair missing channels." if built
                 else "> Next, run `/build` to create the categories and channels.")
    await interaction.followup.send(view=build_card("Server Authorized", [lines], accent=GREEN), ephemeral=True)


@bot.tree.command(name="build", description="Owner only: create the Rivalz categories and channels (run /authorize first).")
@app_commands.guild_only()
async def build(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    guild = interaction.guild
    if not is_authorized(guild.id):
        return await notice(interaction, "Not Authorized", "> Run `/authorize` in this server first, then `/build`.", accent=GREY)
    await interaction.response.defer(ephemeral=True)
    if not guild.me.guild_permissions.manage_channels:
        return await notice(interaction, "Missing Permission", "> Rivalz needs **Manage Channels** to build the channels.")
    try:
        created, kept = await build_guild(guild)
    except discord.HTTPException as exc:
        print(f"Build failed: {exc!r}")
        return await notice(interaction, "Build Failed",
                            "> Discord refused one of the channels. Check that Rivalz has **Manage Channels** and **Manage Roles**, then run `/build` again.")
    await ensure_readme(guild)
    await post_panel(guild)
    await refresh_leaderboards(guild, repost=True)
    sections = [["> Rivalz is built and running in this server."]]
    if created:
        sections.append(["**Created**"] + [f"> {x}" for x in created])
    if kept:
        sections.append(["**Already there**"] + [f"> {x}" for x in kept])
    await interaction.followup.send(view=build_card("Build Complete", sections, accent=GREEN), ephemeral=True)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error: {error!r}")
    await safe_reply(interaction, "Something went wrong. Please try again.")


if __name__ == "__main__":
    if not TOKEN or TOKEN.startswith("PUT_"):
        raise SystemExit("Set DISCORD_TOKEN (in .env or your host's Variables) first.")
    try:
        bot.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Turn on MESSAGE CONTENT INTENT in the Discord Developer Portal (your app > Bot > Privileged Gateway "
            "Intents), save, then restart the bot.")