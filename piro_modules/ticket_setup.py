# SETUP (/setup + when the bot joins a server)
# ----------------------------------------------------------------------------
def can_setup(user) -> bool:
    return user.id == OWNER_ID or (isinstance(user, discord.Member) and user.guild_permissions.manage_guild)


def setup_embed(guild: discord.Guild) -> discord.Embed:
    def show(key: str) -> str:
        cid = cfg_get(guild.id, key) or ENV_DEFAULTS[key]
        return f"<#{cid}>" if guild.get_channel(cid) else "not set"
    text = ("**Auto build** creates channels for MVP results and detailed logs.\n"
            f"The fixed leaderboard is in {show('leaderboard')} and refreshes after each MVP log. `/leaderboard` opens a private copy.\n\n"
            f"MVP source: {show('source')}\nMVP results: {show('results')}\nDivision logs: {show('log')}\n"
            f"Ticket category: {show('category')}")
    return make_embed(f"{BOT_NAME} setup", text)


async def make_text(guild: discord.Guild, category: discord.CategoryChannel, name: str, topic: str, read_only: bool):
    for existing in category.text_channels:
        if existing.name == name:
            return existing
    overwrites = {guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                        attach_files=True, read_message_history=True,
                                                        use_external_emojis=True)}
    if read_only:
        overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
    return await guild.create_text_channel(name, category=category, topic=topic, overwrites=overwrites,
                                           reason=f"{BOT_NAME} auto build")


async def auto_build(guild: discord.Guild) -> str:
    category = guild.get_channel(SETUP_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        category = await guild.create_category("URA | DIV", reason=f"{BOT_NAME} auto build")
    source = await make_text(guild, category, "raid-mvps", "Raid MVP logs from the MVP bot are posted here.", False)
    logs = await make_text(guild, category, "division-logs", "One post per MVP log.", True)
    results = await make_text(guild, category, "mvp-results", "One Division MVP result card per raid.", True)
    tickets = guild.get_channel(cfg_get(guild.id, "category") or ENV_DEFAULTS["category"])
    if not isinstance(tickets, discord.CategoryChannel):
        tickets = await guild.create_category("Tickets", reason=f"{BOT_NAME} auto build")
    for key, ch in (("source", source), ("log", logs), ("results", results), ("category", tickets)):
        cfg_set(guild.id, key, ch.id)
    apply_config(guild.id)
    await refresh_leaderboard(guild)
    await live_sync_now(guild)
    return (f"Built under **{category.name}**:\nMVP source: {source.mention}\nMVP results: {results.mention}\nDivision logs: {logs.mention}\n"
            f"Ticket category: **{tickets.name}**  (live cards are posted inside each ticket)\n"
            f"Point your MVP bot at {source.mention}.")


class SetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Auto build", emoji="\U0001F6E0", style=discord.ButtonStyle.success, custom_id="urdiv:setup_auto")
    async def auto_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await auto_build(interaction.guild)
            await interaction.followup.send(embed=make_embed("Auto build done", text, 0x57F287), ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(embed=make_embed(
                "Auto build failed", "I need the **Manage Channels** permission (and Manage Roles is not needed).", 0xED4245),
                ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=make_embed("Auto build failed", f"`{exc!r}`", 0xED4245), ephemeral=True)

    @discord.ui.button(label="Choose channels", emoji="\U0001F4CC", style=discord.ButtonStyle.primary, custom_id="urdiv:setup_choose")
    async def choose_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=make_embed("Choose your channels", "Pick a channel in each menu. It is saved and used right away."),
            view=ChooseView(), ephemeral=True)


PICKS = [
    ("source", "MVP source channel (where the MVP bot posts)", [discord.ChannelType.text]),
    ("results", "MVP results channel", [discord.ChannelType.text]),
    ("log", "Division logs channel", [discord.ChannelType.text]),
    ("category", "Ticket / backup category", [discord.ChannelType.category]),
]


class PickSelect(discord.ui.ChannelSelect):
    def __init__(self, key: str, label: str, types: list):
        super().__init__(placeholder=label, channel_types=types, min_values=1, max_values=1)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not can_setup(interaction.user):
            await interaction.response.send_message("Only the owner or someone with Manage Server can do this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = guild.get_channel(self.values[0].id)
        if channel is None:
            await interaction.followup.send("I cannot see that channel.", ephemeral=True)
            return
        if isinstance(channel, discord.TextChannel):
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.read_message_history):
                await interaction.followup.send(embed=make_embed(
                    "Missing permissions", f"I need View, Send and Read History in {channel.mention}.", 0xED4245), ephemeral=True)
                return
        cfg_set(guild.id, self.key, channel.id)
        apply_config(guild.id)
        notes = {
            "source": f"MVP logs will be read from {channel.mention}.",
            "results": f"The per-raid MVP result cards will be posted in {channel.mention}.",
            "log": f"Division MVP logs will be posted in {channel.mention}.",
            "category": f"Every new channel in **{channel.name}** is now tracked as a backup/ticket.",
        }
        if self.key == "leaderboard":
            await refresh_leaderboard(guild)
        elif self.key in ("live", "category"):
            await live_sync_now(guild)
        await interaction.followup.send(embed=make_embed("Saved", notes[self.key], 0x57F287), ephemeral=True)


class ChooseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        for key, label, types in PICKS:
            self.add_item(PickSelect(key, label, types))


# ----------------------------------------------------------------------------
