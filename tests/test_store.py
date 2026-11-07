"""Tests for :mod:`usernotes.store`, the whole stack end to end.

Each test drives a `UsernotesStore` against `tests.fake_reddit`, so a note is
added, saved, and read back out of the wiki page text the way a moderator's
toolbox would see it.  Nothing here talks to the real Reddit.
"""

import json
import os
import shutil
import tempfile
import unittest

from tests.fake_reddit import FakeReddit
from usernotes import format as fmt
from usernotes.api import RedditWikiClient
from usernotes.errors import (
    InvalidPageError,
    NoWikiPageError,
    NoteNotFoundError,
    PageFullError,
    StaleStoreError,
    UnknownNoteTypeError,
    UnsupportedSchemaError,
    UserNotFoundError,
)
from usernotes.store import Backup, UsernotesStore, normalize_subreddit

MOD = 'moduser'
MODHASH = 'modhash-abc'

#: A page as toolbox would have written it: one note by `moduser` on alice.
def v6_page(users=None, constants=None):
    users = users if users is not None else {
        'alice': {'ns': [{'t': 1559310750, 'n': 'first note', 'm': 0, 'w': 0, 'l': ''}]}}
    constants = constants if constants is not None else {'users': [MOD], 'warnings': ['ban']}
    return json.dumps({'ver': 6, 'constants': constants, 'blob': fmt.compress_blob(users)})


class StoreTestCase(unittest.TestCase):
    """Shared setup: a fake Reddit with a usernotes page, and a store."""

    pages = {('test', 'usernotes'): None}      # None -> built by `page_content`

    def setUp(self):
        content = self.page_content()
        self.reddit = FakeReddit(cookie='reddit_session=abc',
                                 pages={} if content is None else {('test', 'usernotes'): content})
        self.reddit.start()
        self.addCleanup(self.reddit.stop)
        self.store = self.make_store()
        self.addCleanup(self.store.client.close)

    def page_content(self):
        return v6_page()

    def make_store(self, subreddit='test', **kwargs):
        return UsernotesStore(subreddit, cookies='reddit_session=abc',
                              base_url=self.reddit.base_url, min_interval=0,
                              retry_backoff=0, **kwargs)

    def saved_page(self):
        """The wiki page as it now stands, parsed."""
        return json.loads(self.reddit.page('test'))


class ReadTests(StoreTestCase):

    def test_loads_the_notes(self):
        store = self.store.load()
        self.assertTrue(store.loaded)
        self.assertTrue(store.exists)
        self.assertEqual(store.ver, 6)
        self.assertEqual(store.usernames(), ('alice',))
        self.assertEqual(store.note_count(), 1)
        self.assertEqual(len(store), 1)
        self.assertIn('alice', store)

        note = store.notes('alice')[0]
        self.assertEqual(note.text, 'first note')
        self.assertEqual(note.time, 1559310750000)
        self.assertEqual(note.mod, MOD)
        self.assertEqual(note.type, 'ban')

    def test_read_all(self):
        self.assertEqual(self.store.read_all(),
                         {'alice': (fmt.Note(text='first note', time=1559310750000,
                                             mod=MOD, type='ban', link=''),)})

    def test_reads_once(self):
        self.store.load()
        self.store.load()
        self.store.read_all()
        self.assertEqual(len(self.reddit.requests_to('/r/test/wiki/usernotes.json')), 1)

    def test_lookup_ignores_case_and_the_u_prefix(self):
        self.assertEqual(self.store.find('u/ALICE').name, 'alice')
        self.assertIsNone(self.store.find('bob'))
        self.assertEqual(self.store.notes('nobody'), ())
        with self.assertRaises(UserNotFoundError):
            self.store.require('nobody')

    def test_a_subreddit_with_no_notes_has_no_page_and_is_not_an_error(self):
        store = self.make_store('empty')
        store.load()
        self.assertFalse(store.exists)
        self.assertEqual(store.read_all(), {})
        self.assertEqual(store.note_count(), 0)
        self.assertEqual(store.warnings, ())

    def test_a_damaged_page_reports_warnings_without_failing(self):
        page = json.dumps({'ver': 6, 'constants': {}, 'blob': fmt.compress_blob(
            {'alice': {'ns': ['a note stored as a bare string', {'t': 1, 'n': 'ok'}]}})})
        self.reddit.pages[('test', 'usernotes')] = page
        store = self.make_store()
        store.load()
        # The string kept its text rather than being thrown away, and the
        # damage is reported so the user knows the page is not stock toolbox.
        # Notes are newest first, so the one with no time comes last.
        self.assertEqual([note.text for note in store.notes('alice')],
                         ['ok', 'a note stored as a bare string'])
        self.assertTrue(any('bare string' in warning for warning in store.warnings))

    def test_an_unreadable_page_is_refused(self):
        self.reddit.pages[('test', 'usernotes')] = 'definitely not json'
        with self.assertRaises(InvalidPageError):
            self.make_store().load()

    def test_a_future_schema_is_refused(self):
        self.reddit.pages[('test', 'usernotes')] = json.dumps({'ver': 7, 'blob': 'x'})
        with self.assertRaises(UnsupportedSchemaError):
            self.make_store().load()

    def test_reload_discards_unsaved_changes(self):
        store = self.store.load()
        store.add('bob', 'unsaved')
        store.reload()
        self.assertFalse(store.dirty)
        self.assertIsNone(store.find('bob'))


class NoteTypeTests(StoreTestCase):

    def test_defaults_when_the_config_is_missing(self):
        self.assertEqual([t.key for t in self.store.note_types()],
                         [t.key for t in fmt.DEFAULT_NOTE_TYPES])

    def test_reads_the_subreddit_config(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({
            'usernoteColors': [{'key': 'custom', 'text': 'Custom', 'color': 'pink'}]})
        self.assertEqual([t.key for t in self.store.note_types()], ['custom'])

    def test_a_broken_config_falls_back_to_the_defaults(self):
        self.reddit.pages[('test', 'toolbox')] = 'not json at all'
        self.assertEqual(self.store.note_types(), fmt.DEFAULT_NOTE_TYPES)

    def test_a_config_with_unusable_colors_falls_back(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({'usernoteColors': 'nope'})
        self.assertEqual(self.store.note_types(), fmt.DEFAULT_NOTE_TYPES)

    def test_types_are_read_once(self):
        self.store.note_types()
        self.store.note_types()
        self.assertEqual(len(self.reddit.requests_to('/r/test/wiki/toolbox.json')), 1)


class AddTests(StoreTestCase):

    def test_adds_a_note(self):
        store = self.store.load()
        note = store.add('alice', '  a second note  ', type='Spam Watch',
                         link='https://www.reddit.com/r/test/comments/abc/title/')
        self.assertEqual(note.text, 'a second note')        # trimmed, like toolbox
        self.assertEqual(note.mod, MOD)                     # the signed-in account
        self.assertEqual(note.type, 'spamwatch')            # display text -> key
        self.assertEqual(note.link, 'https://www.reddit.com/r/test/comments/abc/title/')
        self.assertTrue(note.time > 1_500_000_000_000)

        # Newest first, so the store's view matches a moderator's.
        self.assertEqual([n.text for n in store.notes('alice')], ['a second note', 'first note'])

    def test_saves_the_note_to_the_wiki(self):
        store = self.store.load()
        store.add('alice', 'a second note', type='ban')
        self.assertTrue(store.dirty)
        self.assertTrue(store.save())
        self.assertFalse(store.dirty)

        # Read the raw wiki page back the way toolbox would.
        page = self.saved_page()
        self.assertEqual(page['ver'], 6)
        self.assertEqual(page['constants'], {'users': [MOD], 'warnings': ['ban']})
        inner = fmt.decompress_blob(page['blob'])
        self.assertEqual([slot['n'] for slot in inner['alice']['ns']],
                         ['a second note', 'first note'])
        self.assertEqual(inner['alice']['ns'][0]['w'], 0)
        self.assertEqual(inner['alice']['ns'][0]['m'], 0)

    def test_the_revision_reason_says_what_happened(self):
        store = self.store.load()
        store.add('bob', 'a note')
        store.save()
        request = self.reddit.requests_to('/r/test/api/wiki/edit', method='POST')[0]
        self.assertEqual(request.form['reason'],
                         'create new note on user bob via usernotes-cli')

    def test_an_explicit_reason_is_kept(self):
        store = self.store.load()
        store.add('bob', 'a note')
        store.save('adding a note by hand')
        self.assertEqual(self.reddit.requests_to('/r/test/api/wiki/edit')[0].form['reason'],
                         'adding a note by hand via usernotes-cli')

    def test_creates_the_page_when_the_subreddit_has_none(self):
        reddit = FakeReddit(cookie='reddit_session=abc')
        reddit.start()
        self.addCleanup(reddit.stop)
        store = UsernotesStore('fresh', cookies='reddit_session=abc',
                               base_url=reddit.base_url, min_interval=0, retry_backoff=0)
        store.load()
        self.assertFalse(store.exists)
        store.add('alice', 'the very first note', type='ban')
        self.assertTrue(store.save())

        page = json.loads(reddit.page('fresh'))
        self.assertEqual(page['ver'], 6)
        self.assertEqual(list(fmt.decompress_blob(page['blob'])), ['alice'])
        # Creating a page also re-applies its settings, so the notes do not
        # sit in a publicly readable wiki page.
        self.assertEqual(reddit.permlevel, 2)
        self.assertTrue(reddit.listed)

    def test_page_settings_are_re_applied_after_a_save(self):
        self.reddit.permlevel = 0
        self.reddit.listed = False
        store = self.store.load()
        store.add('alice', 'x')
        store.save()
        self.assertEqual(self.reddit.permlevel, 2)
        self.assertTrue(self.reddit.listed)
        settings = self.reddit.requests_to('/r/test/wiki/settings/')[0]
        self.assertEqual(settings.query['page'], 'usernotes')

    def test_settings_can_be_skipped(self):
        self.reddit.permlevel = 0
        store = self.store.load()
        store.add('alice', 'x')
        store.save(update_settings=False)
        self.assertEqual(self.reddit.permlevel, 0)
        self.assertEqual(self.reddit.requests_to('/r/test/wiki/settings/'), [])

    def test_a_failed_settings_call_does_not_fail_the_save(self):
        self.reddit.settings_status = 500
        store = self.store.load()
        store.add('alice', 'x')
        self.assertTrue(store.save())
        self.assertIn('x', json.dumps(fmt.decompress_blob(self.saved_page()['blob'])))

    def test_an_unknown_note_type_is_refused(self):
        store = self.store.load()
        with self.assertRaises(UnknownNoteTypeError) as caught:
            store.add('alice', 'x', type='not a type')
        self.assertIn('spamwatch', str(caught.exception))
        self.assertFalse(store.dirty)

    def test_custom_note_types_from_the_config_are_accepted(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({
            'usernoteColors': [{'key': 'weird', 'text': 'Weird One', 'color': 'teal'}]})
        store = self.store.load()
        store.add('alice', 'x', type='weird one')
        store.save()
        inner = fmt.decompress_blob(self.saved_page()['blob'])
        # The new note's type is pooled first; alice's older note still has
        # its own, so that key is pooled too.
        self.assertEqual(self.saved_page()['constants']['warnings'], ['weird', 'ban'])
        self.assertEqual(inner['alice']['ns'][0]['w'], 0)

    def test_an_explicit_note_type_table_avoids_the_config_read(self):
        store = self.store.load()
        store.add('alice', 'x', type='ban', note_types=fmt.DEFAULT_NOTE_TYPES)
        self.assertEqual(self.reddit.requests_to('/r/test/wiki/toolbox.json'), [])

    def test_author_can_be_given_explicitly(self):
        store = self.store.load()
        note = store.add('alice', 'x', mod='u/SomeoneElse')
        self.assertEqual(note.mod, 'SomeoneElse')
        # ...which saves a request for the account.
        self.assertEqual(self.reddit.requests_to('/api/me.json'), [])

    def test_refuses_an_empty_note(self):
        store = self.store.load()
        for text in ('', '   ', '\n\t'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    store.add('alice', text)
        with self.assertRaises(ValueError):
            store.add('alice', None)
        self.assertFalse(store.dirty)

    def test_refuses_a_bad_username(self):
        with self.assertRaises(ValueError):
            self.store.add('not a username', 'x')

    def test_stores_a_squashed_link_as_a_url(self):
        # Callers that read a note out of an older store and write it back
        # should not have to expand it themselves.
        store = self.store.load()
        note = store.add('alice', 'x', link='l,abc123,def456')
        self.assertEqual(note.link,
                         f'{self.reddit.base_url}/r/test/comments/abc123/-/def456/')
        with self.assertRaises(ValueError):
            store.add('alice', 'y', link='l,')
        with self.assertRaises(ValueError):
            store.add('alice', 'y', link=17)

    def test_notes_can_be_imported_with_their_own_timestamps(self):
        store = self.store.load()
        note = store.add('alice', 'from an importer', time=1559310750123)
        self.assertEqual(note.time, 1559310750123)
        with self.assertRaises(ValueError):
            store.add('alice', 'x', time='soon')
        with self.assertRaises(ValueError):
            store.add('alice', 'x', time=-1)


class EditTests(StoreTestCase):

    def test_updates_a_note(self):
        store = self.store.load()
        note = store.update('alice', 1559310750000, text='reworded', type='spamwatch')
        self.assertEqual(note.text, 'reworded')
        self.assertEqual(note.type, 'spamwatch')
        self.assertEqual(note.mod, MOD)             # untouched
        self.assertEqual(note.time, 1559310750000)  # untouched

        store.save()
        inner = fmt.decompress_blob(self.saved_page()['blob'])
        self.assertEqual(inner['alice']['ns'][0]['n'], 'reworded')
        self.assertEqual(self.saved_page()['constants']['warnings'], ['spamwatch'])

    def test_update_needs_something_to_change(self):
        store = self.store.load()
        with self.assertRaises(ValueError):
            store.update('alice', 1559310750000)

    def test_update_can_clear_a_link(self):
        store = self.store.load()
        store.update('alice', 1559310750000, link='')
        self.assertEqual(store.notes('alice')[0].link, '')

    def test_update_reports_a_note_that_is_not_there(self):
        store = self.store.load()
        with self.assertRaises(NoteNotFoundError):
            store.update('alice', 1, text='x')
        with self.assertRaises(UserNotFoundError):
            store.update('nobody', 1, text='x')

    def test_removes_a_note(self):
        store = self.store.load()
        removed = store.remove('alice', 1559310750000)
        self.assertEqual(removed.text, 'first note')
        # The user goes with their last note, as toolbox does.
        self.assertEqual(store.usernames(), ())
        store.save()
        self.assertEqual(fmt.decompress_blob(self.saved_page()['blob']), {})

    def test_remove_reports_a_note_that_is_not_there(self):
        store = self.store.load()
        with self.assertRaises(NoteNotFoundError):
            store.remove('alice', 1)

    def test_removes_a_user(self):
        store = self.store.load()
        self.assertEqual(store.remove_user('u/ALICE'), 1)
        self.assertEqual(store.usernames(), ())
        with self.assertRaises(UserNotFoundError):
            store.remove_user('alice')

    def test_an_ambiguous_timestamp_takes_the_first_in_stored_order(self):
        # The stored format keeps whole seconds, so two notes can share one
        # exactly - and then nothing distinguishes them.  Toolbox walks the
        # array and takes the first match; so does this, and it warns, because
        # the caller has no way to have meant a particular one.
        self.reddit.pages[('test', 'usernotes')] = v6_page({'alice': {'ns': [
            {'t': 1559310750, 'n': 'first in the array', 'm': 0, 'w': 0},
            {'t': 1559310750, 'n': 'second in the array', 'm': 0, 'w': 0},
        ]}})
        store = self.make_store().load()
        self.assertEqual([n.text for n in store.notes('alice')],
                         ['first in the array', 'second in the array'])
        removed = store.remove('alice', 1559310750000)
        self.assertEqual(removed.text, 'first in the array')
        self.assertEqual([n.text for n in store.notes('alice')], ['second in the array'])


class SaveTests(StoreTestCase):

    def test_saving_with_nothing_changed_writes_nothing(self):
        store = self.store.load()
        self.assertFalse(store.save())
        self.assertEqual(self.reddit.requests_to('/r/test/api/wiki/edit'), [])

    def test_forcing_a_save_writes_anyway(self):
        store = self.store.load()
        self.assertTrue(store.save(force=True))
        self.assertEqual(len(self.reddit.requests_to('/r/test/api/wiki/edit')), 1)
        self.assertEqual(self.reddit.requests_to('/r/test/api/wiki/edit')[0].form['reason'],
                         'rewrote usernotes for /r/test unchanged via usernotes-cli')

    def test_saving_upgrades_an_old_schema(self):
        # toolbox upgrades v4/v5 to v6 on save and refuses to downgrade; so
        # does this, because a v5 page has no room for what v6 adds.
        self.reddit.pages[('test', 'usernotes')] = json.dumps({
            'ver': 5, 'constants': {'users': [MOD], 'warnings': ['ban']},
            'users': {'alice': {'ns': [{'t': 1559310750, 'n': 'first note',
                                        'm': 0, 'w': 0, 'l': ''}]}}})
        store = self.make_store().load()
        self.assertEqual(store.ver, 5)
        store.add('bob', 'new')
        store.save()
        page = self.saved_page()
        self.assertEqual(page['ver'], 6)
        self.assertNotIn('users', page)             # now compressed
        # Users keep the order they were read in; bob is appended.
        self.assertEqual(list(fmt.decompress_blob(page['blob'])), ['alice', 'bob'])

    def test_a_change_made_elsewhere_is_not_overwritten(self):
        store = self.store.load()
        store.add('alice', 'mine')
        self.reddit.pages[('test', 'usernotes')] = v6_page({
            'alice': {'ns': [{'t': 1559310800, 'n': 'theirs', 'm': 0, 'w': 0}]}})
        with self.assertRaises(StaleStoreError) as caught:
            store.save()
        self.assertIn('changed since it was read', str(caught.exception))
        # ...and the other moderator's note is still there, untouched.
        page = json.loads(self.reddit.page('test'))
        self.assertEqual([slot['n'] for slot in fmt.decompress_blob(page['blob'])['alice']['ns']],
                         ['theirs'])

    def test_staleness_can_be_overridden(self):
        store = self.store.load()
        store.add('alice', 'mine')
        self.reddit.pages[('test', 'usernotes')] = v6_page({
            'alice': {'ns': [{'t': 1559310800, 'n': 'theirs', 'm': 0, 'w': 0}]}})
        self.assertTrue(store.save(check_stale=False))
        inner = fmt.decompress_blob(self.saved_page()['blob'])
        self.assertEqual([slot['n'] for slot in inner['alice']['ns']], ['mine', 'first note'])

    def test_a_page_created_meanwhile_is_not_overwritten(self):
        reddit = FakeReddit(cookie='reddit_session=abc')
        reddit.start()
        self.addCleanup(reddit.stop)
        store = UsernotesStore('fresh', cookies='reddit_session=abc',
                               base_url=reddit.base_url, min_interval=0, retry_backoff=0)
        store.load()
        self.assertFalse(store.exists)
        store.add('alice', 'mine')
        reddit.pages[('fresh', 'usernotes')] = v6_page()
        with self.assertRaises(StaleStoreError):
            store.save()

    def test_a_full_page_is_reported_the_way_toolbox_reports_it(self):
        self.reddit.max_page_bytes = 100
        store = self.store.load()
        store.add('alice', 'x' * 500)
        with self.assertRaises(PageFullError) as caught:
            store.save()
        self.assertIn('usernotes full', str(caught.exception))

    def test_a_save_can_be_retried_after_a_failure(self):
        self.reddit.fail(500, path='/r/test/api/wiki/edit')
        store = self.store.load()
        store.add('alice', 'x')
        with self.assertRaises(Exception):
            store.save()
        self.assertTrue(store.dirty)                # the change was not lost
        self.assertTrue(store.save())

    def test_empty_user_entries_are_dropped_on_save(self):
        self.reddit.pages[('test', 'usernotes')] = v6_page({
            'alice': {'ns': [{'t': 1559310750, 'n': 'first note', 'm': 0, 'w': 0}]},
            'ghost': {'ns': []}})
        store = self.make_store().load()
        self.assertEqual(store.usernames(), ('alice', 'ghost'))
        store.add('bob', 'x')
        store.save()
        self.assertEqual(sorted(fmt.decompress_blob(self.saved_page()['blob'])), ['alice', 'bob'])

    def test_a_second_save_after_a_save_is_a_no_op(self):
        store = self.store.load()
        store.add('alice', 'x')
        self.assertTrue(store.save())
        self.assertFalse(store.save())

    def test_the_store_is_not_stale_right_after_its_own_save(self):
        store = self.store.load()
        store.add('alice', 'x')
        store.save()
        store.add('alice', 'y')
        self.assertTrue(store.save())               # no StaleStoreError


class BackupTests(StoreTestCase):

    def setUp(self):
        super().setUp()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_writes_the_page_verbatim(self):
        backup = self.store.backup(directory=self.directory)
        self.assertIsInstance(backup, Backup)
        self.assertTrue(backup.path.startswith(self.directory))
        self.assertIn('test', os.path.basename(backup.path))
        self.assertTrue(os.path.basename(backup.path).endswith('.json'))
        with open(backup.path, 'rb') as handle:
            written = handle.read()
        # Byte for byte, never a re-serialisation.
        self.assertEqual(written, self.reddit.page('test').encode('utf-8'))
        self.assertEqual(backup.size, len(written))
        self.assertEqual(backup.notes, 1)
        self.assertEqual(backup.users, 1)
        self.assertEqual(backup.warnings, ())
        self.assertEqual(backup.subreddit, 'test')
        self.assertIn('1 notes for 1 users', backup.summary())

    def test_a_named_file(self):
        path = os.path.join(self.directory, 'notes.json')
        self.assertEqual(self.store.backup(path).path, path)

    def test_creates_the_directory(self):
        path = os.path.join(self.directory, 'a', 'b', 'notes.json')
        self.store.backup(path)
        self.assertTrue(os.path.exists(path))

    def test_backs_up_a_page_that_cannot_be_decoded(self):
        # The whole point of a backup is that it survives the case where
        # nothing else works.
        self.reddit.pages[('test', 'usernotes')] = '{"ver": 6, "blob": "not base64"}'
        backup = self.store.backup(directory=self.directory)
        with open(backup.path) as handle:
            self.assertEqual(handle.read(), '{"ver": 6, "blob": "not base64"}')
        self.assertIsNone(backup.notes)
        self.assertIsNone(backup.users)
        self.assertTrue(backup.warnings)
        self.assertIn('could not be decoded', backup.summary())

    def test_refuses_to_back_up_a_missing_page(self):
        reddit = FakeReddit(cookie='reddit_session=abc')
        reddit.start()
        self.addCleanup(reddit.stop)
        store = UsernotesStore('fresh', cookies='reddit_session=abc',
                               base_url=reddit.base_url, min_interval=0, retry_backoff=0)
        with self.assertRaises(NoWikiPageError):
            store.backup(directory=self.directory)
        self.assertEqual(os.listdir(self.directory), [])

    def test_no_leftover_temporary_file(self):
        self.store.backup(directory=self.directory)
        self.assertEqual([name for name in os.listdir(self.directory) if name.endswith('.tmp')], [])

    def test_backing_up_does_not_change_the_store(self):
        self.store.load()
        self.store.backup(directory=self.directory)
        self.assertFalse(self.store.dirty)
        self.assertEqual(self.reddit.requests_to('/r/test/api/wiki/edit'), [])


class ConstructionTests(unittest.TestCase):

    def test_normalizes_the_subreddit(self):
        for value, expected in [
            ('toolbox', 'toolbox'),
            ('Toolbox', 'Toolbox'),
            (' r/toolbox ', 'toolbox'),
            ('/r/toolbox', 'toolbox'),
            ('/r/toolbox/', 'toolbox'),
            ('R/Toolbox', 'Toolbox'),
        ]:
            with self.subTest(value=value):
                self.assertEqual(normalize_subreddit(value), expected)

    def test_rejects_a_bad_subreddit(self):
        for value in ('', 'ab', 'has space', 'x' * 22, 'sub!', 5, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_subreddit(value)

    def test_cookies_or_a_client_not_both(self):
        with self.assertRaises(ValueError):
            UsernotesStore('test', client=object(), cookies='a=b')

    def test_no_request_is_made_until_something_is_read(self):
        reddit = FakeReddit(cookie='reddit_session=abc')
        reddit.start()
        self.addCleanup(reddit.stop)
        store = UsernotesStore('test', cookies='reddit_session=abc',
                               base_url=reddit.base_url, min_interval=0, retry_backoff=0)
        self.assertFalse(store.loaded)
        self.assertEqual(reddit.requests, [])
        self.assertIn('not loaded', repr(store))

    def test_a_bad_logger_is_refused(self):
        with self.assertRaises(ValueError):
            UsernotesStore('test', cookies='a=b', logger=object())

    def test_a_shared_client_can_serve_two_stores(self):
        reddit = FakeReddit(cookie='reddit_session=abc',
                            pages={('one', 'usernotes'): v6_page(),
                                   ('two', 'usernotes'): v6_page()})
        reddit.start()
        self.addCleanup(reddit.stop)
        client = RedditWikiClient('reddit_session=abc', base_url=reddit.base_url, min_interval=0)
        self.addCleanup(client.close)
        one = UsernotesStore('one', client=client)
        two = UsernotesStore('two', client=client)
        self.assertEqual(one.note_count(), 1)
        self.assertEqual(two.note_count(), 1)


if __name__ == '__main__':
    unittest.main()
