"""Measure the playable word space and store it.

Reads the hunspell dictionaries the bot actually validates against, keeps the
word-shaped entries, stems them, and records both totals in wod_dictionary.

total_stems is the number that matters. Claiming `chauvinist` also consumes
`chauvinistic`, because collisions are judged on the stem -- so the count of
distinct PLAYS available is the count of distinct stems, not of words.

Stored rather than computed on demand: stemming ~124k entries takes seconds, which
is far too slow for a /leaderboard that people will spam.

    docker compose exec svelte-app python3 discord-wod-bot/src/count_dictionary.py
    ... --dry-run    # report without writing

Re-run it if the dictionaries are ever upgraded; the numbers only move then.

Needs DATABASE_URL. Does not touch Discord.
"""

import asyncio
import glob
import re
import sys

import store
from english_processing import shortest_available_stem

DRY_RUN = '--dry-run' in sys.argv[1:]

# Both dictionaries, because the bot accepts a word found in EITHER.
DIC_GLOB = '/usr/share/hunspell/en_[UG][SB].dic'

# Letters, plus the hyphen and apostrophe that legitimately appear in words. The
# .dic files also carry numerals and ordinals ('0/nm', '1st/p') which enchant would
# accept but nobody would call a word of the day.
WORDISH = re.compile(r"^[a-z][a-z'-]*$")


def measure():
    paths = sorted(glob.glob(DIC_GLOB))
    if not paths:
        raise SystemExit('no hunspell dictionaries found at ' + DIC_GLOB)

    words = set()
    for path in paths:
        with open(path, encoding='utf-8', errors='replace') as fh:
            fh.readline()                  # line 1 is the entry count
            for line in fh:
                # Entries look like `word/FLAGS`; the flags drive affix expansion.
                entry = line.strip().split('/')[0].strip().lower()
                if entry and WORDISH.match(entry):
                    words.add(entry)

    stems = {shortest_available_stem(w) for w in words}
    return paths, len(words), len(stems)


async def main():
    paths, total_words, total_stems = measure()
    print('read: ' + ', '.join(paths))
    print('word-shaped entries (union): {:,}'.format(total_words))
    print('distinct stems:              {:,}'.format(total_stems))

    if DRY_RUN:
        print('\n--dry-run: nothing written.')
        return

    pool = await store.connect()
    try:
        await pool.execute(
            """insert into wod_dictionary (id, total_words, total_stems, computed_at)
               values (1, $1, $2, now())
               on conflict (id) do update set
                   total_words = excluded.total_words,
                   total_stems = excluded.total_stems,
                   computed_at = now()""",
            total_words, total_stems)
        row = await pool.fetchrow(
            """select accepted, dictionary_stems, pct_dictionary_used, words_remaining
               from wod_server_stats""")
        print('\nstored. {:,} of {:,} stems claimed ({}%), {:,} left'.format(
            row['accepted'], row['dictionary_stems'],
            row['pct_dictionary_used'], row['words_remaining']))
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
