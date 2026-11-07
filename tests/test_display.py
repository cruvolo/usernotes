"""Tests for :mod:`usernotes.display`, the listing a terminal is shown.

Everything here is pure: notes in, text out.  The wording of the relative
times and the colours of the note types are checked against what toolbox puts
on the screen, and so is the escaping - note text is other people's data, and
the one thing a listing must never do is let it drive the terminal.
"""

import datetime
import io
import os
import unittest
from unittest import mock

from usernotes.display import (
    COLOR_MODES,
    Style,
    color_enabled,
    color_index,
    format_time,
    parse_time,
    relative_time,
    render_note,
    render_note_types,
    render_search_results,
    render_user_notes,
    safe_text,
)
from usernotes.format import DEFAULT_NOTE_TYPES, Note, NoteType

UTC = datetime.timezone.utc


def at(*args, tzinfo=UTC):
    """An aware datetime, for the tests that need one."""
    return datetime.datetime(*args, tzinfo=tzinfo)


def ms(moment):
    """Milliseconds since the epoch, the unit `usernotes.format.Note` uses."""
    return int(moment.timestamp() * 1000)


class SafeTextTests(unittest.TestCase):
    """What may reach the terminal."""

    def test_control_characters_are_dropped(self):
        # A note that tries to move the cursor, ring the bell or recolour the
        # rest of the listing comes out as plain text.
        self.assertEqual(safe_text('a\x1b[31mb\x07c\x00d'), 'a[31mbcd')

    def test_whitespace_is_collapsed_like_a_browser_would(self):
        self.assertEqual(safe_text('one\n\ntwo\tthree  four'), 'one two three four')
        self.assertEqual(safe_text('  padded  '), 'padded')

    def test_bidi_and_surrogates_are_dropped(self):
        self.assertEqual(safe_text('abc‮def‎ghi'), 'abcdefghi')
        self.assertEqual(safe_text('broken \ud800text'), 'broken text')

    def test_anything_that_is_not_text_is_empty(self):
        self.assertEqual(safe_text(None), '')
        self.assertEqual(safe_text(42), '')


class ColorIndexTests(unittest.TestCase):
    """CSS colours, as xterm-256 colours."""

    def test_the_names_toolbox_uses(self):
        self.assertEqual(color_index('fuchsia'), 201)
        self.assertEqual(color_index('purple'), 93)
        self.assertEqual(color_index('orange'), 208)
        self.assertEqual(color_index('red'), 196)
        self.assertEqual(color_index('darkred'), 124)
        self.assertEqual(color_index('green'), 34)

    def test_black_is_nudged_so_that_it_can_still_be_seen(self):
        # Toolbox's Bot Ban is black, which on a dark terminal is nothing at
        # all; the listing shows it as dark grey instead.
        self.assertEqual(color_index('black'), 240)

    def test_names_are_matched_loosely(self):
        self.assertEqual(color_index('  Fuchsia '), 201)
        self.assertEqual(color_index('FUCHSIA'), 201)

    def test_hex_colours(self):
        self.assertEqual(color_index('#ff00ff'), 201)
        self.assertEqual(color_index('#f0f'), 201)
        self.assertEqual(color_index('#00ff00'), 46)

    def test_greys_come_from_the_grey_ramp_not_the_cube(self):
        # The cube has no grey between its steps, so #808080 would otherwise
        # be shown as a much darker or lighter colour.
        self.assertEqual(color_index('#808080'), 244)
        self.assertEqual(color_index('rgb(50,50,50)'), 236)

    def test_rgb_colours(self):
        self.assertEqual(color_index('rgb(255, 0, 0)'), 196)
        self.assertEqual(color_index('rgba(255, 0, 0, 0.5)'), 196)

    def test_colours_that_mean_no_colour(self):
        for value in ('', '   ', 'transparent', 'inherit', None, 17,
                      'not a colour', 'chartreuse'):
            self.assertIsNone(color_index(value), value)


class RelativeTimeTests(unittest.TestCase):
    """``jquery.timeago``'s wording, which is what toolbox shows."""

    def setUp(self):
        self.now = at(2026, 1, 1, 12, 0, 0)

    def ago(self, **kwargs):
        return relative_time(self.now - datetime.timedelta(**kwargs), self.now)

    def test_the_phrases_match_timeago(self):
        self.assertEqual(relative_time(self.now - datetime.timedelta(seconds=10),
                                       self.now), 'less than a minute ago')
        self.assertEqual(self.ago(seconds=60), 'about a minute ago')
        self.assertEqual(self.ago(minutes=5), '5 minutes ago')
        self.assertEqual(self.ago(minutes=70), 'about an hour ago')
        self.assertEqual(self.ago(hours=3), '3 hours ago')
        self.assertEqual(self.ago(hours=30), 'a day ago')
        self.assertEqual(self.ago(days=3), '3 days ago')
        self.assertEqual(self.ago(days=40), 'about a month ago')
        self.assertEqual(self.ago(days=150), '5 months ago')
        self.assertEqual(self.ago(days=400), 'about a year ago')
        self.assertEqual(self.ago(days=1095), '3 years ago')

    def test_halves_round_up_as_javascript_does(self):
        # 150 minutes is 2.5 hours: Math.round says 3, Python's round says 2.
        self.assertEqual(self.ago(minutes=150), '3 hours ago')

    def test_a_time_in_the_future_reads_forwards(self):
        # timeago would say "ago" here too, which is nonsense; this says "in".
        self.assertEqual(relative_time(self.now + datetime.timedelta(days=2),
                                       self.now), 'in 2 days')


class FormatTimeTests(unittest.TestCase):
    """The absolute time printed next to the relative one."""

    def test_utc_by_default(self):
        self.assertEqual(format_time(at(2020, 1, 2, 3, 4, 5), UTC, zone=True),
                         '2020-01-02 03:04:05 UTC')

    def test_the_zone_can_be_left_off(self):
        self.assertEqual(format_time(at(2020, 1, 2, 3, 4, 5), UTC),
                         '2020-01-02 03:04:05')

    def test_another_zone(self):
        east = datetime.timezone(datetime.timedelta(hours=-5), 'EST')
        self.assertEqual(format_time(at(2020, 1, 2, 3, 4, 5), east, zone=True),
                         '2020-01-01 22:04:05 EST')

    def test_a_zone_of_none_means_the_local_one(self):
        # Only the shape is checked: the machine running the tests can be set
        # to anything.
        text = format_time(at(2020, 1, 2, 3, 4, 5), None)
        self.assertRegex(text, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')


class StyleTests(unittest.TestCase):

    def test_off_is_a_no_op(self):
        plain = Style()
        self.assertFalse(plain)
        self.assertEqual(plain.bold('x'), 'x')
        self.assertEqual(plain.color(196, 'x'), 'x')

    def test_on_wraps_in_escapes(self):
        styled = Style(True)
        self.assertTrue(styled)
        self.assertEqual(styled.bold('x'), '\x1b[1mx\x1b[0m')
        self.assertEqual(styled.color(196, 'x'), '\x1b[38;5;196mx\x1b[0m')

    def test_no_colour_leaves_the_text_alone(self):
        self.assertEqual(Style(True).color(None, 'x'), 'x')

    def test_empty_text_is_not_wrapped(self):
        self.assertEqual(Style(True).bold(''), '')


class ColorEnabledTests(unittest.TestCase):

    def test_the_modes(self):
        self.assertTrue(color_enabled('always'))
        self.assertFalse(color_enabled('never'))
        with self.assertRaises(ValueError):
            color_enabled('sometimes')

    def test_auto_follows_the_stream(self):
        self.assertFalse(color_enabled('auto', io.StringIO()))
        terminal = mock.Mock()
        terminal.isatty.return_value = True
        self.assertTrue(color_enabled('auto', terminal))

    def test_auto_honours_no_color_and_a_dumb_terminal(self):
        terminal = mock.Mock()
        terminal.isatty.return_value = True
        with mock.patch.dict(os.environ, {'NO_COLOR': '1'}):
            self.assertFalse(color_enabled('auto', terminal))
        with mock.patch.dict(os.environ, {'TERM': 'dumb'}):
            self.assertFalse(color_enabled('auto', terminal))

    def test_the_environment_cannot_turn_colour_on_that_was_asked_for(self):
        with mock.patch.dict(os.environ, {'TERM': 'dumb', 'NO_COLOR': '1'}):
            self.assertTrue(color_enabled('always'))

    def test_the_modes_are_the_ones_the_cli_offers(self):
        self.assertEqual(COLOR_MODES, ('auto', 'always', 'never'))


class RenderTests(unittest.TestCase):
    """The listing itself."""

    now = at(2026, 1, 1, 12, 0, 0)

    def setUp(self):
        self.notes = [
            Note(text='second note', time=ms(at(2025, 12, 30, 12, 0, 0)),
                 mod='creesch', type='ban',
                 link='https://www.reddit.com/r/test/comments/abc/'),
            Note(text='first note', time=ms(at(2025, 1, 1, 12, 0, 0)),
                 mod='othermod', type='spamwatch'),
        ]

    def render(self, notes=None, **kwargs):
        kwargs.setdefault('subreddit', 'test')
        kwargs.setdefault('note_types', DEFAULT_NOTE_TYPES)
        kwargs.setdefault('now', self.now)
        return render_user_notes('alice', self.notes if notes is None else notes,
                                 **kwargs)

    def test_the_table_is_toolbox_s(self):
        # Author and date in one column, the type and the note in the other,
        # with the link under the note - the shape of toolbox's user dialog.
        width = len('about a year ago (2025-01-01 12:00:00 UTC)')
        self.assertEqual(self.render().splitlines(), [
            'User Notes - /u/alice',
            '/r/test - 2 notes',
            '',
            f'{"Author":<{width}}  Note',
            f'{"/u/creesch":<{width}}  [Ban] second note',
            f'{"2 days ago (2025-12-30 12:00:00 UTC)":<{width}}  '
            'https://www.reddit.com/r/test/comments/abc/',
            f'{"/u/othermod":<{width}}  [Spam Watch] first note',
            'about a year ago (2025-01-01 12:00:00 UTC)',
        ])

    def test_types_are_coloured_the_way_the_subreddit_configures_them(self):
        listing = self.render(style=Style(True), subreddit='test')
        self.assertIn('\x1b[38;5;196m[Ban]\x1b[0m', listing)          # red
        self.assertIn('\x1b[38;5;201m[Spam Watch]\x1b[0m', listing)   # fuchsia
        self.assertIn('\x1b[38;5;248m/u/creesch\x1b[0m', listing)     # grey
        self.assertIn('\x1b[1mUser Notes - /u/alice\x1b[0m', listing)

    def test_the_note_text_takes_the_type_colour_too(self):
        listing = self.render(style=Style(True), subreddit='test')
        self.assertIn('\x1b[38;5;196m[Ban]\x1b[0m \x1b[38;5;196msecond note\x1b[0m',
                      listing)
        self.assertIn('\x1b[38;5;201m[Spam Watch]\x1b[0m '
                      '\x1b[38;5;201mfirst note\x1b[0m', listing)

    def test_a_note_with_no_type_is_not_coloured(self):
        note = Note(text='just a note', time=ms(self.now), mod='creesch')
        lines = self.render(notes=[note], style=Style(True)).splitlines()
        self.assertTrue(lines[4].endswith('  just a note'), lines[4])
        # The author's grey is the only colour on the row: a note with no type
        # has none to take, so it stays as plain as the uncoloured listing.
        self.assertEqual(lines[4].count('\x1b[38;5;'), 1)

    def test_a_type_the_config_lost_is_not_coloured_either(self):
        note = Note(text='odd', time=ms(self.now), mod='creesch', type='oldtype')
        lines = self.render(notes=[note], style=Style(True)).splitlines()
        self.assertTrue(lines[4].endswith('  [oldtype] odd'), lines[4])
        self.assertEqual(lines[4].count('\x1b[38;5;'), 1)

    def test_a_coloured_listing_still_lines_up(self):
        plain = self.render().splitlines()
        coloured = self.render(style=Style(True)).splitlines()
        self.assertEqual(len(plain), len(coloured))
        for wanted, line in zip(plain, coloured):
            self.assertEqual(self.without_escapes(line), wanted)

    @staticmethod
    def without_escapes(line):
        while '\x1b[' in line:
            start = line.index('\x1b[')
            line = line[:start] + line[line.index('m', start) + 1:]
        return line

    def test_ordering_is_the_caller_s(self):
        # Newest first is `usernotes.model`'s job; the listing must not sort.
        listing = self.render(notes=list(reversed(self.notes)))
        self.assertLess(listing.index('first note'), listing.index('second note'))

    def test_one_note_is_not_one_notes(self):
        listing = self.render(notes=self.notes[:1])
        self.assertIn('/r/test - 1 note\n', listing)

    def test_a_note_with_no_type_shows_no_tag(self):
        note = Note(text='just a note', time=ms(self.now), mod='creesch')
        listing = self.render(notes=[note])
        self.assertIn('just a note', listing)
        self.assertNotIn('[', listing)

    def test_a_type_the_config_lost_is_shown_by_its_key(self):
        # Toolbox would show nothing at all here, which would be hiding stored
        # data from the one tool that can still show it.
        note = Note(text='odd', time=ms(self.now), mod='creesch', type='oldtype')
        self.assertIn('[oldtype] odd', self.render(notes=[note]))

    def test_a_note_without_a_time_says_so(self):
        note = Note(text='imported', time=0, mod='creesch', type='ban')
        listing = self.render(notes=[note])
        self.assertIn('\nunknown date', listing)
        self.assertNotIn('1970', listing)

    def test_a_note_without_an_author_says_so(self):
        note = Note(text='anonymous', time=ms(self.now), type='ban')
        self.assertIn('\nunknown ', self.render(notes=[note]))

    def test_note_text_is_escaped(self):
        note = Note(text='look \x1b[2Jcleared', time=ms(self.now), mod='creesch')
        self.assertIn('look [2Jcleared', self.render(notes=[note]))

    def test_a_moderator_name_is_escaped_too(self):
        # The escape goes; the text that was hiding behind it stays, so the
        # name is still recognisable rather than silently different.
        note = Note(text='x', time=ms(self.now), mod='bo\x1b[31mgu s')
        self.assertIn('bo[31mgu s', self.render(notes=[note]))
        self.assertNotIn('\x1b', self.render(notes=[note]))

    def test_html_entities_read_the_way_toolbox_shows_them(self):
        note = Note(text='spam &amp; abuse', time=ms(self.now), mod='creesch')
        self.assertIn('spam & abuse', self.render(notes=[note]))

    def test_a_user_with_no_notes(self):
        self.assertEqual(self.render(notes=[]).splitlines(),
                         ['User Notes - /u/alice', '/r/test - no notes for /u/alice'])

    def test_a_subreddit_with_no_page(self):
        listing = self.render(notes=[], has_page=False)
        self.assertEqual(listing.splitlines(),
                         ['User Notes - /u/alice',
                          '/r/test - no usernotes page yet'])

    def test_times_can_be_shown_in_the_local_zone(self):
        listing = self.render(tz=None)
        self.assertRegex(listing, r'\(2025-12-30 \d{2}:00:00( \S+)?\)')

    def test_the_username_is_escaped(self):
        listing = render_user_notes('a\x1b[31mb', [], subreddit='test')
        self.assertIn('User Notes - /u/a[31mb', listing)


class ParseTimeTests(unittest.TestCase):
    """Reading back a timestamp a listing printed."""

    def test_it_reads_back_what_format_time_writes(self):
        for moment in (at(2019, 5, 31, 13, 52, 30), at(2026, 11, 7, 7, 23, 0),
                       at(1999, 12, 31, 23, 59, 59)):
            with self.subTest(moment=moment):
                self.assertEqual(parse_time(format_time(moment, UTC, zone=True)),
                                 moment)

    def test_a_time_with_no_zone_is_utc(self):
        self.assertEqual(parse_time('2019-05-31 13:52:30'), at(2019, 5, 31, 13, 52, 30))

    def test_the_zone_may_be_spelled_out(self):
        for zone in ('UTC', 'utc', 'GMT'):
            with self.subTest(zone=zone):
                self.assertEqual(parse_time(f'2019-05-31 13:52:30 {zone}'),
                                 at(2019, 5, 31, 13, 52, 30))

    def test_the_iso_8601_spellings(self):
        self.assertEqual(parse_time('2019-05-31T13:52:30Z'), at(2019, 5, 31, 13, 52, 30))
        self.assertEqual(parse_time('2019-05-31T13:52:30+00:00'),
                         at(2019, 5, 31, 13, 52, 30))
        self.assertEqual(parse_time('2019-05-31T15:52:30+02:00'),
                         at(2019, 5, 31, 13, 52, 30))

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(parse_time('  2019-05-31 13:52:30 UTC\n'),
                         at(2019, 5, 31, 13, 52, 30))

    def test_a_zone_that_cannot_be_resolved_is_refused(self):
        # Two different instants depending on who reads it, so not guessed at.
        with self.assertRaises(ValueError) as caught:
            parse_time('2019-05-31 13:52:30 EST')
        self.assertIn('UTC', str(caught.exception))

    def test_things_that_are_not_timestamps(self):
        for value in ('', '   ', 'yesterday', '13:52:30', '2019-13-45 00:00:00',
                      None, 42):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_time(value)


class NoteLineTests(unittest.TestCase):
    """One note on one line, as toolbox's manager writes it."""

    now = at(2026, 1, 1, 12, 0, 0)

    def setUp(self):
        self.note = Note(text='repeated abuse', time=ms(at(2025, 12, 30, 12, 0, 0)),
                         mod='creesch', type='ban')

    def render(self, note=None, **kwargs):
        kwargs.setdefault('note_types', DEFAULT_NOTE_TYPES)
        kwargs.setdefault('now', self.now)
        return render_note(self.note if note is None else note, **kwargs)

    def test_the_line(self):
        self.assertEqual(
            self.render(),
            '[Ban] repeated abuse - by /u/creesch - 2 days ago '
            '(2025-12-30 12:00:00 UTC)')

    def test_it_has_no_trailing_newline(self):
        self.assertNotIn('\n', self.render())

    def test_a_note_with_nothing_but_text(self):
        self.assertEqual(self.render(Note(text='odd')),
                         'odd - by unknown - unknown date')

    def test_it_is_coloured_the_way_the_listing_is(self):
        listing = self.render(style=Style(True))
        self.assertIn('\x1b[38;5;196m[Ban]\x1b[0m \x1b[38;5;196mrepeated abuse\x1b[0m',
                      listing)
        self.assertIn('\x1b[38;5;248mby /u/creesch\x1b[0m', listing)

    def test_it_is_escaped(self):
        note = Note(text='look \x1b[2Jout', time=ms(self.now), mod='bo\x1b[31b')
        listing = self.render(note)
        self.assertEqual(listing, 'look [2Jout - by /u/bo[31b - less than a minute '
                                  'ago (2026-01-01 12:00:00 UTC)')


class SearchListingTests(unittest.TestCase):
    """The listing ``usernotes search`` prints."""

    now = at(2026, 1, 1, 12, 0, 0)

    def setUp(self):
        self.matches = [
            ('alice', Note(text='second note', time=ms(at(2025, 12, 30, 12, 0, 0)),
                           mod='creesch', type='ban',
                           link='https://www.reddit.com/r/test/comments/abc/')),
            ('bobthespammer', Note(text='spam again',
                                   time=ms(at(2025, 1, 1, 12, 0, 0)),
                                   mod='othermod', type='spamwatch')),
        ]

    def render(self, matches=None, **kwargs):
        kwargs.setdefault('subreddit', 'test')
        kwargs.setdefault('note_types', DEFAULT_NOTE_TYPES)
        kwargs.setdefault('now', self.now)
        return render_search_results('spam', self.matches if matches is None
                                     else matches, **kwargs)

    def test_the_listing(self):
        width = len('bobthespammer')
        self.assertEqual(self.render().splitlines(), [
            "Usernotes matching 'spam' - /r/test",
            '2 notes for 2 users',
            '',
            f'{"User":<{width}}  Note',
            f'{"alice":<{width}}  [Ban] second note - by /u/creesch - '
            '2 days ago (2025-12-30 12:00:00 UTC)',
            ' ' * (width + 2) + 'https://www.reddit.com/r/test/comments/abc/',
            f'{"bobthespammer":<{width}}  [Spam Watch] spam again - by /u/othermod - '
            'about a year ago (2025-01-01 12:00:00 UTC)',
        ])

    def test_one_note_for_one_user(self):
        listing = self.render(matches=self.matches[:1])
        self.assertIn('1 note for 1 user\n', listing)

    def test_the_note_text_takes_the_type_colour(self):
        listing = self.render(style=Style(True))
        self.assertIn('\x1b[38;5;196m[Ban]\x1b[0m \x1b[38;5;196msecond note\x1b[0m',
                      listing)
        self.assertIn('\x1b[1malice\x1b[0m', listing)     # the user stands out
        self.assertIn('\x1b[38;5;33mhttps://www.reddit.com/r/test/comments/abc/',
                      listing)                            # and the link is a link

    def test_a_coloured_listing_still_lines_up(self):
        plain = self.render().splitlines()
        coloured = self.render(style=Style(True)).splitlines()
        self.assertEqual(len(plain), len(coloured))
        for wanted, line in zip(plain, coloured):
            self.assertEqual(RenderTests.without_escapes(line), wanted)

    def test_nothing_matched(self):
        self.assertEqual(self.render(matches=[]).splitlines(),
                         ["Usernotes matching 'spam' - /r/test", 'no notes match'])

    def test_a_subreddit_with_no_page(self):
        listing = self.render(matches=[], has_page=False)
        self.assertEqual(listing.splitlines(),
                         ["Usernotes matching 'spam' - /r/test",
                          '/r/test - no usernotes page yet'])

    def test_the_pattern_is_escaped_and_quoted(self):
        listing = render_search_results('spam\x1b[31m', [], subreddit='test')
        self.assertIn("Usernotes matching 'spam[31m'", listing)
        self.assertNotIn('\x1b', listing)


class NoteTypeListingTests(unittest.TestCase):
    """The list of flags a subreddit offers, for ``usernotes flags``."""

    def test_every_flag_is_listed_with_the_key_to_pass(self):
        lines = render_note_types(DEFAULT_NOTE_TYPES, subreddit='test').splitlines()
        self.assertEqual(lines[0], 'Usernote flags - /r/test')
        self.assertEqual(lines[1], '')
        self.assertEqual(lines[2].split(), ['Flag', 'Key'])
        self.assertEqual(len(lines), 3 + len(DEFAULT_NOTE_TYPES))
        for note_type in DEFAULT_NOTE_TYPES:
            self.assertTrue(any(line.endswith(note_type.key) for line in lines[3:]),
                            f'{note_type.key} is missing')

    def test_the_columns_line_up(self):
        lines = render_note_types(DEFAULT_NOTE_TYPES, subreddit='test').splitlines()
        starts = {len(line) - len(line.split()[-1]) for line in lines[2:]}
        self.assertEqual(len(starts), 1, lines)

    def test_each_flag_is_shown_in_the_colour_given_to_it(self):
        listing = render_note_types(DEFAULT_NOTE_TYPES, subreddit='test',
                                    style=Style(True))
        self.assertIn('\x1b[38;5;34mGood Contributor\x1b[0m', listing)   # green
        self.assertIn('\x1b[38;5;201mSpam Watch\x1b[0m', listing)        # fuchsia
        self.assertIn('\x1b[38;5;248mgooduser\x1b[0m', listing)          # grey key

    def test_a_flag_name_is_escaped_like_everything_else(self):
        types = (NoteType(key='odd', text='o\x1b[31mdd', color='fuchsia'),)
        listing = render_note_types(types, subreddit='test')
        self.assertIn('o[31mdd', listing)
        self.assertNotIn('\x1b', listing)

    def test_a_subreddit_with_no_flags_at_all(self):
        self.assertEqual(render_note_types((), subreddit='test').splitlines(),
                         ['Usernote flags - /r/test', '/r/test - no flags'])


if __name__ == '__main__':
    unittest.main()
