# LIVE TICKET / BACKUP TRACKING
# ----------------------------------------------------------------------------
DIV_TICKET_START_NUMBER = env_int("DIV_TICKET_START_NUMBER", 170)
MIRROR_CREATE_LOCKS: dict[int, asyncio.Lock] = {}
LIVE_PUBLISH_LOCKS: dict[int, asyncio.Lock] = {}
TICKET_DETAIL_LABELS = (
    "Server Region", "Reported Players", "Difficulty Level", "Description", "Session Info",
    "Server Location", "Region", "Players", "Difficulty",
)
_live_columns = {row["name"] for row in db.execute("PRAGMA table_info(live_tickets)")}
if "details_json" not in _live_columns:
    db.execute("ALTER TABLE live_tickets ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
    db.commit()


def mirror_create_lock(source_id: int) -> asyncio.Lock:
    return MIRROR_CREATE_LOCKS.setdefault(int(source_id), asyncio.Lock())


def live_publish_lock(ticket_id: int) -> asyncio.Lock:
    return LIVE_PUBLISH_LOCKS.setdefault(int(ticket_id), asyncio.Lock())


def live_lock(channel_id: int) -> asyncio.Lock:
    return LIVE_LOCKS.setdefault(channel_id, asyncio.Lock())


def is_live_channel(channel) -> bool:
    if channel is None:
        return False
    if channel.id in LIVE_IGNORE_IDS or channel.id in {MVP_CHANNEL_ID, MVP_RESULTS_ID, LOG_ID, LEADERBOARD_ID, LIVE_POST_ID}:
        return False
    if channel.id in LIVE_CATEGORY_IDS:
        return False
    return getattr(channel, "category_id", None) in LIVE_CATEGORY_IDS


def live_new(channel) -> dict:
    return {"channel_id": channel.id, "guild_id": channel.guild.id, "name": channel.name, "opener_id": None,
            "opener_division": None, "total": None, "helpers": [], "post_ref": None, "status": "live",
            "created_at": datetime.now(timezone.utc).isoformat(), "archive_ref": None, "details": {}}


def live_load(channel_id: int) -> dict | None:
    row = db.execute("SELECT * FROM live_tickets WHERE channel_id=?", (channel_id,)).fetchone()
    if row is None:
        return None
    return {"channel_id": row["channel_id"], "guild_id": row["guild_id"], "name": row["name"],
            "opener_id": row["opener_id"], "opener_division": row["opener_division"],
            "total": row["reported_total"], "helpers": json.loads(row["helpers_json"]),
            "post_ref": row["post_ref"], "status": row["status"], "created_at": row["created_at"],
            "archive_ref": row["archive_ref"], "details": json.loads(row["details_json"] or "{}")}


def live_store(st: dict) -> None:
    ended = datetime.now(timezone.utc).isoformat() if st["status"] != "live" else None
    db.execute("""INSERT OR REPLACE INTO live_tickets
        (channel_id,guild_id,name,opener_id,opener_division,reported_total,helpers_json,post_ref,status,created_at,ended_at,archive_ref,details_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (st["channel_id"], st["guild_id"], st["name"], st["opener_id"], st["opener_division"], st["total"],
                json.dumps(st["helpers"]), st["post_ref"], st["status"], st["created_at"], ended,
                 st.get("archive_ref"), json.dumps(st.get("details", {}))))
    db.commit()


def mirror_by_source(source_channel_id: int):
    return db.execute("SELECT * FROM ticket_mirrors WHERE source_channel_id=?", (source_channel_id,)).fetchone()


def mirror_by_division_channel(division_channel_id: int):
    return db.execute("SELECT * FROM ticket_mirrors WHERE division_channel_id=?", (division_channel_id,)).fetchone()


async def update_ticket_messy_prompt(message: discord.Message) -> None:
    """Replace the last support reminder each time a mirrored ticket reaches 25 messages."""
    if message.author.bot or message.webhook_id or not isinstance(message.channel, discord.TextChannel):
        return
    mirror = mirror_by_division_channel(message.channel.id)
    if mirror is None:
        return
    channel_id = message.channel.id
    lock = TICKET_PROMPT_LOCKS.setdefault(channel_id, asyncio.Lock())
    async with lock:
        row = db.execute("SELECT * FROM ticket_mirrors WHERE division_channel_id=?", (channel_id,)).fetchone()
        if row is None or row["last_counted_message_id"] == message.id:
            return
        count = int(row["message_count"] if row["message_count"] is not None else -1)
        if count < 0:
            # On an upgraded ticket, count its existing human messages once so reminders
            # continue from the ticket's real message total instead of starting at zero.
            seen = 0
            current_seen = False
            try:
                async for old in message.channel.history(limit=5000):
                    if not old.author.bot and not old.webhook_id:
                        seen += 1
                        current_seen = current_seen or old.id == message.id
            except discord.HTTPException:
                seen = 0
                current_seen = False
            count = seen if current_seen else seen + 1
        else:
            count += 1
        db.execute("UPDATE ticket_mirrors SET message_count=?, last_counted_message_id=? WHERE division_channel_id=?",
                   (count, message.id, channel_id))
        db.commit()
        if count == 0 or count % 25 != 0:
            return

        old_prompt_id = row["support_message_id"]
        if old_prompt_id:
            try:
                old_prompt = await message.channel.fetch_message(int(old_prompt_id))
                await old_prompt.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                print(f"Could not replace the previous ticket reminder in {channel_id}: {exc!r}")
        elif bot.user is not None:
            try:
                async for old_prompt in message.channel.history(limit=250):
                    if old_prompt.author.id == bot.user.id and "I see a lot of messages in here" in post_text(old_prompt):
                        await old_prompt.delete()
                        break
            except discord.HTTPException:
                pass

        source_url = channel_url(message.guild.id, int(row["source_channel_id"]))
        embed = discord.Embed(
            title="💡 Ah! I see a lot of messages in here.",
            description=("Unsatisfied? **More members = Better support.** Invite friends to help.\n\n"
                         "• The main team alert can get buried by chat.\n"
                         "• Use the button below to jump back to the original request.\n"
                         "• Check the request details before joining."),
            color=0x8B8495)
        embed.set_footer(text=f"{count} messages in this ticket  ·  URA | DIV")
        try:
            prompt = await message.channel.send(embed=embed, view=TicketSessionInfoView(source_url),
                                                allowed_mentions=NO_PING)
        except discord.HTTPException as exc:
            print(f"Could not post the {count}-message ticket reminder in {channel_id}: {exc!r}")
            return
        db.execute("UPDATE ticket_mirrors SET support_message_id=? WHERE division_channel_id=?",
                   (prompt.id, channel_id))
        db.commit()


async def live_apply(st: dict, guild: discord.Guild, message: discord.Message) -> bool:
    """Read one ticket-bot message into the ticket state. Returns True if anything changed.

    Only explicit join/confirmation notices count as helpers. The ticket opener is excluded."""
    text = post_text(message)
    changed = False
    details = extract_ticket_details(message)
    saved_details = st.setdefault("details", {})
    for label, value in details.items():
        if value and saved_details.get(label) != value:
            saved_details[label] = value
            changed = True
    opened = OPENED_RE.search(text)
    if opened and not st["opener_id"]:
        user_id = int(opened.group(1))
        member = await get_member(guild, user_id)
        st["opener_id"] = user_id
        st["opener_division"] = division_of(member)[0] if member else None
        changed = True
    if st.get("opener_id"):
        old_count = len(st["helpers"])
        st["helpers"] = [h for h in st["helpers"] if int(h["user_id"]) != int(st["opener_id"])]
        changed = changed or len(st["helpers"]) != old_count
    total = LIVE_TOTAL_RE.search(text)
    if total and int(total.group(1)) != st["total"]:
        st["total"] = int(total.group(1))
        changed = True
    join_bot = message.author.id in LIVE_JOIN_BOT_IDS
    explicit_join = bool(JOIN_NOTICE_RE.search(text))
    if explicit_join:
        ids = [int(x) for x in HELP_MENTION_RE.findall(text)]
        if join_bot:
            # This bot announces a join with a mention list; only accept it when its text
            # contains an explicit join/confirmation phrase (never every ping it sends).
            ids = [u.id for u in message.mentions] + [int(x) for x in MENTION_RE.findall(text)]
    else:
        ids = []
    if ids:
        known = {h["user_id"] for h in st["helpers"]}
        for user_id in dict.fromkeys(ids):
            if user_id in known or user_id == message.author.id or user_id == st["opener_id"]:
                continue
            member = await get_member(guild, user_id)
            if member is None or member.bot:
                continue
            division, _ = division_of(member)
            st["helpers"].append({"user_id": member.id, "display_name": member.display_name, "division": division})
            changed = True
    return changed


def live_refresh_divisions(st: dict, guild: discord.Guild) -> bool:
    changed = False
    for helper in st["helpers"]:
        member = guild.get_member(helper["user_id"])
        if member is None:
            continue
        division, _ = division_of(member)
        if division != helper.get("division"):
            helper["division"] = division
            changed = True
    return changed


async def live_publish(guild: discord.Guild, st: dict) -> None:
    # Multiple event paths can refresh a card at once (startup catch-up and message events).
    # Serialize publication and reload the saved reference so only one message is created.
    async with live_publish_lock(int(st["channel_id"])):
        saved = live_load(int(st["channel_id"]))
        if saved and saved.get("post_ref"):
            st["post_ref"] = saved["post_ref"]
        await _live_publish_unlocked(guild, st)


async def _live_publish_unlocked(guild: discord.Guild, st: dict) -> None:
    """The live card lives INSIDE the ticket itself (no extra channel, no voice room, no buttons).
    It is posted once and then edited in place as helpers join."""
    destination_id = int(st["channel_id"])
    try:
        ref = await send_or_edit(destination_id, st["post_ref"],
                                 lambda _images: LiveTicketView(guild.id, st, False), with_files=False)
    except discord.HTTPException as exc:
        print(f"Could not post the live card in {st['name']}: {exc!r}")
        return
    if ref != st["post_ref"]:
        st["post_ref"] = ref
    live_store(st)


def is_me(user) -> bool:
    return bot.user is not None and user.id == bot.user.id


def live_relevant(text: str, author_id: int | None = None) -> bool:
    if OPENED_RE.search(text) or LIVE_TOTAL_RE.search(text) or HELP_RE.search(text):
        return True
    if re.search(r"\b(Server Region|Reported Players|Difficulty Level|Session Info|Server Location|Description)\b", text, re.I):
        return True
    return False


def extract_ticket_details(message: discord.Message) -> dict[str, str]:
    """Read the Crusher ticket's labeled fields from embeds or Components V2 text blocks."""
    details: dict[str, str] = {}
    known = {item.casefold(): item for item in TICKET_DETAIL_LABELS}
    for embed in message.embeds:
        for field in embed.fields:
            label = known.get(field.name.strip().strip("*# :").casefold())
            if label:
                value = field.value.strip()
                if value:
                    details[label] = value[:700]

    chunks = [message.content or ""]
    for embed in message.embeds:
        chunks.extend((embed.title or "", embed.description or ""))
    chunks.extend(component_text(getattr(message, "components", None)))
    active: str | None = None
    collected: dict[str, list[str]] = {}
    for raw_line in "\n".join(chunks).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = re.sub(r"^[\s>#*_`]+|[\s*_`]+$", "", line).strip()
        match = re.match(r"([^:：]+)\s*[:：]\s*(.*)$", heading)
        label_text = (match.group(1) if match else heading).strip().strip("*# `")
        label = known.get(label_text.casefold())
        if label:
            active = label
            collected.setdefault(label, [])
            inline = match.group(2).strip() if match else ""
            if inline:
                collected[label].append(inline)
            continue
        if active:
            value = re.sub(r"^\s*>\s?", "", line).strip()
            if value:
                collected[active].append(value)
    for label, values in collected.items():
        value = " ".join(values).strip()
        if value and label not in details:
            details[label] = value[:700]
    return details


async def live_catch_up(st: dict, channel, skip_id: int | None = None) -> None:
    """Read the bots' earlier messages in a ticket channel (used when tracking starts late or on /sync)."""
    try:
        async for old in channel.history(limit=500, oldest_first=True):
            if old.id != skip_id and old.author.bot and not is_me(old.author):
                await live_apply(st, channel.guild, old)
    except discord.HTTPException:
        pass


def live_has_info(st: dict) -> bool:
    """True once Crusher has posted something the tracker can read (opener, helpers, total or details)."""
    return bool(st.get("opener_id") or st.get("helpers") or st.get("total") is not None or st.get("details"))


async def live_bootstrap(channel) -> None:
    if mirror_by_source(channel.id):      # an older ticket that already has a Div mirror: leave it alone
        return
    async with live_lock(channel.id):
        if live_load(channel.id) is not None:
            return
        st = live_new(channel)
        await live_catch_up(st, channel)
        if not live_has_info(st):         # Crusher has not posted yet: the card appears after its first post
            return
        live_refresh_divisions(st, channel.guild)
        live_store(st)
        await live_publish(channel.guild, st)


async def crusher_creator_id(guild: discord.Guild, channel_id: int) -> int | None:
    try:
        async for entry in guild.audit_logs(limit=12, action=discord.AuditLogAction.channel_create):
            if getattr(getattr(entry, "target", None), "id", None) == channel_id:
                return getattr(getattr(entry, "user", None), "id", None)
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Cannot read channel-create audit log for {channel_id}: {exc!r}")
    return None


async def get_ticket_opener(channel: discord.TextChannel) -> int | None:
    if channel.topic:
        match = OPENED_RE.search(channel.topic)
        if match:
            return int(match.group(1))
    try:
        async for message in channel.history(limit=100, oldest_first=True):
            match = OPENED_RE.search(post_text(message))
            if match:
                return int(match.group(1))
    except discord.HTTPException:
        pass
    return None


async def is_crusher_ticket(channel: discord.TextChannel) -> bool:
    # Division mirrors are also allowed in the source category; their name prevents recursion.
    if channel.name.casefold().startswith("div"):
        return False
    if CRUSHER_TICKET_CATEGORY_ID:
        return channel.category_id == CRUSHER_TICKET_CATEGORY_ID
    actor_id = await crusher_creator_id(channel.guild, channel.id)
    allowed = set(LIVE_BOT_IDS)
    if CRUSHER_BOT_ID:
        allowed.add(CRUSHER_BOT_ID)
    return actor_id in allowed


def next_div_ticket_number() -> int:
    row = db.execute("SELECT MAX(ticket_number) AS n FROM ticket_mirrors").fetchone()
    return max(DIV_TICKET_START_NUMBER, int(row["n"] or 0) + 1)


async def source_ticket_number(source: discord.TextChannel) -> int:
    """Prefer the actual Crusher ticket number over a generic trailing channel-name number."""
    patterns = (
        re.compile(r"\b(?:backup|ticket)\s*#\s*0*(\d{1,7})\b", re.I),
        re.compile(r"(?:backup|ticket)[\s_-]+#?0*(\d{1,7})\b", re.I),
    )
    for value in (source.topic or "",):
        for pattern in patterns:
            match = pattern.search(value)
            if match:
                return int(match.group(1))
    try:
        async for message in source.history(limit=50, oldest_first=True):
            text = post_text(message)
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    return int(match.group(1))
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"Could not read the ticket number from {source.name}: {exc!r}")
    # The source channel's ticket-created card is authoritative when its label differs.
    for pattern in patterns:
        match = pattern.search(source.name)
        if match:
            return int(match.group(1))
    return next_div_ticket_number()


async def live_ingest(message: discord.Message, channel=None) -> None:
    channel = channel or message.channel
    text = post_text(message)
    if not live_relevant(text, message.author.id):
        if re.search(r"help|ticket|join", text, re.I) or message.author.id in LIVE_JOIN_BOT_IDS:
            print(f"[live] ignored a message from {message.author} in #{channel.name} (no pattern): "
                  f"{' '.join(text.split())[:120]!r}")
        return
    async with live_lock(channel.id):
        st = live_load(channel.id)
        fresh = st is None
        if fresh:
            st = live_new(channel)
            await live_catch_up(st, channel, skip_id=message.id)
        elif st["status"] != "live":
            return
        if st["name"] != channel.name:
            st["name"] = channel.name
            fresh = True
        before = len(st["helpers"])
        changed = await live_apply(st, channel.guild, message)
        if live_refresh_divisions(st, channel.guild):
            changed = True
        print(f"[live] #{channel.name}: read a message from {message.author} ({message.author.id}); "
              f"helpers {before} -> {len(st['helpers'])}; opener={st['opener_id']}")
        if fresh or changed:
            await live_publish(channel.guild, st)


async def live_from_log(message: discord.Message) -> None:
    """A tracked bot's message outside a ticket (base/log channel): find the ticket it is about
    (a #channel mention, or the ticket's name in the text) and add the helpers there."""
    text = post_text(message)
    if not live_relevant(text, message.author.id):
        return
    guild = message.guild
    ids = set(int(x) for x in CHANNEL_MENTION_RE.findall(text)) | {c.id for c in message.channel_mentions}
    targets = [guild.get_channel(i) for i in ids]
    targets = [c for c in targets if c is not None and is_live_channel(c)]
    if not targets:
        lowered = text.lower()
        for category_id in LIVE_CATEGORY_IDS:
            category = guild.get_channel(category_id)
            for ticket in getattr(category, "text_channels", []):
                if is_live_channel(ticket) and len(ticket.name) >= 4 and ticket.name.lower() in lowered:
                    targets.append(ticket)
    for ticket in targets:
        await live_ingest(message, ticket)


async def live_close(guild: discord.Guild, channel_id: int) -> None:
    async with live_lock(channel_id):
        st = live_load(channel_id)
        if st is None or st.get("archive_ref"):
            return
        archive = dict(st)
        archive["status"] = "closed"
        try:
            ref = await send_or_edit(LOG_ID, st.get("archive_ref"),
                lambda _images: LiveTicketView(guild.id, archive, False, archived=True), with_files=False)
        except discord.HTTPException as exc:
            print(f"Could not archive live backup {st['name']}: {exc!r}")
            return
        if not ref:
            print(f"Could not archive live backup {st['name']}: log channel {LOG_ID} is unavailable.")
            return
        st["status"] = "closed"
        st["archive_ref"] = ref
        await delete_ref(st.get("post_ref"))
        st["post_ref"] = None
        live_store(st)


async def remove_ticket_mirror(guild: discord.Guild, row, *, source_was_deleted: bool) -> None:
    source_id = int(row["source_channel_id"])
    division_id = int(row["division_channel_id"])
    voice_id = int(row["voice_channel_id"] or 0)
    st = live_load(division_id)
    if st:
        st["status"] = "closed"
        st["post_ref"] = None
        st["archive_ref"] = "crusher-ticket-deleted" if source_was_deleted else "division-ticket-deleted"
        live_store(st)  # retain helper history for /my-rank, without awarding MVP points
    db.execute("DELETE FROM ticket_mirrors WHERE source_channel_id=?", (source_id,))
    db.commit()
    if source_was_deleted:
        division_channel = guild.get_channel(division_id)
        if division_channel:
            try:
                await division_channel.delete(reason="Crusher source ticket was deleted")
            except discord.HTTPException as exc:
                print(f"Could not delete linked division ticket {division_id}: {exc!r}")
    if voice_id:
        voice = guild.get_channel(voice_id)
        if voice:
            try:
                await voice.delete(reason="Linked division ticket was deleted")
            except discord.HTTPException as exc:
                print(f"Could not delete linked division voice room {voice_id}: {exc!r}")


async def live_reconcile(guild: discord.Guild) -> None:
    rows = db.execute(
        "SELECT channel_id,status FROM live_tickets WHERE guild_id=? "
        "AND (status='live' OR (status='closed' AND archive_ref IS NULL))", (guild.id,)
    ).fetchall()
    for row in rows:
        if row["status"] == "closed" or guild.get_channel(row["channel_id"]) is None:
            await live_close(guild, row["channel_id"])


async def live_sync(guild: discord.Guild) -> None:
    """After a restart: start tracking tickets created while the bot was offline (newer than LIVE_SYNC_HOURS)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LIVE_SYNC_HOURS)
    for category_id in LIVE_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        for ticket in getattr(category, "text_channels", []):
            if is_live_channel(ticket) and ticket.created_at >= cutoff:
                await live_bootstrap(ticket)


async def mirror_reconcile(guild: discord.Guild) -> None:
    """Remove stale mirror records; only a new Crusher channel event may create a mirror."""
    rows = db.execute("SELECT * FROM ticket_mirrors WHERE guild_id=?", (guild.id,)).fetchall()
    for row in rows:
        source = guild.get_channel(int(row["source_channel_id"]))
        division = guild.get_channel(int(row["division_channel_id"]))
        if source is None:
            await remove_ticket_mirror(guild, row, source_was_deleted=True)
        elif division is None:
            # Do not silently recreate a missing ticket during startup. Creation is only
            # triggered by on_guild_channel_create for a newly opened Crusher ticket.
            await remove_ticket_mirror(guild, row, source_was_deleted=False)


def ticket_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    out = []
    for category_id in LIVE_CATEGORY_IDS:
        category = guild.get_channel(category_id)
        out += [c for c in getattr(category, "text_channels", [])
                if is_live_channel(c) and not mirror_by_source(c.id)]
    return out


async def live_sync_now(guild: discord.Guild) -> list[tuple[discord.TextChannel, int]]:
    """/sync: re-read EVERY ticket/backup channel in the ticket category (any age), rebuild the helper
    lists from the bots' messages and refresh each live post."""
    results = []
    for ticket in ticket_channels(guild):
        async with live_lock(ticket.id):
            st = live_load(ticket.id) or live_new(ticket)
            st["status"] = "live"
            st["name"] = ticket.name
            await live_catch_up(st, ticket)
            if not (live_has_info(st) or st.get("post_ref")):
                continue                  # Crusher has not posted in this ticket yet
            live_refresh_divisions(st, guild)
            live_store(st)
            await live_publish(guild, st)
            results.append((ticket, len(st["helpers"])))
    for base_id in LIVE_IGNORE_IDS:                 # tracked-bot messages posted in the base channels
        base = guild.get_channel(base_id)
        if base is None or not hasattr(base, "history"):
            continue
        try:
            async for m in base.history(limit=100, oldest_first=True):
                if m.author.bot and not is_me(m.author):
                    await live_from_log(m)
        except discord.HTTPException:
            pass
    return results


async def live_manual(guild: discord.Guild, channel, member: discord.Member, remove: bool) -> str:
    async with live_lock(channel.id):
        st = live_load(channel.id) or live_new(channel)
        if remove:
            before = len(st["helpers"])
            st["helpers"] = [h for h in st["helpers"] if h["user_id"] != member.id]
            result = "removed" if len(st["helpers"]) < before else "was not in the list"
        elif any(h["user_id"] == member.id for h in st["helpers"]):
            result = "is already in the list"
        else:
            st["helpers"].append({"user_id": member.id, "display_name": member.display_name,
                                  "division": division_of(member)[0]})
            result = "added"
        live_store(st)
        await live_publish(guild, st)
    return f"<@{member.id}> {result} in <#{channel.id}>."


# ----------------------------------------------------------------------------
