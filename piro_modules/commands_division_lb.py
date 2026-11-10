# SLASH COMMANDS: TOP RAIDERS PER DIVISION (/leaderboarddivision1 ... /leaderboarddivision5)
# ----------------------------------------------------------------------------
# READ-ONLY: these commands only read the saved `raid_results` (every MVP result) and `live_tickets` helper lists.
# They never write, reset or change any saved data.
#
# Every saved raid result adds +1 "raid" to each raider of that division (and +1 "MVP" when that division won the
# raid). Editing a result (Edit button, /recount, /addraid, /rebuild) changes the saved raiders, so the numbers
# here follow automatically. Ticket helps are shown next to them and used as the tie-break.
#
# Anyone can use them, in any server or DM (they are GLOBAL commands, see main.py / events.py).
# The numbers always come from the URA server. Discord slash names must be lowercase, so
# /LeaderboardDivision1 is typed /leaderboarddivision1.
DIV_LB_COMMANDS = {f"leaderboarddivision{d}" for d in range(1, 6)}
DIV_LB_PAGE_SIZE = 10
DIV_LB_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


def div_lb_collect(guild: discord.Guild, d: int) -> tuple[list[tuple[int, int, int, int]], dict[int, str]]:
    """(ranking, names) for ONE division. Ranking rows are (user_id, raids, mvps, helps):
    raids = saved raid results the member was counted in under this division,
    mvps  = how many of those raids this division won (MVP division),
    helps = backup tickets the member helped in while in this division."""
    role_id = DIVISION_ROLES.get(d)
    raids: dict[int, int] = {}
    mvps: dict[int, int] = {}
    helps: dict[int, int] = {}
    names: dict[int, str] = {}

    # 1) every saved raid result (this is what updates with each result)
    clause, channel_params = source_db_filter()
    query = (f"SELECT raiders_json, winners_json FROM raid_results "
             f"WHERE guild_id=? AND counted=1 AND {clause}")
    for row in db.execute(query, (guild.id, *channel_params)):
        try:
            raiders = json.loads(row["raiders_json"] or "[]")
            winners = {int(x) for x in json.loads(row["winners_json"] or "[]")}
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
        for raider in raiders:
            try:
                user_id = int(raider["user_id"])
                division = int(raider["division"])
            except (KeyError, TypeError, ValueError):
                continue
            if division != d or user_id in seen:
                continue
            seen.add(user_id)
            member = guild.get_member(user_id)
            if member is not None and member.bot:
                continue
            raids[user_id] = raids.get(user_id, 0) + 1
            if d in winners:
                mvps[user_id] = mvps.get(user_id, 0) + 1
            names[user_id] = member.display_name if member is not None else str(raider.get("display_name") or user_id)

    # 2) backup ticket helps (kept, shown beside the raid count)
    for row in db.execute("SELECT helpers_json FROM live_tickets WHERE guild_id=?", (guild.id,)):
        try:
            helpers = json.loads(row["helpers_json"] or "[]")
        except (TypeError, ValueError):
            continue
        seen = set()
        for helper in helpers:
            try:
                user_id = int(helper["user_id"])
            except (KeyError, TypeError, ValueError):
                continue
            if user_id in seen:
                continue
            seen.add(user_id)
            member = guild.get_member(user_id)
            if member is not None:
                if member.bot:
                    continue
                in_division = (any(r.id == role_id for r in member.roles) if role_id
                               else division_of(member)[0] == d)
                name = member.display_name
            else:  # left the server: fall back to the division saved with the helper
                in_division = helper.get("division") == d
                name = str(helper.get("display_name") or user_id)
            if not in_division:
                continue
            helps[user_id] = helps.get(user_id, 0) + 1
            names.setdefault(user_id, name)

    ids = set(raids) | set(helps)
    ranking = [(u, raids.get(u, 0), mvps.get(u, 0), helps.get(u, 0)) for u in ids]
    ranking.sort(key=lambda r: (-r[1], -r[2], -r[3], r[0]))
    return ranking, names


class DivisionLeaderboardView(discord.ui.LayoutView):
    """Top raiders of one division and which division is on lead. Nothing else."""

    def __init__(self, guild: discord.Guild, d: int, page: int, viewer_id: int):
        super().__init__(timeout=900)
        self.guild, self.d, self.viewer_id = guild, d, viewer_id
        ranking, names = div_lb_collect(guild, d)
        pages = max(1, -(-len(ranking) // DIV_LB_PAGE_SIZE))
        self.page = max(0, min(page, pages - 1))
        start = self.page * DIV_LB_PAGE_SIZE
        chunk = ranking[start:start + DIV_LB_PAGE_SIZE]

        totals, raid_count = division_totals(guild.id)
        lead = leaders_of(totals)
        card = discord.ui.Container(accent_colour=DIVISION_COLORS.get(d, BRAND_COLOR))
        card.add_item(discord.ui.TextDisplay(
            f"# {DIV_EMOJI.get(d, '')} DIVISION {d} LEADERBOARD\n**{lead_heading(lead)}**\n"
            f"-# Division {d}: `{totals.get(d, 0)}` MVP wins  \u00b7  `{raid_count}` raids saved"))
        card.add_item(discord.ui.Separator())

        lines = []
        for offset, (user_id, raids, mvps, helps) in enumerate(chunk):
            rank = start + offset + 1
            marker = DIV_LB_MEDALS.get(rank, f"**{rank}.**")
            lines.append(f"{marker} `@{names.get(user_id, user_id)}`  \u00b7  `{raids}` raids  \u00b7  "
                         f"`{mvps}` MVP  \u00b7  `{helps}` helps")
        card.add_item(discord.ui.TextDisplay(
            "\n".join(lines) if lines else "Nobody from this division has been counted in a raid result yet."))

        card.add_item(discord.ui.Separator())
        refresh = discord.ui.Button(label="Refresh", emoji="\U0001F501", style=discord.ButtonStyle.secondary)

        async def refresh_callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=DivisionLeaderboardView(self.guild, self.d, self.page, self.viewer_id))

        refresh.callback = refresh_callback
        card.add_item(discord.ui.ActionRow(refresh))
        if pages > 1:
            row = discord.ui.ActionRow()
            first = self.page == 0
            last = self.page >= pages - 1
            row.add_item(self._nav("\u23EE", 0, first))
            row.add_item(self._nav("\u25C0", self.page - 1, first))
            row.add_item(discord.ui.Button(label=f"{self.page + 1}/{pages}", style=discord.ButtonStyle.secondary,
                                           disabled=True))
            row.add_item(self._nav("\u25B6", self.page + 1, last))
            row.add_item(self._nav("\u23ED", pages - 1, last))
            card.add_item(row)
        self.add_item(card)

    def _nav(self, label: str, target: int, disabled: bool) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, disabled=disabled)

        async def callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                view=DivisionLeaderboardView(self.guild, self.d, target, self.viewer_id))

        button.callback = callback
        return button


def make_division_lb_command(d: int) -> None:
    @bot.tree.command(name=f"leaderboarddivision{d}", description=f"Top raiders of Division {d}")
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    async def division_lb_command(interaction: discord.Interaction):
        guild = bot.get_guild(GUILD_ID) or interaction.guild
        if guild is None:
            await interaction.response.send_message("I can't reach the URA server right now. Try again in a moment.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(view=DivisionLeaderboardView(guild, d, 0, interaction.user.id),
                                                allowed_mentions=NO_PING)


for _d in range(1, 6):
    make_division_lb_command(_d)


# ----------------------------------------------------------------------------
