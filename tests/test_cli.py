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
        self.assertIn('creesch', out)
        self.assertIn(f'about a minute ago ({utc(NOW - 60)})', out)
        self.assertIn(f'{self.reddit.base_url}/r/test/comments/abc123/-/def456/', out)

        self.assertIn('[Ban] banned for abuse', out)
        self.assertIn('othermod', out)
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

    def usage_error(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(list(argv), client_options=self.client_options())
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

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
        self.assertIn('read', text)
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


if __name__ == '__main__':
    unittest.main()
