"""
Rivalz - Matchmaking bot for Duels and Spars
Requires: discord.py >= 2.6 (Components V2 + modal selects), aiohttp, python-dotenv
Run:      py main.py
Needs the MESSAGE CONTENT INTENT enabled in the Developer Portal (Bot tab) for the gank image step.
"""
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
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
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "").strip().strip("'\"")  # alt account cookie, used to read live servers

# Channels (main category 1568343167437115423 holds the panel, logs and results channels)
PANEL_CHANNEL_ID = 1568343031294459904
LOG_CHANNEL_ID = 1568525004164042842
RESULTS_CHANNEL_ID = 1568527659334377492
MAIN_CATEGORY_ID = 1568343167437115423
TICKET_CATEGORY_ID = 1568527718419664927  # tickets + voice channels are created here

EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
GANK_SECONDS = 120

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
        for key in ("challenges", "profiles", "tickets", "archive"):
            self.d.setdefault(key, {})

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
        self.csrf = ""

    async def _post(self, url, body, *, auth=False):
        headers = {"Content-Type": "application/json"}
        if auth and ROBLOX_COOKIE:
            headers["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"
            headers["x-csrf-token"] = self.csrf
        for _ in range(2):
            try:
                async with self.s.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
                    if r.status == 403 and auth and "x-csrf-token" in r.headers:
                        self.csrf = r.headers["x-csrf-token"]
                        headers["x-csrf-token"] = self.csrf
                        continue
                    try:
                        return r.status, await r.json(content_type=None)
                    except Exception:
                        return r.status, None
            except Exception:
                return 0, None
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

    async def live_server(self, user_id: int):
        """Where a player is right now. status: ok / no_cookie / api / offline / hidden."""
        if not ROBLOX_COOKIE:
            return {"status": "no_cookie"}
        st, data = await self._post("https://presence.roblox.com/v1/presence/users", {"userIds": [user_id]}, auth=True)
        try:
            p = data["userPresences"][0]
        except Exception:
            return {"status": "api"}
        if p.get("userPresenceType") != 2:
            return {"status": "offline"}
        if not (p.get("placeId") and p.get("gameId")):
            return {"status": "hidden"}  # in a game, but Roblox hides the server (joins are off)
        return {"status": "ok", "place": str(p["placeId"]), "job": str(p["gameId"])}


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
    await send_to(LOG_CHANNEL_ID, build_card(f"{m['name']}-ticket #{t['number']:03d} Opened", sections,
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
    await send_to(LOG_CHANNEL_ID, build_card(f"{m['name']}-ticket #{t['number']:03d} Closed", sections,
                                             accent=GREY, buttons=buttons))


def archive_of(t) -> dict:
    return {
        "mode": t["mode"], "number": t["number"], "rounds": t["rounds"], "p": t["p"], "scores": t["scores"],
        "guild_id": t["guild_id"], "created": t["created"], "result_url": t.get("result_url"),
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
            "id": "reopen", "mode": a["mode"], "rounds": a["rounds"],
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
    chan = await get_channel_safe(RESULTS_CHANNEL_ID)
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
    msg = await send_to(RESULTS_CHANNEL_ID, view, files=files)
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

    category = guild.get_channel(TICKET_CATEGORY_ID) or await guild.fetch_channel(TICKET_CATEGORY_ID)
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
        "status": "open", "p": players, "scores": [None, None], "message_id": None,
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
async def submit_challenge(interaction: discord.Interaction, mode: str, rival, rounds: int):
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
        "requester": interaction.user.id, "opponent": rival.id, "rounds": rounds,
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
         [field("Mode", m["name"]), field("Rounds", rounds_text(rounds))],
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

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rival = self.opponent.values[0]
        choice = self.rounds.values[0] if self.rounds.values else None
        if choice is None:
            return await notice(interaction, "Request Failed", "> Select the number of rounds.")

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
            return await submit_challenge(interaction, self.mode, rival, int(choice))

        mode, rival_id = self.mode, rival.id
        button = discord.ui.Button(label="Enter Rounds", style=discord.ButtonStyle.primary)

        async def open_rounds(i: discord.Interaction):
            await i.response.send_modal(CustomRoundsModal(mode, rival_id))

        button.callback = open_rounds
        await interaction.followup.send(view=build_card(
            "Custom Rounds",
            ["> Press **Enter Rounds** and type how many rounds you want.", f"> Allowed: `1` to `{MAX_ROUNDS}`."],
            accent=MODES[mode]["colour"], buttons=[button], timeout=600), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ChallengeModal error: {error!r}")


class CustomRoundsModal(discord.ui.Modal):
    def __init__(self, mode: str, rival_id: int):
        super().__init__(title="Custom Rounds", timeout=600)
        self.mode, self.rival_id = mode, rival_id
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
        await submit_challenge(interaction, self.mode, rival, int(raw))

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
        await TICKET_HANDLERS[self.act](interaction, t)


# ---------- join (live server lookup -> Roseal -> Roblox)
def roseal_join_url(place, job) -> str:
    return f"https://www.roseal.live/join?placeId={place}&gameInstanceId={job}"


def roblox_join_url(place, job) -> str:
    return f"https://www.roblox.com/games/start?placeId={place}&gameInstanceId={job}"


async def h_join(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can join this match.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> This match has already ended.")
    side = side_of(t, interaction.user.id)
    target = t["p"][1] if side in (None, 0) else t["p"][0]  # join the OTHER player's live server
    await interaction.response.defer(ephemeral=True)
    res = await bot.roblox.live_server(target["roblox_id"])
    name = target["roblox_name"]

    if res["status"] == "ok":
        place, job = res["place"], res["job"]
        await interaction.followup.send(view=build_card(
            "Join Match",
            [[f"> Joining **{md(name)}**'s live server.",
              "> Tap **Open Roseal**. Roseal opens first and then launches Roblox.",
              "> If Roseal does not open, use **Open Roblox Directly**."],
             [field("Server Of", name), field("Server ID", f"{job[:8]}...")]],
            accent=GREEN, timeout=600,
            buttons=[discord.ui.Button(label="Open Roseal", url=roseal_join_url(place, job)),
                     discord.ui.Button(label="Open Roblox Directly", url=roblox_join_url(place, job))]), ephemeral=True)
        return

    reasons = {
        "no_cookie": "> The bot has no Roblox account connected yet. Ask the developer to set `ROBLOX_COOKIE`.",
        "api": "> Roblox did not answer. Try again in a moment.",
        "offline": f"> **{md(name)}** is not in a Roblox game right now. Ask them to join the game, then press **Join** again.",
        "hidden": (f"> **{md(name)}** is in a game, but their joins are off so the server cannot be read. "
                   "They need Roblox Privacy > Who can join me set to **Everyone**."),
    }
    await notice(interaction, "Server Not Found", reasons.get(res["status"], reasons["api"]), accent=GREY)


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
    await send_to(LOG_CHANNEL_ID, deleted_view(a, key))

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
    ["`1` Select a mode and enter your Roblox username (saved for next time).",
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
    "> `2` Enter your username, rival and rounds\n"
    "> `3` Your rival accepts in their DMs\n"
    "> `4` Fight, add scores and finish"
)


class SettingsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Settings", style=discord.ButtonStyle.secondary, custom_id="rv:settings")

    async def callback(self, interaction: discord.Interaction):
        prof = store.d["profiles"].get(str(interaction.user.id), {})
        await interaction.response.send_modal(ConfigModal(prof))


class HelpButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Help", style=discord.ButtonStyle.primary, custom_id="rv:help")

    async def callback(self, interaction: discord.Interaction):
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
class Rivalz(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # needed to read the gank image
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.session: aiohttp.ClientSession = None
        self.roblox: RobloxClient = None
        self._ready_once = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.session)
        self.add_dynamic_items(ChallengeButton, TicketButton, ReopenButton, OpenGankButton)
        await self.tree.sync()
        expiry_loop.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Rivalz online as {self.user} ({self.user.id})")
        if not self._ready_once:
            self._ready_once = True
            await post_panel()


bot = Rivalz()


async def post_panel():
    try:
        channel = bot.get_channel(PANEL_CHANNEL_ID) or await bot.fetch_channel(PANEL_CHANNEL_ID)
        try:
            async for message in channel.history(limit=50):
                if message.author.id == bot.user.id:
                    try:
                        await message.delete()
                    except discord.HTTPException:
                        pass
        except discord.Forbidden:
            print("Missing Read Message History in the panel channel; old panels were not removed.")
        if os.path.exists(GIF_PATH):
            await channel.send(view=PanelView(), file=discord.File(GIF_PATH, filename="panel.gif"))
        else:
            print("panel.gif not found next to main.py (or in assets/), posting the panel without the banner.")
            await channel.send(view=PanelView())
        print("Panel posted.")
    except discord.Forbidden:
        print("Could not post the panel: the bot needs View Channel and Send Messages in the panel channel.")
    except discord.NotFound:
        print("Could not post the panel: PANEL_CHANNEL_ID is wrong or the bot is not in that server.")
    except Exception as exc:
        print(f"Could not post the panel: {exc!r}")


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


@bot.tree.command(name="ping-roblox", description="Check the Roblox connection and the bot's cookie.")
async def ping_roblox(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    t = time.perf_counter()
    user = await bot.roblox.user_by_name("Roblox")
    api = f"Online ({round((time.perf_counter() - t) * 1000)} ms)" if user else "Unreachable"
    cookie = "Not set"
    if ROBLOX_COOKIE:
        try:
            async with bot.session.get(
                "https://users.roblox.com/v1/users/authenticated",
                headers={"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    cookie = f"Valid ({data.get('name', 'unknown')})"
                else:
                    cookie = f"Rejected (HTTP {r.status})"
        except Exception:
            cookie = "Check failed"
    await interaction.followup.send(view=build_card(
        "Roblox Status",
        [[field("Roblox API", api), field("Bot Cookie", cookie)],
         ["> The cookie is used by the Join button to find a player's live server."]],
        accent=GREEN if user and cookie.startswith("Valid") else RED), ephemeral=True)


@bot.tree.command(name="panel", description="Owner only: repost the Rivalz panel.")
async def panel(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await post_panel()
    await interaction.followup.send("Panel reposted.", ephemeral=True)


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