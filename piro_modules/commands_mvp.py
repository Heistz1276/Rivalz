# SLASH COMMANDS: MVP TOOLS
# ----------------------------------------------------------------------------
@bot.tree.command(name="help", description="Show every command")
async def help_command(interaction: discord.Interaction):
    embed = make_embed(f"{BOT_NAME} commands")
    embed.add_field(name="Division", inline=False, value=(
        "`/leaderboard` - show the division leaderboard (only you see it)\n"
        "`/my-rank` - your ticket assists and server rank\n"
        "`/raid` - details of one raid and its raiders\n"
        "`/history` - the last 10 raids\n"
        "`/division` - a member's division and raid stats\n"
        "`/div-assists` - your divisional backup participation and MVP assists\n"
        "`/divisions` - how many members each division has\n"
        "`/leaderboarddivision1` to `5` - top raiders of a division (anyone, anywhere)\n"
        "`/ping` - bot latency, uptime, servers, and members"))
    embed.add_field(name="Live tracking", inline=False, value=(
        "`/sync` - sync every ongoing backup into the live tracking\n"
        "`/live status` `/live list` - what is watched / open posts\n"
        "`/live add` `/live remove` - fix a helper by hand\n"
        "`/live refresh` `/live close` - refresh or close one backup\n"
        "`/live debug` - see why a bot's message is or is not read"))
    embed.add_field(name="Management", inline=False, value=(
        "`/post` - create or refresh the fixed leaderboard\n"
        "`/test` - post a test result and division log; nothing is saved\n"
        "`/recount` - re-read MVP logs and fix the counts\n"
        "`/rebuild` - rebuild saved MVP scores from the source channel history\n"
        "`/addraid` - record an MVP log the bot missed\n"
        "`/deleteraid` - remove a raid and its log\n"
        "`/preview` - preview scoring from typed counts\n"
        "`/status` - configuration and totals"))
    embed.add_field(name="Owner only", inline=False, value=(
        "`/setup` - auto build the channels or choose your own\n"
        "`/trust add` `/trust remove` `/trust list` - choose who can use these commands\n"
        "`/backup` - DM yourself a database backup now (one is also sent every hour)\n"
        "`/restore` - restore the database from a backup `.sqlite3` file\n"
        "**Edit** button on a division log - add or remove raiders"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="status", description="Show configuration and recorded raid count")
@app_commands.guild_only()
async def status_command(interaction: discord.Interaction):
    totals, raids = division_totals(interaction.guild_id)
    embed = make_embed(f"{BOT_NAME} status")
    embed.add_field(name="Saved raids", value=str(raids), inline=True)
    embed.add_field(name="Next raid number", value=f"#{next_raid_number(interaction.guild_id):04d}", inline=True)
    embed.add_field(name="Badges", value=f"{len(BADGES)}/5 ready ({sum(1 for v in BADGE_SOURCE.values() if v == 'file')} custom files)", inline=True)
    embed.add_field(name="MVP channel", value=", ".join(f"<#{c}>" for c in sorted(SOURCE_IDS)), inline=True)
    embed.add_field(name="MVP bot", value=f"<@{MVP_BOT_ID}>", inline=True)
    embed.add_field(name="Owner", value=f"<@{OWNER_ID}>", inline=True)
    embed.add_field(name="Division logs", value=f"<#{LOG_ID}>", inline=True)
    embed.add_field(name="MVP results", value=f"<#{MVP_RESULTS_ID}>", inline=True)
    embed.add_field(name="Fixed leaderboard", value=f"<#{LEADERBOARD_ID}> · updated from MVP logs", inline=True)
    embed.add_field(name="Division ticket category", value=f"<#{DIV_TICKET_CATEGORY_ID}>", inline=True)
    embed.add_field(name="Division roles", value=" \u00b7 ".join(f"D{d}: <@&{r}>" for d, r in DIVISION_ROLES.items()), inline=False)
    embed.add_field(name="MVP awards", value=" \u00b7 ".join(f"D{d}: **{n}**" for d, n in totals.items()) + f"\n{lead_text(leaders_of(totals))}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


@bot.tree.command(name="leaderboard", description="Show the current division leaderboard (only you see it)")
@app_commands.guild_only()
async def leaderboard_command(interaction: discord.Interaction):
    totals, _ = division_totals(interaction.guild_id)
    await send_private_leaderboard(interaction.response.send_message, totals, channel_url(interaction.guild_id, LOG_ID))


@bot.tree.command(name="post", description="Create or refresh the fixed division leaderboard")
@app_commands.guild_only()
async def post_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    message = await refresh_leaderboard(interaction.guild)
    if message is None:
        await interaction.followup.send(embed=make_embed(
            "Leaderboard not updated", f"Check that I can send messages in <#{LEADERBOARD_ID}>.", 0xED4245),
            ephemeral=True)
        return
    await interaction.followup.send(embed=make_embed(
        "Leaderboard refreshed", f"Updated the fixed leaderboard: {message.jump_url}", 0x57F287), ephemeral=True)


@bot.tree.command(name="raid", description="Show the details of one recorded raid")
@app_commands.describe(number="Raid number, e.g. 125")
@app_commands.guild_only()
async def raid_command(interaction: discord.Interaction, number: int):
    row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
    if not row:
        await interaction.response.send_message(embed=make_embed(
            "Raid not found", f"No saved raid with number **{number}**.", 0xED4245), ephemeral=True)
        return
    raiders = json.loads(row["raiders_json"])
    skipped = json.loads(row["skipped_json"] or "[]")
    tallies, winners = tally(raiders)
    embed = make_embed(f"Raid #{number:04d}  \u00b7  Raider Info")
    mvp = ", ".join(f"Division {d}" for d in winners) if winners else "None"
    embed.description = (f"Raiders counted: **{len(raiders)}**  \u00b7  not counted: **{len(skipped)}**\n"
                         f"MVP division(s): **{mvp}**\n[Open the MVP log]"
                         f"({channel_url(interaction.guild_id, row['channel_id'], row['message_id'])})")
    for d in range(1, 6):
        members = [f"<@{r['user_id']}>" for r in raiders if int(r["division"]) == d]
        embed.add_field(name=f"Division {d}  \u00b7  {tallies[d]:02d}",
                        value=fit_list(members, 1000) if members else "No raiders", inline=False)
    if skipped:
        embed.add_field(name="Not counted", inline=False,
                        value=fit_list([f"{who} ({why})" for who, why in skipped], 1000))
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


@bot.tree.command(name="history", description="Show the last 10 recorded raids")
@app_commands.guild_only()
async def history_command(interaction: discord.Interaction):
    rows = db.execute("SELECT raid_number, raiders_json, winners_json FROM raid_results WHERE guild_id=? "
                      "ORDER BY raid_number DESC LIMIT 10", (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message(embed=make_embed("History", "No raids recorded yet."), ephemeral=True)
        return
    lines = []
    for row in rows:
        winners = json.loads(row["winners_json"])
        mvp = " \u00d7 ".join(f"D{d}" for d in winners) if winners else "none"
        lines.append(f"`#{int(row['raid_number']):04d}`  \u00b7  MVP: **{mvp}**  \u00b7  raiders: {len(json.loads(row['raiders_json']))}")
    await interaction.response.send_message(embed=make_embed("Last raids", "\n".join(lines)))


@bot.tree.command(name="division", description="Show a member's division and raid stats")
@app_commands.describe(member="Member to look up (default: you)")
@app_commands.guild_only()
async def division_command(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    division, found = division_of(member)
    joined = mvp_raids = 0
    for row in db.execute("SELECT raiders_json, winners_json FROM raid_results WHERE guild_id=?", (interaction.guild_id,)):
        winners = json.loads(row["winners_json"])
        for raider in json.loads(row["raiders_json"]):
            if raider["user_id"] == member.id:
                joined += 1
                mvp_raids += 1 if int(raider["division"]) in winners else 0
    embed = make_embed(member.display_name, color=DIVISION_COLORS.get(division, BRAND_COLOR))
    embed.add_field(name="Division", value=f"Division {division}" if division else "None", inline=True)
    embed.add_field(name="Raids joined", value=str(joined), inline=True)
    embed.add_field(name="Raids with MVP division", value=str(mvp_raids), inline=True)
    if len(found) > 1:
        embed.add_field(name="Note", value="Holds more than one division role; counted for the highest.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="div-assists", description="Show how many divisional backups you have helped with")
@app_commands.guild_only()
async def div_assists_command(interaction: discord.Interaction):
    member = interaction.user
    today = datetime.now(timezone.utc).date().isoformat()
    total = today_total = mvp_assists = 0
    by_division = {d: 0 for d in range(1, 6)}
    for row in db.execute(
        "SELECT raiders_json, winners_json, created_at FROM raid_results WHERE guild_id=?",
        (interaction.guild_id,),
    ):
        winners = {int(d) for d in json.loads(row["winners_json"])}
        for raider in json.loads(row["raiders_json"]):
            if int(raider["user_id"]) != member.id:
                continue
            division = int(raider["division"])
            total += 1
            by_division[division] += 1
            today_total += row["created_at"][:10] == today
            mvp_assists += division in winners
    embed = make_embed("Divisional Assist Statistics", f"Stats for **{member.display_name}** in **{interaction.guild.name}**")
    embed.add_field(name="User", value=member.mention, inline=False)
    embed.add_field(name="Server Total", value=str(total), inline=True)
    embed.add_field(name="Server Today", value=str(today_total), inline=True)
    embed.add_field(name="MVP assists", value=str(mvp_assists), inline=True)
    embed.add_field(name="Assists by division", value="\n".join(f"Division {d}: **{n}**" for d, n in by_division.items()), inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=NO_PING)


@bot.tree.command(name="my-rank", description="Show your rank for divisional ticket assists")
@app_commands.guild_only()
async def my_rank_command(interaction: discord.Interaction):
    embed = build_my_rank_embed(interaction.guild, interaction.user)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=NO_PING)


def build_my_rank_embed(guild: discord.Guild, member: discord.User | discord.Member) -> discord.Embed:
    counts: dict[int, int] = {}
    by_division = {d: 0 for d in range(1, 6)}
    for row in db.execute("SELECT helpers_json FROM live_tickets WHERE guild_id=?", (guild.id,)):
        try:
            helpers = json.loads(row["helpers_json"] or "[]")
        except (TypeError, ValueError):
            continue
        seen = set()
        for helper in helpers:
            user_id = int(helper["user_id"])
            if user_id in seen:
                continue
            seen.add(user_id)
            counts[user_id] = counts.get(user_id, 0) + 1
            if helper.get("division") in by_division:
                by_division[int(helper["division"])] += 1 if user_id == member.id else 0
    mine = counts.get(member.id, 0)
    rank = 1 + sum(value > mine for value in counts.values())
    population = max(1, guild.member_count or len(guild.members))
    top_percent = min(100.0, rank * 100.0 / population)
    division, _ = division_of(member) if isinstance(member, discord.Member) else (None, set())
    embed = make_embed("📊 Your Division Support Rank", color=DIVISION_COLORS.get(division, 0xA3E635))
    embed.description = f"**Server:** {guild.name}\n**Member:** {member.mention}"
    embed.add_field(name="Rank", value=f"**#{rank}**  ·  Top **{top_percent:.2f}%**", inline=False)
    embed.add_field(name="Ticket assists", value=f"**{mine}**", inline=True)
    embed.add_field(name="Division", value=f"**D{division}**" if division else "No Division role", inline=True)
    embed.add_field(name="Assists by division", value="\n".join(
        f"Division {d}: **{n}**" for d, n in by_division.items()), inline=False)
    return embed


@bot.tree.command(name="divisions", description="How many members each division has")
@app_commands.guild_only()
async def divisions_command(interaction: discord.Interaction):
    counts = {d: 0 for d in range(1, 6)}
    for member in interaction.guild.members:
        division, _ = division_of(member)
        if division and not member.bot:
            counts[division] += 1
    await interaction.response.send_message(embed=make_embed(
        "Division members", "\n".join(f"Division {d}: **{n}**" for d, n in counts.items())))


@bot.tree.command(name="test", description="Preview latest MVP log, result and private leaderboard")
@app_commands.guild_only()
async def test_command(interaction: discord.Interaction):
    global IMAGE_PROBLEM
    await interaction.response.defer(ephemeral=True, thinking=True)
    IMAGE_PROBLEM = None
    message = await latest_mvp_post(interaction.guild)
    if message is None:
        report = await diagnose_channel(interaction.guild)
        await interaction.followup.send(embed=make_embed(
            "No MVP log found",
            f"I couldn't find a recent MVP log with a Raid #number from <@{MVP_BOT_ID}> in <#{MVP_CHANNEL_ID}>.\n\n"
            f"**What the bot sees in that channel**\n{report}", 0xED4245), ephemeral=True, allowed_mentions=NO_PING)
        return

    raid_number = int(RAID_RE.search(post_text(message)).group(1))
    raiders, skipped = await collect_raiders(message)
    tallies, winners = tally(raiders)
    guild_id = interaction.guild_id
    totals, _ = division_totals(guild_id)
    already = db.execute("SELECT 1 FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
    if not already:
        for division in winners:
            totals[division] += 1
    lead = leaders_of(totals)

    log_msg = await post_new(LOG_ID, lambda _images: RaidLogView(
        raid_number, raiders, skipped, winners, message.jump_url, False, test=True, lead=lead), with_files=False)
    logs_url = log_msg.jump_url if log_msg else channel_url(guild_id, LOG_ID)
    result_msg = await post_new(MVP_RESULTS_ID, lambda _images: RaidUpdateView(
        raid_number, tallies, winners, len(raiders), message.jump_url, logs_url, test=True))

    thumbs = "all five badges sent" if IMAGE_PROBLEM is None else f"REJECTED by Discord, sent without images: {IMAGE_PROBLEM}"
    lines = [f"Source: [open the MVP log]({message.jump_url})",
             f"Pinged: **{len(raiders) + len(skipped)}**  \u00b7  counted: **{len(raiders)}**  \u00b7  not counted: **{len(skipped)}**",
             f"Nothing is saved or counted.  \u00b7  bot v{VERSION}",
             f"Badges: **{len(BADGES)}/5** loaded  \u00b7  thumbnails: {thumbs}",
             f"MVP result: {result_msg.jump_url if result_msg else f'could not post in <#{MVP_RESULTS_ID}>'}",
             f"Division log: {log_msg.jump_url if log_msg else f'could not post in <#{LOG_ID}>'}",
             "Leaderboard: private preview below; scores were not saved."]
    if skipped:
        lines.append("Not counted: " + fit_list([f"{who} ({why})" for who, why in skipped], 700))
    ok = log_msg is not None and result_msg is not None
    await interaction.followup.send(embed=make_embed(
        f"Test preview  \u00b7  Raid #{raid_number:04d}", "\n".join(lines), 0xFEE75C if ok else 0xED4245),
        ephemeral=True, allowed_mentions=NO_PING)
    await send_private_leaderboard(interaction.followup.send, totals, channel_url(guild_id, LOG_ID), test=True)


@bot.tree.command(name="recount", description="Re-read MVP logs and fix the counts")
@app_commands.describe(number="Raid number to recount (leave empty to recount every saved raid)")
@app_commands.guild_only()
async def recount_command(interaction: discord.Interaction, number: int | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if number is not None:
        row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
        message = await try_get_message(row["channel_id"], row["message_id"]) if row else None
        if message is None:
            await interaction.followup.send(embed=make_embed(
                "Cannot recount", f"Raid **{number}** is not saved, or its MVP log no longer exists.", 0xED4245), ephemeral=True)
            return
        status = await process_mvp(message, force=True)
        await interaction.followup.send(embed=make_embed(
            "Recount done", f"Raid **#{number:04d}**: {status}. The log and the leaderboard were updated.", 0x57F287), ephemeral=True)
        return
    changed = missing = 0
    for row in db.execute("SELECT channel_id, message_id FROM raid_results WHERE guild_id=?", (interaction.guild_id,)).fetchall():
        message = await try_get_message(row["channel_id"], row["message_id"])
        if message is None:
            missing += 1
            continue
        if await process_mvp(message, force=True, publish=False) == "updated":
            changed += 1
    await refresh_leaderboard(interaction.guild)
    await interaction.followup.send(embed=make_embed(
        "Recount done", f"Raids with changed counts: **{changed}**  \u00b7  logs not found: **{missing}**.\n"
        "Totals and the leaderboard are updated. Use `/recount number:` to also refresh one raid's log.", 0x57F287), ephemeral=True)


@bot.tree.command(name="rebuild", description="Rebuild MVP scores from the configured MVP channel history")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def rebuild_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    scanned = saved = skipped = inaccessible = 0
    for channel_id in sorted(SOURCE_IDS):
        channel = interaction.guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            inaccessible += 1
            continue
        try:
            async for message in channel.history(limit=None, oldest_first=True):
                if not is_mvp_post(message) or not RAID_RE.search(post_text(message)):
                    continue
                scanned += 1
                status = await process_mvp(message, force=True, publish=False)
                if status in {"new", "updated"}:
                    saved += 1
                elif status == "ignored":
                    skipped += 1
        except discord.Forbidden:
            inaccessible += 1
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=make_embed(
                "Rebuild stopped", f"Discord could not read <#{channel_id}>: {exc}", 0xED4245), ephemeral=True)
            return
    board = await refresh_leaderboard(interaction.guild)
    totals, raid_count = division_totals(interaction.guild_id)
    access_note = f"\nChannels I could not read: **{inaccessible}**." if inaccessible else ""
    board_note = "The fixed leaderboard was refreshed." if board else "I could not refresh the fixed leaderboard; check its channel access."
    await interaction.followup.send(embed=make_embed(
        "MVP history rebuilt",
        f"MVP logs found: **{scanned}** · records added or updated: **{saved}** · skipped: **{skipped}**.\n"
        f"Saved raids: **{raid_count}** · points: " + " · ".join(f"D{d}: **{totals[d]}**" for d in range(1, 6))
        + f"\n{board_note}{access_note}\nIndividual raid result cards were not reposted.",
        0x57F287 if board else 0xFEE75C), ephemeral=True)


@bot.tree.command(name="addraid", description="Record an MVP log that the bot missed")
@app_commands.describe(message="Message link or ID of the MVP log")
@app_commands.guild_only()
async def addraid_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    ids = re.findall(r"\d{15,21}", message)
    target = None
    if ids:
        for channel_id in SOURCE_IDS:
            target = await try_get_message(channel_id, int(ids[-1]))
            if target:
                break
    if target is None:
        await interaction.followup.send(embed=make_embed(
            "Message not found", "I could not find that message in the MVP channel.", 0xED4245), ephemeral=True)
        return
    status = await process_mvp(target, force=True)
    text = {"new": "Recorded and posted.", "updated": "Already saved; recounted and updated.",
            "unchanged": "Already saved; nothing changed.", "ignored": "That message is not a valid MVP log (check the bot ID and the Raid #number)."}[status]
    await interaction.followup.send(embed=make_embed("Add raid", text, 0x57F287 if status != "ignored" else 0xED4245), ephemeral=True)


@bot.tree.command(name="deleteraid", description="Remove a recorded raid and its log")
@app_commands.describe(number="Raid number to remove")
@app_commands.guild_only()
async def deleteraid_command(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True, thinking=True)
    row = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND raid_number=?", (interaction.guild_id, number)).fetchone()
    if not row:
        await interaction.followup.send(embed=make_embed("Raid not found", f"No saved raid **{number}**.", 0xED4245), ephemeral=True)
        return
    await delete_ref(row["update_ref"])
    await delete_ref(row["log_ref"])
    db.execute("DELETE FROM raid_results WHERE message_id=?", (row["message_id"],))
    db.commit()
    await refresh_leaderboard(interaction.guild)
    await interaction.followup.send(embed=make_embed(
        "Raid removed", f"Raid **#{number:04d}** was deleted and the leaderboard updated.", 0x57F287), ephemeral=True)


@bot.tree.command(name="preview", description="Preview an MVP result from typed counts (nothing saved)")
@app_commands.describe(d1="Division 1 raiders", d2="Division 2 raiders", d3="Division 3 raiders",
                       d4="Division 4 raiders", d5="Division 5 raiders")
async def preview_command(interaction: discord.Interaction, d1: app_commands.Range[int, 0, 500],
                          d2: app_commands.Range[int, 0, 500], d3: app_commands.Range[int, 0, 500],
                          d4: app_commands.Range[int, 0, 500], d5: app_commands.Range[int, 0, 500]):
    counts = {1: d1, 2: d2, 3: d3, 4: d4, 5: d5}
    high = max(counts.values())
    winners = [d for d, n in counts.items() if high > 0 and n == high]
    winner_text = ", ".join(f"Division {d}" for d in winners) if winners else "None"
    await interaction.response.send_message(embed=make_embed(
        "MVP scoring preview  \u00b7  nothing saved",
        f"MVP division(s): **{winner_text}**\n\n" + " \u00b7 ".join(f"D{d}: **{n:02d}**" for d, n in counts.items()), 0xFEE75C),
        ephemeral=True)


@bot.tree.command(name="backup", description="Owner only: send a database backup to your DMs now")
async def backup_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    text = await send_backup_dm("manual")
    await interaction.followup.send(embed=make_embed("Backup", text, 0x57F287), ephemeral=True)


@bot.tree.command(name="restore", description="Owner only: restore the database from a backup .sqlite3 file")
@app_commands.describe(file="A urdiv_*.sqlite3 backup file from your DMs")
@app_commands.guild_only()
async def restore_command(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if interaction.user.id != OWNER_ID:
        await interaction.followup.send("Only the owner can restore a backup.", ephemeral=True)
        return
    if file.size > 25 * 1024 * 1024 or not file.filename.lower().endswith((".sqlite3", ".sqlite", ".db")):
        await interaction.followup.send(embed=make_embed(
            "Restore", "Attach a `.sqlite3` backup file (under 25 MB).", 0xED4245), ephemeral=True)
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    incoming = BACKUP_DIR / "incoming_restore.sqlite3"
    await file.save(incoming)
    try:
        src = sqlite3.connect(f"file:{incoming}?mode=ro", uri=True)
        try:
            integrity = src.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if integrity != "ok" or "raid_results" not in tables:
                await interaction.followup.send(embed=make_embed(
                    "Restore", f"That file is not a valid backup (integrity: {integrity}).", 0xED4245), ephemeral=True)
                return
            make_backup()
            async with PROCESS_LOCK:
                src.backup(db)
        finally:
            src.close()
        ensure_schema()
        apply_config(interaction.guild_id)
        await refresh_leaderboard(interaction.guild)
        totals, raids = division_totals(interaction.guild_id)
        await interaction.followup.send(embed=make_embed(
            "Restore done", f"Raids loaded: **{raids}**.\n"
            "A copy of the previous data was kept in the backups folder.", 0x57F287), ephemeral=True)
    except Exception as exc:
        print(f"Restore failed: {exc!r}")
        await interaction.followup.send(embed=make_embed("Restore failed", f"`{exc!r}`", 0xED4245), ephemeral=True)
    finally:
        try:
            incoming.unlink()
        except OSError:
            pass


@bot.tree.command(name="ping", description="Show Piro latency and server stats")
async def ping_command(interaction: discord.Interaction):
    seconds = int(time.time() - START_TIME)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    members = sum(g.member_count or len(g.members) for g in bot.guilds)
    embed = make_embed(f"{BOT_NAME} · Ping", color=0xA3E635)
    embed.description = (
        f"🟢 **Latency:** `{round(bot.latency * 1000)} ms`\n"
        f"⏱️ **Uptime:** `{days}d {hours}h {minutes}m`\n"
        f"🌐 **Servers:** `{len(bot.guilds):,}`\n"
        f"👥 **Members:** `{members:,}`"
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure) and str(error) == "wrong_server":
        embed = make_embed("Wrong server", "This bot is set up for the URA server only.", 0xED4245)
    elif isinstance(error, app_commands.CheckFailure):
        embed = make_embed("Not allowed", "Only the owner and trusted users can use this command.", 0xED4245)
    else:
        embed = make_embed(f"{BOT_NAME} command error", "The command failed. Check the bot console for details.", 0xED4245)
        print(f"Slash command error: {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


# ----------------------------------------------------------------------------
