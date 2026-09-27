"""One-shot: replay the whole word-of-the-day channel into the database.

The bot used to do this on every connect and keep the result in memory. Now it
happens once, here, and the bot reads the database instead.

Deliberately writes nothing to Discord: no reactions, no replies. Every historical
message already carries whatever the old bot did to it, and a new bot application
has a different identity, so re-reacting would stack a second emoji onto hundreds of
old messages.

Idempotent — message_id is the primary key, so re-running updates rows in place.
Safe to run again after changing the stemmer or the word rules.

    docker compose exec svelte-app python3 discord-wod-bot/src/backfill.py

Needs TOKEN, CHANNEL_ID and DATABASE_URL in the environment.
"""

import os
import sys

from discord import Client, Intents, MessageType

import store
from adjudicate import adjudicate

token = os.environ['TOKEN']
channel_id = int(os.environ['CHANNEL_ID'])

intents = Intents.default()
intents.message_content = True


def log(*args):
    print(*args, file=sys.stderr, flush=True)


class Backfill(Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # on_ready fires again on every reconnect; this must run exactly once.
        self._done = False

    async def on_ready(self):
        if self._done:
            return
        self._done = True

        counts = {}
        scanned = 0
        try:
            rulings = await store.rulings()
            valid = sum(1 for v in rulings.values() if v == store.VALID)
            log('rulings on record: {} valid, {} invalid'.format(valid, len(rulings) - valid))

            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            log('scanning #{} oldest first...'.format(getattr(channel, 'name', channel_id)))

            # Oldest first is what makes "the first person to say it owns it" fall out
            # naturally: each message is judged against everything already recorded.
            async for message in channel.history(limit=None, oldest_first=True):
                scanned += 1
                if message.author.id == self.user.id or message.type != MessageType.default:
                    continue

                decision = await adjudicate(
                    message.id, message.author.id, message.content,
                    message.created_at, rulings)

                if decision.status is not None:
                    counts[decision.status] = counts.get(decision.status, 0) + 1

                if scanned % 500 == 0:
                    log('  ...{} messages, {} submissions'.format(scanned, sum(counts.values())))

            log('\nscanned {} messages'.format(scanned))
            for status in (store.ACCEPTED, store.RECYCLED, store.DUPLICATE_DAY, store.INVALID):
                log('  {:<14} {}'.format(status, counts.get(status, 0)))
            log('  {:<14} {}'.format('(not a word)', scanned - sum(counts.values())))
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        finally:
            await store.close()
            await self.close()


Backfill(intents=intents).run(token)
