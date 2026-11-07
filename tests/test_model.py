"""Tests for :mod:`usernotes.model`, the in-memory notes collection.

No network: this layer is pure bookkeeping, and the cases that matter are the
awkward ones toolbox also has to cope with - usernames that were stored with
the wrong case, notes whose timestamp is only second-accurate, and users left
behind with no notes.
"""

import unittest

from usernotes import model
from usernotes.errors import NoteNotFoundError, UserNotFoundError
from usernotes.format import Note


def note(text, time=0, **kwargs):
    return Note(text=text, time=time, **kwargs)


class NormalizeUsernameTests(unittest.TestCase):

    def test_accepts_what_moderators_type(self):
        self.assertEqual(model.normalize_username('alice'), 'alice')
        self.assertEqual(model.normalize_username('Alice'), 'Alice')
        self.assertEqual(model.normalize_username('  alice  '), 'alice')
        self.assertEqual(model.normalize_username('u/alice'), 'alice')
        self.assertEqual(model.normalize_username('/u/Alice'), 'Alice')
        self.assertEqual(model.normalize_username('  /u/alice '), 'alice')
        # Case of the prefix itself is not significant, and reddit usernames
        # may contain underscores and hyphens.
        self.assertEqual(model.normalize_username('U/alice'), 'alice')
        self.assertEqual(model.normalize_username('u/some_mod-1'), 'some_mod-1')

    def test_rejects_what_reddit_would_not_accept(self):
        for value in ('', '  ', 'a b', 'x' * 21, 'alice!', 'u/', 'u/u/alice'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    model.normalize_username(value)

    def test_rejects_things_that_are_not_text(self):
        for value in (5, None, ['alice']):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    model.normalize_username(value)


class UserNotesTests(unittest.TestCase):

    def test_sorts_newest_first_and_keeps_ties_in_stored_order(self):
        user = model.UserNotes('alice', [note('old', 1000), note('new', 3000), note('same', 3000)])
        self.assertEqual([n.text for n in user.notes], ['new', 'same', 'old'])

    def test_add_prepends(self):
        user = model.UserNotes('alice', [note('existing', 1000)])
        user.add(note('fresh', 2000))
        self.assertEqual([n.text for n in user.notes], ['fresh', 'existing'])

    def test_find_matches_milliseconds_or_whole_seconds(self):
        # The stored format keeps whole seconds, which is what toolbox matches
        # on; a caller that holds the full millisecond value should work too.
        user = model.UserNotes('alice', [note('x', 1559310750123)])
        self.assertEqual(user.find(1559310750123).text, 'x')
        self.assertEqual(user.find(1559310750000).text, 'x')
        self.assertIsNone(user.find(1559310751000))
        self.assertIsNone(user.find(0))

    def test_find_takes_the_newest_of_an_ambiguous_second(self):
        user = model.UserNotes('alice', [note('earlier', 1559310750100),
                                         note('later', 1559310750900)])
        self.assertEqual(user.find(1559310750000).text, 'later')
        self.assertEqual(len(user.find_all(1559310750000)), 2)

    def test_require_explains_what_would_have_worked(self):
        # The times come back as a listing renders them, not as the numbers the
        # store works in, so that they can be checked against a note.
        user = model.UserNotes('alice', [note('x', 1559310750000)])
        with self.assertRaises(NoteNotFoundError) as caught:
            user.require(9000)
        self.assertIn('2019-05-31 13:52:30 UTC', str(caught.exception))

        empty = model.UserNotes('bob')
        with self.assertRaises(NoteNotFoundError) as caught:
            empty.require(1)
        self.assertIn('no notes', str(caught.exception))

    def test_remove(self):
        user = model.UserNotes('alice', [note('x', 5000)])
        self.assertEqual(user.remove(5000).text, 'x')
        self.assertEqual(len(user.notes), 0)
        with self.assertRaises(NoteNotFoundError):
            user.remove(5000)

    def test_times(self):
        user = model.UserNotes('alice', [note('x', 1000), note('y', 9000)])
        self.assertEqual(user.times(), (9000, 1000))

    def test_as_dict(self):
        user = model.UserNotes('alice', [note('x', 5000, mod='mod', type='ban')],
                               aliases=('ALICE',))
        self.assertEqual(user.as_dict(), {
            'name': 'alice',
            'aliases': ['ALICE'],
            'notes': [{'text': 'x', 'time': 5000,
                       'time_iso': '1970-01-01T00:00:05+00:00',
                       'mod': 'mod', 'type': 'ban', 'link': None}],
        })


class NotesTests(unittest.TestCase):

    def test_lookup_ignores_case(self):
        notes = model.Notes({'Alice': [note('x', 1)]})
        self.assertEqual(notes.find('alice').name, 'Alice')
        self.assertEqual(notes.find('u/ALICE').name, 'Alice')
        self.assertIn('alice', notes)
        self.assertNotIn('bob', notes)

    def test_a_lower_cased_duplicate_is_merged_and_remembered(self):
        # A third-party writer stored `alice` next to `Alice`; toolbox merges
        # them on read and writes them back under one spelling, and so does
        # this - the duplicate key is what `aliases` records.
        notes = model.Notes({'Alice': [note('proper', 2000)], 'alice': [note('legacy', 1000)]})
        user = notes.find('Alice')
        self.assertEqual(user.name, 'Alice')
        self.assertEqual(user.aliases, ('alice',))
        self.assertEqual([n.text for n in user.notes], ['proper', 'legacy'])
        self.assertEqual(notes.usernames(), ('Alice',))
        self.assertEqual(list(notes.as_dict()), ['Alice'])
        self.assertEqual([n.text for n in notes.as_dict()['Alice']], ['proper', 'legacy'])

    def test_add_uses_the_spelling_already_stored(self):
        notes = model.Notes({'Alice': [note('x', 1)]})
        notes.add('aLiCe', note('y', 2))
        self.assertEqual(notes.usernames(), ('Alice',))
        self.assertEqual([n.text for n in notes.find('alice').notes], ['y', 'x'])

    def test_add_creates_a_new_user(self):
        notes = model.Notes()
        notes.add('u/bob', note('first', 1))
        self.assertEqual(notes.usernames(), ('bob',))
        self.assertEqual(notes.find('BOB').notes[0].text, 'first')

    def test_remove_note_drops_a_user_who_has_nothing_left(self):
        notes = model.Notes({'alice': [note('only', 1)],
                             'bob': [note('x', 1), note('y', 2)]})
        notes.remove_note('alice', 1)
        self.assertEqual(notes.usernames(), ('bob',))
        notes.remove_note('bob', 1)
        self.assertEqual(notes.usernames(), ('bob',))
        self.assertEqual(len(notes.find('bob')), 1)

    def test_remove_note_for_an_unknown_user(self):
        with self.assertRaises(UserNotFoundError):
            model.Notes().remove_note('nobody', 1)

    def test_remove_user(self):
        notes = model.Notes({'alice': [note('x', 1), note('y', 2)]})
        self.assertEqual(notes.remove_user('ALICE'), 2)
        self.assertNotIn('alice', notes)
        with self.assertRaises(UserNotFoundError):
            notes.remove_user('alice')

    def test_prune_drops_empty_entries_only(self):
        notes = model.Notes({'empty': [], 'kept': [note('x', 1)]})
        self.assertEqual(notes.prune(), 1)
        self.assertEqual(notes.usernames(), ('kept',))
        self.assertEqual(notes.prune(), 0)

    def test_require_reports_a_missing_user(self):
        with self.assertRaises(UserNotFoundError) as caught:
            model.Notes().require('nobody')
        self.assertIn('nobody', str(caught.exception))

    def test_counts_and_iteration(self):
        notes = model.Notes({'a': [note('x', 1)], 'b': [note('y', 1), note('z', 2)]})
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes.note_count(), 3)
        self.assertEqual(list(notes), ['a', 'b'])
        self.assertEqual([name for name, _ in notes.items()], ['a', 'b'])
        self.assertEqual(notes.find('b').times(), (2, 1))

    def test_as_json(self):
        data = model.Notes({'alice': [note('x', 5000, mod='mod')]}).as_json()
        self.assertEqual(list(data), ['alice'])
        self.assertEqual(data['alice']['notes'][0]['text'], 'x')
        self.assertEqual(data['alice']['name'], 'alice')


if __name__ == '__main__':
    unittest.main()
