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
            import aiohttp, dotenv  # noqa: F401
            import discord
            return hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "Label")
        except ImportError:
            return False

    if ok():
        return
    pkgs = ["discord.py>=2.6.0", "aiohttp>=3.10", "python-dotenv>=1.0"]
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
PANEL_CHANNEL_ID = env_int("PANEL_CHANNEL_ID", 1568343031294459904)
TICKET_CATEGORY_ID = env_int("TICKET_CATEGORY_ID", 1568343167437115423)
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID")
STAFF_ROLE_ID = env_int("STAFF_ROLE_ID")
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "").strip().strip("'\"")
EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
GANK_SECONDS = 120

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GIF_PATH = os.path.join(BASE_DIR, "assets", "panel.gif")
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
        for key in ("challenges", "profiles", "tickets"):
            self.d.setdefault(key, {})

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=2)
        os.replace(tmp, self.path)


store = Store(DATA_FILE)


# ============================================================ roblox helpers
def dig(obj, names):
    """Find the first non-empty value for any key in `names`, searching nested JSON."""
    wanted = {n.lower() for n in names}
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in wanted and isinstance(v, (str, int)) and str(v).strip():
                    return str(v)
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def parse_private_link(link: str):
    """Return {'place','code','share'} for a Roblox private server link, or None if it is not one."""
    try:
        u = urlparse(link.strip())
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if u.scheme not in ("http", "https") or not (host == "roblox.com" or host.endswith(".roblox.com")):
        return None
    q = parse_qs(u.query)
    code = (q.get("privateServerLinkCode") or q.get("linkCode") or [None])[0]
    m = re.search(r"/games/(\d+)", u.path)
    place = m.group(1) if m else (q.get("placeId") or [None])[0]
    if code and place:
        return {"place": place, "code": code, "share": None}
    share = (q.get("code") or [None])[0]
    if u.path.startswith("/share") and share and (q.get("type") or [""])[0].lower() == "server":
        return {"place": None, "code": None, "share": share}
    return None


def roseal_url(server: dict):
    if server.get("place") and server.get("code"):
        return f"https://www.roseal.live/join?placeId={server['place']}&linkCode={server['code']}"
    return None


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

    async def resolve_share(self, code: str):
        """Turn a roblox.com/share?code=...&type=Server link into (placeId, linkCode) when possible."""
        st, data = await self._post(
            "https://apis.roblox.com/sharelinks/v1/resolve-link",
            {"linkId": code, "linkType": "Server"}, auth=True)
        if not data:
            return None, None
        place, link_code = dig(data, ["placeId"]), dig(data, ["linkCode"])
        return (place, link_code) if place and link_code else (None, None)


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


def ticket_view(t, ping=False):
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    status = t["status"]
    view = discord.ui.LayoutView(timeout=None)
    if ping:
        view.add_item(discord.ui.TextDisplay(f"<@{p0['discord_id']}> <@{p1['discord_id']}>"))

    c = discord.ui.Container(accent_colour=m["colour"] if status == "open" else GREY)
    c.add_item(discord.ui.TextDisplay(f"## {m['name']} Ticket #{t['number']:03d}"))

    for label, p in (("Challenger", p0), ("Rival", p1)):
        c.add_item(discord.ui.Separator())
        body = discord.ui.TextDisplay(
            f"### [{md(p['roblox_display'])}]({profile_url(p)}) (`@{esc(p['roblox_name'])}`)\n"
            f"> **{label}:** <@{p['discord_id']}>")
        if p.get("avatar"):
            c.add_item(discord.ui.Section(body, accessory=discord.ui.Thumbnail(p["avatar"])))
        else:
            c.add_item(body)

    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(
        "### Match Info\n"
        f"> **Mode:** `{m['name']}`\n"
        f"> **Rounds:** `{rounds_text(t['rounds'])}`\n"
        f"> **Status:** `{STATUS_TEXT[status]}`"))

    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(f"### Scoreboard\n> {scoreboard(t)}"))

    if status != "open":
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(f"### Result\n> `{esc(result_text(t))}`"))

    c.add_item(discord.ui.Separator())
    session = (
        "### Session Info\n"
        "> **Server:** `Private Server` `Locked`\n"
        f"> **Host:** `{esc(p0['roblox_name'])}`"
    )
    if t.get("vc_id"):
        session += f"\n> **Voice:** <#{t['vc_id']}>"
    c.add_item(discord.ui.TextDisplay(session))

    c.add_item(discord.ui.Separator())
    c.add_item(discord.ui.TextDisplay(
        f"-# Ticket opened by <@{p0['discord_id']}> | `{esc(p0['roblox_name'])}` | {STATUS_TEXT[status]}"))

    if status == "open":
        buttons = [TicketButton("join"), TicketButton("prof"), TicketButton("score"), TicketButton("end"), TicketButton("guide")]
    else:
        buttons = [TicketButton("prof"), TicketButton("guide")]
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


# ============================================================ ticket creation + logs
async def log_card(guild, t, title, extra=None, media_file=None):
    if not LOG_CHANNEL_ID:
        return
    log = guild.get_channel(LOG_CHANNEL_ID)
    if not log:
        return
    m = MODES[t["mode"]]
    p0, p1 = t["p"]
    sections = [[
        f"**Ticket:** <#{t['channel_id']}>" if extra is None else field("Ticket", t["name"]),
        f"**Challenger:** <@{p0['discord_id']}> (`{esc(p0['roblox_name'])}`)",
        f"**Rival:** <@{p1['discord_id']}> (`{esc(p1['roblox_name'])}`)",
        field("Rounds", rounds_text(t["rounds"])),
    ]]
    if extra:
        sections.append(extra)
    view = build_card(f"{m['name']} #{t['number']:03d} {title}", sections, accent=GREY if extra else m["colour"],
                      media=f"attachment://{media_file.filename}" if media_file else None)
    try:
        if media_file:
            await log.send(view=view, file=media_file)
        else:
            await log.send(view=view)
    except discord.HTTPException as exc:
        print(f"Log send failed: {exc!r}")


async def create_ticket(guild: discord.Guild, ch):
    m = MODES[ch["mode"]]
    store.d["counters"][ch["mode"]] += 1
    n = store.d["counters"][ch["mode"]]
    store.save()
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
        "server": {"link": req["server_link"], "place": req.get("place"), "code": req.get("code")},
        "gank_url": None, "gank_by": None, "gank_deadline": 0, "gank_msg": None,
    }
    msg = await channel.send(view=ticket_view(t, ping=True))
    t["message_id"] = msg.id
    store.d["tickets"][str(channel.id)] = t
    store.save()
    await log_card(guild, t, "Ticket Created")
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
    if not prof or not prof.get("roblox_name") or not prof.get("server_link"):
        return await notice(interaction, "Settings Needed",
                            "> Press **Settings** on the panel and save your Roblox username and private server link first.")

    pending = [c for c in store.d["challenges"].values()
               if c["status"] == "pending" and c["requester"] == interaction.user.id]
    if any(c["opponent"] == rival.id and c["mode"] == mode for c in pending):
        return await notice(interaction, "Request Failed", "> You already have a pending request to this rival.")
    if len(pending) >= 3:
        return await notice(interaction, "Request Failed", "> You have too many pending requests. Wait for a reply first.")

    ch = {
        "id": uuid.uuid4().hex[:8], "mode": mode, "status": "pending", "guild_id": guild.id,
        "requester": interaction.user.id, "opponent": rival.id, "rounds": rounds,
        "req": {k: prof.get(k) for k in ("roblox_id", "roblox_name", "roblox_display", "server_link", "place", "code")},
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
         [field("Mode", m["name"]), field("Rounds", rounds_text(rounds)), field("Server", "Your saved private server")],
         [f"> Expires <t:{int(ch['expires'])}:R>."]],
        accent=m["colour"]), ephemeral=True)


class ChallengeModal(discord.ui.Modal):
    def __init__(self, mode: str):
        super().__init__(title=f"{MODES[mode]['name']} Request", timeout=600)
        self.mode = mode

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
    "score": ("Add Score", discord.ButtonStyle.primary),
    "end": ("End", discord.ButtonStyle.danger),
    "guide": ("Guide", discord.ButtonStyle.secondary),
    "gank": ("Upload Gank", discord.ButtonStyle.primary),
    "finish": ("Finish", discord.ButtonStyle.success),
    "delete": ("Delete Ticket", discord.ButtonStyle.danger),
}


class TicketButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"rv:t:(?P<act>join|prof|score|end|guide|gank|finish|delete)"):
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


async def h_join(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can join this match.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> This match has already ended.")
    server = t["server"]
    buttons = []
    ru = roseal_url(server)
    if ru:
        buttons.append(discord.ui.Button(label="Open Roseal", url=ru))
    buttons.append(discord.ui.Button(label="Open Roblox Directly", url=server["link"]))
    await interaction.response.send_message(view=build_card(
        "Join Match",
        [["> Tap **Open Roseal**. Roseal opens first and then launches Roblox.",
          "> If Roseal does not open, use **Open Roblox Directly**."],
         [field("Server", "Private Server"), field("Host", t["p"][0]["roblox_name"])]],
        accent=GREEN, buttons=buttons, timeout=600), ephemeral=True)


async def h_prof(interaction, t):
    buttons = [discord.ui.Button(label=f"{p['roblox_name']}'s Profile"[:80], url=profile_url(p)) for p in t["p"]]
    await interaction.response.send_message(view=build_card(
        "Open Profile", ["> Choose whose Roblox profile you want to open."], buttons=buttons, timeout=600), ephemeral=True)


async def h_guide(interaction, t):
    await interaction.response.send_message(view=build_card(
        "Ticket Guide",
        [["> `Join` opens your private server through Roseal and then Roblox.",
          "> `Profile` lets you pick which player's Roblox profile to open.",
          "> `Join VC` takes you to this match's voice channel."],
         ["> `Add Score` records the rounds **you** won. You can only edit your own score.",
          "> `End` closes the match once both scores are in."],
         ["> After ending, send the gank image in this channel within `2 minutes`, press `Finish`, then `Delete Ticket`."]],
        timeout=600), ephemeral=True)


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
        if not raw.isdigit() or int(raw) > t["rounds"]:
            return await notice(interaction, "Invalid Score", f"> Enter a whole number from `0` to `{t['rounds']}`.")
        value = int(raw)
        other = t["scores"][1 - self.side]
        if other is not None and value + other > t["rounds"]:
            return await notice(interaction, "Invalid Score",
                                f"> Both scores together cannot be more than `{t['rounds']}` rounds. Check your number.")
        t["scores"][self.side] = value
        store.save()
        await interaction.response.send_message(view=build_card(
            "Score Saved", [[field("Your Score", value), f"> {scoreboard(t)}"]], accent=GREEN), ephemeral=True)
        await refresh_ticket(t)
        try:
            await interaction.channel.send(view=build_card(
                "Score Recorded", [f"> **{md(t['p'][self.side]['roblox_name'])}** added their score."], accent=GREY))
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ScoreModal error: {error!r}")


async def h_score(interaction, t):
    side = side_of(t, interaction.user.id)
    if side is None:
        return await notice(interaction, "Not Allowed", "> Only the two fighters can add a score.")
    if t["status"] != "open":
        return await notice(interaction, "Match Ended", "> Scores can no longer be changed.")
    await interaction.response.send_modal(ScoreModal(t["channel_id"], side, t))


# ---------- end, gank, finish, delete
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
        sections.append([f"> Send your gank image in this channel within `2 minutes`.",
                         f"> Closes <t:{int(t['gank_deadline'])}:R>."])
        buttons = [TicketButton("gank"), TicketButton("finish")]
    elif state == "received":
        sections.append([f"> Gank image received from <@{t['gank_by']}>.", "> Press **Finish** to record the match."])
        buttons = [TicketButton("finish")]
    elif state == "expired":
        sections.append(["> No gank image arrived in time.",
                         "> Press **Upload Gank** to try again or **Finish** to continue without it."])
        buttons = [TicketButton("gank"), TicketButton("finish")]
    else:
        sections.append(["> Gank step complete." if t.get("gank_url") else "> Finished without a gank image."])
    return build_card("Match Ended", sections, accent=GREY, buttons=buttons)


async def edit_gank_prompt(t, state):
    try:
        channel = bot.get_channel(t["channel_id"]) or await bot.fetch_channel(t["channel_id"])
        await channel.get_partial_message(t["gank_msg"]).edit(view=gank_view(t, state))
    except discord.HTTPException as exc:
        print(f"Could not edit the gank prompt: {exc!r}")


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
        if t and t["status"] == "ended" and not t.get("gank_url"):
            await edit_gank_prompt(t, "expired")
        return
    t = store.d["tickets"].get(str(cid))
    if not t or t["status"] != "ended":
        return
    t["gank_url"] = image_attachment(msg).url
    t["gank_by"] = msg.author.id
    store.save()
    await edit_gank_prompt(t, "received")
    try:
        await msg.reply(view=build_card("Gank Received", ["> Your gank image was saved. Press **Finish** to record the match."],
                                        accent=GREEN))
    except discord.HTTPException:
        pass


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
    t["gank_deadline"] = time.time() + GANK_SECONDS
    t["gank_url"] = None
    store.save()
    await interaction.response.edit_message(view=ticket_view(t))
    msg = await interaction.followup.send(view=gank_view(t, "waiting"), wait=True)
    t["gank_msg"] = msg.id
    store.save()
    start_gank(interaction.channel, t)


async def h_gank(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can upload the gank.")
    if t["status"] != "ended":
        return await notice(interaction, "Not Ready", "> End the match first.")
    if t.get("gank_url"):
        return await notice(interaction, "Already Received", "> The gank image is saved. Press **Finish**.", accent=GREEN)
    task = GANK_TASKS.get(t["channel_id"])
    if t["gank_deadline"] > time.time() and task and not task.done():
        return await notice(interaction, "Window Open",
                            f"> Send the image in this channel. It closes <t:{int(t['gank_deadline'])}:R>.", accent=GREEN)
    t["gank_deadline"] = time.time() + GANK_SECONDS
    store.save()
    await interaction.response.edit_message(view=gank_view(t, "waiting"))
    start_gank(interaction.channel, t)


async def h_finish(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters can finish this match.")
    if t["status"] != "ended":
        return await notice(interaction, "Not Ready", "> End the match first.")
    if not t.get("gank_url") and t["gank_deadline"] > time.time():
        return await notice(interaction, "Waiting For Gank",
                            f"> Send the gank image here or wait for the timer. It closes <t:{int(t['gank_deadline'])}:R>.")
    task = GANK_TASKS.pop(t["channel_id"], None)
    if task:
        task.cancel()
    t["status"] = "finished"
    store.save()
    await interaction.response.edit_message(view=gank_view(t, "done"))
    await interaction.followup.send(view=build_card(
        "This match has been recorded",
        ["> You can delete this ticket now. Its voice channel is removed with it."],
        accent=GREY, buttons=[TicketButton("delete")]))
    await refresh_ticket(t)


async def send_final_log(guild, t):
    media = None
    if t.get("gank_url"):
        try:
            async with bot.session.get(t["gank_url"], timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    data = await r.read()
                    ext = os.path.splitext(urlparse(t["gank_url"]).path)[1] or ".png"
                    if len(data) < 8_000_000:
                        media = discord.File(io.BytesIO(data), filename=f"gank{ext}")
        except Exception as exc:
            print(f"Could not download the gank image: {exc!r}")
    extra = [f"**Result:** `{esc(result_text(t))}`", f"**Score:** {scoreboard(t)}",
             f"**Gank:** `{'Attached below' if media else 'Not submitted'}`"]
    await log_card(guild, t, "Match Recorded", extra=extra, media_file=media)


async def h_delete(interaction, t):
    if not can_act(t, interaction.user):
        return await notice(interaction, "Not Allowed", "> Only the two fighters or staff can delete this ticket.")
    if t["status"] != "finished":
        return await notice(interaction, "Not Ready", "> Press **Finish** first.")
    await interaction.response.send_message(view=build_card(
        "Ticket Deleting", ["> This ticket and its voice channel will be removed in `5` seconds."], accent=GREY))
    guild = interaction.guild
    await send_final_log(guild, t)
    store.d["tickets"].pop(str(t["channel_id"]), None)
    store.save()
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
    "join": h_join, "prof": h_prof, "score": h_score, "end": h_end,
    "guide": h_guide, "gank": h_gank, "finish": h_finish, "delete": h_delete,
}


# ============================================================ settings + help + main panel
class ConfigModal(discord.ui.Modal):
    def __init__(self, prof: dict):
        super().__init__(title="Configuration", timeout=600)
        self.username = discord.ui.TextInput(placeholder="Your Roblox username", default=prof.get("roblox_name") or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", component=self.username))
        self.link = discord.ui.TextInput(placeholder="https://www.roblox.com/games/...?privateServerLinkCode=...",
                                         default=prof.get("server_link") or None, max_length=300)
        self.add_item(discord.ui.Label(text="Private Server Link", description="Saved once and used in every match.",
                                       component=self.link))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rb = await bot.roblox.user_by_name(self.username.value.strip())
        if not rb:
            return await notice(interaction, "Not Saved", "> That Roblox username was not found.")
        link = self.link.value.strip()
        info = parse_private_link(link)
        if not info:
            return await notice(interaction, "Not Saved",
                                ["> That is not a Roblox private server link.",
                                 "> Open your private server on Roblox and copy the link that contains `privateServerLinkCode`."])
        place, code = info["place"], info["code"]
        if info["share"]:
            place, code = await bot.roblox.resolve_share(info["share"])
        store.d["profiles"][str(interaction.user.id)] = {
            "roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"],
            "server_link": link, "place": place, "code": code,
        }
        store.save()
        await interaction.followup.send(view=build_card(
            "Configuration Saved",
            [[field("Roblox", f"{rb['name']} (Display: {rb['display']})"), field("Private Server", "Saved and locked")],
             ["> This same private server link is used in every match you start.",
              "> Press **Settings** again whenever you want to edit it."]],
            accent=GREEN, buttons=[discord.ui.Button(label="Roblox Profile", url=f"https://www.roblox.com/users/{rb['id']}/profile")]),
            ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ConfigModal error: {error!r}")


HELP_SECTIONS = [
    ["> Matchmaking pairs you with a rival for a **Duel** or a **Spar**. Follow the steps below."],
    ["### 1. Save your settings",
     "> Press **Settings** and enter your Roblox username and your private server link.",
     "> Open the game on Roblox, make or open your private server and copy its link.",
     "> It is saved once, and the same link is used in every match you start."],
    ["### 2. Send a challenge",
     "> Pick **Duel** or **Spar** from the menu.",
     "> Choose your rival and the number of rounds. Pick **Custom** to type your own number.",
     "> Your rival gets a DM with **Accept** and **Decline**."],
    ["### 3. Your rival accepts",
     "> A private ticket and a voice channel open for the two of you.",
     "> A rival who never saved a Roblox username is asked for it when they accept."],
    ["### 4. Fight",
     "> Press **Join** in the ticket. Roseal opens first, then Roblox.",
     "> Use **Profile** to open either player's Roblox profile.",
     "> Use **Join VC** to talk between rounds."],
    ["### 5. Score and finish",
     "> Each player presses **Add Score** and enters only their own score, honestly.",
     "> When both scores are in, press **End**. Send the gank image in the channel within `2 minutes`.",
     "> Press **Finish**, then **Delete Ticket**."],
    ["### Good to know",
     "> Challenges expire if your rival does not answer in time.",
     "> Your rival needs direct messages open to receive a challenge.",
     "> Fake challenges and fake scores lead to a blacklist."],
]

PANEL_INTRO = "> Challenge a rival to a duel or train in a spar. Private matches, tracked scores, one ticket per fight."
PANEL_WELCOME = (
    "**Welcome** to the arena. Matchmaking connects you with a rival of your choice, opens a private ticket with its "
    "own voice channel and keeps score so every fight is on record. No random lobbies, no teamers, just you and your rival."
)
PANEL_WHAT = (
    "### What is this\n"
    "> A matchmaking panel for **duels** and **spars** on Roblox battleground games.\n"
    "> Pick a mode, choose your rival and send the challenge. Your rival answers in their DMs.\n"
    "> Once they accept, a private ticket and a voice channel open for both of you."
)
PANEL_MODES = (
    "### Duel\n"
    "> `Competitive` An all-out match with a winner and a loser.\n"
    "> Agree on the rounds, fight in your private server and record the final score.\n"
    "> Settle a rivalry, test your build or prove who is better.\n"
    "### Spar\n"
    "> `Practice` A friendly session to warm up, learn matchups and sharpen your combos.\n"
    "> Nothing is on the line, so try new tactics and trade tips between rounds.\n"
    "> Great before a big fight or after a long break."
)
PANEL_HOW = (
    "### How it works\n"
    "> `1` Open **Settings** and save your Roblox username and private server link\n"
    "> `2` Select **Duel** or **Spar** from the menu below\n"
    "> `3` Pick your rival and the number of rounds\n"
    "> `4` Your rival gets a DM and presses **Accept** or **Decline**\n"
    "> `5` A ticket opens with a voice channel. Join your server and fight\n"
    "> `6` Both players add their own score, end the match and send the gank image"
)
PANEL_RULES = (
    "### Rules\n"
    "> **No Fake Challenges**: Spam or fake requests result in a blacklist.\n"
    "> **Honest Scores**: Add only the rounds you actually won.\n"
    "> **Active Profile**: Use the Roblox account you are fighting on.\n"
    "> **Respect Your Rival**: Toxicity and rage quitting are not tolerated.\n"
    "> **Gank Proof**: Send the gank image within `2 minutes` when the match ends."
)
PANEL_SETTINGS = (
    "### Settings\n"
    "> Save your **Roblox username** and **private server link** once. They fill in every match automatically, "
    "and your server link stays the same every time. Tap **Settings** again to edit them."
)
PANEL_START = "### Start a match\n> Select **Duel** or **Spar** from the menu below."


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
        prof = store.d["profiles"].get(str(interaction.user.id))
        if not prof or not prof.get("roblox_name") or not prof.get("server_link"):
            await interaction.response.send_message(view=build_card(
                "Settings Needed",
                ["> Press **Settings** first and save your Roblox username and private server link.",
                 "> You only have to do this once."], accent=RED), ephemeral=True)
        else:
            await interaction.response.send_modal(ChallengeModal(mode))
        try:  # reset the menu so the same mode can be picked again
            await interaction.message.edit(view=PanelView(), attachments=list(interaction.message.attachments))
        except discord.HTTPException:
            pass


class PanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=0x2B2D31)
        c.add_item(discord.ui.TextDisplay("## Matchmaking"))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(PANEL_INTRO))
        if os.path.exists(GIF_PATH):
            gallery = discord.ui.MediaGallery()
            gallery.add_item(media="attachment://panel.gif")
            c.add_item(gallery)
        c.add_item(discord.ui.TextDisplay(PANEL_WELCOME))
        for text in (PANEL_WHAT, PANEL_MODES, PANEL_HOW, PANEL_RULES, PANEL_SETTINGS):
            c.add_item(discord.ui.Separator())
            c.add_item(discord.ui.TextDisplay(text))
        row = discord.ui.ActionRow()
        row.add_item(SettingsButton())
        row.add_item(HelpButton())
        c.add_item(row)
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(PANEL_START))
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
        self.add_dynamic_items(ChallengeButton, TicketButton)
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
            print("assets/panel.gif not found, posting the panel without the banner.")
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
         ["> The cookie is only needed to resolve Roblox share links in Settings."]],
        accent=GREEN if user else RED), ephemeral=True)


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