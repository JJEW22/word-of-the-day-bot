"""Catch up on the period the bot was offline.

Two jobs, in this order:

  1. Add the approval reaction to every submission the database marked `accepted`
     that doesn't have it yet. Silent and in-place -- no notifications, no new
     messages -- and it makes the backlog stop looking half-finished.

  2. Post ONE summary message naming the repeats that slipped through. One message
     rather than a reply per offence: a reply posts as a NEW message at the bottom
     of the channel and pings its author, so 30-odd of them would be a wall of spam
     about words from months ago.

Presence is checked by EMOJI ID, deliberately not by `reaction.me`. If the bot is
running under a new Discord application, the old bot's reactions don't belong to the
current user, and `reaction.me` would report all ~3,300 historical messages as
unreacted. Checking the emoji id is identity-independent, which also makes this
script safe to run twice.

    # see exactly what it would do, touching nothing
    python3 reconcile.py --dry-run

    # do it
    python3 reconcile.py

    # override the auto-detected start of the absence
    python3 reconcile.py --since 2026-07-01

Needs TOKEN, CHANNEL_ID and DATABASE_URL.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from discord import Client, Intents, MessageType

import store

token = os.environ['TOKEN']
channel_id = int(os.environ['CHANNEL_ID'])

# Whose name goes in the teaser. Confirmed from the database: this account posted
# 'delayed' at 2026-08-25 16:36 UTC, matching the channel screenshot.
OWNER_ID = 592749952531431440

EMOJI_ID = 1259346961627086918
FALLBACK_EMOJI = '✅'

# Discord's hard limit on one message.
MESSAGE_LIMIT = 2000
# Discord allows roughly one reaction per quarter-second per channel; this leaves
# room so a long catch-up doesn't start collecting 429s.
REACTION_DELAY_S = 0.5

args = sys.argv[1:]
DRY_RUN = '--dry-run' in args
REACT_ALL = '--react-all' in args
SINCE = None
if '--since' in args:
    SINCE = datetime.fromisoformat(args[args.index('--since') + 1]).replace(tzinfo=timezone.utc)
# Inclusive last day of the absence. Matters because the bot is live again: without
# an upper bound the catch-up would re-report repeats the live bot has already
# replied to.
UNTIL = None
if '--until' in args:
    UNTIL = (datetime.fromisoformat(args[args.index('--until') + 1])
             .replace(tzinfo=timezone.utc) + timedelta(days=1))

intents = Intents.default()
intents.message_content = True


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def has_approval(message) -> bool:
    for reaction in message.reactions:
        emoji = reaction.emoji
        if getattr(emoji, 'id', None) == EMOJI_ID or emoji == FALLBACK_EMOJI:
            return True
    return False


def bucket_offences(rows) -> list:
    """Rows of (user_id, status, word) -> [(user_id, {status: [words]})], worst first.

    Ranked by total offences across every category, not just repeats.
    """
    by_user = {}
    for r in rows:
        buckets = by_user.setdefault(r['user_id'], {})
        words = buckets.setdefault(r['status'], [])
        if r['word'] not in words:      # the same word twice in one bucket
            words.append(r['word'])
    return sorted(by_user.items(),
                  key=lambda kv: sum(len(v) for v in kv[1].values()), reverse=True)


def build_summary(by_user, emoji) -> list:
    """The catch-up message, split across as many messages as the limit needs.

    `emoji` goes straight into the text. A discord.Emoji stringifies to
    `<:name:id>`, which Discord renders as the image -- so the message shows the
    actual approval emoji rather than describing it.
    """
    intro = (
        'Hi @everyone, I hope you missed me. I appreciate all of you who submitted in my '
        'absence I have given {} to the messages of yours that were accepted but by '
        'keeping it alive you are all winners in my heart. But some of you are losers and '
        'these are the ones that were plagiarising losers:'.format(emoji)
    )
    teaser = (
        'Some new features/updates now that <@{}> is in control will be coming very soon, '
        'feel free to request some of your own.'.format(OWNER_ID)
    )
    # recycled first -- it's the actual crime -- then the lesser offences, each
    # section omitted entirely when empty.
    labels = [(store.RECYCLED, 'recycled'),
              (store.DUPLICATE_DAY, 'two in one day'),
              (store.INVALID, 'not a word')]
    lines = []
    for uid, buckets in by_user:
        parts = ['{}: {}'.format(label, ', '.join(buckets[key]))
                 for key, label in labels if buckets.get(key)]
        lines.append('<@{}> - {}'.format(uid, ' | '.join(parts)))
    lines.append(teaser)

    # Pack lines into messages, never splitting a person across two.
    chunks = []
    current = intro
    for line in lines:
        if len(current) + 1 + len(line) > MESSAGE_LIMIT:
            chunks.append(current)
            current = line
        else:
            current += '\n' + line
    chunks.append(current)
    return chunks


class Reconcile(Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._done = False

    async def on_ready(self):
        if self._done:
            return
        self._done = True
        try:
            await self.run_once()
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            await store.close()
            await self.close()

    async def run_once(self):
        pool = await store.connect()

        accepted = {
            r['message_id']: r
            for r in await pool.fetch(
                "select message_id, word, posted_at from wod_submissions where status = 'accepted'")
        }
        log('{} accepted submissions on record'.format(len(accepted)))

        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)

        # Every accepted word in channel order, each flagged with whether the
        # approval emoji is on it.
        timeline = []
        scanned = 0
        async for message in channel.history(limit=None, oldest_first=True):
            scanned += 1
            row = accepted.get(message.id)
            if row is not None and message.type == MessageType.default:
                timeline.append((message, row, has_approval(message)))
            if scanned % 1000 == 0:
                log('  ...{} messages'.format(scanned))

        total_missing = sum(1 for _, _, ok in timeline if not ok)
        log('scanned {} messages; {} accepted words, {} without the approval emoji'.format(
            scanned, len(timeline), total_missing))

        # An outage is a CONTIGUOUS run of unreacted words. Taking the earliest
        # unreacted word instead would drag the window back to whichever stray old
        # message never got a reaction -- the channel's first week predates the bot,
        # so that alone reached back to the very beginning.
        runs = []
        start = None
        for i, (_m, _r, ok) in enumerate(timeline):
            if not ok and start is None:
                start = i
            elif ok and start is not None:
                runs.append((start, i - 1))
                start = None
        if start is not None:
            runs.append((start, len(timeline) - 1))

        if runs:
            log('')
            log('gaps in the reaction record ({} total):'.format(len(runs)))
            for a, b in sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:6]:
                log('  {} -> {}  ({} words)'.format(
                    str(timeline[a][1]['posted_at'])[:10],
                    str(timeline[b][1]['posted_at'])[:10], b - a + 1))

        if SINCE is not None:
            since = SINCE
            log('')
            log('absence start (from --since): {}'.format(since.date()))
        elif runs:
            # The longest run is the outage; the short ones are incidental misses.
            a, b = max(runs, key=lambda r: r[1] - r[0])
            since = timeline[a][1]['posted_at']
            log('')
            log('absence start (longest gap): {} -> {}  ({} words)'.format(
                str(since)[:10], str(timeline[b][1]['posted_at'])[:10], b - a + 1))
        else:
            log('nothing missing a reaction; no absence to reconcile')
            return

        # The absence ends where the reacted record resumes, unless told otherwise.
        if UNTIL is not None:
            until = UNTIL
        else:
            last = timeline[b][1]['posted_at'] if not SINCE else timeline[-1][1]['posted_at']
            until = last.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        log('absence end (exclusive): {}'.format(str(until)[:10]))

        # React only inside the window, so the message's claim matches what was done.
        # Older strays are reported but left alone unless --react-all is passed.
        in_window = [(m, r) for m, r, ok in timeline
                     if not ok and since <= r['posted_at'] < until]
        older = total_missing - len(in_window)
        if older:
            log('({} unreacted words predate the window; pass --react-all to include them)'.format(older))
        missing = [(m, r) for m, r, ok in timeline if not ok] if REACT_ALL else in_window

        # Every offence that slipped through the window, not just the repeats: a
        # word posted on a day you'd already had one, and a word that isn't a word,
        # both went uncalled-out too.
        rows = await pool.fetch(
            """select user_id, status, word, posted_at
               from wod_submissions
               where status in ('recycled', 'duplicate_day', 'invalid')
                 and posted_at >= $1 and posted_at < $2
               order by user_id, posted_at""",
            since, until)

        ranked = bucket_offences(rows)

        log('{} offences from {} people in the window'.format(len(rows), len(ranked)))
        emoji = self.get_emoji(EMOJI_ID) or FALLBACK_EMOJI
        chunks = build_summary(ranked, emoji)

        if DRY_RUN:
            log('\n--- would add the approval reaction to {} messages ---'.format(len(missing)))
            for message, row in missing[:15]:
                log('  {}  {}'.format(str(row['posted_at'])[:10], row['word']))
            if len(missing) > 15:
                log('  ... and {} more'.format(len(missing) - 15))
            log('\n--- would post {} message(s) ---'.format(len(chunks)))
            for c in chunks:
                log('\n' + c)
                log('[{} chars]'.format(len(c)))
            log('\n--dry-run: nothing sent.')
            return

        added = 0
        for message, _row in missing:
            try:
                await message.add_reaction(emoji)
                added += 1
            except Exception as err:
                log('  could not react to {}: {}'.format(message.id, err))
            await asyncio.sleep(REACTION_DELAY_S)
        log('added {} reactions'.format(added))

        # Sent after the reactions, so "I have given thumbs" is true by the time
        # anyone reads it.
        for c in chunks:
            await channel.send(c)
        log('posted {} summary message(s)'.format(len(chunks)))


if __name__ == '__main__':
    Reconcile(intents=intents).run(token)
