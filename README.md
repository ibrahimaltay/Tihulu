# Tihulu

A small Discord music bot that streams audio from YouTube into a voice channel.

## Commands

- `/m <search_query>`: searches YouTube and shows five results in a private dropdown. A direct YouTube video URL is queued immediately.
- `/skip`: skips the current track and starts the next queued track.
- `/loop`: toggles looping for the full current playlist.
- `/stop`: clears the playlist, disables looping, and disconnects the bot.

Music state is separate for each server. Playback begins when the first track is queued, and tracks added later play in FIFO order. The bot stays in the voice channel after a queue ends until `/stop` is used.

## Tihulu

Place your audio clip at `assets/tihulu.mp3`. On each eligible song playback, Tihulu has an independent 5% chance to appear at the song's midpoint. The music continues advancing while listeners hear one second of silence followed by the clip, then the later point in the song becomes audible again.

Tracks with an unknown duration, or tracks whose second half is too short for the complete effect, are not interrupted. If the file is missing or cannot be decoded, the bot logs a warning and normal music playback continues.

## Setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications?new_application=true). A bot user is enabled by default for new applications.
2. Open the application's **Bot** page, select **Reset Token**, and copy the generated token into `.env` as `DISCORD_TOKEN`. Discord will not show the token again unless you regenerate it.
3. On **Installation**, configure **Guild Install** with the `applications.commands` and `bot` scopes. Select **View Channels**, **Send Messages**, **Connect**, and **Speak**, then use the generated install link to add it to your server.
4. Install Python 3.10 or newer, FFmpeg (including `ffprobe`), and a JavaScript runtime for yt-dlp. Deno is yt-dlp's recommended runtime; Node.js is also supported.
5. Install the Python dependencies:

	```sh
	python3 -m venv .venv
	source .venv/bin/activate
	python3 -m pip install -r requirements.txt
	```

6. Start the bot:

	```sh
	python3 bot.py
	```

`ffmpeg`, `ffprobe`, and either `deno` or `node` must be available on `PATH` when the bot runs.

## Limitations

- YouTube playlists and live or upcoming streams are not supported.
- Queue and loop state are stored in memory and are lost when the bot restarts.
- Search menus expire after five minutes and can only be used by the member who opened them.
