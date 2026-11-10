# SLASH COMMANDS: TRUST + SETUP (owner only)
# ----------------------------------------------------------------------------
trust_group = app_commands.Group(name="trust", description="Choose who may use the bot's commands", guild_only=True)


@trust_group.command(name="add", description="Allow a user to use the bot's commands")
@app_commands.describe(user="User to trust")
async def trust_add(interaction: discord.Interaction, user: discord.User):
    if user.bot or user.id == OWNER_ID:
        await interaction.response.send_message(embed=make_embed(
            "Trust", "That user does not need to be trusted.", 0xED4245), ephemeral=True)
        return
    db.execute("INSERT OR IGNORE INTO trusted(user_id) VALUES(?)", (user.id,))
    db.commit()
    await interaction.response.send_message(embed=make_embed(
        "Trust added", f"<@{user.id}> can now use the bot's commands.", 0x57F287),
        ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="remove", description="Stop a user from using the bot's commands")
@app_commands.describe(user="User to remove")
async def trust_remove(interaction: discord.Interaction, user: discord.User):
    cur = db.execute("DELETE FROM trusted WHERE user_id=?", (user.id,))
    db.commit()
    text = f"<@{user.id}> can no longer use the bot's commands." if cur.rowcount else f"<@{user.id}> was not trusted."
    await interaction.response.send_message(embed=make_embed(
        "Trust removed" if cur.rowcount else "Trust", text, 0x57F287 if cur.rowcount else 0xED4245),
        ephemeral=True, allowed_mentions=NO_PING)


@trust_group.command(name="list", description="Show every trusted user")
async def trust_list(interaction: discord.Interaction):
    ids = [r["user_id"] for r in db.execute("SELECT user_id FROM trusted")]
    text = "\n".join(f"<@{i}>" for i in ids) if ids else "No trusted users yet."
    await interaction.response.send_message(embed=make_embed("Trusted users", text),
                                            ephemeral=True, allowed_mentions=NO_PING)


bot.tree.add_command(trust_group)


@bot.tree.command(name="setup", description="Owner only: auto build the channels or choose your own")
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction):
    await interaction.response.send_message(embed=setup_embed(interaction.guild), view=SetupView(), ephemeral=True)


# ----------------------------------------------------------------------------
