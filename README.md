# Wrapped Together

A Telegram bot for a "guess who picked this song" party game. Everyone
submits songs anonymously, then the group guesses who picked each one as
it plays.

## How it plays

1. Someone sends `/newgame` in the group chat — the bot posts a "Submit
   songs" button.
2. Everyone taps it and sends up to 3 songs to the bot in a private chat.
   Submissions are anonymous — nobody else can see who submitted what.
3. The host sends `/startgame`. Songs are shuffled and round 1 is posted.
4. Everyone taps who they think picked the current song. Votes are secret.
5. The host sends `/next` to reveal the picker and award points:
   - +1 point for everyone who guessed the picker correctly
   - +1 point to the picker for every person they fooled
6. Repeat step 4–5 until all songs are revealed, then final scores are posted.

## Setup

**1. Get a Telegram bot token**

Message [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`,
and follow the prompts. You'll get a token that looks like
`123456:ABC-DEF...`.

**2. Install dependencies**

```
pip install -r requirements.txt
```

**3. Set environment variables**

| Variable | Required? | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` | Yes | The token from BotFather |
| `MAX_SONGS` | No (default 3) | Max songs each player can submit |
| `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` | No | Enables Spotify mode (see below) |
| `DEV_MODE` | No | Enables solo-testing helpers (see below) |

**4. Run it**

```
python bot.py
```

## Spotify mode (optional)

By default the bot runs in **manual mode**: people submit a song name or
link as plain text, and no playlist is auto-created — each round's message
just shows that song's text/link so players can look it up themselves.

To enable **Spotify mode** (song search while submitting, plus an
auto-generated shuffled playlist when `/startgame` runs):

1. Create an app at [developer.spotify.com](https://developer.spotify.com/)
   and add the redirect URI `http://127.0.0.1:8888/callback`.
2. Set `SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET` to that app's
   credentials.
3. Run the bot as normal. On first run it prints a login link — open it,
   log in with the Spotify account that should own the created playlists,
   and approve access. The token is cached in `.spotify_cache` afterward.

No code changes are needed to switch between the two modes — it's
detected automatically from whether those two env vars are set.

## Commands

**In the group:**

| Command | Who | What it does |
|---|---|---|
| `/newgame` | Anyone | Starts a new game and posts the submit button |
| `/players` | Anyone | Shows who's submitted so far |
| `/startgame` | Host | Locks submissions, shuffles songs, posts round 1 |
| `/next` | Host | Reveals the current song's picker, scores the round, posts the next one |
| `/scores` | Anyone | Shows the current leaderboard |
| `/endgame` | Host | Ends the game early and shows final scores |
| `/simulate [count]` | Host, `DEV_MODE` only | Adds fake players + songs for solo testing |

**In a DM with the bot:**

| Command | What it does |
|---|---|
| `/start` | Joins the game linked from the group's submit button |
| *(any text)* | Submits a song (name/link in manual mode, or triggers a Spotify search in Spotify mode) |
| `/mysongs` | Lists the songs you've submitted |
| `/undo` | Removes your most recently submitted song |

## Testing solo (`DEV_MODE`)

The game normally needs 2+ people submitting songs, plus others tapping
vote buttons — hard to exercise alone. Set `DEV_MODE=1` to unlock:

- **`/simulate [count]`** (default 2) — adds fake players (`Test Bot 1`,
  `Test Bot 2`, ...) with placeholder songs already submitted, so you can
  hit the 2-submitter minimum instantly.
- **Auto-voting** — fake players automatically cast a random guess as
  soon as each round is posted, so `/next` has real vote data to score
  even if you're the only real person tapping buttons.
- **Relaxed minimum** — `/startgame` only needs 1 real submitter instead
  of 2.
- **Vote breakdown after `/next`** — a follow-up message lists every
  eligible player (everyone except that round's picker) and who they
  guessed, or `(no vote)` if they haven't voted yet — handy for checking
  the simulated votes landed the way you expected.

Example solo playthrough:

```
/newgame
/simulate 3
/startgame
/next   (repeat until the game ends)
```

Never set `DEV_MODE=1` when running the bot for a real game — it changes
game rules and adds fake players.

## Notes

- Game state is stored in `games.json` next to `bot.py`, and the Spotify
  login cache in `.spotify_cache`. Both are gitignored — they contain
  player names/IDs (and, for `.spotify_cache`, an auth token), so don't
  commit or share them.
- Keep `TELEGRAM_TOKEN` and the Spotify credentials out of source control;
  set them as environment variables instead (or a `.env` file that's also
  gitignored).
