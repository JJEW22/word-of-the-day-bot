"""Neon-backed storage for the word-of-the-day bot.

Replaces two things the bot used to do in memory:

  * `self._words`, a dict rebuilt by replaying the entire channel on every connect.
    `on_ready` fires again on every reconnect, so a network blip cost a full history
    scan. Now the channel is scanned once, by backfill.py, and this is the record.

  * the blacklist and whitelist flat files. Those were opened with mode 'w+', which
    TRUNCATES, so every restart wiped them and read back nothing — meaning no
    dispute-poll verdict ever survived. They are one table here, with a verdict
    column, because a stem can only be on one of the two lists.

Shares the Postgres instance behind the website, so DATABASE_URL is already set in
the environment this runs in.
"""

import os
from datetime import date
from typing import Optional

import asyncpg

# Verdicts on a submission attempt. Rejections are recorded too, not just winners:
# the per-user stats ("errors", "streak") are not derivable from accepted words alone.
ACCEPTED = 'accepted'
RECYCLED = 'recycled'
DUPLICATE_DAY = 'duplicate_day'
INVALID = 'invalid'

VALID = 'valid'      # whitelisted by a poll
INVALID_RULING = 'invalid'   # blacklisted by a poll

_pool: Optional[asyncpg.Pool] = None


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # One long-lived process, and Neon caps connections, so the pool stays small.
        # asyncpg reads the DSN's sslmode=require itself; no ssl context needed.
        _pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=4)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------- submissions ----------

async def accepted_for_stem(stem: str) -> Optional[asyncpg.Record]:
    """The submission already holding this stem, or None if it is unclaimed."""
    pool = await connect()
    return await pool.fetchrow(
        """select message_id, user_id, raw_message, word, stem, posted_at
           from wod_submissions where stem = $1 and status = $2""",
        stem, ACCEPTED)


async def record(message_id: int, user_id: int, raw_message: str, word: Optional[str],
                 stem: Optional[str], posted_at, status: str,
                 recycled_of: Optional[int] = None) -> None:
    """Write one submission attempt.

    Keyed on Discord's message_id, so re-running the backfill over a channel updates
    rows instead of duplicating them.
    """
    pool = await connect()
    await pool.execute(
        """insert into wod_submissions
               (message_id, user_id, raw_message, word, stem, posted_at, status, recycled_of)
           values ($1, $2, $3, $4, $5, $6, $7, $8)
           on conflict (message_id) do update set
               user_id     = excluded.user_id,
               raw_message = excluded.raw_message,
               word        = excluded.word,
               stem        = excluded.stem,
               posted_at   = excluded.posted_at,
               status      = excluded.status,
               recycled_of = excluded.recycled_of""",
        message_id, user_id, raw_message, word, stem, posted_at, status, recycled_of)


async def try_accept(message_id: int, user_id: int, raw_message: str, word: str,
                     stem: str, posted_at) -> Optional[asyncpg.Record]:
    """Claim `stem` for this message.

    Returns None when the claim succeeded, or the submission that already owns the
    stem when it did not. The partial unique index is what decides, so two people
    posting the same word in the same instant can never both be accepted — which the
    old in-memory dict could not prevent.
    """
    try:
        await record(message_id, user_id, raw_message, word, stem, posted_at, ACCEPTED)
        return None
    except asyncpg.UniqueViolationError:
        return await accepted_for_stem(stem)


async def posted_on(user_id: int, day: date) -> bool:
    """Has this person already had a word accepted on this (Eastern) date?

    Only accepted words count, matching the original behaviour: a rejected attempt
    did not use up your one-per-day.
    """
    pool = await connect()
    row = await pool.fetchrow(
        """select 1 from wod_submissions
           where user_id = $1 and status = $2
             and (posted_at at time zone 'America/New_York')::date = $3
           limit 1""",
        user_id, ACCEPTED, day)
    return row is not None


async def submission(message_id: int) -> Optional[asyncpg.Record]:
    pool = await connect()
    return await pool.fetchrow(
        """select message_id, user_id, raw_message, word, stem, posted_at, status
           from wod_submissions where message_id = $1""",
        message_id)


async def forget(message_id: int) -> Optional[asyncpg.Record]:
    """Drop a submission, returning what was removed so the caller can announce it."""
    pool = await connect()
    return await pool.fetchrow(
        """delete from wod_submissions where message_id = $1
           returning message_id, user_id, raw_message, word, stem, status""",
        message_id)


# ---------- dispute rulings ----------

async def rulings() -> dict:
    """stem -> 'valid' | 'invalid', for every word a poll has settled."""
    pool = await connect()
    rows = await pool.fetch('select stem, verdict from wod_word_rulings')
    return {r['stem']: r['verdict'] for r in rows}


async def set_ruling(stem: str, verdict: str, poll_message_id: Optional[int] = None,
                     yes_count: Optional[int] = None, no_count: Optional[int] = None) -> None:
    """Record a poll outcome. Re-polling the same word overwrites the old verdict,
    which is also what moves a stem between the two lists."""
    pool = await connect()
    await pool.execute(
        """insert into wod_word_rulings
               (stem, verdict, poll_message_id, yes_count, no_count, decided_at)
           values ($1, $2, $3, $4, $5, now())
           on conflict (stem) do update set
               verdict         = excluded.verdict,
               poll_message_id = excluded.poll_message_id,
               yes_count       = excluded.yes_count,
               no_count        = excluded.no_count,
               decided_at      = excluded.decided_at""",
        stem, verdict, poll_message_id, yes_count, no_count)


# ---------- display names ----------

async def upsert_user(user_id: int, display_name: str) -> None:
    """Remember what someone is currently called.

    Only for the website, which cannot resolve a Discord id. Discord itself is
    told nothing -- a <@id> mention renders the live name, so anything shown in
    Discord is never stale by construction.
    """
    pool = await connect()
    await pool.execute(
        """insert into wod_users (user_id, display_name, updated_at)
           values ($1, $2, now())
           on conflict (user_id) do update
               set display_name = excluded.display_name, updated_at = now()
           where wod_users.display_name <> excluded.display_name""",
        user_id, display_name)


# ---------- stats (all the arithmetic lives in sql/018_wod_stats.sql) ----------

async def server_stats():
    pool = await connect()
    return await pool.fetchrow('select * from wod_server_stats')


async def leaderboard(limit: int = 25):
    pool = await connect()
    return await pool.fetch('select * from wod_leaderboard order by rank limit $1', limit)


async def user_stats(user_id: int):
    pool = await connect()
    return await pool.fetchrow(
        """select l.* from wod_leaderboard l where l.user_id = $1""", user_id)


async def nemesis(user_id: int):
    """Who has stolen the most words from this person."""
    pool = await connect()
    return await pool.fetchrow(
        """select thief_id, times from wod_plagiarist_pairs
           where victim_id = $1 and thief_id <> victim_id
           order by times desc, thief_id limit 1""",
        user_id)
