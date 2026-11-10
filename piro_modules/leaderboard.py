# FIXED LEADERBOARD + MVP PROCESSING
# ----------------------------------------------------------------------------
async def find_leaderboard_post(channel_id: int) -> str | None:
    channel = await get_channel(channel_id)
    if channel is None or not hasattr(channel, "history") or bot.user is None:
        return None
    try:
        async for m in channel.history(limit=100):
            if m.author.id != bot.user.id:
                continue
            text = post_text(m)
            upper = text.upper()
            if "DIVISION" in upper and "LEADERBOARD" in upper and "TEST PREVIEW" not in upper:
                return f"{channel.id}:{m.id}"
    except discord.HTTPException:
        pass
    return None


async def refresh_leaderboard(guild: discord.Guild) -> discord.Message | None:
    """Create or edit this server's fixed leaderboard using saved MVP-log scores."""
    if not LEADERBOARD_ID:
        return None
    async with LB_LOCK:
        key = f"leaderboard_fixed:{guild.id}"
        saved = get_setting(key)
        ref = saved
        if ref and ":" in ref:
            channel_text, message_text = ref.split(":", 1)
            if await try_get_message(int(channel_text), int(message_text)) is None:
                ref = None
        else:
            ref = None
        if ref is None:
            ref = await find_leaderboard_post(LEADERBOARD_ID)
        totals, _ = division_totals(guild.id)
        history_url = channel_url(guild.id, LOG_ID)
        try:
            ref = await send_or_edit(LEADERBOARD_ID, ref, lambda _images: LeaderboardView(
                totals, history_url, True, fixed=True), with_files=False)
        except discord.HTTPException as exc:
            print(f"Could not update the fixed leaderboard: {exc!r}")
            return None
        if not ref or ":" not in ref:
            return None
        if ref != saved:
            set_setting(key, ref)
        set_setting("leaderboard_updated_ts", str(int(time.time())))
        channel_text, message_text = ref.split(":", 1)
        return await try_get_message(int(channel_text), int(message_text))


async def publish_raid(guild: discord.Guild, row_ids: tuple[int, int], raid_number: int, raiders: list[dict],
                       skipped: list, update_ref: str | None, log_ref: str | None) -> tuple[str | None, str | None]:
    channel_id, message_id = row_ids
    tallies, winners = tally(raiders)
    info_url = channel_url(guild.id, channel_id, message_id)
    lead = leaders_of(division_totals(guild.id)[0])
    try:
        log_ref = await send_or_edit(LOG_ID, log_ref, lambda _images: RaidLogView(
            raid_number, raiders, skipped, winners, info_url, False, lead=lead), with_files=False)
    except discord.HTTPException as exc:
        print(f"Could not publish the division log for raid {raid_number}: {exc!r}")
    logs_url = ref_url(guild.id, log_ref, LOG_ID)
    try:
        update_ref = await send_or_edit(MVP_RESULTS_ID, update_ref, lambda _images: RaidUpdateView(
            raid_number, tallies, winners, len(raiders), info_url, logs_url), with_files=False)
    except discord.HTTPException as exc:
        print(f"Could not publish the MVP result for raid {raid_number}: {exc!r}")
    await refresh_leaderboard(guild)
    return update_ref, log_ref


def raider_signature(raiders: list[dict]):
    return sorted((int(r["user_id"]), int(r["division"])) for r in raiders)


async def process_mvp(message: discord.Message, *, force: bool = False, publish: bool = True) -> str:
    if not source_message(message) or not is_mvp_post(message):
        return "ignored"
    async with PROCESS_LOCK:
        raid_match = RAID_RE.search(post_text(message))
        if not raid_match:
            print(f"Skipped MVP post {message.id}: no explicit Raid #number was found.")
            return "ignored"
        raid_number = int(raid_match.group(1))
        raiders, skipped = await collect_raiders(message)
        guild = message.guild

        row = db.execute("SELECT * FROM raid_results WHERE message_id=?", (message.id,)).fetchone()
        merged = False
        if row is None:
            clause, channel_params = source_db_filter()
            row = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND raid_number=? AND {clause}",
                             (guild.id, raid_number, *channel_params)).fetchone()
            merged = row is not None

        if row is not None:
            old_raiders = json.loads(row["raiders_json"])
            if merged:
                known = {r["user_id"] for r in raiders}
                raiders = raiders + [r for r in old_raiders if r["user_id"] not in known]
                skipped = json.loads(row["skipped_json"] or "[]") + skipped
            changed = raider_signature(old_raiders) != raider_signature(raiders)
            if not changed and not force:
                return "unchanged"
            tallies, winners = tally(raiders)
            db.execute("UPDATE raid_results SET raiders_json=?, tallies_json=?, winners_json=?, skipped_json=? WHERE message_id=?",
                       (json.dumps(raiders), json.dumps({str(k): v for k, v in tallies.items()}),
                        json.dumps(winners), json.dumps(skipped), row["message_id"]))
            update_ref, log_ref = row["update_ref"], row["log_ref"]
            ids = (row["channel_id"], row["message_id"])
            status = "updated" if changed else "unchanged"
        else:
            tallies, winners = tally(raiders)
            db.execute("""INSERT INTO raid_results
                (message_id,guild_id,channel_id,raid_number,raiders_json,tallies_json,winners_json,created_at,skipped_json)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                       (message.id, guild.id, message.channel.id, raid_number, json.dumps(raiders),
                        json.dumps({str(k): v for k, v in tallies.items()}), json.dumps(winners),
                        message.created_at.astimezone(timezone.utc).isoformat(), json.dumps(skipped)))
            update_ref = log_ref = None
            ids = (message.channel.id, message.id)
            status = "new"
        db.commit()
        if skipped:
            print(f"Raid #{raid_number}: {len(skipped)} pinged member(s) not counted: " +
                  ", ".join(f"{who} ({why})" for who, why in skipped))

        if publish:
            update_ref, log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped,
                                                     update_ref, log_ref)
            db.execute("UPDATE raid_results SET update_ref=?,log_ref=? WHERE message_id=?",
                       (update_ref, log_ref, ids[1]))
            db.commit()
        return status


async def apply_edit(guild: discord.Guild, raid_number: int, to_add: list[int], to_remove: list[int]) -> str:
    async with PROCESS_LOCK:
        clause, channel_params = source_db_filter()
        row = db.execute(f"SELECT * FROM raid_results WHERE guild_id=? AND raid_number=? AND {clause}",
                         (guild.id, raid_number, *channel_params)).fetchone()
        if row is None:
            return f"Raid **#{raid_number:04d}** is not saved."
        raiders = json.loads(row["raiders_json"])
        skipped = json.loads(row["skipped_json"] or "[]")

        drop = set(to_remove)
        removed = [r for r in raiders if r["user_id"] in drop]
        raiders = [r for r in raiders if r["user_id"] not in drop]

        added, failed = [], []
        for user_id in dict.fromkeys(to_add):
            if any(r["user_id"] == user_id for r in raiders):
                continue
            member = await get_member(guild, user_id)
            if member is None:
                failed.append(f"<@{user_id}> (not in the server)")
                continue
            division, found = division_of(member)
            if member.bot or division is None:
                failed.append(f"<@{user_id}> (no division role)")
                continue
            raiders.append({"user_id": member.id, "display_name": member.display_name,
                            "division": division, "multi": len(found) > 1})
            added.append(f"<@{user_id}>")
        added_ids = {int(re.sub(r"\D", "", a)) for a in added}
        skipped = [s for s in skipped if int(re.sub(r"\D", "", s[0]) or 0) not in added_ids]

        tallies, winners = tally(raiders)
        db.execute("UPDATE raid_results SET raiders_json=?, tallies_json=?, winners_json=?, skipped_json=? WHERE message_id=?",
                   (json.dumps(raiders), json.dumps({str(k): v for k, v in tallies.items()}),
                    json.dumps(winners), json.dumps(skipped), row["message_id"]))
        db.commit()
        ids = (row["channel_id"], row["message_id"])
        update_ref, log_ref = await publish_raid(guild, ids, raid_number, raiders, skipped,
                                                 row["update_ref"], row["log_ref"])
        db.execute("UPDATE raid_results SET update_ref=?,log_ref=? WHERE message_id=?",
                   (update_ref, log_ref, ids[1]))
        db.commit()

    lines = [f"Raid **#{raid_number:04d}** updated. Raiders counted: **{len(raiders)}**."]
    if added:
        lines.append("Added: " + fit_list(added, 500))
    if removed:
        lines.append("Removed: " + fit_list([f"<@{r['user_id']}>" for r in removed], 500))
    if failed:
        lines.append("Could not add: " + fit_list(failed, 500))
    if not (added or removed or failed):
        lines.append("Nothing changed.")
    return "\n".join(lines)


async def latest_mvp_post(guild: discord.Guild) -> discord.Message | None:
    latest = None
    for channel_id in SOURCE_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            continue
        try:
            async for message in channel.history(limit=200):
                if is_mvp_post(message) and RAID_RE.search(post_text(message)):
                    if latest is None or message.created_at > latest.created_at:
                        latest = message
                    break
        except discord.Forbidden:
            print(f"No history permission in channel {channel_id}; cannot find the latest MVP log.")
    return latest


async def diagnose_channel(guild: discord.Guild) -> str:
    lines = [f"discord.py version: {discord.__version__} (Components V2 logs need 2.6 or newer)"]
    for channel_id in SOURCE_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None:
            lines.append(f"<#{channel_id}>: the bot cannot see this channel (check View Channel).")
            continue
        try:
            shown = empty = 0
            async for m in channel.history(limit=25):
                if shown >= 6:
                    break
                if not m.author.bot:
                    continue
                text = post_text(m)
                shown += 1
                empty += 0 if text.strip() else 1
                preview = " ".join(text.split())[:90] or "(no readable text)"
                has_mvp = "yes" if re.search(r"\bMVPs?\b", text, re.I) else "NO"
                has_raid = "yes" if re.search(r"\bRaid\b", text, re.I) else "NO"
                id_ok = "yes" if m.author.id == MVP_BOT_ID else "NO"
                has_number = "found" if RAID_RE.search(text) else "NOT found"
                lines.append(f"**{m.author}** `{m.author.id}`  \u00b7  bot ID match: {id_ok}  \u00b7  MVP word: {has_mvp}"
                             f"  \u00b7  Raid word: {has_raid}  \u00b7  Raid #: {has_number}\n> {preview}")
            if shown == 0:
                lines.append(f"<#{channel_id}>: no bot posts in the last 25 messages (or the bot cannot read the history).")
            elif empty == shown:
                lines.append("Every bot post has no readable text. Turn on Message Content Intent and use discord.py 2.6+.")
        except discord.Forbidden:
            lines.append(f"<#{channel_id}>: the bot lacks Read Message History here.")
    return "\n".join(lines)[:3000]


async def check_channels(guild: discord.Guild) -> None:
    routes = {"Division log": (LOG_ID, True), "Fixed leaderboard": (LEADERBOARD_ID, True),
              "Live ticket feed": (LIVE_POST_ID, True), "MVP source": (MVP_CHANNEL_ID, False)}
    print("---- channel routing ----")
    for label, (cid, needs_send) in routes.items():
        if not cid:
            print(f"  {label}: disabled")
            continue
        channel = guild.get_channel(cid)
        if channel is None:
            print(f"  {label}: {cid}  NOT FOUND in {guild.name} (wrong ID, or the bot cannot see it)")
            continue
        perms = channel.permissions_for(guild.me)
        ok = perms.view_channel and perms.read_message_history and (perms.send_messages if needs_send else True)
        needed = "View/History/Send" if needs_send else "View/History"
        print(f"  {label}: #{channel.name} ({cid})  {'ok' if ok else f'MISSING {needed} permission'}")
    for label, cid in (("Crusher source category", CRUSHER_TICKET_CATEGORY_ID),
                       ("Division ticket category", DIV_TICKET_CATEGORY_ID)):
        channel = guild.get_channel(cid) if cid else None
        status = "audit-log detection" if label == "Crusher source category" and not cid else (
            "NOT FOUND" if channel is None else type(channel).__name__)
        print(f"  {label}: {cid or 0}  {status}")
    print("  Join bots read:", ", ".join(str(i) for i in sorted(LIVE_JOIN_BOT_IDS)))
    print("-------------------------")


def register_saved_views(guild: discord.Guild) -> None:
    fixed_ref = get_setting(f"leaderboard_fixed:{guild.id}")
    if fixed_ref and ":" in fixed_ref:
        try:
            _, fixed_mid = fixed_ref.split(":", 1)
            totals, _ = division_totals(guild.id)
            bot.add_view(LeaderboardView(totals, channel_url(guild.id, LOG_ID), True, fixed=True),
                         message_id=int(fixed_mid))
        except Exception as exc:
            print(f"Could not re-arm the fixed leaderboard buttons: {exc!r}")
    result_rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND update_ref IS NOT NULL "
                             "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in result_rows:
        try:
            _, mid = row["update_ref"].split(":", 1)
            raiders = json.loads(row["raiders_json"])
            tallies, winners = tally(raiders)
            info = channel_url(guild.id, row["channel_id"], row["message_id"])
            logs = ref_url(guild.id, row["log_ref"], LOG_ID)
            bot.add_view(RaidUpdateView(int(row["raid_number"]), tallies, winners, len(raiders), info, logs),
                         message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the result buttons for raid {row['raid_number']}: {exc!r}")
    rows = db.execute("SELECT * FROM raid_results WHERE guild_id=? AND log_ref IS NOT NULL "
                      "ORDER BY raid_number DESC LIMIT 50", (guild.id,)).fetchall()
    for row in rows:
        try:
            _, mid = row["log_ref"].split(":", 1)
            bot.add_view(build_log_view(guild.id, row, images=False), message_id=int(mid))
        except Exception as exc:
            print(f"Could not re-arm the buttons for raid {row['raid_number']}: {exc!r}")


# ----------------------------------------------------------------------------
