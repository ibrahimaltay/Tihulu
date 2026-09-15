import asyncio
import threading
import unittest
from unittest.mock import AsyncMock

from music import GuildPlayer, Track


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


if __name__ == "__main__":
    unittest.main()