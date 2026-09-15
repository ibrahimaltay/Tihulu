import os
from urllib.parse import parse_qs, urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from music import (
    ExtractionError,
    MusicError,
    MusicManager,
    Track,
    resolve_video,
    search_youtube,
)


class KabileKahyasi(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.music = MusicManager()

    async def setup_hook(self) -> None:
        await self.tree.sync()

    async def on_ready(self) -> None:
        if self.user is not None:
            print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = KabileKahyasi()


def voice_channel_for(interaction: discord.Interaction) -> discord.abc.Connectable:
    if not isinstance(interaction.user, discord.Member):
        raise MusicError("This command can only be used in a server.")
    voice = interaction.user.voice
    if voice is None or voice.channel is None:
        raise MusicError("Join a voice channel first.")
    return voice.channel


def controllable_player(interaction: discord.Interaction):
    assert interaction.guild_id is not None
    player = bot.music.get(interaction.guild_id)
    if (
        player is None
        or player.voice_client is None
        or not player.voice_client.is_connected()
    ):
        raise MusicError("I am not connected to a voice channel.")

    member_channel = voice_channel_for(interaction)
    if player.voice_client.channel.id != member_channel.id:
        raise MusicError("Join my voice channel to control playback.")
    return player


def is_youtube_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    hostname = (parsed.hostname or "").lower()
    return hostname == "youtu.be" or hostname.endswith(".youtube.com")


def is_playlist_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return "list" in parse_qs(parsed.query)


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "Duration unknown"
    minutes, remaining_seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


class SearchResultSelect(discord.ui.Select):
    def __init__(self, tracks: list[Track]) -> None:
        self.tracks = tracks
        options = [
            discord.SelectOption(
                label=track.title[:100],
                description=format_duration(track.duration)[:100],
                value=str(index),
            )
            for index, track in enumerate(tracks)
        ]
        super().__init__(
            placeholder="Choose a track",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, SearchResultView):
            return

        try:
            voice_channel = voice_channel_for(interaction)
        except MusicError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        await interaction.response.defer()
        assert interaction.guild_id is not None
        track = self.tracks[int(self.values[0])]
        player = view.bot.music.get_or_create(interaction.guild_id)
        try:
            started = await player.enqueue(
                track, voice_channel, interaction.channel
            )
        except MusicError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            await interaction.followup.send(
                "I could not start that track. Check FFmpeg and try again.",
                ephemeral=True,
            )
            return

        for child in view.children:
            child.disabled = True
        view.stop()
        await interaction.edit_original_response(
            content=f"Selected **{discord.utils.escape_markdown(track.title)}**.",
            view=view,
        )
        action = "Now playing" if started else "Queued"
        await interaction.followup.send(
            f"{action}: **{discord.utils.escape_markdown(track.title)}**"
        )


class SearchResultView(discord.ui.View):
    def __init__(
        self, bot_instance: KabileKahyasi, requester_id: int, tracks: list[Track]
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot_instance
        self.requester_id = requester_id
        self.message: discord.WebhookMessage | None = None
        self.add_item(SearchResultSelect(tracks))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who searched can choose this track.", ephemeral=True
        )
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="Search expired.", view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="m", description="Search YouTube and add a song to the queue")
@app_commands.describe(search_query="Song or artist to search for")
@app_commands.guild_only()
async def music(interaction: discord.Interaction, search_query: str) -> None:
    voice_channel = voice_channel_for(interaction)
    await interaction.response.defer(ephemeral=True, thinking=True)

    if is_youtube_url(search_query):
        if is_playlist_url(search_query):
            raise ExtractionError("YouTube playlists are not supported yet.")
        track = await resolve_video(search_query.strip(), interaction.user.id)
        assert interaction.guild_id is not None
        player = bot.music.get_or_create(interaction.guild_id)
        started = await player.enqueue(track, voice_channel, interaction.channel)
        action = "Now playing" if started else "Queued"
        await interaction.followup.send(
            f"{action}: **{discord.utils.escape_markdown(track.title)}**",
            ephemeral=False,
        )
        return

    tracks = await search_youtube(search_query, interaction.user.id)
    view = SearchResultView(bot, interaction.user.id, tracks)
    message = await interaction.followup.send(
        "Choose a track:", view=view, ephemeral=True, wait=True
    )
    view.message = message


@bot.tree.command(name="stop", description="Stop music and leave the voice channel")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    controllable_player(interaction)
    assert interaction.guild_id is not None
    await interaction.response.defer(ephemeral=True)
    await bot.music.stop(interaction.guild_id)
    await interaction.followup.send("Stopped playback and disconnected.")


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction) -> None:
    player = controllable_player(interaction)
    skipped = await player.skip()
    if skipped is None:
        await interaction.response.send_message(
            "There is nothing playing.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"Skipped **{discord.utils.escape_markdown(skipped.title)}**."
    )


@bot.tree.command(name="loop", description="Toggle playlist looping")
@app_commands.guild_only()
async def loop(interaction: discord.Interaction) -> None:
    player = controllable_player(interaction)
    enabled = await player.toggle_loop()
    state = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Playlist loop {state}.")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    cause = error.original if isinstance(error, app_commands.CommandInvokeError) else error
    message = str(cause) if isinstance(cause, MusicError) else "Something went wrong."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)


if __name__ == "__main__":
    main()
