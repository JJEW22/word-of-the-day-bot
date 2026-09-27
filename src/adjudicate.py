"""What happens to one word-of-the-day submission, and why.

The only place that decision is made. backfill.py replays the channel through this
and the live handler runs new messages through it, so a word judged one way by the
history scan cannot be judged differently when posted.

Takes plain values rather than a discord.py Message, so it can be exercised without
a gateway connection.
"""

from datetime import datetime, timezone
from typing import NamedTuple, Optional
from zoneinfo import ZoneInfo

import store
from english_processing import get_word_candidate, is_dictionary_word, is_word_candidate, shortest_available_stem

EASTERN = ZoneInfo('America/New_York')


def to_est(moment: datetime) -> datetime:
    """The server's day boundary, and so the one-per-day rule, is Eastern.

    discord.py always hands back tz-aware UTC; the naive branch is belt and braces.
    (The original assigned to `moment.tzinfo`, which raises -- datetimes are
    immutable -- and was only unreachable by luck.)
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(EASTERN)


class Decision(NamedTuple):
    # None means "not a submission at all" -- an ordinary message in the channel.
    # Nothing is recorded for those; they are not failed attempts.
    status: Optional[str]
    word: Optional[str] = None
    stem: Optional[str] = None
    # The submission that already held the stem, when status is RECYCLED.
    original: Optional[object] = None


async def adjudicate(message_id: int, user_id: int, content: str, created_at: datetime,
                     rulings: dict, *, write: bool = True) -> Decision:
    """Judge one message and, unless `write` is off, record the attempt.

    `rulings` maps STEM -> 'valid' | 'invalid', as settled by the WRONG polls. A
    ruling beats the dictionary in both directions: that is the entire point of
    being able to vote on a word.

    Order matters and matches the original: a recycled word is called out even if the
    poster had already used up their day, because the recycling is the greater crime.
    """
    # Shape first. Anything else is just someone talking in the channel.
    if not is_word_candidate(content):
        return Decision(None)

    word = get_word_candidate(content)
    if not word:
        return Decision(None)

    stem = shortest_available_stem(word)
    ruling = rulings.get(stem)

    # Blacklisted by a poll, or not a word in either dictionary and never whitelisted.
    # Recorded either way, because it counts against you in the stats.
    if ruling == store.INVALID_RULING or (ruling != store.VALID and not is_dictionary_word(word)):
        if write:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.INVALID)
        return Decision(store.INVALID, word, stem)

    owner = await store.accepted_for_stem(stem)
    if owner is not None and owner['message_id'] != message_id:
        if write:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.RECYCLED, recycled_of=owner['message_id'])
        return Decision(store.RECYCLED, word, stem, owner)

    if await store.posted_on(user_id, to_est(created_at).date()):
        # ...unless the word already accepted that day is this very message, which is
        # what a re-run of the backfill looks like.
        existing = await store.submission(message_id)
        if existing is None or existing['status'] != store.ACCEPTED:
            if write:
                await store.record(message_id, user_id, content, word, stem,
                                   created_at, store.DUPLICATE_DAY)
            return Decision(store.DUPLICATE_DAY, word, stem)

    if write:
        clash = await store.try_accept(message_id, user_id, content, word, stem, created_at)
        if clash is not None:
            # Lost a race for the stem between the lookup above and the insert.
            return Decision(store.RECYCLED, word, stem, clash)
    return Decision(store.ACCEPTED, word, stem)
