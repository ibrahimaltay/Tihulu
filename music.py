import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import discord
import yt_dlp
from yt_dlp.utils import DownloadError

LOGGER = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"
YT_DLP_RUNTIME_OPTIONS: dict[str, Any] = {
    "js_runtimes": {"deno": {}, "node": {}}
}


class MusicError(Exception):
    pass


class ExtractionError(MusicError):
    pass


class VoiceChannelError(MusicError):
    pass


@dataclass(frozen=True, slots=True)
class Track:
    title: str
    webpage_url: str
    duration: int | None
    requester_id: int


def _normalise_track(info: dict[str, Any], requester_id: int) -> Track:
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        raise ExtractionError("Playlists are not supported yet.")

    live_status = info.get("live_status")
    if info.get("is_live") or live_status in {"is_live", "is_upcoming"}:
        raise ExtractionError("Live and upcoming streams are not supported.")

    title = info.get("title")
    webpage_url = info.get("webpage_url") or info.get("url")
    if not isinstance(title, str) or not isinstance(webpage_url, str):
        raise ExtractionError("YouTube did not return a playable video.")

    duration = info.get("duration")
    return Track(
        title=title,
        webpage_url=webpage_url,
        duration=duration if isinstance(duration, int) else None,
        requester_id=requester_id,
    )


def _extract_info(target: str, options: dict[str, Any]) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
            result = ydl.extract_info(target, download=False)
    except DownloadError as error:
        raise ExtractionError("YouTube could not load that video.") from error

    if not isinstance(result, dict):
        raise ExtractionError("YouTube returned an invalid response.")
    return cast(dict[str, Any], result)


async def search_youtube(
    query: str, requester_id: int, limit: int = 5
) -> list[Track]:
    options = {
        **YT_DLP_RUNTIME_OPTIONS,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    result = await asyncio.to_thread(
        _extract_info, f"ytsearch{limit}:{query}", options
    )
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ExtractionError("No YouTube results were found.")

    tracks: list[Track] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            tracks.append(_normalise_track(entry, requester_id))
        except ExtractionError:
            continue

    if not tracks:
        raise ExtractionError("No playable YouTube results were found.")
    return tracks


async def resolve_video(url: str, requester_id: int) -> Track:
    options = {
        **YT_DLP_RUNTIME_OPTIONS,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    info = await asyncio.to_thread(_extract_info, url, options)
    return _normalise_track(info, requester_id)


async def resolve_stream(track: Track) -> str:
    options = {
        **YT_DLP_RUNTIME_OPTIONS,
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    info = await asyncio.to_thread(_extract_info, track.webpage_url, options)
    stream_url = info.get("url")
    if not isinstance(stream_url, str):
        raise ExtractionError(f"No audio stream was found for {track.title}.")
    return stream_url


StreamResolver = Callable[[Track], Awaitable[str]]
SourceFactory = Callable[[str], discord.AudioSource]


def create_audio_source(stream_url: str) -> discord.AudioSource:
    return discord.FFmpegPCMAudio(
        stream_url,
        before_options=FFMPEG_BEFORE_OPTIONS,
        options=FFMPEG_OPTIONS,
    )


class GuildPlayer:
    def __init__(
        self,
        guild_id: int,
        *,
        stream_resolver: StreamResolver = resolve_stream,
        source_factory: SourceFactory = create_audio_source,
    ) -> None:
        self.guild_id = guild_id
        self.playlist: list[Track] = []
        self.cursor = 0
        self.current: Track | None = None
        self.loop_enabled = False
        self.voice_client: Any | None = None
        self.text_channel: Any | None = None
        self._stream_resolver = stream_resolver
        self._source_factory = source_factory
        self._lock = asyncio.Lock()
        self._generation = 0

    @property
    def is_playing(self) -> bool:
        return self.current is not None

    async def enqueue(
        self, track: Track, voice_channel: Any, text_channel: Any
    ) -> bool:
        async with self._lock:
            await self._ensure_voice_client(voice_channel)
            self.text_channel = text_channel
            self.playlist.append(track)
            started = self.current is None
            if started:
                await self._play_from_cursor()
            return started

    async def skip(self) -> Track | None:
        async with self._lock:
            skipped = self.current
            if skipped is not None and self.voice_client is not None:
                self.voice_client.stop()
            return skipped

    async def toggle_loop(self) -> bool:
        async with self._lock:
            if not self.playlist:
                raise MusicError("There is no playlist to loop.")
            self.loop_enabled = not self.loop_enabled
            if (
                self.loop_enabled
                and self.current is None
                and self.cursor >= len(self.playlist)
                and self.voice_client is not None
            ):
                self.cursor = 0
                await self._play_from_cursor()
            return self.loop_enabled

    async def stop(self) -> None:
        async with self._lock:
            self._generation += 1
            voice_client = self.voice_client
            self.voice_client = None
            self.current = None
            self.playlist.clear()
            self.cursor = 0
            self.loop_enabled = False

        if voice_client is not None:
            await voice_client.disconnect(force=True)

    async def _ensure_voice_client(self, voice_channel: Any) -> None:
        if self.voice_client is None or not self.voice_client.is_connected():
            self.voice_client = await voice_channel.connect(self_deaf=True)
            return

        if self.voice_client.channel.id != voice_channel.id:
            raise VoiceChannelError("I am already playing in another voice channel.")

    async def _play_from_cursor(self) -> None:
        attempts = 0
        while self.playlist and attempts < len(self.playlist):
            if self.cursor >= len(self.playlist):
                if not self.loop_enabled:
                    self.current = None
                    return
                self.cursor = 0

            track = self.playlist[self.cursor]
            try:
                stream_url = await self._stream_resolver(track)
                source = self._source_factory(stream_url)
            except Exception as error:
                LOGGER.exception("Could not prepare %s", track.title)
                await self._notify_failure(track)
                self.cursor += 1
                attempts += 1
                continue

            self.current = track
            self._generation += 1
            generation = self._generation
            event_loop = asyncio.get_running_loop()

            def after_playback(error: Exception | None) -> None:
                future = asyncio.run_coroutine_threadsafe(
                    self._after_track(generation, error), event_loop
                )
                future.add_done_callback(self._log_callback_failure)

            assert self.voice_client is not None
            try:
                self.voice_client.play(source, after=after_playback)
            except Exception:
                LOGGER.exception("Could not start %s", track.title)
                self.current = None
                await self._notify_failure(track)
                self.cursor += 1
                attempts += 1
                continue
            return

        self.current = None

    async def _after_track(
        self, generation: int, error: Exception | None
    ) -> None:
        async with self._lock:
            if generation != self._generation or self.current is None:
                return
            if error is not None:
                LOGGER.error("Audio playback failed", exc_info=error)
                await self._notify_failure(self.current)
            self.current = None
            self.cursor += 1
            await self._play_from_cursor()

    async def _notify_failure(self, track: Track) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(
                f"Could not play **{discord.utils.escape_markdown(track.title)}**; skipping."
            )
        except discord.HTTPException:
            LOGGER.warning("Could not send playback failure message")

    @staticmethod
    def _log_callback_failure(future: Any) -> None:
        try:
            future.result()
        except Exception:
            LOGGER.exception("Playback completion callback failed")


class MusicManager:
    def __init__(self) -> None:
        self.players: dict[int, GuildPlayer] = {}

    def get(self, guild_id: int) -> GuildPlayer | None:
        return self.players.get(guild_id)

    def get_or_create(self, guild_id: int) -> GuildPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = GuildPlayer(guild_id)
            self.players[guild_id] = player
        return player

    async def stop(self, guild_id: int) -> bool:
        player = self.players.pop(guild_id, None)
        if player is None:
            return False
        await player.stop()
        return True