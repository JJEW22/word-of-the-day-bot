"""Fill in from_dictionary for submissions recorded before the column existed.

Reads the `word` already stored on each row and asks enchant about it, so no
Discord connection is needed -- this is a pure database pass.

Idempotent: only rows where the flag is still null are touched, unless --all is
given (which is what you want if the dictionaries are ever upgraded).

    docker compose exec svelte-app python3 discord-wod-bot/src/backfill_from_dictionary.py

Needs DATABASE_URL.
"""

import asyncio
import sys

import store
from english_processing import is_dictionary_word

ALL = '--all' in sys.argv[1:]
DRY_RUN = '--dry-run' in sys.argv[1:]


async def main():
    pool = await store.connect()
    try:
        where = 'word is not null' if ALL else 'word is not null and from_dictionary is null'
        rows = await pool.fetch(
            'select message_id, word, status from wod_submissions where ' + where)
        print('{} row(s) to classify'.format(len(rows)))
        if not rows:
            return

        # Same word appears many times; ask enchant once each.
        verdict = {}
        for r in rows:
            w = r['word']
            if w not in verdict:
                verdict[w] = is_dictionary_word(w)
        yes = sum(1 for w in verdict.values() if w)
        print('distinct words: {} ({} in the dictionary, {} not)'.format(
            len(verdict), yes, len(verdict) - yes))

        if DRY_RUN:
            outside = [r for r in rows if not verdict[r['word']] and r['status'] == 'accepted']
            print('\naccepted despite not being in the dictionary (poll-whitelisted): {}'
                  .format(len(outside)))
            for r in outside[:20]:
                print('   ', r['word'])
            print('\n--dry-run: nothing written.')
            return

        # One statement per distinct verdict rather than per row.
        for flag in (True, False):
            ids = [r['message_id'] for r in rows if verdict[r['word']] is flag]
            if not ids:
                continue
            await pool.execute(
                'update wod_submissions set from_dictionary = $1 where message_id = any($2)',
                flag, ids)
            print('set from_dictionary = {} on {} row(s)'.format(flag, len(ids)))

        left = await pool.fetchval(
            'select count(*) from wod_submissions where word is not null and from_dictionary is null')
        print('rows still unclassified:', left)
        s = await pool.fetchrow(
            """select accepted, accepted_from_dictionary, pct_dictionary_used, words_remaining
               from wod_server_stats""")
        print('\naccepted {} of which {} came from the dictionary'.format(
            s['accepted'], s['accepted_from_dictionary']))
        print('{}% used, {:,} left to claim'.format(
            s['pct_dictionary_used'], s['words_remaining']))
    finally:
        await store.close()


if __name__ == '__main__':
    asyncio.run(main())
