"""Slash commands: /leaderboard and /stats.

Slash commands rather than a text prefix on purpose. In this channel a message
like "!stats" naive-tokenises to a single token, so adjudicate() would file it as
an `invalid` submission and pollute the very stats it was asking for. Interactions
never reach on_message, so that can't happen.

No arithmetic here -- every number comes from the views in sql/018_wod_stats.sql,
which the website reads too. This module only formats.
"""

import discord
from discord import app_commands

import store

# The leaderboard is a code block so its columns line up. That costs mentions --
# they render as raw ids inside code fences -- which is why wod_users exists.
#
# 20 fits every current nickname but the longest two, and keeps the whole row at
# 46 characters, which still renders without wrapping in a mobile code block.
NAME_WIDTH = 20


def _name(row) -> str:
    """Stored display name, falling back to a short id if we've never seen them."""
    name = row['display_name'] or ('user ' + str(row['user_id'])[-4:])
    return name[:NAME_WIDTH]


def _fmt_leaderboard(rows) -> str:
    header = '{:<3}{:<{w}} {:>5} {:>4} {:>4} {:>6}'.format(
        '#', 'Name', 'Clean', 'Day', 'Run', 'Words', w=NAME_WIDTH)
    lines = [header, '-' * len(header)]
    for r in rows:
        lines.append('{:<3}{:<{w}} {:>5} {:>4} {:>4} {:>6}'.format(
            r['rank'], _name(r), r['current_clean_day_streak'],
            r['current_day_streak'], r['current_clean_run'], r['accepted'],
            w=NAME_WIDTH))
    return '```\n' + '\n'.join(lines) + '\n```'


def _fmt_channel(s) -> str:
    """The channel-wide block: one stat per line, day-based figures first.

    A single stacked list rather than side-by-side inline fields, because Discord
    reflows inline fields into columns differently on mobile and desktop -- stacked
    is the only layout that reads the same everywhere.
    """
    # The dictionary lines sit right under the word count, and are dropped entirely
    # if count_dictionary.py has never run. Better a shorter block than a confident
    # "0.00% used".
    dictionary = ''
    if s['dictionary_stems']:
        dictionary = ('% of the dictionary used: **{}%**\n'
                      'Words left to claim: **{:,}**\n'
                      ).format(s['pct_dictionary_used'], s['words_remaining'])

    # "Twice in a day" isn't strictly one of the requested stats, but without it the
    # four sub-categories don't sum to the stated total and the block looks wrong.
    return ('Days running: **{:,}**\n'
            'Days with a word: **{:,}**\n'
            '% of days with a word: **{}%**\n'
            'Total words: **{:,}**\n'
            '{}'
            'Total errors: **{:,}**\n'
            'Plagiarisms: **{:,}**\n'
            'Self-cites: **{:,}**\n'
            'Twice in a day: **{:,}**\n'
            'Disallowed words: **{:,}**\n'
            'Accuracy: **{}%**'
            ).format(s['days_running'], s['active_days'], s['pct_days_with_word'],
                     s['accepted'], dictionary, s['errors'], s['total_thefts'],
                     s['total_self_recycles'], s['duplicate_day'],
                     s['invalid'], s['accuracy_pct'])


def register(tree: app_commands.CommandTree) -> None:
    @tree.command(name='leaderboard',
                  description='Channel stats, records and the standings')
    async def leaderboard(interaction: discord.Interaction):
        # Defer first: Discord gives an interaction 3 seconds, and a cold Neon
        # connection can eat most of that on its own.
        await interaction.response.defer()
        rows = await store.leaderboard(limit=25)
        s = await store.server_stats()

        e = discord.Embed(title='🏆 Word of the Day', colour=0xFEE75C)

        # The table goes in the DESCRIPTION, not a field: field values cap at 1024
        # characters, which 25 rows would overflow. Descriptions allow 4096.
        e.description = (
            'Ranked by **errorless days in a row**. Ties go to the sum of the other '
            'two streaks, then to total words.\n'
            '**Clean** = errorless days running · **Day** = days running with a word · '
            '**Run** = words since your last mistake\n'
            + _fmt_leaderboard(rows)
        )

        # One stat per line, in a single non-inline field. Side-by-side inline
        # fields would column these up and Discord reflows them differently on
        # mobile, so a stacked list is the only layout that reads the same
        # everywhere. Day-based figures lead, since the third is the ratio of the
        # first two.
        #
        # "Twice in a day" isn't in the requested list, but without it the four
        # sub-categories sum to 1,442 against a stated 1,652 total -- the 210 has
        # to appear somewhere or the breakdown looks wrong.
        e.add_field(name='The channel', value=_fmt_channel(s), inline=False)

        # Mentions, not the stored names: they render inside an embed and always
        # show what someone is called right now.
        e.add_field(
            name='All-time records',
            value=('Errorless days: **{}** — <@{}>\n'
                   'Days running: **{}** — <@{}>\n'
                   'Clean run: **{}** — <@{}>'
                   ).format(s['record_clean_day_streak'], s['record_clean_day_user_id'],
                            s['record_day_streak'], s['record_day_user_id'],
                            s['record_clean_run'], s['record_clean_run_user_id']),
            inline=False)

        await interaction.followup.send(embed=e)

    @tree.command(name='stats', description='Someone\'s word-of-the-day record')
    @app_commands.describe(user='Whose stats to show. Leave empty for your own.')
    async def stats(interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()
        target = user or interaction.user
        row = await store.user_stats(target.id)

        if row is None:
            await interaction.followup.send(
                '{} has never submitted a word of the day.'.format(target.mention))
            return

        e = discord.Embed(
            title=target.display_name,
            description='Rank **#{}** of the channel'.format(row['rank']),
            colour=0x5865F2)
        e.set_thumbnail(url=target.display_avatar.url)

        e.add_field(
            name='Words',
            value='**{}** accepted of {} submitted\n{}% stuck'.format(
                row['accepted'], row['submitted'], row['accuracy_pct']),
            inline=True)
        e.add_field(
            name='Turnout',
            value='A word on **{}%** of days\n{} of {} days since your first'.format(
                row['submission_rate_pct'], row['accepted_days'], row['days_since_first']),
            inline=True)
        e.add_field(
            name='Mistakes',
            value='{} recycled\n{} twice in a day\n{} not words'.format(
                row['recycled'], row['duplicate_day'], row['invalid']),
            inline=True)
        e.add_field(
            name='Streaks (now / best)',
            value='Errorless days: **{} / {}**\nDays running: {} / {}\nClean run: {} / {}'.format(
                row['current_clean_day_streak'], row['longest_clean_day_streak'],
                row['current_day_streak'], row['longest_day_streak'],
                row['current_clean_run'], row['longest_clean_run']),
            inline=False)

        # Counts only, never the word itself. /stats works on anyone, so naming
        # someone's most-contested word would let people fish for taken words by
        # looking each player up in turn -- and finding out what's already been
        # used is meant to be the hard part of the game.
        contested = ''
        if row['most_contested_count']:
            contested = '\nYour most fought-over word: taken {}x'.format(
                row['most_contested_count'])
        e.add_field(
            name='Collisions',
            value='Stolen from you **{}** times by {} people{}\nYou re-stole your own word {} times'.format(
                row['times_plagiarised'], row['distinct_thieves'], contested,
                row['self_recycles']),
            inline=False)

        foe = await store.nemesis(target.id)
        if foe is not None:
            e.add_field(name='Nemesis',
                        value='<@{}> - {} of your words'.format(foe['thief_id'], foe['times']),
                        inline=False)

        e.set_footer(text='First word {} · latest {}'.format(
            row['first_at'].date(), row['last_at'].date()))
        await interaction.followup.send(embed=e)
