# BACKUPS (hourly DM to the owner + rolling copies on disk)
# ----------------------------------------------------------------------------
def make_backup() -> tuple[Path, Path, str, int]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    sqlite_path = BACKUP_DIR / f"urdiv_{stamp}.sqlite3"
    json_path = BACKUP_DIR / f"urdiv_{stamp}.json"
    dest = sqlite3.connect(sqlite_path)
    try:
        db.backup(dest)
    finally:
        dest.close()
    check = sqlite3.connect(sqlite_path)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    raids = []
    for row in db.execute("SELECT * FROM raid_results ORDER BY raid_number"):
        item = dict(row)
        for key in ("raiders_json", "tallies_json", "winners_json", "skipped_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.pop(key) or "null")
            except (TypeError, ValueError):
                pass
        raids.append(item)
    export = {"version": VERSION, "saved_at": datetime.now(timezone.utc).isoformat(), "raids": raids,
              "settings": {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM settings")},
              "trusted": [r["user_id"] for r in db.execute("SELECT user_id FROM trusted")]}
    json_path.write_text(json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8")
    for pattern in ("urdiv_*.sqlite3", "urdiv_*.json"):
        for old in sorted(BACKUP_DIR.glob(pattern))[:-KEEP_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass
    return sqlite_path, json_path, integrity, len(raids)


async def send_backup_dm(reason: str = "hourly") -> str:
    try:
        sqlite_path, json_path, integrity, count = make_backup()
    except Exception as exc:
        print(f"Backup failed: {exc!r}")
        return f"Backup failed: {exc!r}"
    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        stamp = int(datetime.now(timezone.utc).timestamp())
        await owner.send(
            f"**{BOT_NAME} backup** ({reason})  \u00b7  <t:{stamp}:f>\n"
            f"Raids saved: **{count}**  \u00b7  integrity check: **{integrity}**\n"
            f"Restore with `/restore` and the `.sqlite3` file. The `.json` is a readable copy.",
            files=[discord.File(sqlite_path), discord.File(json_path)])
        set_setting("last_backup_ts", str(stamp))
        return f"Backup sent to your DMs ({count} raids, integrity: {integrity})."
    except discord.Forbidden:
        print("Backup made but the owner's DMs are closed. Open DMs from server members.")
        return "Backup saved on the server, but I can't DM you. Turn on DMs from server members."
    except discord.HTTPException as exc:
        print(f"Could not DM the backup: {exc!r}")
        return f"Backup saved on the server, but the DM failed: {exc!r}"


@tasks.loop(hours=1)
async def hourly_backup():
    last = get_setting("last_backup_ts")
    if last and time.time() - int(last) < 50 * 60:
        return
    await send_backup_dm("hourly")


@hourly_backup.before_loop
async def _before_hourly_backup():
    await bot.wait_until_ready()


# ----------------------------------------------------------------------------
