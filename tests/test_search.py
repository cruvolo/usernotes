# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Tests for :mod:`usernotes.search`, the matching behind ``usernotes search``.

Nothing here is a network test: a search is a predicate over notes that have
already been read, so the tests hand it notes and check what comes back.
"""

import unittest

from usernotes.format import DEFAULT_NOTE_TYPES, Note
from usernotes.search import fields, matcher, search

#: 2019-05-31 13:52:30 UTC and the second after it, and a day after that.
FIRST = 1559310750000
SECOND = 1559310751000
DAY = 86400000


def note(text='a note', time=FIRST, mod='creesch', type=None, link=''):
    return Note(text=text, time=time, mod=mod, type=type, link=link)


class FieldsTests(unittest.TestCase):
    """What a search key is matched against."""

    def test_the_username_is_there_in_both_spellings(self):
        values = fields('alice', note())
        self.assertIn('alice', values)
        self.assertIn('/u/alice', values)

    def test_the_note_text_is_there_as_a_listing_shows_it(self):
        # Entities decoded and whitespace collapsed, so that what is searched
        # for can be what is on the screen.
        values = fields('alice', note(text='spam &amp; abuse\nsecond line'))
        self.assertIn('spam & abuse second line', values)

    def test_the_flag_is_there_by_key_and_by_the_name_shown(self):
        values = fields('alice', note(type='spamwatch'), DEFAULT_NOTE_TYPES)
        self.assertIn('spamwatch', values)
        self.assertIn('Spam Watch', values)

    def test_a_flag_the_config_lost_is_there_by_its_key_only(self):
        values = fields('alice', note(type='oldtype'), DEFAULT_NOTE_TYPES)
        self.assertIn('oldtype', values)
        for known in DEFAULT_NOTE_TYPES:
            self.assertNotIn(known.text, values)

    def test_the_time_is_there_in_both_spellings(self):
        values = fields('alice', note(time=FIRST))
        self.assertIn('2019-05-31 13:52:30 UTC', values)
        self.assertIn('2019-05-31T13:52:30+00:00', values)

    def test_a_note_with_no_time_is_not_matched_by_1970(self):
        values = fields('alice', note(time=0))
        self.assertFalse([value for value in values if '1970' in value], values)


class MatcherTests(unittest.TestCase):
    """The predicate itself: substrings, regular expressions, case."""

    def test_a_substring_anywhere_in_a_field(self):
        matches = matcher('pam')
        self.assertTrue(matches(('spam',)))
        self.assertFalse(matches(('eggs',)))

    def test_case_is_ignored_unless_asked_otherwise(self):
        self.assertTrue(matcher('SPAM')(('spam watch',)))
        self.assertFalse(matcher('SPAM', ignore_case=False)(('spam watch',)))

    def test_a_regular_expression(self):
        matches = matcher(r'^spam\b', regex=True)
        self.assertTrue(matches(('spam watch',)))
        self.assertFalse(matches(('eggs and spam',)))

    def test_a_regular_expression_ignores_case_too(self):
        self.assertTrue(matcher('^SPAM', regex=True)(('spam',)))
        self.assertFalse(matcher('^SPAM', regex=True, ignore_case=False)(('spam',)))

    def test_a_pattern_that_does_not_compile(self):
        with self.assertRaises(ValueError) as caught:
            matcher('spam(', regex=True)
        self.assertIn('not a regular expression', str(caught.exception))

    def test_a_pattern_that_is_not_text(self):
        with self.assertRaises(ValueError):
            matcher(None)


class SearchTests(unittest.TestCase):
    """Matching a pattern against a subreddit's notes."""

    def setUp(self):
        self.users = [
            ('alice', [note(text='spam watch, second time', type='spamwatch'),
                       note(text='was polite about it', time=SECOND, mod='othermod')]),
            ('bobthespammer', [note(text='banned for spam', time=SECOND + DAY,
                                    type='ban')]),
        ]

    def find(self, pattern, **kwargs):
        note_types = kwargs.pop('note_types', DEFAULT_NOTE_TYPES)
        return search(self.users, matcher(pattern, **kwargs), note_types)

    def texts(self, found):
        return [each.text for _, each in found]

    def test_by_the_note_text(self):
        self.assertEqual(self.texts(self.find('polite')), ['was polite about it'])

    def test_by_the_username(self):
        self.assertEqual(self.texts(self.find('bob')),
                         ['banned for spam'])

    def test_by_the_flag_key_or_the_name_shown(self):
        for pattern in ('spamwatch', 'Spam Watch', 'spam watch'):
            with self.subTest(pattern=pattern):
                found = self.find(pattern)
                self.assertIn('spam watch, second time', self.texts(found))

    def test_by_the_timestamp(self):
        # The time as a listing prints it, and as ISO 8601 - and the date alone
        # finds the notes of that day.
        self.assertEqual(self.texts(self.find('2019-05-31 13:52:31')),
                         ['was polite about it'])
        self.assertEqual(self.texts(self.find('2019-05-31T13:52:30')),
                         ['spam watch, second time'])
        self.assertEqual(self.texts(self.find('2019-06-01')), ['banned for spam'])

    def test_case_is_ignored_by_default(self):
        self.assertEqual(len(self.find('SPAM')), 2)
        self.assertEqual(self.find('SPAM', ignore_case=False), [])

    def test_a_regular_expression(self):
        self.assertEqual(self.texts(self.find(r'^banned', regex=True)),
                         ['banned for spam'])
        self.assertEqual(self.texts(self.find(r'second', regex=True)),
                         ['spam watch, second time'])

    def test_a_note_is_shown_once_however_many_fields_match(self):
        # 'spam' is in alice's text, her flag's key and its name.
        self.assertEqual(self.texts(self.find('spam')),
                         ['spam watch, second time', 'banned for spam'])

    def test_the_order_is_the_order_the_notes_came_in(self):
        self.assertEqual([username for username, _ in self.find('spam')],
                         ['alice', 'bobthespammer'])

    def test_nothing_matching(self):
        self.assertEqual(self.find('nothing here at all'), [])

    def test_a_user_with_no_notes(self):
        self.assertEqual(search([('nobody', [])], matcher('spam')), [])

    def test_a_flag_the_config_lost_is_still_findable(self):
        users = [('alice', [note(text='odd', type='oldtype')])]
        self.assertEqual(len(search(users, matcher('oldtype'), ())), 1)


if __name__ == '__main__':
    unittest.main()
