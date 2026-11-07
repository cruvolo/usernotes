"""Tests for :mod:`usernotes.format`, the on-wiki storage format.

No network, no Reddit: the format is pure data, and the vectors here come from
``usernotes-storage.md`` (which transcribes toolbox v7.0.0-beta.17) and from
toolbox's own code.

Run from the repository root::

    python3 -m unittest discover -v
"""

import json
import unittest
import zlib
import base64

from usernotes import format as fmt
from usernotes.errors import (
    InvalidConfigError,
    InvalidPageError,
    UnsupportedSchemaError,
)

#: The example ``blob`` from ``usernotes-storage.md`` section 2.  It decodes
#: to the note map below, and - this is the point of keeping it here - zlib at
#: its default level reproduces it byte for byte, which is what pins this
#: module's compression settings to toolbox's pako ones.
DOCUMENTED_BLOB = (
    'eJyrVkpPzTc0sLBQsqpWyitWsoquVipRsjI0NbU0NjQwNzXQUcpTslLyLFEvVkhUKE5NLkotUSjJV0gt'
    'Sy2qzM9LVdJRKgcq11HKBZK1sbW1AKKCF4Q=')
DOCUMENTED_USERS = {
    'geo1088': {'ns': [{'t': 1559310750, 'n': "It's a secret to everyone", 'w': 1, 'm': 1}]}}
DOCUMENTED_PAGE = json.dumps({
    'ver': 6,
    'constants': {'users': ['creesch', 'geo1088'], 'warnings': ['abusewarn', 'gooduser', None, 'ban']},
    'blob': DOCUMENTED_BLOB,
})

#: The decoded users object from section 4 of the same document.
SECTION_4_USERS = {
    'geo1088': {'ns': [
        {'t': 1559310750, 'n': "It's a secret to everyone", 'w': 1, 'm': 1},
        {'t': 1559310836, 'n': 'All your usernote are belong to us', 'w': 2, 'm': 0,
         'l': 'l,bfgb5y,eldfyai'},
    ]},
    'creesch': {'ns': [{'t': 1559310623, 'n': 'heh', 'w': 0, 'm': 1, 'l': None}]},
}


class BlobTests(unittest.TestCase):
    """The base64 + zlib layer, which is where byte-level compatibility lives."""

    def test_decodes_the_documented_blob(self):
        self.assertEqual(fmt.decompress_blob(DOCUMENTED_BLOB), DOCUMENTED_USERS)

    def test_reproduces_the_documented_blob_byte_for_byte(self):
        # Not merely "round trips": any difference in zlib level, window size,
        # JSON separators or ASCII escaping would change the bytes.
        self.assertEqual(fmt.compress_blob(fmt.decompress_blob(DOCUMENTED_BLOB)),
                         DOCUMENTED_BLOB)

    def test_compression_is_zlib_not_raw_deflate_or_gzip(self):
        blob = fmt.compress_blob({'a': {'ns': []}})
        raw = base64.b64decode(blob)
        # zlib's RFC 1950 header: 0x78 and a check value.
        self.assertEqual(raw[0], 0x78)
        self.assertEqual(zlib.decompress(raw), b'{"a":{"ns":[]}}')

    def test_keeps_non_ascii_raw(self):
        # toolbox's JSON.stringify does not escape non-ASCII, so neither may we.
        users = {'a': {'ns': [{'n': 'café 中文', 't': 0, 'm': 0, 'l': '', 'w': 0}]}}
        raw = zlib.decompress(base64.b64decode(fmt.compress_blob(users)))
        self.assertIn('café 中文'.encode('utf-8'), raw)
        self.assertNotIn(b'\\u00e9', raw)
        self.assertEqual(fmt.decompress_blob(fmt.compress_blob(users)), users)

    def test_rejects_junk(self):
        for blob in ('not base64!!', base64.b64encode(b'not zlib').decode(), '',
                     base64.b64encode(zlib.compress(b'[1,2,3]')).decode()):
            with self.subTest(blob=blob):
                with self.assertRaises(InvalidPageError):
                    fmt.decompress_blob(blob)


class ParsePageTests(unittest.TestCase):

    def test_reads_a_v6_page(self):
        page = fmt.parse_page(DOCUMENTED_PAGE, 'test')
        self.assertEqual(page.ver, 6)
        self.assertEqual(page.warnings, ())
        notes = page.users['geo1088']
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note.text, "It's a secret to everyone")
        self.assertEqual(note.time, 1559310750000)      # seconds -> milliseconds
        self.assertEqual(note.mod, 'geo1088')           # constants.users[1]
        self.assertEqual(note.type, 'gooduser')         # constants.warnings[1]

    def test_reads_a_v5_page_with_inline_users(self):
        # v5 is identical to v6 but the note map is not compressed.
        page = fmt.parse_page(json.dumps({
            'ver': 5,
            'constants': {'users': ['creesch', 'geo1088'],
                          'warnings': ['abusewarn', 'gooduser', None, 'ban']},
            'users': DOCUMENTED_USERS,
        }), 'test')
        self.assertEqual(page.ver, 5)
        self.assertEqual(page.users['geo1088'][0].text, "It's a secret to everyone")

    def test_reads_a_v4_page_whose_times_are_milliseconds(self):
        # v4 is v5 with millisecond timestamps; toolbox tells them apart by
        # digit count, and so does this module.
        page = fmt.parse_page(json.dumps({
            'ver': 4,
            'constants': {'users': ['mod'], 'warnings': ['ban']},
            'users': {'alice': {'ns': [{'t': 1559310750123, 'n': 'x', 'm': 0, 'w': 0}]}},
        }), 'test')
        self.assertEqual(page.ver, 4)
        self.assertEqual(page.users['alice'][0].time, 1559310750123)

    def test_reads_a_v5_page_that_stored_milliseconds(self):
        # The digit heuristic is per note, not per page.
        page = fmt.parse_page(json.dumps({
            'ver': 5, 'constants': {'users': [], 'warnings': []},
            'users': {'alice': {'ns': [{'t': 1559310750123, 'n': 'x'}]}},
        }), 'test')
        self.assertEqual(page.users['alice'][0].time, 1559310750123)

    def test_expands_squashed_links(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {}, 'blob': fmt.compress_blob(SECTION_4_USERS),
        }), 'toolbox')
        links = [note.link for note in page.users['geo1088']]
        self.assertEqual(links, ['',
                                 'https://www.reddit.com/r/toolbox/comments/bfgb5y/-/eldfyai/'])

    def test_missing_constants_pool_leaves_author_and_type_unset(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'blob': fmt.compress_blob({'alice': {'ns': [{'t': 1, 'n': 'x', 'm': 0, 'w': 0}]}}),
        }), 'test')
        note = page.users['alice'][0]
        self.assertIsNone(note.mod)
        self.assertIsNone(note.type)
        self.assertEqual(len(page.warnings), 2)

    def test_notes_with_no_type_point_at_a_null_pool_entry(self):
        # A note created without a type makes toolbox pool `undefined`, which
        # JSON writes as null; `w` then points at it and means "no type" - a
        # legitimate value, not damage, so it must not be reported as one.
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {'users': ['mod'], 'warnings': [None]},
            'blob': fmt.compress_blob({'a': {'ns': [{'t': 1, 'n': 'x', 'm': 0, 'w': 0}]}}),
        }), 'test')
        self.assertIsNone(page.users['a'][0].type)
        self.assertEqual(page.users['a'][0].mod, 'mod')
        self.assertEqual(page.warnings, ())

    def test_a_note_with_no_link_reads_as_empty(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {}, 'blob': fmt.compress_blob(
                {'a': {'ns': [{'t': 1, 'n': 'x', 'l': None}, {'t': 2, 'n': 'y', 'l': ''}]}}),
        }), 'test')
        self.assertEqual([note.link for note in page.users['a']], ['', ''])

    def test_time_of_zero_means_unknown(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {}, 'blob': fmt.compress_blob(
                {'a': {'ns': [{'t': 0, 'n': 'time deleters get no time'}]}}),
        }), 'test')
        note = page.users['a'][0]
        self.assertEqual(note.time, 0)
        self.assertFalse(note.has_time)
        self.assertIsNone(note.datetime())

    # -- damaged stores -----------------------------------------------------

    def test_damage_is_reported_and_skipped(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {'users': ['mod'], 'warnings': ['ban']},
            'blob': fmt.compress_blob({
                'alice': {'ns': [
                    'not an object',
                    {'t': 'whenever', 'n': 42, 'm': 9, 'w': 'x', 'l': 7},
                    {'t': 1559310750, 'n': 'fine', 'm': 0, 'w': 0},
                ]},
                'bob': 'not an object either',
                'carol': {},
            }),
        }), 'test')
        # A note stored as a bare string keeps its text (toolbox would crash
        # on the page at all); the malformed object is kept as an empty note
        # with its timestamp and link dropped; the good note is untouched.
        self.assertEqual([note.text for note in page.users['alice']],
                         ['not an object', '', 'fine'])
        self.assertEqual(page.users['alice'][2].time, 1559310750000)
        self.assertEqual(page.users['alice'][0].time, 0)
        self.assertEqual(page.users['alice'][1].time, 0)
        self.assertIsNone(page.users['alice'][1].mod)
        self.assertEqual(page.users['bob'], ())
        self.assertEqual(page.users['carol'], ())
        self.assertTrue(any('bare string' in warning for warning in page.warnings))
        self.assertTrue(any('not a number' in warning for warning in page.warnings))
        self.assertTrue(any('link is' in warning for warning in page.warnings))

    def test_strict_mode_refuses_damage(self):
        with self.assertRaises(InvalidPageError):
            fmt.parse_page(json.dumps({
                'ver': 6, 'constants': {}, 'blob': fmt.compress_blob({'a': {'ns': ['nope']}}),
            }), 'test', strict=True)

    def test_reads_numbers_that_were_stored_as_text(self):
        # JavaScript coerces these, so toolbox reads them; a hand-edited or
        # foreign-written store should not silently lose the date or author.
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {'users': ['mod'], 'warnings': ['ban']},
            'blob': fmt.compress_blob({'alice': {'ns': [
                {'t': '1559310750', 'n': 'x', 'm': '0', 'w': '0'}]}}),
        }), 'test')
        note = page.users['alice'][0]
        self.assertEqual(note.time, 1559310750000)
        self.assertEqual(note.mod, 'mod')
        self.assertEqual(note.type, 'ban')
        self.assertEqual(len(page.warnings), 3)     # said so, three times
        self.assertTrue(all('stored as text' in warning for warning in page.warnings))
        # ...and the date survives being written back.
        rebuilt = fmt.parse_page(fmt.build_page({'alice': [note]}), 'test')
        self.assertEqual(rebuilt.users['alice'][0].time, 1559310750000)

    def test_a_number_that_is_not_a_number_is_unset(self):
        page = fmt.parse_page(json.dumps({
            'ver': 6, 'constants': {'users': ['mod'], 'warnings': ['ban']},
            'blob': fmt.compress_blob({'alice': {'ns': [
                {'t': 'soon', 'n': 'x', 'm': 'first', 'w': 0},
                {'t': 1, 'n': 'y', 'm': 99, 'w': -1}]}}),
        }), 'test')
        first, second = page.users['alice']
        self.assertEqual(first.time, 0)
        self.assertIsNone(first.mod)
        self.assertEqual(first.type, 'ban')
        self.assertIsNone(second.mod)               # out of range
        self.assertIsNone(second.type)              # negative is not a position
        # Nothing is dropped silently: a time that cannot be read, a text
        # index, an index past the end and a negative index are all reported.
        self.assertEqual(len(page.warnings), 4)
        self.assertTrue(any('treated as unknown' in warning for warning in page.warnings))
        self.assertEqual(sum('treated as unset' in warning for warning in page.warnings), 3)

    def test_rejects_things_that_are_not_pages(self):
        for content in ('', '   ', 'not json at all', '[1, 2, 3]', '"a string"',
                        '{"constants": {}}', '{"ver": "6"}', '{"ver": null}'):
            with self.subTest(content=content):
                with self.assertRaises(InvalidPageError):
                    fmt.parse_page(content, 'test')

    def test_rejects_a_v6_page_with_no_blob(self):
        with self.assertRaises(InvalidPageError):
            fmt.parse_page('{"ver": 6, "constants": {}}', 'test')

    def test_refuses_schemas_it_does_not_understand(self):
        # v3 and older stored the note map as an array; v7+ was written by a
        # toolbox newer than this one.  Either way, writing back would be worse
        # than refusing.
        for ver in (0, 1, 2, 3, 7, 99):
            with self.subTest(ver=ver):
                with self.assertRaises(UnsupportedSchemaError):
                    fmt.parse_page(json.dumps({'ver': ver, 'users': {}}), 'test')

    def test_a_page_with_trailing_whitespace_is_fine(self):
        # Wiki pages pick up newlines whenever a human edits one by hand.
        page = fmt.parse_page(DOCUMENTED_PAGE + '\n', 'test')
        self.assertEqual(page.note_count(), 1)


class BuildPageTests(unittest.TestCase):

    def test_writes_v6_with_rebuilt_constants_and_minified_json(self):
        users = {'alice': [fmt.Note(text='one', time=1559310750000, mod='mod',
                                    type='ban', link='https://www.reddit.com/r/s/comments/a/'),
                           fmt.Note(text='two', time=1559310836000, mod='other',
                                    type='ban')],
                 'bob': [fmt.Note(text='three', time=0, mod='mod', type=None)]}
        text = fmt.build_page(users, base_url='https://www.reddit.com')

        self.assertNotIn(': ', text)            # minified, no spaces
        self.assertNotIn('\n', text)
        doc = json.loads(text)
        self.assertEqual(doc['ver'], 6)
        self.assertEqual(list(doc), ['ver', 'constants', 'blob'])   # toolbox's order
        # Pools are rebuilt in first-seen order over the notes, as toolbox does.
        self.assertEqual(doc['constants']['users'], ['mod', 'other'])
        self.assertEqual(doc['constants']['warnings'], ['ban', None])

        inner = fmt.decompress_blob(doc['blob'])
        self.assertEqual(list(inner), ['alice', 'bob'])
        self.assertEqual([slot['n'] for slot in inner['alice']['ns']], ['one', 'two'])
        # Every note carries all five fields, in toolbox's order.
        self.assertEqual(list(inner['alice']['ns'][0]),
                         ['n', 't', 'm', 'l', 'w'])
        self.assertEqual(inner['alice']['ns'][0]['t'], 1559310750)   # seconds on the wire
        self.assertEqual(inner['alice']['ns'][0]['l'], 'l,a')
        self.assertEqual(inner['bob']['ns'][0]['w'], 1)              # the null entry
        self.assertEqual(inner['bob']['ns'][0]['t'], 0)

    def test_writes_v5_inline(self):
        doc = json.loads(fmt.build_page(
            {'alice': [fmt.Note(text='x', time=1559310750000, mod='mod', type='ban')]},
            ver=5))
        self.assertEqual(doc['ver'], 5)
        self.assertEqual(list(doc), ['ver', 'users', 'constants'])
        self.assertEqual(doc['users']['alice']['ns'][0]['n'], 'x')
        self.assertNotIn('blob', doc)

    def test_refuses_to_write_v4(self):
        # Toolbox refuses to downgrade a store, and so does this.
        with self.assertRaises(ValueError):
            fmt.build_page({}, ver=4)

    def test_round_trips_a_store_through_a_page(self):
        users = {'Alice': [fmt.Note(text='one', time=1559310750000, mod='mod', type='ban',
                                    link='https://www.reddit.com/r/s/comments/a/-/b/'),
                           fmt.Note(text='café', time=1559310836000, mod='mod', type=None)],
                 'bob': [fmt.Note(text='with\nnewline & <tags>', time=0, mod=None, type='ban')]}
        for ver in (5, 6):
            with self.subTest(ver=ver):
                page = fmt.parse_page(fmt.build_page(users, ver=ver), 's')
                self.assertEqual(page.ver, ver)
                self.assertEqual(page.warnings, ())
                self.assertEqual(page.users, {
                    name: tuple(notes) for name, notes in users.items()})
                # ...and writing it again gives the same bytes.
                rebuilt = fmt.build_page({n: list(v) for n, v in page.users.items()}, ver=ver)
                self.assertEqual(rebuilt, fmt.build_page(users, ver=ver))

    def test_pools_deduplicate_a_repeated_author_and_type(self):
        users = {'a': [fmt.Note(text='1', time=1, mod='mod', type='ban'),
                       fmt.Note(text='2', time=2, mod='mod', type='ban')]}
        doc = json.loads(fmt.build_page(users))
        self.assertEqual(doc['constants']['users'], ['mod'])
        self.assertEqual(doc['constants']['warnings'], ['ban'])

    def test_rejects_a_note_that_is_not_text(self):
        with self.assertRaises(ValueError):
            fmt.build_page({'a': [fmt.Note(text=None)]})
        with self.assertRaises(ValueError):
            fmt.build_page({'a': [fmt.Note(text='x', time='soon')]})


class TimeTests(unittest.TestCase):

    def test_stored_seconds_become_milliseconds_for_v5_and_v6(self):
        self.assertEqual(fmt.time_from_stored(6, 1559310750), 1559310750000)
        self.assertEqual(fmt.time_from_stored(5, 0), 0)

    def test_v4_is_already_milliseconds(self):
        self.assertEqual(fmt.time_from_stored(4, 1559310750123), 1559310750123)

    def test_milliseconds_are_truncated_to_seconds_on_write(self):
        self.assertEqual(fmt.time_to_stored(6, 1559310750999), 1559310750)
        self.assertEqual(fmt.time_to_stored(5, 1559310750999), 1559310750)

    def test_no_time_is_written_as_zero(self):
        self.assertEqual(fmt.time_to_stored(6, None), 0)
        self.assertEqual(fmt.time_to_stored(6, 0), 0)

    def test_v4_keeps_milliseconds(self):
        self.assertEqual(fmt.time_to_stored(4, 1559310750999), 1559310750999)

    def test_junk_times(self):
        self.assertEqual(fmt.time_from_stored(6, 'yesterday'), 0)
        self.assertEqual(fmt.time_from_stored(6, None), 0)
        self.assertEqual(fmt.time_from_stored(6, True), 0)
        with self.assertRaises(ValueError):
            fmt.time_to_stored(6, 'soon')


class LinkTests(unittest.TestCase):
    """Squashing, from the table in ``usernotes-storage.md`` section 6."""

    def test_squashes_and_unsquashes_each_form(self):
        cases = [
            # full URL (or path) in, squashed stored form, URL out
            ('https://www.reddit.com/r/test/comments/abc123/title/',
             'l,abc123', 'https://www.reddit.com/r/test/comments/abc123/'),
            ('/r/test/comments/abc123/title/def456/',
             'l,abc123,def456', 'https://www.reddit.com/r/test/comments/abc123/-/def456/'),
            ('https://old.reddit.com/r/test/comments/abc123/',
             'l,abc123', 'https://www.reddit.com/r/test/comments/abc123/'),
            ('https://www.reddit.com/r/test/message/messages/xyz789',
             'm,xyz789', 'https://www.reddit.com/r/test/message/messages/xyz789'),
            ('https://mod.reddit.com/mail/all/abc',
             'https://mod.reddit.com/mail/all/abc',
             'https://mod.reddit.com/mail/all/abc'),
        ]
        for permalink, squashed, unsquashed in cases:
            with self.subTest(permalink=permalink):
                self.assertEqual(fmt.squash_link(permalink), squashed)
                self.assertEqual(fmt.unsquash_link('test', squashed), unsquashed)

    def test_keeps_legacy_urls_toolbox_would_drop(self):
        # toolbox reads an unrecognised link back as '' and so loses it on the
        # next save; this module keeps it, which is what its own docs advise.
        legacy = 'https://example.com/some/old/permalink'
        self.assertEqual(fmt.squash_link(legacy), legacy)
        self.assertEqual(fmt.unsquash_link('test', legacy), legacy)

    def test_no_link(self):
        for value in ('', None):
            self.assertEqual(fmt.squash_link(value), '')
            self.assertEqual(fmt.unsquash_link('test', value), '')

    def test_unrecognised_values_become_no_link(self):
        for value in ('l', 'l,', 'm,', 'banana', 'l,,,,'):
            with self.subTest(value=value):
                self.assertEqual(fmt.unsquash_link('test', value), '')

    def test_squashing_rejects_non_text(self):
        with self.assertRaises(ValueError):
            fmt.squash_link(42)

    def test_links_use_the_given_origin(self):
        self.assertEqual(
            fmt.unsquash_link('test', 'l,abc', base_url='https://old.reddit.com'),
            'https://old.reddit.com/r/test/comments/abc/')
        self.assertEqual(
            fmt.squash_link('https://old.reddit.com/r/test/comments/abc/'),
            'l,abc')


class NoteTypeTests(unittest.TestCase):

    def test_defaults_match_toolbox(self):
        # From tbcore.js:116-124; the CLI's flags are these display texts.
        self.assertEqual([(t.key, t.color, t.text) for t in fmt.DEFAULT_NOTE_TYPES], [
            ('gooduser', 'green', 'Good Contributor'),
            ('spamwatch', 'fuchsia', 'Spam Watch'),
            ('spamwarn', 'purple', 'Spam Warning'),
            ('abusewarn', 'orange', 'Abuse Warning'),
            ('ban', 'red', 'Ban'),
            ('permban', 'darkred', 'Permanent Ban'),
            ('botban', 'black', 'Bot Ban'),
        ])

    def test_finds_a_type_by_key_or_display_text(self):
        for value in ('spamwatch', 'Spam Watch', 'spam watch', 'SPAM_WATCH', ' spam-watch '):
            with self.subTest(value=value):
                self.assertEqual(fmt.note_type(value).key, 'spamwatch')
        self.assertEqual(fmt.note_type('permanent ban').key, 'permban')
        self.assertEqual(fmt.note_type('Ban').key, 'ban')

    def test_unknown_type_is_none(self):
        self.assertIsNone(fmt.note_type('not a type'))

    def test_a_note_type_passes_through(self):
        wanted = fmt.DEFAULT_NOTE_TYPES[0]
        self.assertIs(fmt.note_type(wanted), wanted)

    def test_config_parsing(self):
        types = fmt.note_types_from_config([
            {'key': 'custom', 'text': 'Custom', 'color': 'pink'},
            {'key': 'notype'},
        ])
        self.assertEqual([t.key for t in types], ['custom', 'notype'])
        self.assertEqual(types[1].text, 'notype')       # falls back to the key
        self.assertEqual(types[1].color, '')

    def test_config_falls_back_to_defaults(self):
        self.assertEqual(fmt.note_types_from_config(None), fmt.DEFAULT_NOTE_TYPES)
        self.assertEqual(fmt.note_types_from_config([]), fmt.DEFAULT_NOTE_TYPES)

    def test_bad_config_is_refused(self):
        for value in ('nope', [{'text': 'no key'}], [{'key': 'has space'}],
                      [{'key': 'a'}, {'key': 'a', 'text': 'dupe'}]):
            with self.subTest(value=value):
                with self.assertRaises(InvalidConfigError):
                    fmt.note_types_from_config(value)


class NoteTests(unittest.TestCase):

    def test_derived_values(self):
        note = fmt.Note(text='x', time=1559310750123, mod='mod', type='ban',
                        link='https://www.reddit.com/r/s/comments/a/')
        self.assertEqual(note.seconds, 1559310750)      # the precision the store has
        self.assertEqual(note.datetime().isoformat(), '2019-05-31T13:52:30.123000+00:00')
        self.assertEqual(note.as_dict(), {
            'text': 'x', 'time': 1559310750123,
            'time_iso': '2019-05-31T13:52:30.123000+00:00',
            'mod': 'mod', 'type': 'ban',
            'link': 'https://www.reddit.com/r/s/comments/a/'})

    def test_display_text_decodes_entities_like_toolbox(self):
        # Display only: the stored text is what gets written back.
        note = fmt.Note(text='caf&eacute; &amp; more')
        self.assertEqual(note.display_text(), 'café & more')
        self.assertEqual(note.text, 'caf&eacute; &amp; more')

    def test_copy_replaces_only_what_is_given(self):
        note = fmt.Note(text='x', time=5, mod='mod')
        self.assertEqual(note.copy(text='y'), fmt.Note(text='y', time=5, mod='mod'))


if __name__ == '__main__':
    unittest.main()
