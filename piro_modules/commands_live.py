# SLASH COMMANDS: LIVE TRACKING
# ----------------------------------------------------------------------------
live_group = app_commands.Group(name="live", description="Live backup/ticket tracking", guild_only=True)


def pick_ticket(interaction: discord.Interaction, channel):
    channel = channel or interaction.channel
    return channel if is_live_channel(channel) else None


NOT_TICKET = ("That channel is not a tracked ticket/backup (it must be inside the ticket category). "
              "Run `/live status` to see what is watched.")


@live_group.command(name="status", description="Show what the live tracker is watching")
async def live_status(interaction: discord.Interaction):
    guild = interaction.guild
    watching = []
    for cid in sorted(LIVE_CATEGORY_IDS):
        channel = guild.get_channel(cid)
        if channel is None:
            watching.append(f"`{cid}`  NOT FOUND (wrong ID, or the bot cannot see it)")
        elif isinstance(channel, discord.CategoryChannel):
            tickets = [c for c in channel.text_channels if is_live_channel(c)]
            watching.append(f"Category **{channel.name}**  \u00b7  {len(tickets)} channel(s) now")
        else:
            watching.append(f"`{cid}` is not a category (set the category ID)")
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC LIMIT 10",
                      (guild.id,)).fetchall()
    open_tickets = [f"<#{r['channel_id']}>  \u00b7  helpers: {len(json.loads(r['helpers_json']))}" for r in rows]
    embed = make_embed("Live tracker")
    embed.add_field(name="Watching (every new channel in)", value="\n".join(watching), inline=False)
    embed.add_field(name="Base channels (not tickets)", value=" ".join(f"<#{i}>" for i in sorted(LIVE_IGNORE_IDS)) or "None", inline=False)
    embed.add_field(name="Bots read", value=" ".join(f"<@{i}>" for i in sorted(LIVE_BOT_IDS)), inline=True)
    embed.add_field(name="Join bots (every ping = helper)", value=" ".join(f"<@{i}>" for i in sorted(LIVE_JOIN_BOT_IDS)), inline=True)
    embed.add_field(name="Live posts", value="Inside each ticket", inline=True)
    embed.add_field(name="Open tracked", value="\n".join(open_tickets) if open_tickets else "None right now", inline=False)
    if LIVE_SEEN:
        embed.add_field(name="Other bots seen in ticket channels (not tracked)", inline=False,
                        value="\n".join(f"{name}  `{uid}`" for uid, name in LIVE_SEEN.items())[:900])
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="list", description="List every open live post with its helper count")
async def live_list(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM live_tickets WHERE guild_id=? AND status='live' ORDER BY created_at DESC",
                      (interaction.guild_id,)).fetchall()
    lines = []
    for r in rows:
        link = ref_url(interaction.guild_id, r["post_ref"], r["channel_id"])
        lines.append(f"<#{r['channel_id']}>  \u00b7  helpers **{len(json.loads(r['helpers_json']))}**  \u00b7  [live post]({link})")
    await interaction.response.send_message(embed=make_embed(
        "Open live posts", "\n".join(lines)[:3900] if lines else "Nothing is being tracked right now."),
        ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="add", description="Manually add a helper to a backup/ticket")
@app_commands.describe(member="Who joined", channel="The ticket channel (default: this one)")
async def live_add(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await live_manual(interaction.guild, target, member, False),
                                    ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="remove", description="Remove a helper from a backup/ticket (until the next /sync)")
@app_commands.describe(member="Who to remove", channel="The ticket channel (default: this one)")
async def live_remove(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.followup.send(await live_manual(interaction.guild, target, member, True),
                                    ephemeral=True, allowed_mentions=NO_PING)


@live_group.command(name="close", description="Mark a backup/ticket as closed")
@app_commands.describe(channel="The ticket channel (default: this one)")
async def live_close_command(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    await live_close(interaction.guild, target.id)
    await interaction.followup.send(f"<#{target.id}> marked as closed.", ephemeral=True)


@live_group.command(name="refresh", description="Re-read one backup/ticket and refresh its live post")
@app_commands.describe(channel="The ticket channel (default: this one)")
async def live_refresh(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    target = pick_ticket(interaction, channel)
    if target is None:
        await interaction.response.send_message(NOT_TICKET, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with live_lock(target.id):
        st = live_load(target.id) or live_new(target)
        st["status"] = "live"
        st["name"] = target.name
        mirror = mirror_by_division_channel(target.id)
        source = interaction.guild.get_channel(int(mirror["source_channel_id"])) if mirror else None
        await live_catch_up(st, source or target)
        live_refresh_divisions(st, interaction.guild)
        live_store(st)
        await live_publish(interaction.guild, st)
    await interaction.followup.send(f"<#{target.id}> refreshed. Helpers: **{len(st['helpers'])}**.", ephemeral=True)


@live_group.command(name="debug", description="Show what the bot sees from other bots in a channel")
@app_commands.describe(channel="Channel to inspect (default: this one)")
async def live_debug(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.channel
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = [f"Tracked ticket channel: **{'yes' if is_live_channel(channel) else 'NO'}**"]
    try:
        async for m in channel.history(limit=20):
            if not m.author.bot:
                continue
            text = post_text(m)
            join = "join bot" if m.author.id in LIVE_JOIN_BOT_IDS else ("listed" if m.author.id in LIVE_BOT_IDS else "not listed")
            match = "MATCH" if live_relevant(text, m.author.id) else "no pattern"
            preview = " ".join(text.split())[:140] or "(no readable text)"
            lines.append(f"**{m.author}** `{m.author.id}`  \u00b7  {join}  \u00b7  {match}\n> {preview}")
    except discord.Forbidden:
        lines.append("I cannot read the history of that channel.")
    await interaction.followup.send(embed=make_embed("Live debug", "\n".join(lines)[:3900]), ephemeral=True)


bot.tree.add_command(live_group)


@bot.tree.command(name="sync", description="Sync every ongoing backup/ticket into the live tracking")
@app_commands.guild_only()
async def sync_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    results = await live_sync_now(interaction.guild)
    if not results:
        await interaction.followup.send(embed=make_embed(
            "Nothing to sync", "No channels were found in the ticket category. Run `/live status` to check it.", 0xED4245),
            ephemeral=True)
        return
    lines = [f"<#{c.id}>  \u00b7  helpers joined: **{n}**" for c, n in results]
    await interaction.followup.send(embed=make_embed(
        f"Synced {len(results)} backup(s)", "\n".join(lines)[:3900] + "\nLive cards are posted inside each ticket.", 0x57F287),
        ephemeral=True, allowed_mentions=NO_PING)


# ----------------------------------------------------------------------------
