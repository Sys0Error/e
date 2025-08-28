import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------- Config ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Superuser & punish role (replace punish role ID)
SUPERUSER_ID = 1164793629374697493
PUNISH_ROLE_ID = 123456789012345678  # <-- replace with your punishment role ID

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

AUDIT_WINDOW = timedelta(seconds=30)
PUNISH_DURATION = timedelta(minutes=30)

# Track punished users with expiration times
punished_users: dict[int, datetime] = {}  # {user_id: expire_time}


# ---------- Helpers ----------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _recent(entry: discord.AuditLogEntry) -> bool:
    return (_now_utc() - entry.created_at) <= AUDIT_WINDOW


async def _add_punish_role(member: discord.Member, guild: discord.Guild) -> Optional[str]:
    """Assign punishment role safely & schedule removal."""
    role = guild.get_role(PUNISH_ROLE_ID)
    if role is None:
        return "Punish role not found."

    if member.bot:
        return "Actor is a bot; skipping."
    if guild.owner_id == member.id:
        return "Actor is the guild owner; skipping."
    if member.id == SUPERUSER_ID:   # ✅ Skip superuser
        return "Actor is the superuser; skipping."

    me = guild.me or (await guild.fetch_member(bot.user.id))
    if me.top_role <= role:
        return "My top role is not above the punish role."

    try:
        await member.add_roles(role, reason="Anti-nuke protection")
        punished_users[member.id] = _now_utc() + PUNISH_DURATION
        return None
    except discord.Forbidden:
        return "Missing permissions to assign role."
    except discord.HTTPException:
        return "HTTP error while assigning role."


async def _remove_punish_role(member: discord.Member, guild: discord.Guild) -> Optional[str]:
    """Remove punishment role if user has it."""
    role = guild.get_role(PUNISH_ROLE_ID)
    if role is None:
        return "Punish role not found."

    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Punish expired/unpunished manually")
        punished_users.pop(member.id, None)
        return None
    except discord.Forbidden:
        return "Missing permissions to remove role."
    except discord.HTTPException:
        return "HTTP error while removing role."


async def _get_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int) -> Optional[discord.Member]:
    """Find the user who performed the action in audit logs."""
    async for entry in guild.audit_logs(limit=6, action=action):
        if getattr(entry.target, "id", None) == target_id and _recent(entry):
            user = entry.user
            if isinstance(user, discord.Member):
                return user
            elif isinstance(user, discord.User):
                try:
                    return await guild.fetch_member(user.id)
                except discord.NotFound:
                    return None
    return None


# ---------- Tasks ----------
@tasks.loop(minutes=1)
async def punish_cleanup():
    """Background task to auto-remove punish role after expiration."""
    now = _now_utc()
    expired = [uid for uid, exp in punished_users.items() if exp <= now]

    for uid in expired:
        punished_users.pop(uid, None)
        for guild in bot.guilds:
            member = guild.get_member(uid)
            if member:
                role = guild.get_role(PUNISH_ROLE_ID)
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Punish duration expired")
                        print(f"[anti-nuke] Auto-removed punish role from {member}")
                    except Exception as e:
                        print(f"[anti-nuke] Failed to auto-remove punish role: {e}")


# ---------- Events ----------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    punish_cleanup.start()


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    actor = await _get_actor(channel.guild, discord.AuditLogAction.channel_create, channel.id)
    if actor:
        err = await _add_punish_role(actor, channel.guild)
        if err:
            print(f"[anti-nuke] Could not punish {actor}: {err}")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    actor = await _get_actor(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
    if actor:
        err = await _add_punish_role(actor, channel.guild)
        if err:
            print(f"[anti-nuke] Could not punish {actor}: {err}")


@bot.event
async def on_guild_role_create(role: discord.Role):
    actor = await _get_actor(role.guild, discord.AuditLogAction.role_create, role.id)
    if actor:
        err = await _add_punish_role(actor, role.guild)
        if err:
            print(f"[anti-nuke] Could not punish {actor}: {err}")


@bot.event
async def on_guild_role_delete(role: discord.Role):
    actor = await _get_actor(role.guild, discord.AuditLogAction.role_delete, role.id)
    if actor:
        err = await _add_punish_role(actor, role.guild)
        if err:
            print(f"[anti-nuke] Could not punish {actor}: {err}")


# ---------- Commands ----------
@bot.tree.command(name="ping", description="Check if bot is alive (superuser only).")
async def ping(interaction: discord.Interaction):
    if interaction.user.id != SUPERUSER_ID:
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return
    await interaction.response.send_message("✅ Pong! Bot is running.", ephemeral=True)


@bot.tree.command(name="unpunish", description="Remove the punish role from a user (superuser only).")
async def unpunish(interaction: discord.Interaction, member: discord.Member):
    if interaction.user.id != SUPERUSER_ID:
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return

    err = await _remove_punish_role(member, interaction.guild)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
    else:
        await interaction.response.send_message(f"✅ Removed punish role from {member.mention}", ephemeral=True)


@bot.tree.command(name="lockdown", description="Lock all channels (superuser only).")
async def lockdown(interaction: discord.Interaction):
    if interaction.user.id != SUPERUSER_ID:
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return

    for channel in interaction.guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = False
            try:
                await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="Lockdown enabled")
            except Exception:
                pass

    await interaction.response.send_message("🔒 Lockdown enabled: all channels blocked.", ephemeral=True)


@bot.tree.command(name="unlockdown", description="Unlock all channels (superuser only).")
async def unlockdown(interaction: discord.Interaction):
    if interaction.user.id != SUPERUSER_ID:
        await interaction.response.send_message("⛔ You are not authorized.", ephemeral=True)
        return

    for channel in interaction.guild.channels:
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            overwrite = channel.overwrites_for(interaction.guild.default_role)
            overwrite.send_messages = None
            try:
                await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason="Lockdown disabled")
            except Exception:
                pass

    await interaction.response.send_message("🔓 Lockdown lifted: channels restored.", ephemeral=True)


# ---------- Run ----------
if not TOKEN:
    raise RuntimeError("⚠️ Set DISCORD_TOKEN in your .env file.")

bot.run(TOKEN)
