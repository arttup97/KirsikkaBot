"""
Wrapped Together - a Telegram bot for guessing who picked which song.

Flow:
  1. In your group:   /newgame          -> bot posts a "Submit songs" button
  2. In private DM:   send links/names  -> songs are stored anonymously
  3. In your group:   /startgame (host) -> shuffled songs, round 1 posted
  4. Everyone taps who they think picked each song (votes are secret)
  5. Host:            /next             -> reveals the picker, awards points, next song

Spotify mode is OPTIONAL. If SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET are
not set, the bot runs in "manual" mode: people just type a song name or
paste a link, no Spotify app/account is needed, and no auto-generated
playlist is created. Set those two env vars whenever you're able to create
a Spotify app (see the setup notes) and the bot will automatically go back
to searching Spotify and building a real shuffled playlist - no code
changes needed.
"""
import json
import os
import random
import re
from collections import Counter

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["TELEGRAM_TOKEN"]
MAX_SONGS = int(os.environ.get("MAX_SONGS", 3))
DATA_FILE = "games.json"

# Set DEV_MODE=1 to unlock solo-testing helpers: /simulate seeds fake
# players+songs into the current game, fake players auto-vote each round,
# and the "need 2+ submitters" rule relaxes to 1. Never set this in prod.
DEV_MODE = os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes")

# Spotify integration is optional - see the module docstring. When the
# client id/secret aren't set we skip importing/using spotipy entirely so
# the bot runs with zero Spotify setup.
SPOTIFY_ENABLED = bool(os.environ.get("SPOTIPY_CLIENT_ID")) and bool(os.environ.get("SPOTIPY_CLIENT_SECRET"))

sp = None
if SPOTIFY_ENABLED:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    # Reads SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET from the environment.
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope="playlist-modify-public playlist-modify-private",
        redirect_uri="http://127.0.0.1:8888/callback",
        open_browser=False,
        cache_path=".spotify_cache",
    ))

LINK_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?track/(\w+)")
MEDALS = ["🥇", "🥈", "🥉"]


# ---------- storage ----------

def load():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"games": {}, "user_game": {}}


db = load()


def save():
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=1)


def dm_game(uid):
    """The game a user is submitting to from their private chat."""
    chat = db["user_game"].get(uid)
    return db["games"].get(chat) if chat else None


def host_game(update):
    """The group's game, but only if the sender is its host."""
    g = db["games"].get(str(update.effective_chat.id))
    return g if g and g["host"] == str(update.effective_user.id) else None


def say(update, text, **kw):
    return update.message.reply_text(text, **kw)


# ---------- helpers ----------

def track_title(t):
    return f'{t["name"]} – {", ".join(a["name"] for a in t["artists"])}'


def spotify_entry(track):
    """Build a stored song entry from a Spotify track object (Spotify mode)."""
    return {"uri": track["uri"], "title": track_title(track)}


FAKE_SONGS = [
    "Fake Song One – Tester A", "Fake Song Two – Tester B", "Fake Song Three – Tester C",
    "Fake Song Four – Tester D", "Fake Song Five – Tester E", "Fake Song Six – Tester F",
]


def manual_entry(text):
    """Build a stored song entry from raw user text (manual mode).

    If it's a Spotify link, the track id is used to de-dupe submissions;
    otherwise the lowercased text is. The full original text is kept as
    the display title either way (links stay tappable).
    """
    text = text.strip()
    m = LINK_RE.search(text)
    key = m.group(1) if m else text.lower()
    return {"uri": key, "title": text}


def add_song(g, uid, name, entry):
    mine = [s for s in g["songs"] if s["by"] == uid]
    if len(mine) >= MAX_SONGS:
        return f"You've already submitted {MAX_SONGS} songs. /undo to swap one out."
    if any(s["uri"] == entry["uri"] for s in g["songs"]):
        return "Someone already submitted that one! Pick another."
    g["songs"].append({"uri": entry["uri"], "title": entry["title"], "by": uid})
    g["names"][uid] = name
    save()
    return f"✅ Added: {entry['title']} ({len(mine) + 1}/{MAX_SONGS})"


def round_text(g, r):
    s = g["songs"][r]
    votes = len(g["votes"].get(str(r), {}))
    return (f"🎵 Song {r + 1}/{len(g['songs'])}\n{s['title']}\n\n"
            f"Who picked this? Your vote is secret.\n🗳 {votes} vote(s)")


def all_names(g):
    return {**g["voters"], **g["names"]}


def leaderboard(g):
    names = all_names(g)
    rows = sorted(g["scores"].items(), key=lambda x: -x[1])
    if not rows:
        return "No points yet."
    return "\n".join(f"{MEDALS[i] if i < 3 else '▫️'} {names.get(u, '?')}: {p}"
                     for i, (u, p) in enumerate(rows))


def _simulate_votes(g, r):
    """Dev-only: fake players cast a random guess so /next has more than
    just your own vote to work with while testing solo."""
    candidates = list(g["names"])
    for uid in g["names"]:
        if uid.startswith("bot_"):
            g["votes"].setdefault(str(r), {})[uid] = random.choice(candidates)
    save()


async def post_round(ctx, chat_id, g):
    r = g["round"]
    if DEV_MODE:
        _simulate_votes(g, r)
    btns = [Btn(n, callback_data=f"vote:{r}:{u}")
            for u, n in sorted(g["names"].items(), key=lambda x: x[1])]
    kb = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    await ctx.bot.send_message(chat_id, round_text(g, r), reply_markup=Kb(kb))


# ---------- group commands ----------

async def newgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = str(update.effective_chat.id)
    g = db["games"].get(chat)
    if g and g["status"] != "done":
        return await say(update, "A game is already running. The host can /endgame first.")
    db["games"][chat] = {"host": str(update.effective_user.id), "status": "collecting",
                         "names": {}, "voters": {}, "songs": [], "round": -1,
                         "votes": {}, "scores": {}}
    save()
    link = f"https://t.me/{ctx.bot.username}?start={chat}"
    await say(update,
              f"🎶 New game! Tap below and send me up to {MAX_SONGS} songs in private.\n"
              "Nobody will see who submitted what.\n\n"
              "/players shows who's in. Host: /startgame when everyone's ready.",
              reply_markup=Kb([[Btn("🤫 Submit songs", url=link)]]))


async def players(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = db["games"].get(str(update.effective_chat.id))
    if not g:
        return await say(update, "No game here. Start one with /newgame.")
    counts = Counter(s["by"] for s in g["songs"])
    lines = [f"• {g['names'][u]}: {n} song(s)" for u, n in counts.items()]
    await say(update, "\n".join(lines) or "Nobody has submitted yet.")


async def startgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = host_game(update)
    if not g or g["status"] != "collecting":
        return await say(update, "Only the host can start a game that's collecting songs.")
    submitters = {s["by"] for s in g["songs"]}
    min_players = 1 if DEV_MODE else 2
    if len(submitters) < min_players:
        return await say(update, f"Need songs from at least {min_players} people first.")

    g["names"] = {u: n for u, n in g["names"].items() if u in submitters}
    random.shuffle(g["songs"])

    if SPOTIFY_ENABLED:
        # Uses the endpoints Spotify requires since its Feb 2026 API changes.
        playlist = sp._post("me/playlists", payload={
            "name": f"{update.effective_chat.title} – Wrapped Together",
            "public": True,
            "description": "Guess who picked each song!",
        })
        uris = [s["uri"] for s in g["songs"]]
        for i in range(0, len(uris), 100):
            sp._post(f"playlists/{playlist['id']}/items", payload={"uris": uris[i:i + 100]})
        await say(update, f"🎧 Playlist is ready ({len(uris)} songs):\n"
                          f"{playlist['external_urls']['spotify']}\n\n"
                          "Play along in order. Host: /next reveals each answer.")
    else:
        await say(update, f"🎶 {len(g['songs'])} songs are shuffled and ready!\n"
                          "Each round's message includes the song so you can look it "
                          "up or play it yourself.\n\nHost: /next reveals each answer.")

    g["status"], g["round"] = "voting", 0
    save()
    await post_round(ctx, update.effective_chat.id, g)


async def simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dev-only: seed the current game with fake players + songs so you can
    play through /startgame, voting, and /next without other real accounts.
    Usage: /simulate [count] (default 2)."""
    if not DEV_MODE:
        return await say(update, "Set DEV_MODE=1 when running the bot to enable /simulate.")
    g = host_game(update)
    if not g or g["status"] != "collecting":
        return await say(update, "Start a game with /newgame first (while it's still collecting).")
    n = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 2
    n = max(1, min(n, len(FAKE_SONGS)))
    existing = sum(1 for u in g["names"] if u.startswith("bot_"))
    for i in range(n):
        idx = existing + i + 1
        add_song(g, f"bot_{idx}", f"Test Bot {idx}",
                  {"uri": f"fake:{idx}", "title": FAKE_SONGS[(idx - 1) % len(FAKE_SONGS)]})
    await say(update, f"Added {n} fake player(s) with songs. /startgame when ready.")


def _dev_breakdown(g, r):
    """Dev-only: every eligible player in order with their guess (or none) -
    shown after /next, not during voting."""
    s = g["songs"][r]
    votes = g["votes"].get(str(r), {})
    names = all_names(g)
    picker = s["by"]
    eligible = sorted((u, n) for u, n in g["names"].items() if u != picker)
    lines = [f"{name}→{names.get(votes[uid], votes[uid])}" if uid in votes
             else f"{name}→(no vote)"
             for uid, name in eligible]
    return "🔍 [dev]\n" + "\n".join(lines)


async def next_round(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = host_game(update)
    if not g or g["status"] != "voting":
        return await say(update, "Only the host can do that while voting.")
    r = g["round"]
    song = g["songs"][r]
    picker = song["by"]
    names = all_names(g)
    votes = {v: guess for v, guess in g["votes"].get(str(r), {}).items() if v != picker}

    right = [v for v, guess in votes.items() if guess == picker]
    fooled = len(votes) - len(right)
    for v in right:
        g["scores"][v] = g["scores"].get(v, 0) + 1
    # The picker gets a point for every friend they fooled.
    g["scores"][picker] = g["scores"].get(picker, 0) + fooled

    await say(update,
              f"🎤 {song['title']}\nwas picked by… {names[picker]}!\n\n"
              f"✅ Guessed right: {', '.join(names[v] for v in right) or 'nobody'}\n"
              f"😈 {names[picker]} fooled {fooled} friend(s)")

    if DEV_MODE:
        await say(update, _dev_breakdown(g, r))

    g["round"] += 1
    if g["round"] >= len(g["songs"]):
        g["status"] = "done"
        save()
        return await say(update, "🏁 Game over! Final scores:\n\n" + leaderboard(g))
    save()
    await post_round(ctx, update.effective_chat.id, g)


async def scores(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = db["games"].get(str(update.effective_chat.id))
    await say(update, leaderboard(g) if g else "No game here.")


async def endgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    g = host_game(update)
    if not g:
        return await say(update, "Only the host can end the game.")
    g["status"] = "done"
    save()
    await say(update, "Game ended. Scores:\n\n" + leaderboard(g))


# ---------- private chat ----------

async def dm_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    chat = ctx.args[0] if ctx.args else None
    g = db["games"].get(chat) if chat else None
    if not g or g["status"] != "collecting":
        return await say(update, "Tap the 'Submit songs' button in your group to join a game.")
    db["user_game"][uid] = chat
    save()
    hint = ("a Spotify track link or a song name" if SPOTIFY_ENABLED
            else "the song name or a link to it")
    await say(update, f"You're in! 🤫 Send {hint} "
                      f"(up to {MAX_SONGS}).\n/mysongs shows yours, /undo removes the last one.")


async def dm_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    g = dm_game(uid)
    if not g or g["status"] != "collecting":
        return await say(update, "No open game. Tap 'Submit songs' in your group first.")
    text = update.message.text

    if not SPOTIFY_ENABLED:
        return await say(update, add_song(g, uid, update.effective_user.full_name, manual_entry(text)))

    m = LINK_RE.search(text)
    if m:
        msg = add_song(g, uid, update.effective_user.full_name, spotify_entry(sp.track(m.group(1))))
        return await say(update, msg)
    results = sp.search(q=text, type="track", limit=5)["tracks"]["items"]
    if not results:
        return await say(update, "No matches. Try 'artist title', or paste a Spotify link.")
    kb = [[Btn(track_title(t)[:60], callback_data=f"add:{t['id']}")] for t in results]
    await say(update, "Which one?", reply_markup=Kb(kb))


async def on_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Only reachable in Spotify mode, since manual mode never shows these buttons.
    q = update.callback_query
    uid = str(q.from_user.id)
    g = dm_game(uid)
    if not g or g["status"] != "collecting":
        return await q.answer("Submissions are closed.", show_alert=True)
    msg = add_song(g, uid, q.from_user.full_name, spotify_entry(sp.track(q.data[4:])))
    await q.answer()
    await q.edit_message_text(msg)


async def mysongs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    g = dm_game(uid)
    mine = [f"• {s['title']}" for s in g["songs"] if s["by"] == uid] if g else []
    await say(update, "\n".join(mine) or "No songs yet.")


async def undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    g = dm_game(uid)
    if g and g["status"] == "collecting":
        for i in range(len(g["songs"]) - 1, -1, -1):
            if g["songs"][i]["by"] == uid:
                song = g["songs"].pop(i)
                save()
                return await say(update, f"Removed: {song['title']}")
    await say(update, "Nothing to undo.")


# ---------- voting ----------

async def on_vote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, r, guess = q.data.split(":")
    g = db["games"].get(str(q.message.chat.id))
    if not g or g["status"] != "voting" or int(r) != g["round"]:
        return await q.answer("This round is over.")
    voter = str(q.from_user.id)
    g["votes"].setdefault(r, {})[voter] = guess
    g["voters"][voter] = q.from_user.full_name
    save()
    # This popup is only visible to the person who tapped.
    await q.answer(f"You guessed {g['names'][guess]} 🤫 (tap again to change)")
    try:
        await q.edit_message_text(round_text(g, int(r)), reply_markup=q.message.reply_markup)
    except BadRequest:
        pass  # text unchanged (someone changed their vote)


def main():
    if SPOTIFY_ENABLED:
        me = sp.current_user()  # triggers the one-time Spotify login on first run
        print("Spotify account:", me["display_name"])
    else:
        print("Running in manual mode (no Spotify app configured).")
        print("Set SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET later to enable "
              "song search and auto-generated playlists - no code changes needed.")

    app = ApplicationBuilder().token(TOKEN).build()
    group, dm = filters.ChatType.GROUPS, filters.ChatType.PRIVATE
    for name, fn in [("newgame", newgame), ("players", players), ("startgame", startgame),
                     ("next", next_round), ("scores", scores), ("endgame", endgame),
                     ("simulate", simulate)]:
        app.add_handler(CommandHandler(name, fn, filters=group))
    for name, fn in [("start", dm_start), ("mysongs", mysongs), ("undo", undo)]:
        app.add_handler(CommandHandler(name, fn, filters=dm))
    app.add_handler(MessageHandler(dm & filters.TEXT & ~filters.COMMAND, dm_text))
    app.add_handler(CallbackQueryHandler(on_add, pattern=r"^add:"))
    app.add_handler(CallbackQueryHandler(on_vote, pattern=r"^vote:"))
    if DEV_MODE:
        print("DEV_MODE is on: /simulate is available, fake players auto-vote, min players = 1.")
    print("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    main()