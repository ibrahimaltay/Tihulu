# Kabile Kahyasi

This is a discord bot designed to handle specific requests.

## Functionalities

### Play Music
Music playing is done in a queue system.
When a song finishes playing, the next song starts.

`/m <search_query>` -> search YouTube and return five results in a private dropdown. Only the requester can use the dropdown, and it expires after five minutes. Direct YouTube video URLs are queued immediately.

Selecting a result adds that song to the playlist. Playlists and live or upcoming streams are rejected.

if no song is playing, it should start playing.
if a song is already playing, it should be added to the queue.

`/stop` -> stop playing music, clear the playlist and loop state, and leave the voice channel

`/skip` -> to skip the current song

`/loop` -> toggle loop on / off. if loop is on, it should play the whole playlist and then start over again. Newly queued songs join the current cycle.

Playback and queue state are isolated per server and are not persisted across restarts. Members must be in the bot's voice channel to use playback controls.

### Tihulu

The local `assets/tihulu.mp3` clip has a 5% chance to play at the midpoint of each eligible song playback, including loop replays. The song keeps advancing while one second of silence and the Tihulu clip replace its audible PCM frames, then playback becomes audible again at the later song position.

Songs with unknown duration or insufficient time after the midpoint are excluded. A missing or invalid clip disables only Tihulu and does not prevent music playback.