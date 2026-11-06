"""
Rivalz - Duel and Spar matchmaking bot
Requires: discord.py >= 2.6 (Components V2 + modal selects), aiohttp, python-dotenv
Run:      py main.py
"""
import asyncio
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
RALVORA_API_URL = os.getenv("RALVORA_API_URL", "").strip()
RALVORA_API_KEY = os.getenv("RALVORA_API_KEY", "").strip()
EXPIRY_MINUTES = env_int("CHALLENGE_EXPIRY_MINUTES", 30)
MAX_ROUNDS = env_int("MAX_ROUNDS", 25)
DATA_DIR = os.getenv("DATA_DIR", "").strip() or os.path.dirname(os.path.abspath(__file__))
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "rivalz_data.json")

MODES = {
    "duel": {
        "name": "Duel",
        "prefix": "clash",
        "taunt": "> Prove who is stronger. Accept to open a private ticket.",
        "colour": 0xE74C3C,
    },
    "spar": {
        "name": "Spar",
        "prefix": "warmup",
        "taunt": "> Warm up and sharpen your skills together. Accept to open a private ticket.",
        "colour": 0x3498DB,
    },
}
GREEN, RED, GREY = 0x2ECC71, 0xE74C3C, 0x95A5A6


# ============================================================ storage
class Store:
    def __init__(self, path: str):
        self.path = path
        self.d = {"counters": {"duel": 0, "spar": 0}, "challenges": {}, "usernames": {}, "tickets": {}}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    self.d.update(json.load(fh))
            except Exception as exc:
                print(f"Could not read {path}, starting fresh: {exc}")
        for key, default in (("counters", {"duel": 0, "spar": 0}), ("challenges", {}), ("usernames", {}), ("tickets", {})):
            self.d.setdefault(key, default)
        for mode in ("duel", "spar"):
            self.d["counters"].setdefault(mode, 0)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.d, fh, indent=2)
        os.replace(tmp, self.path)


store = Store(DATA_FILE)


# ============================================================ roblox + region helpers
EU = {"GB", "DE", "FR", "NL", "IE", "ES", "IT", "PL", "SE", "NO", "FI", "DK", "BE", "AT", "CH",
      "PT", "CZ", "RO", "HU", "GR", "UA", "BG", "RS", "HR", "SK", "LT", "LV", "EE", "LU"}
NA = {"US", "CA", "MX"}
SA = {"BR", "AR", "CL", "CO", "PE", "UY", "EC", "VE"}
OCE = {"AU", "NZ"}


def region_label(cc, country=None, city=None) -> str:
    cc = (cc or "").upper()
    if cc in NA:
        code = "NA"
    elif cc in EU:
        code = "EU"
    elif cc in SA:
        code = "SA"
    elif cc in OCE:
        code = "OCE"
    elif cc in ("JP", "IN"):
        code = cc
    elif cc:
        code = "AS"
    else:
        return country or "Unknown"
    where = city or country or cc
    return f"{code} ({where})"


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


def parse_server_link(link: str):
    """Return (place_id, job_id) found in a Roblox link."""
    try:
        u = urlparse(link)
        q = parse_qs(u.query)
        place = (q.get("placeId") or [None])[0]
        job = (q.get("gameInstanceId") or [None])[0]
        if not place:
            m = re.search(r"/games/(\d+)", u.path)
            place = m.group(1) if m else None
        return place, job
    except Exception:
        return None, None


def valid_roblox_link(link: str) -> bool:
    try:
        u = urlparse(link)
        host = (u.netloc or "").lower()
        return u.scheme in ("http", "https") and (host == "roblox.com" or host.endswith(".roblox.com") or host == "ro.blox.com")
    except Exception:
        return False


class RobloxClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.csrf = ""

    async def _post(self, url, body, *, auth=False, ua=None):
        headers = {"Content-Type": "application/json"}
        if ua:
            headers["User-Agent"] = ua
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

    async def find_server_link(self, user_id: int):
        """Live server link of a player, using the bot's cookie (needs Roblox to expose the player's server)."""
        st, data = await self._post("https://presence.roblox.com/v1/presence/users", {"userIds": [user_id]}, auth=True)
        try:
            p = data["userPresences"][0]
            if p.get("userPresenceType") == 2 and p.get("placeId") and p.get("gameId"):
                return f"https://www.roblox.com/games/start?placeId={p['placeId']}&gameInstanceId={p['gameId']}"
        except Exception:
            pass
        return None

    async def server_ip(self, place_id, job_id):
        st, data = await self._post(
            "https://gamejoin.roblox.com/v1/join-game-instance",
            {"placeId": int(place_id), "isTeleport": False, "gameId": job_id, "gameJoinAttemptId": str(uuid.uuid4())},
            auth=True,
            ua="Roblox/WinInet",
        )
        if not data:
            return None
        js = data.get("joinScript")
        if isinstance(js, str):
            try:
                js = json.loads(js)
            except Exception:
                js = None
        if not isinstance(js, dict):
            return None
        try:
            return js["UdmuxEndpoints"][0]["Address"]
        except Exception:
            return js.get("MachineAddress")

    async def ralvora_lookup(self, ip: str):
        if not RALVORA_API_URL:
            return None
        headers = {}
        if RALVORA_API_KEY:
            headers["Authorization"] = f"Bearer {RALVORA_API_KEY}"
            headers["X-API-Key"] = RALVORA_API_KEY
        try:
            async with self.s.get(RALVORA_API_URL, params={"ip": ip}, headers=headers,
                                  timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return None
                data = await r.json(content_type=None)
        except Exception:
            return None
        # Adjust these key names if the Ralvora response uses different ones.
        cc = dig(data, ["countryCode", "country_code", "cc"])
        country = dig(data, ["country", "country_name"])
        city = dig(data, ["city"])
        if cc or country:
            return region_label(cc, country, city)
        direct = dig(data, ["serverRegion", "server_region", "regionCode", "region_code", "region"])
        return direct

    async def fallback_lookup(self, ip: str):
        try:
            async with self.s.get(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city",
                                  timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json(content_type=None)
            if data.get("status") == "success":
                return region_label(data.get("countryCode"), data.get("country"), data.get("city"))
        except Exception:
            pass
        return None

    async def detect_region(self, link: str):
        place, job = parse_server_link(link)
        if not (place and job):
            return None
        ip = await self.server_ip(place, job)
        if not ip:
            return None
        return (await self.ralvora_lookup(ip)) or (await self.fallback_lookup(ip))


# ============================================================ embed (card) builders
def esc(v) -> str:
    return str(v).replace("`", "'")


def field(label: str, value) -> str:
    return f"**{label}:** `{esc(value)}`"


def rounds_text(n: int) -> str:
    return f"{n} Round" if n == 1 else f"{n} Rounds"


def build_card(title, sections, *, accent=None, buttons=None, top_text=None):
    view = discord.ui.LayoutView(timeout=None)
    if top_text:
        view.add_item(discord.ui.TextDisplay(top_text))
    container = discord.ui.Container(accent_colour=accent)
    container.add_item(discord.ui.TextDisplay(f"## {title}"))
    for sec in sections:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(sec if isinstance(sec, str) else "\n".join(sec)))
    if buttons:
        container.add_item(discord.ui.Separator(visible=False))
        for i in range(0, len(buttons), 5):
            row = discord.ui.ActionRow()
            for b in buttons[i:i + 5]:
                row.add_item(b)
            container.add_item(row)
    view.add_item(container)
    return view


def link_buttons(ch):
    out = [discord.ui.Button(label="Roblox Profile", url=f"https://www.roblox.com/users/{ch['roblox_id']}/profile")]
    if ch.get("link") and ch["link"].startswith(("http://", "https://")):
        out.append(discord.ui.Button(label="Server Link", url=ch["link"]))
    return out


def roblox_line(ch):
    return field("Requester Roblox", f"{ch['roblox_name']} (Display: {ch['roblox_display']})")


def challenge_dm_view(ch, requester):
    m = MODES[ch["mode"]]
    sections = [
        [f"> **{requester.display_name}** has challenged you to a **{m['name']}**.", m["taunt"]],
        [
            f"**Challenger:** {requester.mention} (`@{requester.name}`)",
            roblox_line(ch),
            field("Mode", m["name"]),
            field("Rounds", rounds_text(ch["rounds"])),
            field("Region", ch["region"] or "Not detected"),
        ],
        [f"> This challenge expires <t:{int(ch['expires'])}:R>."],
    ]
    buttons = [ChallengeButton("acc", ch["id"]), ChallengeButton("dec", ch["id"])] + link_buttons(ch)
    return build_card("You Have Been Challenged", sections, accent=m["colour"], buttons=buttons)


def summary_sections(ch, requester_mention, opponent_mention):
    m = MODES[ch["mode"]]
    return [
        [
            f"**Challenger:** {requester_mention}",
            f"**Rival:** {opponent_mention}",
            field("Mode", m["name"]),
            field("Rounds", rounds_text(ch["rounds"])),
        ]
    ]


async def safe_reply(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
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


# ============================================================ ticket creation
async def create_ticket(guild: discord.Guild, ch):
    m = MODES[ch["mode"]]
    store.d["counters"][ch["mode"]] += 1
    n = store.d["counters"][ch["mode"]]
    store.save()
    name = f"{m['prefix']}-{n:03d}"

    category = guild.get_channel(TICKET_CATEGORY_ID) or await guild.fetch_channel(TICKET_CATEGORY_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                              embed_links=True, read_message_history=True),
    }
    for uid in (ch["requester"], ch["opponent"]):
        member = guild.get_member(uid) or await guild.fetch_member(uid)
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                         read_message_history=True, attach_files=True)
    if STAFF_ROLE_ID:
        role = guild.get_role(STAFF_ROLE_ID)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)

    channel = await guild.create_text_channel(
        name, category=category, overwrites=overwrites,
        topic=f"{m['name']} #{n:03d} | challenge {ch['id']}", reason=f"Rivalz {m['name']} ticket",
    )
    store.d["tickets"][str(channel.id)] = {
        "requester": ch["requester"], "opponent": ch["opponent"], "name": name, "mode": ch["mode"], "number": n,
    }
    store.save()

    now = int(time.time())
    sections = [
        [
            f"**Ticket Channel:** {channel.mention}",
            f"**Challenger:** <@{ch['requester']}>",
            f"**Rival:** <@{ch['opponent']}>",
            f"**Created:** <t:{now}:F>",
        ],
        [
            roblox_line(ch),
            field("Mode", m["name"]),
            field("Rounds", rounds_text(ch["rounds"])),
            field("Region", ch["region"] or "Not detected"),
        ],
        ["> Agree on the details here and report the result when the match ends."],
    ]
    ticket_view = build_card(
        f"{m['name']} #{n:03d} Ticket Created", sections, accent=m["colour"],
        buttons=link_buttons(ch) + [CloseButton()],
        top_text=f"<@{ch['requester']}> <@{ch['opponent']}>",
    )
    await channel.send(view=ticket_view)

    if LOG_CHANNEL_ID:
        log = guild.get_channel(LOG_CHANNEL_ID)
        if log:
            log_view = build_card(
                f"{m['name']} #{n:03d} Ticket Created", sections, accent=m["colour"],
                buttons=link_buttons(ch),
            )
            try:
                await log.send(view=log_view)
            except discord.HTTPException:
                pass
    return channel, n


# ============================================================ dynamic buttons
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

        # accept
        ch["status"] = "accepted"
        store.save()
        await interaction.response.defer()
        guild = bot.get_guild(ch["guild_id"])
        try:
            channel, n = await create_ticket(guild, ch)
        except Exception as exc:
            ch["status"] = "pending"
            store.save()
            return await interaction.followup.send(
                f"Could not open the ticket right now. Try again in a moment. ({type(exc).__name__})", ephemeral=True)

        open_btn = discord.ui.Button(label="Open Ticket", url=channel.jump_url)
        await interaction.edit_original_response(view=build_card(
            "Challenge Accepted",
            summary_sections(ch, f"<@{ch['requester']}>", f"<@{ch['opponent']}>")
            + [f"> Your ticket is ready: {channel.mention}"],
            accent=GREEN, buttons=[open_btn]))
        try:
            requester = await bot.fetch_user(ch["requester"])
            await try_dm(requester, build_card(
                "Challenge Accepted",
                [f"> **{interaction.user.display_name}** accepted your **{m['name']}** request.",
                 [f"**Ticket Channel:** {channel.mention}", field("Rounds", rounds_text(ch["rounds"]))]],
                accent=GREEN, buttons=[discord.ui.Button(label="Open Ticket", url=channel.jump_url)]))
        except discord.HTTPException:
            pass


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rv:close"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.secondary, custom_id="rv:close"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"CloseButton error: {error!r}")

    async def callback(self, interaction: discord.Interaction):
        t = store.d["tickets"].get(str(interaction.channel_id))
        user = interaction.user
        allowed = bool(t and user.id in (t["requester"], t["opponent"]))
        if isinstance(user, discord.Member):
            allowed = allowed or user.guild_permissions.manage_channels or (
                STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in user.roles))
        if not allowed:
            return await interaction.response.send_message("Only the two fighters or staff can close this ticket.", ephemeral=True)

        await interaction.response.send_message(view=build_card(
            "Ticket Closing", [f"> Closed by {user.mention}.", "> This channel will be deleted in `5` seconds."], accent=GREY))
        if LOG_CHANNEL_ID and t:
            log = interaction.guild.get_channel(LOG_CHANNEL_ID)
            if log:
                m = MODES[t["mode"]]
                try:
                    await log.send(view=build_card(
                        f"{m['name']} #{t['number']:03d} Ticket Closed",
                        [[f"**Closed By:** {user.mention} (`@{user.name}`)",
                          f"**Closed:** <t:{int(time.time())}:F>",
                          field("Ticket", t["name"])]], accent=GREY))
                except discord.HTTPException:
                    pass
        store.d["tickets"].pop(str(interaction.channel_id), None)
        store.save()
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Rivalz ticket closed by {user}")
        except discord.HTTPException:
            pass


# ============================================================ request modal
class ChallengeModal(discord.ui.Modal):
    def __init__(self, mode: str, default_name: str = ""):
        super().__init__(title=f"{MODES[mode]['name']} Request", timeout=600)
        self.mode = mode

        self.username = discord.ui.TextInput(placeholder="Your Roblox username", default=default_name or None,
                                             min_length=3, max_length=20)
        self.add_item(discord.ui.Label(text="Roblox Username", component=self.username))

        self.opponent = discord.ui.UserSelect(placeholder="Select your rival", min_values=1, max_values=1)
        self.add_item(discord.ui.Label(text="Opponent / Rival", description="The player you want to face.",
                                       component=self.opponent))

        self.link = discord.ui.TextInput(placeholder="Leave empty to detect automatically", required=False, max_length=300)
        self.add_item(discord.ui.Label(text="Server Link (Optional)", component=self.link))

        self.rounds = discord.ui.Select(
            placeholder="Select number of rounds",
            options=[
                discord.SelectOption(label="1 Round", value="1", description="Single deciding round."),
                discord.SelectOption(label="3 Rounds", value="3", description="Best of three."),
                discord.SelectOption(label="5 Rounds", value="5", description="Best of five."),
                discord.SelectOption(label="Custom", value="custom", description="Type your own number below."),
            ],
        )
        self.add_item(discord.ui.Label(text="Number of Rounds", component=self.rounds))

        self.custom = discord.ui.TextInput(placeholder=f"Only if Custom is selected (1-{MAX_ROUNDS})",
                                           required=False, max_length=3)
        self.add_item(discord.ui.Label(text="Custom Rounds", component=self.custom))

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        def fail(text):
            return interaction.followup.send(view=build_card("Request Failed", [f"> {text}"], accent=RED), ephemeral=True)

        guild = interaction.guild
        rival = self.opponent.values[0]
        if rival.id == interaction.user.id:
            return await fail("You cannot challenge yourself.")
        if rival.bot:
            return await fail("You cannot challenge a bot.")
        try:
            await guild.fetch_member(rival.id)
        except discord.NotFound:
            return await fail("That rival is not in this server.")

        choice = self.rounds.values[0] if self.rounds.values else None
        if choice is None:
            return await fail("Select the number of rounds.")
        if choice == "custom":
            raw = (self.custom.value or "").strip()
            if not raw.isdigit() or not (1 <= int(raw) <= MAX_ROUNDS):
                return await fail(f"Custom rounds must be a number from `1` to `{MAX_ROUNDS}`.")
            rounds = int(raw)
        else:
            rounds = int(choice)

        pending = [c for c in store.d["challenges"].values()
                   if c["status"] == "pending" and c["requester"] == interaction.user.id]
        if any(c["opponent"] == rival.id and c["mode"] == self.mode for c in pending):
            return await fail("You already have a pending request to this rival.")
        if len(pending) >= 3:
            return await fail("You have too many pending requests. Wait for a reply first.")

        rb = await bot.roblox.user_by_name(self.username.value.strip())
        if not rb:
            return await fail("That Roblox username was not found.")

        link = (self.link.value or "").strip() or None
        if link and not valid_roblox_link(link):
            return await fail("That server link is not a valid Roblox link.")
        if not link:
            link = await bot.roblox.find_server_link(rb["id"])
        region = await bot.roblox.detect_region(link) if link else None

        ch = {
            "id": uuid.uuid4().hex[:8], "mode": self.mode, "status": "pending", "guild_id": guild.id,
            "requester": interaction.user.id, "opponent": rival.id,
            "roblox_id": rb["id"], "roblox_name": rb["name"], "roblox_display": rb["display"],
            "rounds": rounds, "link": link, "region": region,
            "created": time.time(), "expires": time.time() + EXPIRY_MINUTES * 60,
        }

        rival_user = await bot.fetch_user(rival.id)
        msg = await try_dm(rival_user, challenge_dm_view(ch, interaction.user))
        if not msg:
            return await fail("That rival has direct messages closed, so the challenge could not be delivered.")
        ch["dm_channel_id"], ch["dm_message_id"] = msg.channel.id, msg.id
        store.d["challenges"][ch["id"]] = ch
        store.d["usernames"][str(interaction.user.id)] = rb["name"]
        store.save()

        m = MODES[self.mode]
        await interaction.followup.send(view=build_card(
            "Challenge Sent",
            [[f"> Your **{m['name']}** request was delivered to {rival.mention}.",
              f"> You will be notified once they respond."],
             [roblox_line(ch), field("Mode", m["name"]), field("Rounds", rounds_text(rounds)),
              field("Region", region or "Not detected"),
              field("Server Link", "Detected" if link and not (self.link.value or "").strip() else ("Provided" if link else "Not found"))],
             [f"> Expires <t:{int(ch['expires'])}:R>."]],
            accent=m["colour"]), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await safe_reply(interaction, "Something went wrong. Please try again.")
        print(f"ChallengeModal error: {error!r}")


# ============================================================ main panel
class ModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            custom_id="rv:mode",
            placeholder="Select a mode...",
            options=[
                discord.SelectOption(label="Duel", value="duel", description="Competitive match. Win the rounds to win the clash."),
                discord.SelectOption(label="Spar", value="spar", description="Friendly practice round. Warm up together."),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        mode = self.values[0]
        default = store.d["usernames"].get(str(interaction.user.id), "")
        await interaction.response.send_modal(ChallengeModal(mode, default))
        try:  # reset the menu so the same mode can be picked again
            await interaction.message.edit(view=PanelView())
        except discord.HTTPException:
            pass


class PanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        c = discord.ui.Container(accent_colour=0x2B2D31)
        c.add_item(discord.ui.TextDisplay(
            "## Rivalz\n"
            "> Duel and spar matchmaking for Roblox battleground games."
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(
            "### How it works\n"
            "> `1` Select a mode below\n"
            "> `2` Pick your rival and the rounds\n"
            "> `3` Rival accepts and a private ticket opens"
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(
            "### Modes\n"
            "> `Duel` Competitive match, win the rounds.\n"
            "> `Spar` Friendly practice, warm up together."
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay(
            "### Rules\n"
            "> **No Fake Challenges**: Spam requests result in a blacklist.\n"
            "> **Active Profile**: Use the Roblox account you are playing on.\n"
            "> **Fair Play**: Respect your rival and the agreed rounds."
        ))
        c.add_item(discord.ui.Separator())
        c.add_item(discord.ui.TextDisplay("### Start a match\n> Select a mode from the menu below."))
        row = discord.ui.ActionRow()
        row.add_item(ModeSelect())
        c.add_item(row)
        self.add_item(c)


# ============================================================ bot
class Rivalz(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.session: aiohttp.ClientSession = None
        self.roblox: RobloxClient = None
        self._ready_once = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.roblox = RobloxClient(self.session)
        self.add_dynamic_items(ChallengeButton, CloseButton)
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
    await interaction.followup.send(view=build_card(
        "Rivalz Online",
        [[field("Gateway", f"{round(bot.latency * 1000)} ms"),
          field("API Round Trip", f"{api_ms} ms"),
          field("Uptime", fmt_uptime(time.time() - START_TIME))],
         [field("Servers", len(bot.guilds)), field("Pending Challenges", pending)]],
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
         ["> The cookie is used to find live server links and regions."]],
        accent=GREEN if user and cookie.startswith("Valid") else RED), ephemeral=True)


@bot.tree.command(name="ping-region", description="Test the Ralvora region lookup.")
async def ping_region(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not RALVORA_API_URL:
        return await interaction.followup.send(view=build_card(
            "Region Lookup",
            [[field("Ralvora API", "Not configured")],
             ["> Set RALVORA_API_URL and RALVORA_API_KEY. Until then the free fallback is used."]],
            accent=RED), ephemeral=True)
    t = time.perf_counter()
    result = await bot.roblox.ralvora_lookup("8.8.8.8")
    ms = round((time.perf_counter() - t) * 1000)
    await interaction.followup.send(view=build_card(
        "Region Lookup",
        [[field("Ralvora API", "Online" if result else "No usable answer"),
          field("Test Result", result or "None"), field("Response Time", f"{ms} ms")]],
        accent=GREEN if result else RED), ephemeral=True)


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
    if not ROBLOX_COOKIE or ROBLOX_COOKIE.startswith("PUT_"):
        print("Warning: ROBLOX_COOKIE is not set. Auto server link and region will not work.")
    if not RALVORA_API_URL:
        print("Note: RALVORA_API_URL is empty. The free ip-api fallback will be used for regions.")
    bot.run(TOKEN)