# HELPERS
# ----------------------------------------------------------------------------
def make_embed(title: str, description: str | None = None, color: int = BRAND_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=BOT_NAME)
    return embed


def division_of(member: discord.Member) -> tuple[int | None, set[int]]:
    found: dict[int, int] = {}
    for role in member.roles:
        d = DIVISION_ROLE_IDS.get(role.id)
        if d is None:
            match = DIVISION_NAME_RE.search(role.name)
            d = int(match.group(1)) if match else None
        if d:
            found[d] = max(found.get(d, -1), role.position)
    if not found:
        return None, set()
    return max(found, key=lambda d: found[d]), set(found)


def component_text(components) -> list[str]:
    out: list[str] = []

    def walk(node):
        content = getattr(node, "content", None)
        if isinstance(content, str):
            out.append(content)
        for attr in ("children", "items"):
            for child in getattr(node, attr, None) or []:
                walk(child)
        accessory = getattr(node, "accessory", None)
        if accessory is not None:
            walk(accessory)

    try:
        for component in components or []:
            walk(component)
    except Exception:
        pass
    return out


def post_text(message: discord.Message) -> str:
    parts = [message.content]
    for embed in message.embeds:
        parts += [embed.title or "", embed.description or ""]
        parts += [f"{f.name} {f.value}" for f in embed.fields]
    parts += component_text(getattr(message, "components", None))
    return " ".join(parts)


def is_mvp_post(message: discord.Message) -> bool:
    if not message.author.bot:
        return False
    if MVP_BOT_ID and message.author.id != MVP_BOT_ID:
        return False
    text = post_text(message)
    return bool(re.search(r"\bMVPs?\b", text, re.I) and re.search(r"\bRaid\b", text, re.I))


def source_message(message: discord.Message) -> bool:
    return message.guild is not None and message.channel.id in SOURCE_IDS


def source_db_filter() -> tuple[str, tuple[int, ...]]:
    ids = tuple(sorted(SOURCE_IDS))
    return f"channel_id IN ({','.join('?' for _ in ids)})", ids


def channel_url(guild_id: int, channel_id: int, message_id: int | None = None) -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def ref_url(guild_id: int, ref: str | None, fallback_channel: int) -> str:
    if ref and ":" in ref:
        cid, mid = ref.split(":", 1)
        return channel_url(guild_id, int(cid), int(mid))
    return channel_url(guild_id, fallback_channel)


def tally(raiders: list[dict]) -> tuple[dict[int, int], list[int]]:
    tallies = {d: 0 for d in range(1, 6)}
    for raider in raiders:
        tallies[int(raider["division"])] += 1
    high = max(tallies.values())
    winners = [d for d, n in tallies.items() if high > 0 and n == high]
    return tallies, winners


def leaders_of(totals: dict[int, int]) -> list[int]:
    high = max(totals.values()) if totals else 0
    return [d for d, n in sorted(totals.items()) if high > 0 and n == high]


def lead_text(leaders: list[int]) -> str:
    crown = CROWN_EMOJI or "♛"
    if not leaders:
        return "No division is on lead yet"
    if len(leaders) == 1:
        return f"{crown} Division {leaders[0]} Is on Lead!"
    return f"{crown} " + " \u00d7 ".join(f"Division {d}" for d in leaders) + " Are on Lead!"


def next_raid_number(guild_id: int) -> int:
    clause, channel_params = source_db_filter()
    row = db.execute(f"SELECT MAX(raid_number) AS m FROM raid_results WHERE guild_id=? AND {clause}",
                     (guild_id, *channel_params)).fetchone()
    return RAID_START_NUMBER if row["m"] is None else int(row["m"]) + 1


def division_totals(guild_id: int) -> tuple[dict[int, int], int]:
    totals = {d: 0 for d in range(1, 6)}
    clause, channel_params = source_db_filter()
    params = (guild_id, *channel_params)
    for row in db.execute(f"SELECT winners_json FROM raid_results WHERE guild_id=? AND counted=1 AND {clause}", params):
        for division in json.loads(row["winners_json"]):
            totals[int(division)] += 1
    raids = db.execute(f"SELECT COUNT(*) AS n FROM raid_results WHERE guild_id=? AND {clause}", params).fetchone()["n"]
    return totals, int(raids)


def fit_list(items: list[str], limit: int) -> str:
    out, used = [], 0
    for i, item in enumerate(items):
        if used + len(item) + 2 > limit:
            out.append(f"+{len(items) - i} more")
            break
        out.append(item)
        used += len(item) + 2
    return ", ".join(out)


async def get_channel(channel_id: int):
    if not channel_id:
        return None
    try:
        return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        return None


async def try_get_message(channel_id: int, message_id: int):
    channel = await get_channel(channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return None
    try:
        return await channel.fetch_message(message_id)
    except discord.HTTPException:
        return None


async def get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return member


def is_bad_form(exc: discord.HTTPException) -> bool:
    return exc.status == 400 and getattr(exc, "code", None) == 50035


def note_image_problem(exc: discord.HTTPException) -> None:
    global IMAGE_PROBLEM
    IMAGE_PROBLEM = str(getattr(exc, "text", "") or exc)[:300]
    print(f"Discord rejected the badge images ({IMAGE_PROBLEM}); sending without thumbnails.")


async def _deliver(channel, ref: str | None, view, files: list) -> str:
    if ref and ":" in ref:
        cid, mid = ref.split(":", 1)
        if int(cid) == channel.id:
            try:
                old = await channel.fetch_message(int(mid))
            except discord.NotFound:
                old = None
            if old is not None:
                await old.edit(view=view, attachments=files)
                return ref
    sent = await channel.send(view=view, files=files, allowed_mentions=NO_PING)
    return f"{channel.id}:{sent.id}"


async def send_or_edit(channel_id: int, ref: str | None, build, with_files: bool = True) -> str | None:
    """Edit the message saved in `ref` ("channel:message"); send a new one if it is gone or in another channel.
    If Discord rejects the badge images (50035) it is sent again without thumbnails."""
    channel = await get_channel(channel_id)
    if channel is None:
        print(f"Channel {channel_id} is not reachable.")
        return ref
    for images in ((True, False) if with_files else (False,)):
        try:
            return await _deliver(channel, ref, build(images), badge_files() if images else [])
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                note_image_problem(exc)
                continue
            raise
    return ref


async def post_new(channel_id: int, build, *, with_files: bool = True) -> discord.Message | None:
    channel = await get_channel(channel_id)
    if channel is None:
        return None
    for images in ((True, False) if with_files else (False,)):
        try:
            return await channel.send(view=build(images), files=badge_files() if images else [],
                                      allowed_mentions=NO_PING)
        except discord.HTTPException as exc:
            if images and is_bad_form(exc):
                note_image_problem(exc)
                continue
            print(f"Could not post in {channel_id}: {exc!r}")
            return None
    return None


async def send_private_leaderboard(send, totals: dict[int, int], history_url: str, test: bool = False) -> None:
    await send(view=LeaderboardView(totals, history_url, True, test), ephemeral=True)


async def delete_ref(ref: str | None) -> None:
    if not ref or ":" not in ref:
        return
    cid, mid = ref.split(":", 1)
    msg = await try_get_message(int(cid), int(mid))
    if msg:
        try:
            await msg.delete()
        except discord.HTTPException:
            pass


async def collect_raiders(message: discord.Message):
    ids = [int(x) for x in MENTION_RE.findall(post_text(message))] + [u.id for u in message.mentions]
    raiders, skipped = [], []
    for user_id in dict.fromkeys(ids):
        if user_id == message.author.id:
            continue
        member = await get_member(message.guild, user_id)
        if member is None:
            skipped.append([f"<@{user_id}>", "not in the server"])
            continue
        if member.bot:
            continue
        division, found = division_of(member)
        if division is None:
            skipped.append([f"<@{user_id}>", "no division role"])
        else:
            raiders.append({"user_id": member.id, "display_name": member.display_name,
                            "division": division, "multi": len(found) > 1})
    return raiders, skipped


# ----------------------------------------------------------------------------
