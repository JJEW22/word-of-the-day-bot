"""Recover the dispute-poll verdicts that the old flat files threw away.

The blacklist and whitelist were opened with mode 'w+', which truncates, so every
`WRONG` poll the channel ever ran was wiped on the next restart. 146 verdicts were
announced in the channel and none of them survived -- which is why words the
channel voted to ALLOW are currently filed as `invalid`, and their authors lost the
credit.

The bot's own announcements are still in the channel, so the outcomes are
recoverable: find each "DEEMED **VALID**/**INVALID**" message, read the word off the
poll message it replied to, and write the ruling against that WORD.

Applied OLDEST FIRST, so a re-poll's later verdict wins -- 'Diss' was voted invalid
in May 2025 and valid twice since.

    python3 rebuild_rulings.py --dry-run    # report only, writes nothing
    python3 rebuild_rulings.py              # write the rulings

Writing the rulings alone changes no verdicts. Re-run backfill.py afterwards to
re-adjudicate the channel with them in place:

    python3 backfill.py

Needs TOKEN, CHANNEL_ID and DATABASE_URL.
"""

import re
import sys

from discord import Client, Intents

import store
from english_processing import get_word_candidate

import os

token = os.environ['TOKEN']
channel_id = int(os.environ['CHANNEL_ID'])

DRY_RUN = '--dry-run' in sys.argv[1:]

# The old bot's announcement, and the poll message it was replying to.
VERDICT = re.compile(r'DEEMED\s+\*\*(VALID|INVALID)\*\*', re.I)
# The bold markers are OPTIONAL: polls before about April 2025 wrote the word plain
# ("Is orifice an acceptable word of the day?") and only later versions bolded it.
# Requiring the asterisks lost the 15 oldest verdicts.
POLL_WORD = re.compile(
    r'Is\s+(?:\*\*)?(.+?)(?:\*\*)?\s+an acceptable word of the day', re.I | re.S)

intents = Intents.default()
intents.message_content = True


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def extract_word(text):
    """The disputed word, from the poll message's '**...**' capture.

    get_word_candidate handles a parenthesised gloss -- 'joe (coffee)' -> 'joe' --
    but it rejects a hyphenated word, because punkt splits 'piss-baby' into three
    tokens and the second isn't a bracket. Hence the plain fallback.
    """
    word = get_word_candidate(text)
    if word:
        return word
    first = text.strip().split()
    return first[0].lower() if first else None


class Rebuild(Client):
    def __init__(self, **kw):
        super().__init__(**kw)
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
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)

        seen = {}
        found, unresolved, scanned = [], [], 0

        async for m in channel.history(limit=None, oldest_first=True):
            scanned += 1
            seen[m.id] = m
            hit = VERDICT.search(m.content)
            if not hit:
                continue
            verdict = store.VALID if hit.group(1).upper() == 'VALID' else store.INVALID_RULING
            ref = m.reference.message_id if m.reference else None
            src = seen.get(ref)
            raw = None
            if src is not None:
                w = POLL_WORD.search(src.content)
                if w:
                    raw = w.group(1).strip()
            if not raw:
                unresolved.append((str(m.created_at)[:10], verdict, m.jump_url))
                continue
            word = extract_word(raw)
            if not word:
                unresolved.append((str(m.created_at)[:10], verdict, m.jump_url))
                continue
            found.append((m.created_at, word, verdict, raw, m.id))
            if scanned % 2000 == 0:
                log('  ...{} messages'.format(scanned))

        log('\nscanned {} messages; {} verdicts resolved, {} not'.format(
            scanned, len(found), len(unresolved)))

        # Oldest first, so a later re-poll overwrites an earlier verdict.
        found.sort(key=lambda r: r[0])
        final = {}
        for created, word, verdict, raw, msg_id in found:
            final[word] = (verdict, raw, str(created)[:10], msg_id)

        valid = sum(1 for v in final.values() if v[0] == store.VALID)
        log('{} distinct words after collapsing re-polls ({} valid, {} invalid)'.format(
            len(final), valid, len(final) - valid))

        # Blast radius, in both directions.
        ok_words = [w for w, v in final.items() if v[0] == store.VALID]
        no_words = [w for w, v in final.items() if v[0] == store.INVALID_RULING]
        promote = await pool.fetchval(
            "select count(*) from wod_submissions where status = 'invalid' and word = any($1)",
            ok_words) if ok_words else 0
        demote = await pool.fetchval(
            "select count(*) from wod_submissions where status = 'accepted' and word = any($1)",
            no_words) if no_words else 0

        log('\nwhat re-running backfill.py afterwards would change:')
        log('  invalid -> accepted (or recycled, if an earlier one claims the stem): {}'.format(promote))
        log('  accepted -> invalid (the channel voted the word down):               {}'.format(demote))

        if DRY_RUN:
            log('\n--- rulings that would be written ---')
            for word in sorted(final, key=lambda w: final[w][2]):
                verdict, raw, when, _ = final[word]
                # Full word, never truncated: a clipped report misled an analysis once.
                log('  {}  {:<8}  {}'.format(when, verdict, word))
            if unresolved:
                log('\n--- {} verdicts whose word could not be read ---'.format(len(unresolved)))
                log('    (older polls used different wording; judge these by hand)')
                for when, verdict, url in unresolved:
                    log('  {}  {:<8}  {}'.format(when, verdict, url))
            log('\n--dry-run: nothing written.')
            return

        for word, (verdict, _raw, _when, msg_id) in final.items():
            await store.set_ruling(word, verdict, msg_id)
        log('\nwrote {} rulings'.format(len(final)))
        log('now re-run backfill.py to re-adjudicate the channel with them applied.')


if __name__ == '__main__':
    Rebuild(intents=intents).run(token)
