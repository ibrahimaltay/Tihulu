import asyncio
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from music import (
    GuildPlayer,
    TIHULU_SILENCE_FRAMES,
    TihuluAudioSource,
    Track,
    load_tihulu_frames,
)


def track(title: str) -> Track:
    return Track(
        title=title,
        webpage_url=f"https://www.youtube.com/watch?v={title}",
        duration=60,
        requester_id=1,
    )


class FakeVoiceClient:
    def __init__(self) -> None:
        self.channel = type("VoiceChannel", (), {"id": 10})()
        self.callbacks = []
        self.sources = []
        self.connected = True
        self.disconnect = AsyncMock()

    def is_connected(self) -> bool:
        return self.connected

    def play(self, source, *, after) -> None:
        self.sources.append(source)
        self.callbacks.append(after)

    def stop(self) -> None:
        self.finish()

    def finish(self, error=None, index: int = -1) -> None:
        callback = self.callbacks[index]
        thread = threading.Thread(target=callback, args=(error,))
        thread.start()
        thread.join()


class FakeVoiceChannel:
    id = 10

    def __init__(self, voice_client: FakeVoiceClient) -> None:
        self.voice_client = voice_client

    async def connect(self, *, self_deaf: bool) -> FakeVoiceClient:
        return self.voice_client


class FakeAudioSource:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = iter(frames)
        self.read_count = 0
        self.cleanup_count = 0

    def read(self) -> bytes:
        self.read_count += 1
        return next(self.frames, b"")

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self.cleanup_count += 1


class TihuluAudioSourceTests(unittest.TestCase):
    def test_substitution_consumes_song_and_resumes_at_later_frame(self) -> None:
        quote_frames = (b"quote-one", b"quote-two")
        total_frames = 2 + TIHULU_SILENCE_FRAMES + len(quote_frames) + 1
        song_frames = [index.to_bytes(4) for index in range(total_frames)]
        song = FakeAudioSource(song_frames)
        source = TihuluAudioSource(song, quote_frames, midpoint_frame=2)

        output = [source.read() for _ in range(total_frames)]

        self.assertEqual(output[:2], song_frames[:2])
        self.assertEqual(
            output[2 : 2 + TIHULU_SILENCE_FRAMES],
            [b"\0" * 4] * TIHULU_SILENCE_FRAMES,
        )
        quote_start = 2 + TIHULU_SILENCE_FRAMES
        self.assertEqual(output[quote_start : quote_start + 2], list(quote_frames))
        self.assertEqual(output[-1], song_frames[-1])
        self.assertEqual(song.read_count, total_frames)

    def test_underlying_eof_ends_effect_immediately(self) -> None:
        song = FakeAudioSource([b"song"])
        source = TihuluAudioSource(song, (b"quote",), midpoint_frame=0)

        self.assertEqual(source.read(), b"\0" * 4)
        self.assertEqual(source.read(), b"")

    def test_cleanup_delegates_once(self) -> None:
        song = FakeAudioSource([])
        source = TihuluAudioSource(song, (), midpoint_frame=0)

        source.cleanup()
        source.cleanup()

        self.assertEqual(song.cleanup_count, 1)


class TihuluAssetTests(unittest.TestCase):
    def test_missing_asset_raises_before_starting_ffmpeg(self) -> None:
        with patch("music.discord.FFmpegPCMAudio") as ffmpeg:
            with self.assertRaises(FileNotFoundError):
                load_tihulu_frames(Path("does-not-exist.mp3"))

        ffmpeg.assert_not_called()

    def test_asset_is_loaded_and_cleaned_up(self) -> None:
        source = FakeAudioSource([b"one", b"two"])
        with (
            patch.object(Path, "is_file", return_value=True),
            patch("music.discord.FFmpegPCMAudio", return_value=source),
        ):
            frames = load_tihulu_frames(Path("tihulu.mp3"))

        self.assertEqual(frames, (b"one", b"two"))
        self.assertEqual(source.cleanup_count, 1)


class GuildPlayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.voice_client = FakeVoiceClient()
        self.voice_channel = FakeVoiceChannel(self.voice_client)
        self.text_channel = type(
            "TextChannel", (), {"send": AsyncMock()}
        )()

        async def resolve(item: Track) -> str:
            return f"stream:{item.title}"

        self.player = GuildPlayer(
            1, stream_resolver=resolve, source_factory=lambda url: url
        )

    async def allow_callback(self) -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def enqueue(self, title: str) -> bool:
        return await self.player.enqueue(
            track(title), self.voice_channel, self.text_channel
        )

    async def test_fifo_progression_and_loop_off_exhaustion(self) -> None:
        self.assertTrue(await self.enqueue("one"))
        self.assertFalse(await self.enqueue("two"))
        self.assertEqual(self.player.current, track("one"))

        self.voice_client.finish()
        await self.allow_callback()
        self.assertEqual(self.player.current, track("two"))

        self.voice_client.finish()
        await self.allow_callback()
        self.assertIsNone(self.player.current)
        self.assertEqual(self.player.cursor, 2)

    async def test_loop_cycles_and_includes_new_tracks(self) -> None:
        await self.enqueue("one")
        await self.player.toggle_loop()
        await self.enqueue("two")

        self.voice_client.finish()
        await self.allow_callback()
        self.assertEqual(self.player.current, track("two"))

        self.voice_client.finish()
        await self.allow_callback()
        self.assertEqual(self.player.current, track("one"))

    async def test_enabling_loop_restarts_exhausted_playlist(self) -> None:
        await self.enqueue("one")
        self.voice_client.finish()
        await self.allow_callback()

        self.assertTrue(await self.player.toggle_loop())
        self.assertEqual(self.player.current, track("one"))

    async def test_skip_advances_through_completion_callback(self) -> None:
        await self.enqueue("one")
        await self.enqueue("two")

        skipped = await self.player.skip()
        await self.allow_callback()

        self.assertEqual(skipped, track("one"))
        self.assertEqual(self.player.current, track("two"))
        self.assertEqual(self.player.cursor, 1)

    async def test_stop_rejects_stale_completion_callback(self) -> None:
        await self.enqueue("one")
        old_callback = self.voice_client.callbacks[0]

        await self.player.stop()
        thread = threading.Thread(target=old_callback, args=(None,))
        thread.start()
        thread.join()
        await self.allow_callback()

        self.assertEqual(self.player.playlist, [])
        self.assertIsNone(self.player.current)
        self.assertEqual(self.player.cursor, 0)
        self.voice_client.disconnect.assert_awaited_once_with(force=True)

    async def test_resolution_failure_skips_to_next_track(self) -> None:
        attempts = 0

        async def fail_once(item: Track) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("expired stream")
            return f"stream:{item.title}"

        self.player._stream_resolver = fail_once
        self.player.playlist.extend([track("one"), track("two")])
        self.player.voice_client = self.voice_client
        self.player.text_channel = self.text_channel

        await self.player._play_from_cursor()

        self.assertEqual(self.player.current, track("two"))
        self.text_channel.send.assert_awaited_once()

    async def test_voice_play_failure_skips_to_next_track(self) -> None:
        original_play = self.voice_client.play
        attempts = 0

        def fail_once(source, *, after) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("voice client rejected source")
            original_play(source, after=after)

        self.voice_client.play = fail_once
        self.player.playlist.extend([track("one"), track("two")])
        self.player.voice_client = self.voice_client
        self.player.text_channel = self.text_channel

        await self.player._play_from_cursor()

        self.assertEqual(self.player.current, track("two"))
        self.text_channel.send.assert_awaited_once()

    async def test_tihulu_rolls_again_for_loop_replay(self) -> None:
        rolls = iter((0.04, 0.06))

        async def resolve(item: Track) -> str:
            return f"stream:{item.title}"

        player = GuildPlayer(
            1,
            stream_resolver=resolve,
            source_factory=lambda url: FakeAudioSource([b"song"] * 3000),
            tihulu_frames=(b"quote",),
            random_source=lambda: next(rolls),
        )
        await player.enqueue(track("one"), self.voice_channel, self.text_channel)
        await player.toggle_loop()

        self.assertIsInstance(self.voice_client.sources[0], TihuluAudioSource)
        self.voice_client.finish()
        await self.allow_callback()

        self.assertIsInstance(self.voice_client.sources[1], FakeAudioSource)

    async def test_unknown_duration_does_not_roll_or_wrap(self) -> None:
        roll_count = 0

        def roll() -> float:
            nonlocal roll_count
            roll_count += 1
            return 0.0

        async def resolve(item: Track) -> str:
            return f"stream:{item.title}"

        player = GuildPlayer(
            1,
            stream_resolver=resolve,
            source_factory=lambda url: FakeAudioSource([b"song"]),
            tihulu_frames=(b"quote",),
            random_source=roll,
        )
        unknown = Track("unknown", "https://example.com", None, 1)

        await player.enqueue(unknown, self.voice_channel, self.text_channel)

        self.assertEqual(roll_count, 0)
        self.assertIsInstance(self.voice_client.sources[0], FakeAudioSource)

    async def test_short_track_does_not_roll_or_wrap(self) -> None:
        roll_count = 0

        def roll() -> float:
            nonlocal roll_count
            roll_count += 1
            return 0.0

        async def resolve(item: Track) -> str:
            return f"stream:{item.title}"

        player = GuildPlayer(
            1,
            stream_resolver=resolve,
            source_factory=lambda url: FakeAudioSource([b"song"]),
            tihulu_frames=tuple(b"quote" for _ in range(100)),
            random_source=roll,
        )
        short = Track("short", "https://example.com", 2, 1)

        await player.enqueue(short, self.voice_channel, self.text_channel)

        self.assertEqual(roll_count, 0)
        self.assertIsInstance(self.voice_client.sources[0], FakeAudioSource)


if __name__ == "__main__":
    unittest.main()