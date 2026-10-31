import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from submission_store import SubmissionRejected, SubmissionStore


STATUS_LABELS = {
    "queued": "Waiting in queue",
    "preparing": "Preparing replay",
    "rendering": "Rendering on o!rdr",
    "ready": "Added to the stream queue",
    "failed": "Failed",
    "cancelled": "Cancelled",
}

STATUS_COLORS = {
    "queued": 0xF2C94C,
    "preparing": 0xF2994A,
    "rendering": 0x2D9CDB,
    "ready": 0x27AE60,
    "failed": 0xEB5757,
    "cancelled": 0x828282,
}


class DiscordReplayService:
    def __init__(
        self,
        config: Any,
        submissions: SubmissionStore,
        validate_replay: Callable[[Path], tuple[int, int, str, str]],
    ):
        self.config = config
        self.submissions = submissions
        self.validate_replay = validate_replay
        intents = discord.Intents.none()
        intents.guilds = True
        self.bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
        self.task: asyncio.Task[Any] | None = None
        self.notification_task: asyncio.Task[Any] | None = None
        self.synced = False
        self._register_events()
        self._register_commands()

    def _right_channel(self, interaction: discord.Interaction) -> bool:
        return interaction.guild_id == self.config.discord_guild_id and interaction.channel_id == self.config.discord_channel_id

    async def _reject_wrong_channel(self, interaction: discord.Interaction) -> bool:
        if self._right_channel(interaction):
            return False
        message = f"Use this command in <#{self.config.discord_channel_id}>."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return True

    @staticmethod
    def _is_admin(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(permissions and permissions.manage_messages)

    def _status_label(self, row: dict[str, Any]) -> str:
        status = row["status"]
        if status == "rendering" and self.config.render_backend == "danser":
            return "Rendering locally with danser"
        return STATUS_LABELS.get(status, status.title())

    async def _embed(self, row: dict[str, Any]) -> discord.Embed:
        status = row["status"]
        position = await self.submissions.queue_position(int(row["id"]))
        embed = discord.Embed(
            title=f"Replay submission #{row['id']}",
            description=self._status_label(row),
            color=STATUS_COLORS.get(status, 0x5865F2),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Submitted by", value=f"<@{row['external_user_id']}>", inline=True)
        embed.add_field(name="Mods", value=row["mods_label"], inline=True)
        embed.add_field(name="Skin", value=row["skin"], inline=True)
        embed.add_field(name="File", value=discord.utils.escape_markdown(row["original_name"])[:1024], inline=False)
        if position is not None:
            embed.add_field(name="Queue position", value=f"#{position}", inline=True)
        if row.get("render_id"):
            embed.add_field(name="o!rdr render", value=f"#{row['render_id']}", inline=True)
        if row.get("video_name"):
            embed.add_field(name="Stream video", value=row["video_name"][:1024], inline=False)
        if row.get("error"):
            embed.add_field(name="Error", value=row["error"][:1024], inline=False)
        if self.config.render_backend == "ordr":
            footer = "Submitted replays and generated videos may appear publicly on o!rdr and the stream."
        else:
            footer = "Submitted replays are rendered locally and may appear publicly on the stream."
        embed.set_footer(text=footer)
        return embed

    async def _send_active_queue(self, interaction: discord.Interaction) -> None:
        rows = await self.submissions.list_active(21)
        if not rows:
            await interaction.response.send_message(
                "No replays are waiting, preparing, or rendering.",
                ephemeral=True,
            )
            return

        visible_rows = rows[:20]
        lines = [
            f"**{index}.** `#{row['id']}` <@{row['external_user_id']}> · "
            f"**{row['mods_label']}** · {self._status_label(row)}"
            for index, row in enumerate(visible_rows, start=1)
        ]
        embed = discord.Embed(
            title="Active replay jobs",
            description="\n".join(lines),
            color=0x5865F2,
            timestamp=discord.utils.utcnow(),
        )
        if len(rows) > len(visible_rows):
            embed.set_footer(text="Showing the first 20 active submissions.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    async def _download_attachment(replay: discord.Attachment, destination: Path) -> None:
        """Download an attachment, preferring its original CDN URL.

        Discord's proxy URL occasionally responds with 415 for binary file
        attachments (including .osr files).  The original URL is valid while
        the message exists, so try it first and only use the proxy URL as a
        fallback for transient CDN failures.
        """
        try:
            data = await replay.read(use_cached=False)
        except discord.HTTPException as original_error:
            logging.warning(
                "Could not download Discord attachment from its original URL (%s); trying proxy URL",
                original_error,
            )
            data = await replay.read(use_cached=True)
        destination.write_bytes(data)

    def _register_events(self) -> None:
        @self.bot.event
        async def on_ready() -> None:
            if not self.synced:
                guild = discord.Object(id=self.config.discord_guild_id)
                self.bot.tree.copy_global_to(guild=guild)
                commands_synced = await self.bot.tree.sync(guild=guild)
                self.synced = True
                logging.info("Discord bot connected as %s; synced %d commands", self.bot.user, len(commands_synced))
            if self.notification_task is None or self.notification_task.done():
                self.notification_task = asyncio.create_task(self._notification_loop(), name="discord-notifications")
            await self.bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="the replay queue"))

    def _register_commands(self) -> None:
        @self.bot.tree.command(name="submit", description="Submit an osu!standard replay to the stream queue")
        @app_commands.describe(replay="The .osr replay file")
        async def submit(interaction: discord.Interaction, replay: discord.Attachment) -> None:
            if await self._reject_wrong_channel(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not replay.filename.lower().endswith(".osr"):
                await interaction.followup.send("Only `.osr` replay files are accepted.", ephemeral=True)
                return
            if replay.size > self.config.discord_max_file_bytes:
                limit_mib = self.config.discord_max_file_bytes // (1024 * 1024)
                await interaction.followup.send(f"Replay is too large. Limit: {limit_mib} MiB.", ephemeral=True)
                return
            temporary = self.submissions.temp_dir / f"discord-{uuid.uuid4().hex}.osr.part"
            try:
                await self._download_attachment(replay, temporary)
                if temporary.stat().st_size < 32:
                    raise SubmissionRejected("Replay is empty or truncated.", "invalid-file")
                mode, mods, mods_label, skin = self.validate_replay(temporary)
                if mode != 0:
                    raise SubmissionRejected("Only osu!standard replays are supported.", "unsupported-mode")
                if mods & (1 << 11):
                    raise SubmissionRejected("Autoplay replays are not accepted by o!rdr.", "autoplay")
                row, position = await self.submissions.create(
                    source_path=temporary,
                    platform="discord",
                    external_user_id=str(interaction.user.id),
                    external_user_name=str(interaction.user),
                    guild_id=str(interaction.guild_id),
                    channel_id=str(interaction.channel_id),
                    original_name=Path(replay.filename).name,
                    mode=mode,
                    mods=mods,
                    mods_label=mods_label,
                    skin=skin,
                )
                channel = interaction.channel
                if channel is None or not hasattr(channel, "send"):
                    raise RuntimeError("Discord channel is unavailable")
                message = await channel.send(embed=await self._embed(row))
                await self.submissions.attach_status_message(
                    int(row["id"]),
                    int(interaction.guild_id),
                    int(interaction.channel_id),
                    message.id,
                )
                await interaction.followup.send(
                    f"Replay accepted as **#{row['id']}**, queue position **#{position}**. [Status message]({message.jump_url})",
                    ephemeral=True,
                )
                logging.info(
                    "Discord replay #%d accepted from %s (%s, %s)",
                    row["id"],
                    interaction.user,
                    mods_label,
                    skin,
                )
            except SubmissionRejected as error:
                await interaction.followup.send(f"Replay rejected: {error}", ephemeral=True)
            except (discord.HTTPException, OSError, ValueError, RuntimeError) as error:
                logging.exception("Discord replay submission failed")
                await interaction.followup.send(f"Could not accept replay: {error}", ephemeral=True)
            finally:
                temporary.unlink(missing_ok=True)

        @self.bot.tree.command(name="status", description="Show replays that are waiting, preparing, or rendering")
        async def status(interaction: discord.Interaction) -> None:
            if await self._reject_wrong_channel(interaction):
                return
            await self._send_active_queue(interaction)

        @self.bot.tree.command(name="queue", description="Show the active replay queue (alias of /status)")
        async def queue(interaction: discord.Interaction) -> None:
            if await self._reject_wrong_channel(interaction):
                return
            await self._send_active_queue(interaction)

        @self.bot.tree.command(name="cancel", description="Cancel your waiting replay submission")
        @app_commands.describe(submission_id="Submission number")
        async def cancel(interaction: discord.Interaction, submission_id: int) -> None:
            if await self._reject_wrong_channel(interaction):
                return
            try:
                row = await self.submissions.cancel(
                    submission_id,
                    str(interaction.user.id),
                    self._is_admin(interaction),
                )
                await interaction.response.send_message(
                    f"Submission **#{row['id']}** cancelled.",
                    ephemeral=True,
                )
            except SubmissionRejected as error:
                await interaction.response.send_message(str(error), ephemeral=True)

    async def _notification_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                rows = await self.submissions.pending_notifications()
                for row in rows:
                    try:
                        channel_id = int(row["channel_id"])
                        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        message = await channel.fetch_message(int(row["status_message_id"]))
                        await message.edit(embed=await self._embed(row))
                    except (discord.NotFound, discord.Forbidden):
                        logging.warning("Discord status message for submission #%d is unavailable", row["id"])
                    except discord.HTTPException as error:
                        logging.warning("Could not update Discord submission #%d: %s", row["id"], error)
                        continue
                    await self.submissions.mark_notified(int(row["id"]), row["status"])
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Discord notification loop failed")
            await asyncio.sleep(3)

    async def start(self) -> None:
        await self.bot.start(self.config.discord_token, reconnect=True)

    async def close(self) -> None:
        if self.notification_task:
            self.notification_task.cancel()
            await asyncio.gather(self.notification_task, return_exceptions=True)
        if not self.bot.is_closed():
            await self.bot.close()
