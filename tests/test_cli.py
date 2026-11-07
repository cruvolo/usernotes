"""Tests for :mod:`usernotes.cli`, driven the way a shell drives it.

Every test runs the real command line - `usernotes.cli.main` with argv, and
the session cookies read out of a Netscape ``cookies.txt`` file - against
`tests.fake_reddit`, so the argument parsing, the cookie handling, the wiki
API and the listing are exercised together.  Nothing here touches the real
reddit, and the commands under test only read: the last test here asserts
that no write was ever attempted.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse

from tests.fake_reddit import FakeReddit
from usernotes import format as fmt
from usernotes.cli import EXIT_ERROR, EXIT_OK, main
from usernotes._version import __version__

SUBREDDIT = 'test'
NOW = int(time.time())
DAY = 86400

#: The notes the tests read back: two on alice, newest first.
USERS = {
    'alice': {'ns': [
        {'t': NOW - 60, 'n': 'spam watch, again', 'm': 0, 'w': 0,
         'l': 'l,abc123,def456'},
        {'t': NOW - 400 * DAY, 'n': 'banned for abuse', 'm': 1, 'w': 1, 'l': ''},
    ]},
}

#: The pools that give those notes their author and type.
CONSTANTS = {'users': ['creesch', 'othermod'], 'warnings': ['spamwatch', 'ban']}


def notes_page(users=None, constants=None):
    """A usernotes wiki page, as toolbox writes one (schema v6)."""
    doc = {'ver': 6,
           'constants': CONSTANTS if constants is None else constants,
           'blob': fmt.compress_blob(USERS if users is None else users)}
    return json.dumps(doc)


def utc(seconds):
    """A stored timestamp, as the listing prints it."""
    moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.strftime('%Y-%m-%d %H:%M:%S UTC')


class CliTestCase(unittest.TestCase):
    """A fake reddit with a usernotes page, and cookies to read it with."""

    def setUp(self):
        self.reddit = self.start_reddit()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.cookies = self.write_cookies('reddit_session=abc')

    def start_reddit(self, **kwargs):
        """Run another fake reddit, and point the tests at it."""
        kwargs.setdefault('cookie', 'reddit_session=abc')
        kwargs.setdefault('pages', {('test', 'usernotes'): notes_page()})
        reddit = FakeReddit(**kwargs)
        reddit.start()
        self.addCleanup(reddit.stop)
        self.reddit = reddit
        return reddit

    def write_cookies(self, *pairs, host=None):
        """Write a ``cookies.txt`` holding *pairs*, and return its path."""
        host = urllib.parse.urlsplit(self.reddit.base_url).hostname if host is None else host
        path = os.path.join(self.directory, 'cookies.txt')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('# Netscape HTTP Cookie File\n')
            for pair in pairs:
                name, _, value = pair.partition('=')
                handle.write(f'{host}\tFALSE\t/\tFALSE\t0\t{name}\t{value}\n')
        return path

    def run_cli(self, *argv):
        """Run the tool the way a shell would; ``(status, stdout, stderr)``."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(argv), client_options=self.client_options())
        return status, out.getvalue(), err.getvalue()

    def read(self, *argv):
        """``usernotes read`` with the cookies file, and what it printed."""
        return self.run_cli('read', *argv, '--cookies', self.cookies)

    def client_options(self):
        """Point the CLI at the fake reddit, with the pacing turned off."""
        return {'base_url': self.reddit.base_url, 'min_interval': 0,
                'retry_backoff': 0}

    def requests_to(self, page):
        return self.reddit.requests_to(f'/r/{SUBREDDIT}/wiki/{page}.json')

    def usage_error(self, *argv):
        """Run a command line that should not parse; returns what it said."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(list(argv), client_options=self.client_options())
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()


class ReadTests(CliTestCase):

    def test_prints_the_toolbox_listing(self):
        status, out, err = self.read(SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(err, '')

        lines = out.splitlines()
        self.assertEqual(lines[0], 'User Notes - /u/alice')
        self.assertEqual(lines[1], '/r/test - 2 notes')
        self.assertEqual(lines[2], '')
        self.assertEqual(lines[3].split(), ['Author', 'Note'])

        # The newest note first, with its author, type, text, date and link.
        self.assertIn('[Spam Watch] spam watch, again', out)
        # The author is spelled the way reddit spells a username, so that a
        # terminal which linkifies them can make it clickable.
        self.assertIn('/u/creesch', out)
        self.assertIn(f'about a minute ago ({utc(NOW - 60)})', out)
        self.assertIn(f'{self.reddit.base_url}/r/test/comments/abc123/-/def456/', out)

        self.assertIn('[Ban] banned for abuse', out)
        self.assertIn('/u/othermod', out)
        self.assertIn(f'about a year ago ({utc(NOW - 400 * DAY)})', out)

        self.assertLess(out.index('spam watch, again'), out.index('banned for abuse'))

    def test_finds_the_user_however_it_is_spelled(self):
        for spelling in ('alice', 'ALICE', 'u/alice', '/u/Alice'):
            with self.subTest(spelling=spelling):
                status, out, _ = self.read(SUBREDDIT, spelling)
                self.assertEqual(status, EXIT_OK)
                self.assertIn('User Notes - /u/alice', out)

    def test_a_subreddit_name_may_be_written_with_the_r_prefix(self):
        self.reddit.pages[('r/prefixed', 'usernotes')] = notes_page()
        self.assertEqual(self.read('r/prefixed', 'alice')[0], EXIT_OK)

    def test_note_types_come_from_the_subreddits_config(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({'usernoteColors': [
            {'key': 'spamwatch', 'text': 'Spam Watch!!', 'color': 'fuchsia'},
            {'key': 'ban', 'text': 'Banned', 'color': '#ff0000'},
        ]})
        status, out, _ = self.read(SUBREDDIT, 'alice', '--color', 'always')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('\x1b[38;5;201m[Spam Watch!!]\x1b[0m', out)   # fuchsia
        self.assertIn('\x1b[38;5;196m[Banned]\x1b[0m', out)         # #ff0000
        # The colour carries onto the note text, not just the tag.
        self.assertIn('\x1b[38;5;201mspam watch, again\x1b[0m', out)
        self.assertIn('\x1b[38;5;196mbanned for abuse\x1b[0m', out)

    def test_a_user_with_no_notes_is_not_an_error(self):
        status, out, err = self.read(SUBREDDIT, 'nobody')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(out.splitlines(),
                         ['User Notes - /u/nobody',
                          '/r/test - no notes for /u/nobody'])
        self.assertEqual(err, '')

    def test_a_subreddit_with_no_usernotes_page(self):
        self.start_reddit(pages={})
        status, out, _ = self.read('empty', 'alice')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(out.splitlines(),
                         ['User Notes - /u/alice',
                          '/r/empty - no usernotes page yet'])

    def test_a_public_page_can_be_read_without_cookies(self):
        self.start_reddit(cookie=None)
        status, out, err = self.run_cli('read', SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('[Spam Watch] spam watch, again', out)
        self.assertEqual(err, '')

    def test_colour_is_off_when_stdout_is_not_a_terminal(self):
        self.assertNotIn('\x1b', self.read(SUBREDDIT, 'alice')[1])

    def test_colour_can_be_forced_on_and_off(self):
        self.assertIn('\x1b[', self.read(SUBREDDIT, 'alice', '--color', 'always')[1])
        self.assertNotIn('\x1b', self.read(SUBREDDIT, 'alice', '--color', 'never')[1])

    def test_the_note_types_are_only_looked_up_when_they_are_shown(self):
        self.read(SUBREDDIT, 'nobody')              # nothing to label
        self.read(SUBREDDIT, 'alice', '--json')     # the JSON has the keys
        self.assertEqual(self.requests_to('toolbox'), [])
        self.read(SUBREDDIT, 'alice')
        self.assertEqual(len(self.requests_to('toolbox')), 1)

    def test_nothing_is_ever_written(self):
        self.read(SUBREDDIT, 'alice')
        self.assertEqual([record.method for record in self.reddit.requests
                          if record.method != 'GET'], [])


class JsonTests(CliTestCase):

    def test_the_document_describes_the_store_and_the_notes(self):
        status, out, err = self.read(SUBREDDIT, 'alice', '--json')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(err, '')
        document = json.loads(out)
        self.assertEqual(document['subreddit'], 'test')
        self.assertEqual(document['page'], 'usernotes')
        self.assertEqual(document['schema'], 6)
        self.assertEqual(document['exists'], True)
        self.assertEqual(document['name'], 'alice')
        self.assertEqual(document['aliases'], [])
        self.assertEqual(document['warnings'], [])

        self.assertEqual(len(document['notes']), 2)
        note = document['notes'][0]
        self.assertEqual(note['text'], 'spam watch, again')
        self.assertEqual(note['time'], (NOW - 60) * 1000)
        self.assertEqual(note['mod'], 'creesch')
        self.assertEqual(note['type'], 'spamwatch')
        self.assertEqual(note['link'],
                         f'{self.reddit.base_url}/r/test/comments/abc123/-/def456/')

    def test_a_user_with_no_notes_has_an_empty_list(self):
        document = json.loads(self.read(SUBREDDIT, 'nobody', '--json')[1])
        self.assertEqual(document['name'], 'nobody')
        self.assertEqual(document['notes'], [])
        self.assertEqual(document['exists'], True)

    def test_a_subreddit_with_no_page_says_so(self):
        self.start_reddit(pages={})
        document = json.loads(self.read('empty', 'alice', '--json')[1])
        self.assertEqual(document['exists'], False)
        self.assertEqual(document['notes'], [])

    def test_json_is_never_coloured(self):
        self.assertNotIn('\x1b', self.read(SUBREDDIT, 'alice', '--json',
                                            '--color', 'always')[1])

    def test_json_keeps_the_note_text_as_it_is_stored(self):
        self.reddit.pages[('test', 'usernotes')] = notes_page(
            users={'alice': {'ns': [{'t': NOW, 'n': 'not &lt;escaped&gt;', 'm': 0,
                                     'w': None, 'l': ''}]}})
        document = json.loads(self.read(SUBREDDIT, 'alice', '--json')[1])
        self.assertEqual(document['notes'][0]['text'], 'not &lt;escaped&gt;')
        self.assertIn('not <escaped>', self.read(SUBREDDIT, 'alice')[1])


class AddTests(CliTestCase):
    """``usernotes add``: a write, then the user's notes read back."""

    def add(self, *argv):
        """``usernotes add`` with the cookies file, and what it printed."""
        return self.run_cli('add', *argv, '--cookies', self.cookies)

    def stored(self, username='alice'):
        """The notes the fake reddit now holds for *username*, newest first."""
        page = fmt.parse_page(self.reddit.page(SUBREDDIT), SUBREDDIT)
        return page.users.get(username, ())

    def edits(self):
        return self.reddit.requests_to(f'/r/{SUBREDDIT}/api/wiki/edit', method='POST')

    def test_writes_the_note_and_shows_the_users_notes_again(self):
        status, out, err = self.add(SUBREDDIT, 'alice', 'a brand new note',
                                    '--flag', 'ban')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(err, '')

        # What is printed is their notes, newest first, with the new one in it.
        self.assertEqual(out.splitlines()[0], 'User Notes - /u/alice')
        self.assertEqual(out.splitlines()[1], '/r/test - 3 notes')
        self.assertIn('[Ban] a brand new note', out)

        notes = self.stored()
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0].text, 'a brand new note')
        self.assertEqual(notes[0].type, 'ban')
        self.assertEqual(notes[0].mod, 'moduser')       # the signed-in account

    def test_the_notes_shown_are_read_back_from_the_wiki(self):
        self.add(SUBREDDIT, 'alice', 'a note', '--flag', 'ban')
        # one read to load, one for the staleness check before writing, and
        # one for the listing afterwards.
        self.assertEqual(len(self.requests_to('usernotes')), 3)
        self.assertEqual(len(self.edits()), 1)

    def test_adding_writes_nothing_but_the_page_and_its_settings(self):
        self.add(SUBREDDIT, 'alice', 'a note', '--flag', 'ban')
        self.assertEqual(
            [record.path for record in self.reddit.requests if record.method == 'POST'],
            [f'/r/{SUBREDDIT}/api/wiki/edit', f'/r/{SUBREDDIT}/wiki/settings/'])

    def test_the_revision_says_what_was_done(self):
        self.add(SUBREDDIT, 'alice', 'a note', '--flag', 'ban')
        reason = self.edits()[0].form['reason']
        self.assertIn('create new note on user alice', reason)
        self.assertIn('usernotes-cli', reason)

    def test_the_flag_carries_over_from_the_users_most_recent_note(self):
        # alice's newest note is flagged spamwatch
        status, out, _ = self.add(SUBREDDIT, 'alice', 'and again')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('[Spam Watch] and again', out)
        self.assertEqual(self.stored()[0].type, 'spamwatch')

    def test_the_carry_over_looks_past_notes_with_no_flag(self):
        self.start_reddit(pages={('test', 'usernotes'): notes_page(users={'bob': {'ns': [
            {'t': NOW - 60, 'n': 'no flag here', 'm': 0, 'w': None, 'l': ''},
            {'t': NOW - 400 * DAY, 'n': 'banned before', 'm': 0, 'w': 1, 'l': ''}]}})})
        status, out, _ = self.add(SUBREDDIT, 'bob', 'and again')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('[Ban] and again', out)
        self.assertEqual(self.stored('bob')[0].type, 'ban')

    def test_no_flag_is_carried_over_when_the_user_has_none(self):
        status, out, _ = self.add(SUBREDDIT, 'nobody', 'a first note')
        self.assertEqual(status, EXIT_OK)
        self.assertIsNone(self.stored('nobody')[0].type)
        self.assertIn('a first note', out)

    def test_the_flag_can_be_none(self):
        status, _, _ = self.add(SUBREDDIT, 'alice', 'no flag please',
                                '--flag', 'none')
        self.assertEqual(status, EXIT_OK)
        self.assertIsNone(self.stored()[0].type)

    def test_the_flag_may_be_named_by_any_spelling_of_its_name(self):
        for spelling in ('spamwatch', 'Spam Watch', 'spam watch', 'SPAMWATCH'):
            with self.subTest(spelling=spelling):
                status, _, _ = self.add(SUBREDDIT, 'alice', f'note {spelling}',
                                        '--flag', spelling)
                self.assertEqual(status, EXIT_OK)
                self.assertEqual(self.stored()[0].type, 'spamwatch')

    def test_the_subreddits_own_flags_are_used(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({'usernoteColors': [
            {'key': 'silly', 'text': 'Silly', 'color': 'fuchsia'}]})
        status, out, _ = self.add(SUBREDDIT, 'alice', 'a silly note',
                                  '--flag', 'silly', '--color', 'always')
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(self.stored()[0].type, 'silly')
        self.assertIn('\x1b[38;5;201m[Silly]\x1b[0m', out)

    def test_a_flag_the_subreddit_lost_is_not_carried_over(self):
        # Someone flagged this user with a type the config no longer defines.
        self.start_reddit(pages={('test', 'usernotes'): notes_page(
            users={'bob': {'ns': [
                {'t': NOW - 60, 'n': 'an old note', 'm': 0, 'w': 2, 'l': ''}]}},
            constants={'users': ['moduser'], 'warnings': ['ban', 'permban', 'gone']})})
        status, out, err = self.add(SUBREDDIT, 'bob', 'a new note')
        self.assertEqual(status, EXIT_OK)
        self.assertIsNone(self.stored('bob')[0].type)
        self.assertIn('a new note', out)
        self.assertIn('without a flag', err)

    def test_an_unknown_flag_is_refused_before_anything_is_written(self):
        before = self.reddit.page(SUBREDDIT)
        status, out, err = self.add(SUBREDDIT, 'alice', 'a note',
                                    '--flag', 'silly')
        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(out, '')
        self.assertIn('spamwatch', err)     # the flags that would have worked
        self.assertEqual(self.reddit.page(SUBREDDIT), before)
        self.assertEqual(self.edits(), [])

    def test_a_link_is_stored_the_way_toolbox_stores_it(self):
        link = f'{self.reddit.base_url}/r/test/comments/abc123/'
        status, out, _ = self.add(SUBREDDIT, 'alice', 'about that comment',
                                  '--flag', 'ban', '--link', link)
        self.assertEqual(status, EXIT_OK)
        self.assertIn(link, out)
        stored = json.loads(self.reddit.page(SUBREDDIT))
        note = fmt.decompress_blob(stored['blob'])['alice']['ns'][0]
        self.assertEqual(note['l'], 'l,abc123')

    def test_a_link_that_is_not_one_is_a_usage_error(self):
        self.assertIn('not a reddit link',
                      self.usage_error('add', SUBREDDIT, 'alice', 'a note',
                                       '--link', 'somewhere over there'))

    def test_the_note_may_not_be_empty(self):
        self.assertIn('the note is empty',
                      self.usage_error('add', SUBREDDIT, 'alice', '   '))

    def test_the_first_note_creates_the_page_and_locks_it_down(self):
        self.start_reddit(pages={})
        status, out, _ = self.add(SUBREDDIT, 'alice', 'the first note ever',
                                  '--flag', 'ban')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('[Ban] the first note ever', out)
        self.assertTrue(self.reddit.page(SUBREDDIT))
        # Toolbox leaves the page listed and moderators-only; so does this.
        self.assertEqual(self.reddit.permlevel, 2)
        self.assertTrue(self.reddit.listed)

    def test_json_after_adding(self):
        status, out, _ = self.add(SUBREDDIT, 'alice', 'a note', '--flag', 'ban',
                                  '--json')
        self.assertEqual(status, EXIT_OK)
        document = json.loads(out)
        self.assertEqual(document['name'], 'alice')
        self.assertEqual(document['notes'][0]['text'], 'a note')
        self.assertEqual(document['notes'][0]['type'], 'ban')
        self.assertEqual(document['notes'][0]['mod'], 'moduser')


class FlagsTests(CliTestCase):
    """``usernotes flags``: how a moderator finds out what --flag accepts."""

    def flags(self, *argv):
        return self.run_cli('flags', *argv, '--cookies', self.cookies)

    def test_lists_toolboxs_defaults_when_the_subreddit_defines_none(self):
        status, out, err = self.flags(SUBREDDIT)
        self.assertEqual(status, EXIT_OK)
        self.assertEqual(err, '')
        self.assertIn('Usernote flags - /r/test', out)
        for name in ('Good Contributor', 'Spam Watch', 'Spam Warning',
                     'Abuse Warning', 'Ban', 'Permanent Ban', 'Bot Ban'):
            self.assertIn(name, out)
        for key in ('gooduser', 'spamwatch', 'spamwarn', 'abusewarn', 'ban',
                    'permban', 'botban'):
            self.assertIn(key, out)

    def test_lists_the_subreddits_own_flags_in_their_colours(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({'usernoteColors': [
            {'key': 'silly', 'text': 'Silly', 'color': 'fuchsia'},
            {'key': 'bad', 'text': 'Very Bad', 'color': '#ff0000'}]})
        status, out, _ = self.flags(SUBREDDIT, '--color', 'always')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('\x1b[38;5;201mSilly\x1b[0m', out)
        self.assertIn('\x1b[38;5;196mVery Bad\x1b[0m', out)
        self.assertNotIn('Good Contributor', out)

    def test_json(self):
        document = json.loads(self.flags(SUBREDDIT, '--json')[1])
        self.assertEqual(document['subreddit'], 'test')
        self.assertEqual([flag['key'] for flag in document['flags']][:2],
                         ['gooduser', 'spamwatch'])
        self.assertEqual(document['flags'][0]['text'], 'Good Contributor')
        self.assertEqual(document['flags'][0]['color'], 'green')

    def test_nothing_is_written(self):
        self.flags(SUBREDDIT)
        self.assertEqual([record.method for record in self.reddit.requests
                          if record.method != 'GET'], [])


class FailureTests(CliTestCase):
    """What the tool says, and what a shell can tell from its status."""

    def test_reading_without_cookies_says_how_to_get_a_session(self):
        status, out, err = self.run_cli('read', SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_ERROR)
        self.assertEqual(out, '')
        self.assertIn('moderator-only', err)
        self.assertIn('--cookies-from-browser', err)

    def test_being_forbidden_is_reported_without_the_hint(self):
        self.reddit.fail(403, path=f'/r/{SUBREDDIT}/wiki/usernotes.json',
                         body={'reason': 'FORBIDDEN', 'message': 'Forbidden'})
        status, _, err = self.read(SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn('moderates', err)
        self.assertNotIn('hint:', err)

    def test_a_cookie_file_that_cannot_be_read(self):
        status, _, err = self.run_cli('read', SUBREDDIT, 'alice', '--cookies',
                                      os.path.join(self.directory, 'missing.txt'))
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn('could not load the browser cookies', err)
        self.assertIn('--cookies FILE', err)

    def test_cookies_for_another_site_are_reported_before_the_request(self):
        path = self.write_cookies('reddit_session=abc', host='example.com')
        status, _, err = self.run_cli('read', SUBREDDIT, 'alice', '--cookies', path)
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn('none of the cookies that were loaded apply to', err)

    def test_a_page_that_is_not_a_usernotes_store(self):
        self.reddit.pages[('test', 'usernotes')] = 'not json at all'
        status, _, err = self.read(SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_ERROR)
        self.assertIn('not valid JSON', err)

    def test_a_damaged_page_is_reported_but_still_listed(self):
        self.reddit.pages[('test', 'usernotes')] = notes_page(users={'alice': 'oops'})
        status, out, err = self.read(SUBREDDIT, 'alice')
        self.assertEqual(status, EXIT_OK)
        self.assertIn('no notes for /u/alice', out)
        self.assertIn('warning:', err)
        self.assertIn('not an object', err)

    def test_quiet_hides_the_warnings_but_not_the_listing(self):
        self.reddit.pages[('test', 'usernotes')] = notes_page(users={'alice': 'oops'})
        status, out, err = self.run_cli('-q', 'read', SUBREDDIT, 'alice',
                                        '--cookies', self.cookies)
        self.assertEqual(status, EXIT_OK)
        self.assertIn('no notes for /u/alice', out)
        self.assertEqual(err, '')

    def test_verbose_shows_the_requests_without_ever_showing_the_cookies(self):
        status, out, err = self.run_cli('-vv', 'read', SUBREDDIT, 'alice',
                                        '--cookies', self.cookies)
        self.assertEqual(status, EXIT_OK)
        self.assertIn('GET', err)
        self.assertIn(f'/r/{SUBREDDIT}/wiki/usernotes.json', err)
        self.assertNotIn('reddit_session', err)
        self.assertNotIn('abc', err)


class UsageTests(CliTestCase):
    """Argument mistakes are argparse's: usage on stderr, status 2."""

    def test_a_command_is_required(self):
        self.assertIn('COMMAND', self.usage_error())

    def test_an_unknown_command(self):
        self.assertIn('invalid choice', self.usage_error('nonsense'))

    def test_a_missing_user(self):
        self.assertIn('USERNAME', self.usage_error('read', SUBREDDIT))

    def test_a_missing_subreddit(self):
        self.assertIn('SUBREDDIT', self.usage_error('read'))

    def test_a_subreddit_that_is_not_one(self):
        self.assertIn('not a subreddit name',
                      self.usage_error('read', 'not a subreddit', 'alice'))

    def test_a_username_that_is_not_one(self):
        self.assertIn('not a reddit username',
                      self.usage_error('read', SUBREDDIT, 'not a username!'))

    def test_a_browser_specification_that_is_not_one(self):
        message = self.usage_error('read', SUBREDDIT, 'alice',
                                   '--cookies-from-browser', 'netscape')
        self.assertIn('unsupported browser', message)
        self.assertIn('firefox', message)

    def test_a_colour_mode_that_is_not_one(self):
        self.assertIn('invalid choice',
                      self.usage_error('read', SUBREDDIT, 'alice', '--color', 'maybe'))

    def test_add_needs_the_note_itself(self):
        self.assertIn('TEXT', self.usage_error('add', SUBREDDIT, 'alice'))

    def test_flags_needs_a_subreddit(self):
        self.assertIn('SUBREDDIT', self.usage_error('flags'))

    def test_nothing_is_sent_when_the_arguments_are_wrong(self):
        self.usage_error('read', SUBREDDIT, 'alice', '--color', 'maybe')
        self.assertEqual(self.reddit.requests, [])


class HelpTests(CliTestCase):

    def output(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(list(argv), client_options=self.client_options())
        self.assertEqual(caught.exception.code, 0)
        return out.getvalue()

    def test_the_top_level_help(self):
        text = self.output('--help')
        for command in ('read', 'add', 'flags'):
            self.assertIn(command, text)
        self.assertIn('--cookies-from-browser', text)
        self.assertIn('--verbose', text)

    def test_the_version(self):
        self.assertIn(__version__, self.output('--version'))

    def test_the_help_for_reading(self):
        text = self.output('read', '--help')
        self.assertIn('SUBREDDIT', text)
        self.assertIn('USERNAME', text)
        self.assertIn('--json', text)
        self.assertIn('--local', text)
        self.assertIn('--cookies', text)

    def test_the_help_for_adding(self):
        text = self.output('add', '--help')
        self.assertIn('SUBREDDIT', text)
        self.assertIn('USERNAME', text)
        self.assertIn('TEXT', text)
        self.assertIn('--flag', text)
        self.assertIn('--link', text)
        self.assertIn('--cookies-from-browser', text)
        # How to find out what a flag can be, from the help itself.
        self.assertIn('usernotes flags', text)

    def test_the_help_for_flags(self):
        text = self.output('flags', '--help')
        self.assertIn('SUBREDDIT', text)
        self.assertIn('--json', text)
        self.assertIn('--color', text)


if __name__ == '__main__':
    unittest.main()
