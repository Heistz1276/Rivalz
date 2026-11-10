# EVENTS
# ----------------------------------------------------------------------------
async def safe_step(label: str, coro) -> None:
    """Run one startup step. A failure is printed but never stops the steps after it."""
    try:
        await coro
    except Exception:
        print(f"[startup] {label} failed:")
        traceback.print_exc()


async def sync_global_commands() -> None:
    """The 5 division leaderboards are GLOBAL: they work for anyone, in any server or DM."""
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} global slash commands ({', '.join(c.name for c in synced)})")


async def sync_commands(guild: discord.Guild) -> None:
    """Everything else is registered for the URA server only."""
    target = discord.Object(id=guild.id)
    synced = await bot.tree.sync(guild=target)
    print(f"Synced {len(synced)} slash commands to {guild.name}")


async def prepare_guild(guild: discord.Guild) -> None:
    apply_config(guild.id)
    # Commands come first so they work even if a later step has a problem.
    await safe_step("command sync", sync_commands(guild))
    try:
        if not guild.chunked:
            await guild.chunk()
    except Exception as exc:
        print(f"Could not load the member list for {guild.id}: {exc!r}")
    await safe_step("channel check", check_channels(guild))
    try:
        register_saved_views(guild)
    except Exception:
        print("[startup] register_saved_views failed:")
        traceback.print_exc()
    await safe_step("leaderboard refresh", refresh_leaderboard(guild))
    await safe_step("ticket mirrors", mirror_reconcile(guild))
    await safe_step("live reconcile", live_reconcile(guild))
    await safe_step("live sync", live_sync(guild))
    for row in db.execute("SELECT channel_id FROM live_tickets WHERE guild_id=? AND status='live'",
                          (guild.id,)).fetchall():
        try:
            mirror = mirror_by_division_channel(int(row["channel_id"]))
            st = live_load(int(row["channel_id"])) if mirror else None
            source = guild.get_channel(int(mirror["source_channel_id"])) if mirror else None
            if st is not None:
                # Rebuild active ticket helpers from explicit join notices, then redraw the card.
                st["helpers"] = []
                st["opener_id"] = None
                st["opener_division"] = None
                st["total"] = None
                if isinstance(source, discord.TextChannel):
                    await live_catch_up(st, source)
                live_refresh_divisions(st, guild)
                live_store(st)
                await live_publish(guild, st)
        except Exception:
            print(f"[startup] live ticket {row['channel_id']} failed:")
            traceback.print_exc()


@bot.event
async def on_ready():
    global STARTED
    print(f"{BOT_NAME} v{VERSION} online as {bot.user}  |  database: {DB_PATH}")
    if STARTED:
        return
    STARTED = True
    for step in (load_art, load_badges):
        try:
            step()
        except Exception:
            print(f"[startup] {step.__name__} failed:")
            traceback.print_exc()
    try:
        bot.add_view(SetupView())
    except Exception:
        print("[startup] add_view failed:")
        traceback.print_exc()
    await safe_step("global command sync", sync_global_commands())
    guilds = [bot.get_guild(GUILD_ID)] if GUILD_ID else list(bot.guilds)
    if GUILD_ID and guilds == [None]:
        print(f"[startup] The bot is not in server {GUILD_ID}. Invite it there, then restart.")
    for guild in guilds:
        if guild is not None:
            await prepare_guild(guild)      # syncs slash commands first
    await safe_step("app emojis", ensure_app_emojis())
    if not hourly_backup.is_running():
        hourly_backup.start()
    print("Startup finished. Commands are ready.")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """The bot was added to a server: register the commands and ask how to set it up."""
    channel = guild.system_channel
    if channel is None or not channel.permissions_for(guild.me).send_messages:
        channel = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
    embed = setup_embed(guild)
    embed.description = "**Thanks for adding me! Choose how to set me up.**\n\n" + (embed.description or "")
    if channel is not None:
        try:
            await channel.send(embed=embed, view=SetupView())
            return
        except discord.HTTPException:
            pass
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        await owner.send(f"I joined **{guild.name}** but cannot post there. Run `/setup` in that server.")
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.guild is None:
        return
    if isinstance(message.channel, discord.TextChannel) and mirror_by_division_channel(message.channel.id):
        await update_ticket_messy_prompt(message)
    if source_message(message):
        await process_mvp(message)
        return
    mirror = mirror_by_source(message.channel.id)
    if mirror and message.author.bot and not is_me(message.author):
        target = message.guild.get_channel(int(mirror["division_channel_id"]))
        if isinstance(target, discord.TextChannel):
            await live_ingest(message, channel=target)
        return
    if not message.author.bot or is_me(message.author):
        return
    if is_live_channel(message.channel):
        if message.author.id not in LIVE_BOT_IDS:
            LIVE_SEEN[message.author.id] = str(message.author)
        await live_ingest(message)                      # any bot: only the ticket/join patterns count
    elif message.channel.id in LIVE_IGNORE_IDS or message.author.id in LIVE_JOIN_BOT_IDS:
        await live_from_log(message)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.channel_id in SOURCE_IDS:
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.guild and source_message(message):
            await process_mvp(message)
        return
    mirror = mirror_by_source(payload.channel_id)
    if mirror:
        message = await try_get_message(payload.channel_id, payload.message_id)
        target = bot.get_channel(int(mirror["division_channel_id"]))
        if message is not None and target is not None and message.author.bot and not is_me(message.author):
            await live_ingest(message, channel=target)
        return
    if payload.channel_id in LIVE_IGNORE_IDS:
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.guild and message.author.bot and not is_me(message.author):
            await live_from_log(message)
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is not None and is_live_channel(channel):
        message = await try_get_message(payload.channel_id, payload.message_id)
        if message is not None and message.author.bot and not is_me(message.author):
            await live_ingest(message)


@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if channel.name.casefold().startswith("div |"):
        return  # an older Div mirror channel
    if is_live_channel(channel):
        # No new channel and no voice room: the live card is posted inside this Crusher ticket,
        # right after Crusher's first post (live_bootstrap waits until there is something to read).
        await live_bootstrap(channel)


@bot.event
async def on_guild_channel_update(before, after):
    if not isinstance(after, discord.TextChannel) or not is_live_channel(after):
        return
    st = live_load(after.id)
    if st is None:
        await live_bootstrap(after)
        return
    if st["status"] == "live" and st["name"] != after.name:
        async with live_lock(after.id):
            st = live_load(after.id)
            if st is not None and st["name"] != after.name:
                st["name"] = after.name
                await live_publish(after.guild, st)


@bot.event
async def on_guild_channel_delete(channel):
    source_mirror = mirror_by_source(channel.id)
    if source_mirror:
        await remove_ticket_mirror(channel.guild, source_mirror, source_was_deleted=True)
        return
    division_mirror = mirror_by_division_channel(channel.id)
    if division_mirror:
        await remove_ticket_mirror(channel.guild, division_mirror, source_was_deleted=False)
        return
    if is_live_channel(channel):
        await live_close(channel.guild, channel.id)


# ----------------------------------------------------------------------------
