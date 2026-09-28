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

    `rulings` maps WORD -> 'valid' | 'invalid', as settled by the WRONG polls. A
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
    # The ruling is looked up by WORD; the stem below is only for the collision
    # check. Sharing one key made a downvote contagious across the word family.
    ruling = rulings.get(word)
    # Recorded on every row. Only an accepted word that came out of the
    # dictionary consumes one of its stems -- a poll-whitelisted word was never
    # in there to consume.
    in_dictionary = is_dictionary_word(word)

    # Blacklisted by a poll, or not a word in either dictionary and never whitelisted.
    # Recorded either way, because it counts against you in the stats.
    if ruling == store.INVALID_RULING or (ruling != store.VALID and not in_dictionary):
        if write:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.INVALID, from_dictionary=in_dictionary)
        return Decision(store.INVALID, word, stem)

    # Only an EARLIER claim makes this the copy. A dispute poll runs for six hours,
    # so a word can be judged long after it was posted -- and precedence has to
    # follow when it was posted, not when the verdict landed. Against
    # accepted_for_stem (any holder) an appeal you WON could leave your earlier word
    # marked as a copy of someone else's later one.
    owner = await store.accepted_for_stem_before(stem, created_at)
    if owner is not None and owner['message_id'] != message_id:
        if write:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.RECYCLED, recycled_of=owner['message_id'],
                               from_dictionary=in_dictionary)
        return Decision(store.RECYCLED, word, stem, owner)

    # Likewise only a word accepted EARLIER that day uses up the day. The bot also
    # refuses new submissions while your own poll is open, so in practice the only
    # word that can beat an appealed one is one you posted first.
    if await store.posted_on_before(user_id, to_est(created_at).date(), created_at):
        if write:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.DUPLICATE_DAY,
                               from_dictionary=in_dictionary)
        return Decision(store.DUPLICATE_DAY, word, stem)

    if write:
        # Displaces a LATER holder if one exists, repointing its repeats here.
        clash = await store.claim_stem(message_id, user_id, content, word, stem, created_at,
                                       from_dictionary=in_dictionary)
        if clash is not None:
            await store.record(message_id, user_id, content, word, stem,
                               created_at, store.RECYCLED, recycled_of=clash['message_id'],
                               from_dictionary=in_dictionary)
            return Decision(store.RECYCLED, word, stem, clash)
    return Decision(store.ACCEPTED, word, stem)
