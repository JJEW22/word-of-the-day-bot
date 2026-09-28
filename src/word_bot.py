import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

from discord import Client, Intents, Message, MessageType
from discord import app_commands

import wod_commands
import store
from adjudicate import adjudicate, to_est
from english_processing import get_word_candidate, is_word_candidate, shortest_available_stem


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, flush=True, **kwargs)


token = os.environ['TOKEN']
channel_id = int(os.environ['CHANNEL_ID'])
april_fools_link = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

APRIL_FOOLS = datetime(month=4, day=1, year=2025)

EMOJI_ID = 1259346961627086918
# get_emoji returns None when the bot shares no server with the custom emoji, and
# add_reaction(None) raises. Falling back keeps a missing emoji from taking out the
# whole approval path.
FALLBACK_EMOJI = '✅'
POLL_DURATION_HRS = 6
DISPUTE_MESSAGE = 'WRONG'

# A poll opened at or after this hour (Eastern) closes late enough to threaten
# the day -- 4pm plus six hours is 10pm, and anything later crosses midnight.
# Only those appeals offer a backup queue; an earlier poll resolves with hours
# to spare, so its author is simply asked to wait.
QUEUE_CUTOFF_HOUR = 16
QUEUE_MAX = 5

HOURGLASS = '⏳'
YES_EMOJI = '✔️'
NO_EMOJI = '❌'

intents = Intents.default()
intents.members = True
intents.message_content = True


class WordBot(Client):
    """Moderates the word-of-the-day channel.

    State lives in Postgres (see store.py). It used to be an in-memory dict rebuilt
    by replaying the entire channel in on_ready -- which fires again on every
    reconnect, so a dropped websocket meant a full history re-scan. The channel is
    now scanned once, by backfill.py.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._started = False
        self._rulings = {}
        # user_id -> how many of their words are under appeal right now. While
        # that is non-zero they cannot submit again, which is what stops a second
        # word claiming the day out from under the one being appealed. A count
        # rather than a set, because two of your words can be on trial at once
        # and the first verdict must not lift the block for the second.
        self._pending_polls = Counter()
        # user_id -> [(message_id, content, created_at, stem), ...] backup words
        # waiting on that user's appeal, in the order they were posted.
        self._queues = {}
        # Users whose current appeal started late enough to allow a queue.
        self._queue_open = set()
        # stem -> user_id. A queued word is held for its owner: without this,
        # somebody else could claim the stem while the appeal runs and the backup
        # would fail as a repeat, which is the very thing the queue exists to stop.
        self._reserved = {}
        # discord.Client has no command tree of its own (that's commands.Bot), so
        # the tree is built here and the commands registered onto it.
        self.tree = app_commands.CommandTree(self)
        wod_commands.register(self.tree)

    async def _refresh_rulings(self):
        """word -> 'valid' | 'invalid', as settled by the WRONG polls."""
        self._rulings = await store.rulings()

    async def on_ready(self):
        # on_ready fires on every reconnect, not just at startup.
        if self._started:
            return
        self._started = True
        await self._refresh_rulings()
        valid = sum(1 for v in self._rulings.values() if v == store.VALID)
        eprint('ready: {} rulings ({} valid, {} invalid)'.format(
            len(self._rulings), valid, len(self._rulings) - valid))

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as err:
                eprint('could not fetch channel {}: {}'.format(channel_id, err))
        guild = getattr(channel, 'guild', None)
        if guild is None:
            eprint('WARNING: channel {} not visible; commands not synced'.format(channel_id))
            return

        await self._refresh_names(guild)

        # Synced to the ONE guild rather than globally: a guild sync is available
        # immediately, while a global one can take up to an hour to propagate.
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        eprint('synced {} command(s) to {}: {}'.format(
            len(synced), guild.name, ', '.join(c.name for c in synced)))

    async def _refresh_names(self, guild):
        """Store every member's current display name, for the website.

        Discord needs none of this -- a <@id> mention always renders the live name.
        The website cannot resolve an id, so it reads wod_users instead, and this
        keeps it honest across nickname changes. Runs on every startup; on_message
        keeps active people current in between.
        """
        stored = 0
        try:
            async for member in guild.fetch_members(limit=None):
                await store.upsert_user(member.id, member.display_name)
                stored += 1
        except Exception as err:
            eprint('name refresh failed ({}); names may be stale on the website'.format(err))
        eprint('refreshed {} display names'.format(stored))

    def _emoji(self):
        return self.get_emoji(EMOJI_ID) or FALLBACK_EMOJI

    async def on_message(self, message: Message):
        # This channel only, and never our own messages.
        if message.author.id == self.user.id or message.channel.id != channel_id:
            return

        # Cheap, and it means a rename shows on the website before the next restart.
        await store.upsert_user(message.author.id, message.author.display_name)

        if message.type == MessageType.reply:
            if message.content == DISPUTE_MESSAGE:
                disputed = await message.channel.fetch_message(message.reference.message_id)
                await self.dispute_word(disputed, message)
            return

        if message.type != MessageType.default:
            return

        # Nothing below is recorded. These are attempts that were never allowed to
        # happen, and filing them as errors would punish people for appealing.
        candidate = get_word_candidate(message.content) if is_word_candidate(message.content) else None
        if candidate:
            stem = shortest_available_stem(candidate)
            holder = self._reserved.get(stem)
            if holder is not None and holder != message.author.id:
                await message.reply(
                    'That one is on hold ⏳ <@{}> has it queued behind an open poll.'
                    .format(holder))
                return

            if self._pending_polls[message.author.id]:
                if message.author.id not in self._queue_open:
                    await message.reply(
                        'You have a word on trial ⚖️ wait for the poll to close before '
                        'submitting another.')
                    return
                await self._enqueue(message, candidate, stem)
                return

        decision = await adjudicate(
            message.id, message.author.id, message.content, message.created_at, self._rulings)
        await self._respond(message, decision)

    async def _respond(self, message: Message, decision):
        """React or reply to a verdict.

        Shared by the live path and the queue, so a backup word judged six hours
        late gets exactly the same treatment as one judged on the spot.
        """
        if decision.status == store.ACCEPTED:
            await message.add_reaction(self._emoji())
            if to_est(message.created_at).date() == APRIL_FOOLS.date():
                await message.reply(
                    ':bangbang:Recycled word alert:bangbang:\n {} already said [{}](<{}>)'
                    .format(message.author.mention, message.content, april_fools_link))

        elif decision.status == store.RECYCLED:
            original = await message.channel.fetch_message(decision.original['message_id'])
            await message.reply(
                ':bangbang:Recycled word alert:bangbang:\n {} already said [{}]({})'
                .format(original.author.mention, original.content, original.jump_url))

        elif decision.status == store.DUPLICATE_DAY:
            await message.reply('Only one word of the day per day, bozo 💀')

        # INVALID and None draw no response, as before: the bot stays quiet about
        # things that were never plausible words of the day.

    async def _enqueue(self, message: Message, word: str, stem: str):
        """Hold a backup word until the author's appeal is decided."""
        queue = self._queues.setdefault(message.author.id, [])
        if self._reserved.get(stem) == message.author.id:
            await message.reply('Already in your queue ⏳')
            return
        if len(queue) >= QUEUE_MAX:
            await message.reply('Your queue is full ({} words) ⏳'.format(QUEUE_MAX))
            return

        queue.append((message.id, message.content, message.created_at, stem))
        self._reserved[stem] = message.author.id
        await message.add_reaction(HOURGLASS)
        await message.reply('Queued {}/{} ⏳ — {}'.format(
            len(queue), QUEUE_MAX, ', '.join(q[1] for q in queue)))

    async def _resolve_queue(self, user_id: int, appeal_won: bool, channel):
        """Judge the backup words, or throw them away if the appeal succeeded.

        Each keeps ITS OWN timestamp, so a word posted at 10:05pm and judged at
        4am still claims the 10:05pm day -- without that the queue would protect
        nothing, which is the whole reason it exists.
        """
        queue = self._queues.pop(user_id, [])
        self._queue_open.discard(user_id)
        for _mid, _content, _at, stem in queue:
            if self._reserved.get(stem) == user_id:
                del self._reserved[stem]
        if not queue:
            return

        if appeal_won:
            await channel.send(
                '<@{}> won the appeal, so their {} queued word(s) were not needed.'
                .format(user_id, len(queue)))
            return

        for message_id, content, created_at, _stem in queue:
            decision = await adjudicate(message_id, user_id, content, created_at, self._rulings)
            try:
                msg = await channel.fetch_message(message_id)
            except Exception:
                continue        # deleted while it waited
            await self.remove_reaction_emoji(msg, HOURGLASS)
            await self._respond(msg, decision)
            if decision.status == store.ACCEPTED:
                return          # stop at the first one that sticks

    async def remove_reaction_emoji(self, msg, emoji):
        try:
            await msg.remove_reaction(emoji, self.user)
        except Exception:
            pass

    async def on_message_edit(self, before, after):
        await self.remove_wotd(before)
        await self.on_message(after)

    async def on_raw_message_delete(self, message_event):
        # One lookup rather than scanning a dict -- and crucially not mutating that
        # dict mid-iteration, which is what made the old version raise
        # "dictionary changed size during iteration" every time a word was deleted.
        gone = await store.forget(message_event.message_id)
        if gone is None or gone['status'] != store.ACCEPTED:
            return
        user = self.get_user(gone['user_id'])
        who = user.mention if user is not None else '<@{}>'.format(gone['user_id'])
        await self.get_channel(channel_id).send(
            '{} deleted their word of the day "{}"! Kinda embarrassing, not gonna lie... 💀'
            .format(who, gone['raw_message']))

    async def remove_wotd(self, msg, deleted=False):
        gone = await store.forget(msg.id)
        if gone is not None and not deleted:
            await self.remove_reaction(msg)

    async def remove_reaction(self, msg):
        for reaction in msg.reactions:
            if reaction.me:
                await reaction.remove(self.user)

    async def add_reaction(self, msg):
        for reaction in msg.reactions:
            if reaction.me:
                return
        await msg.add_reaction(self._emoji())

    async def dispute_word(self, msg: Message, dispute_msg: Message):
        dispute_text = '{} has thrown down the gauntlet 😱😱\nIs **{}** an acceptable word of the day?'.format(
            dispute_msg.author.mention, msg.content)
        word = get_word_candidate(msg.content)
        if word is None:
            await dispute_msg.reply('bot abuser 😱')
            return

        poll = await msg.reply('{}\nHours to close: {}'.format(dispute_text, POLL_DURATION_HRS))
        await poll.add_reaction(YES_EMOJI)
        await poll.add_reaction(NO_EMOJI)

        # A poll opened late closes late, and its author would otherwise have no way
        # left to save the day if the vote goes against them. Those get a queue.
        if to_est(poll.created_at).hour >= QUEUE_CUTOFF_HOUR:
            self._queue_open.add(msg.author.id)
            closes = to_est(poll.created_at + timedelta(hours=POLL_DURATION_HRS))
            await poll.reply(
                '{}, this closes around {} ⏳\n'
                'You can queue up to **{}** backup words — post them now and I will '
                'judge them in order if this appeal fails, stopping at the first one '
                'that sticks. Nobody else can use a queued word while you wait.'
                .format(msg.author.mention,
                        closes.strftime('%I:%M %p').lstrip('0'),
                        QUEUE_MAX))

        # The author cannot submit normally for as long as this runs. Held in a
        # try/finally so a crash mid-poll can't leave them locked out forever.
        self._pending_polls[msg.author.id] += 1
        try:
            for i in range(1, POLL_DURATION_HRS + 1):
                await asyncio.sleep(3600)
                await poll.edit(
                    content='{}\nHours to close: {}'.format(dispute_text, POLL_DURATION_HRS - i))
        finally:
            self._pending_polls[msg.author.id] -= 1
            if self._pending_polls[msg.author.id] <= 0:
                del self._pending_polls[msg.author.id]

        completed_poll = await msg.channel.fetch_message(poll.id)
        yes_count = 0
        no_count = 0
        for reaction in completed_poll.reactions:
            if reaction.emoji == YES_EMOJI:
                yes_count = reaction.count - 1   # less the bot's own seed reaction
            elif reaction.emoji == NO_EMOJI:
                no_count = reaction.count - 1

        verdict = store.VALID if yes_count > no_count else store.INVALID_RULING
        # One row per WORD carrying a verdict, so a re-poll moves that word between
        # allowed and disallowed. Keyed by word rather than stem: a downvote judges
        # one word, and rejecting `nutcrack` must not invalidate `nutcracker`.
        # Survives a restart, which the old flat files never did.
        await store.set_ruling(word, verdict, poll.id, yes_count, no_count)
        await self._refresh_rulings()

        await completed_poll.reply(
            'THE PEOPLE HAVE SPOKEN 😤\nTHIS WORD HAS BEEN DEEMED **{}**!!'
            .format('VALID' if verdict == store.VALID else 'INVALID'))
        await completed_poll.edit(content='{}\nPOLL HAS CLOSED.\nVotes YAY: {}\nVotes NAY: {}'
                                  .format(dispute_text, yes_count, no_count))

        # Re-judge the disputed message under the new ruling. on_message is not used
        # here: the author still counts as having a poll open at this point, so it
        # would queue the word instead of judging it.
        await self.remove_wotd(msg)
        decision = await adjudicate(
            msg.id, msg.author.id, msg.content, msg.created_at, self._rulings)
        await self._respond(msg, decision)

        # Then the backups: discarded if the appeal stuck, judged in order if not.
        await self._resolve_queue(
            msg.author.id, decision.status == store.ACCEPTED, msg.channel)

    async def close(self):
        await store.close()
        await super().close()


if __name__ == '__main__':
    WordBot(intents=intents).run(token)
