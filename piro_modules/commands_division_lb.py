# SLASH COMMANDS: TOP RAIDERS PER DIVISION (/leaderboarddivision1 ... /leaderboarddivision5)
# ----------------------------------------------------------------------------
# READ-ONLY: these commands only read the saved `live_tickets` helper lists and `raid_results` MVP scores.
# They never write, reset or change any saved data.
#
# Anyone can use them, in any server or DM (they are GLOBAL commands, see main.py / events.py).
# The numbers always come from the URA server. Discord slash names must be lowercase, so
# /LeaderboardDivision1 is typed /leaderboarddivision1.
DIV_LB_COMMANDS = {f"leaderboarddivision{d}" for d in range(1, 6)}
DIV_LB_PAGE_SIZE = 10
DIV_LB_MEDALS = {1: "\U0001F947", 2: "\U0001F948", 3: "\U0001F949"}


def div_lb_collect(guild: discord.Guild, d: int) -> tuple[list[tuple[int, int]], dict[int, str]]:
    """(ranking, names): every member of ONE division with how many backups they helped in."""
    role_id = DIVISION_ROLES.get(d)
    counts: dict[int, int] = {}
    names: dict[int, str] = {}
    for row in db.execute("SELECT helpers_json FROM live_tickets WHERE guild_id=?", (guild.id,)):
        try:
            helpers = json.loads(row["helpers_json"] or "[]")
        except (TypeError, ValueError):
            continue
        seen: set[int] = set()
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
            counts[user_id] = counts.get(user_id, 0) + 1
            names[user_id] = name
    return sorted(counts.items(), key=lambda item: (-item[1], item[0])), names


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

        lead = leaders_of(division_totals(guild.id)[0])
        card = discord.ui.Container(accent_colour=DIVISION_COLORS.get(d, BRAND_COLOR))
        card.add_item(discord.ui.TextDisplay(
            f"# {DIV_EMOJI.get(d, '')} DIVISION {d} LEADERBOARD\n**{lead_heading(lead)}**"))
        card.add_item(discord.ui.Separator())

        lines = []
        for offset, (user_id, helps) in enumerate(chunk):
            rank = start + offset + 1
            marker = DIV_LB_MEDALS.get(rank, f"**{rank}.**")
            lines.append(f"{marker} `@{names.get(user_id, user_id)}`  \u00b7  `{helps}` helps")
        card.add_item(discord.ui.TextDisplay("\n".join(lines) if lines else "Nobody from this division has helped yet."))

        if pages > 1:
            card.add_item(discord.ui.Separator())
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
