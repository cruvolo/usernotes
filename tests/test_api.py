"""Tests for :mod:`usernotes.api`, the Reddit wiki client.

Everything here runs against `tests.fake_reddit`, a throwaway HTTP server on
127.0.0.1.  That means the URL building, query and form encoding, cookie
handling, status mapping and retry logic are all exercised over a real socket -
while the real Reddit, and real moderators' notes, are never touched.
"""

import http.cookiejar
import json
import time
import unittest
import urllib.error
import urllib.parse

from tests.fake_reddit import FakeReddit
from usernotes import cookies as cookies_module
from usernotes.api import DEFAULT_USER_AGENT, RedditWikiClient, User, cookie_jar
from usernotes.errors import (
    ApiError,
    ForbiddenError,
    NetworkError,
    NoWikiPageError,
    NotLoggedInError,
    PageFullError,
    RateLimitedError,
)


class CookieJarTests(unittest.TestCase):
    """How the caller's cookie is turned into something urllib will send."""

    def test_a_jar_is_used_as_it_is(self):
        jar = cookies_module.load_cookie_jar(cookie_file=self._netscape_file())
        self.assertIs(cookie_jar(jar), jar)

    def _netscape_file(self):
        import tempfile, os
        handle = tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False)
        handle.write('# Netscape HTTP Cookie File\n')
        handle.write('.reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\tsession-value\n')
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_none_is_an_empty_jar(self):
        jar = cookie_jar(None)
        self.assertEqual(len(jar), 0)

    def test_a_mapping(self):
        jar = cookie_jar({'reddit_session': 'abc', 'token': 'def'})
        self.assertEqual(sorted(c.name for c in jar), ['reddit_session', 'token'])
        self.assertIn('reddit_session=abc', jar.get_cookie_header('https://www.reddit.com/'))

    def test_a_mapping_can_target_a_domain(self):
        jar = cookie_jar({'a': 'b'}, domain='localhost', secure=False)
        self.assertEqual(jar.get_cookie_header('http://localhost:8080/'), 'a=b')
        self.assertIsNone(jar.get_cookie_header('https://www.reddit.com/'))
        jar = cookie_jar({'a': 'b'}, domain='reddit.com')
        self.assertEqual(jar.get_cookie_header('https://old.reddit.com/'), 'a=b')

    def test_an_ip_address_domain(self):
        jar = cookie_jar({'a': 'b'}, domain='127.0.0.1', secure=False)
        self.assertEqual(jar.get_cookie_header('http://127.0.0.1:8080/'), 'a=b')

    def test_a_secure_cookie_is_only_sent_over_https(self):
        secure = cookie_jar({'a': 'b'}, domain='example.com')
        insecure = cookie_jar({'a': 'b'}, domain='example.com', secure=False)
        self.assertIsNone(secure.get_cookie_header('http://example.com/'))
        self.assertIn('a=b', insecure.get_cookie_header('http://example.com/'))

    def test_a_cookie_header(self):
        jar = cookie_jar('reddit_session=abc; token=def')
        self.assertIn('reddit_session=abc', jar.get_cookie_header('https://www.reddit.com/'))
        # Values may contain '=', and the "Cookie:" prefix is tolerated.
        jar = cookie_jar('Cookie: a=b=c; d=')
        self.assertEqual(jar.get_cookie_header('https://www.reddit.com/'), 'a=b=c; d=')

    def test_netscape_file_content(self):
        jar = cookie_jar('# Netscape HTTP Cookie File\n'
                         '#HttpOnly_.reddit.com\tTRUE\t/\tTRUE\t0\treddit_session\tabc\n'
                         '.reddit.com\tTRUE\t/\tFALSE\t0\ttoken\tdef\n'
                         '\n# a comment\n')
        self.assertEqual(sorted(c.name for c in jar), ['reddit_session', 'token'])
        self.assertIn('reddit_session=abc', jar.get_cookie_header('https://www.reddit.com/'))
        # The session cookie was marked https-only, the other one was not.
        self.assertEqual(jar.get_cookie_header('http://www.reddit.com/'), 'token=def')

    def test_pairs(self):
        jar = cookie_jar([('a', 'b')])
        self.assertEqual(jar.get_cookie_header('https://www.reddit.com/'), 'a=b')

    def test_rejects_what_it_cannot_understand(self):
        for value in ('not a cookie', 'a=1; garbage', 'nope\nnope\t1', 42,
                      ['a=1'], [('a',)]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    cookie_jar(value)


def make_client(reddit, cookies='reddit_session=abc', **kwargs):
    """A client pointed at *reddit*, with pacing and retry delays turned off.

    Real pacing would make the suite wait seconds per retry for no benefit;
    the waits are the subject of one test of their own.
    """
    client = RedditWikiClient(cookies, base_url=reddit.base_url, min_interval=0,
                              retry_backoff=0, **kwargs)
    return client


class ClientTests(unittest.TestCase):
    """The client against the fake Reddit."""

    def setUp(self):
        self.reddit = FakeReddit(cookie='reddit_session=abc')
        self.reddit.start()
        self.addCleanup(self.reddit.stop)
        self.client = make_client(self.reddit)
        self.addCleanup(self.client.close)

    def test_reads_a_page(self):
        self.reddit.pages[('test', 'usernotes')] = '{"ver": 6}'
        self.assertEqual(self.client.read_page('test'), '{"ver": 6}')
        request = self.reddit.requests_to('/r/test/wiki/usernotes.json')[0]
        self.assertEqual(request.method, 'GET')
        # raw_json=1 is what keeps note text out of reddit's HTML escaping.
        self.assertEqual(request.query, {'raw_json': '1'})
        self.assertEqual(request.headers['user-agent'], DEFAULT_USER_AGENT)
        self.assertEqual(request.headers['cookie'], 'reddit_session=abc')

    def test_a_missing_page(self):
        with self.assertRaises(NoWikiPageError) as caught:
            self.client.read_page('test')
        self.assertEqual(caught.exception.reason, 'PAGE_NOT_CREATED')
        self.assertEqual(caught.exception.status, 404)
        self.assertIsNone(self.client.read_page('test', missing_ok=True))

    def test_an_empty_page_counts_as_missing(self):
        self.reddit.pages[('test', 'usernotes')] = ''
        self.assertIsNone(self.client.read_page('test', missing_ok=True))
        with self.assertRaises(NoWikiPageError):
            self.client.read_page('test')

    def test_a_disabled_wiki(self):
        self.reddit.disabled_wikis.add('test')
        with self.assertRaises(NoWikiPageError) as caught:
            self.client.read_page('test')
        self.assertEqual(caught.exception.reason, 'WIKI_DISABLED')
        self.assertFalse(self.client.page_exists('test'))

    def test_reading_without_a_cookie_is_refused(self):
        client = make_client(self.reddit, cookies=None)
        with self.assertRaises(NotLoggedInError):
            client.read_page('test')

    def test_reading_a_page_we_may_not_see(self):
        self.reddit.pages[('private', 'usernotes')] = '{}'
        self.reddit.cookie = 'reddit_session=other-account'
        # A logged-in account that is not a moderator gets a plain refusal,
        # not "no such page".
        with self.assertRaises((NotLoggedInError, ForbiddenError)):
            self.client.read_page('private')

    def test_reads_json(self):
        self.reddit.pages[('test', 'toolbox')] = json.dumps({'usernoteColors': []})
        self.assertEqual(self.client.read_json_page('test', 'toolbox'), {'usernoteColors': []})
        self.assertIsNone(self.client.read_json_page('test', 'nothing', missing_ok=True))

    def test_read_json_reports_a_page_that_is_not_json(self):
        self.reddit.pages[('test', 'toolbox')] = 'this is not json'
        from usernotes.errors import InvalidPageError
        with self.assertRaises(InvalidPageError):
            self.client.read_json_page('test', 'toolbox')

    def test_page_exists(self):
        self.reddit.pages[('test', 'usernotes')] = '{}'
        self.assertTrue(self.client.page_exists('test'))
        self.assertFalse(self.client.page_exists('test', 'other'))

    # -- the account -------------------------------------------------------

    def test_account_and_modhash(self):
        account = self.client.account()
        self.assertEqual(account, User(name='moduser', modhash='modhash-abc', id='t2_test'))
        self.assertEqual(self.client.modhash(), 'modhash-abc')
        # Fetched once and cached: one request for both calls.
        self.assertEqual(len(self.reddit.requests_to('/api/me.json')), 1)
        self.assertEqual(self.client.account(refresh=True),
                         self.client.account())
        self.assertEqual(len(self.reddit.requests_to('/api/me.json')), 2)

    def test_not_logged_in(self):
        client = make_client(self.reddit, cookies=None)
        with self.assertRaises(NotLoggedInError) as caught:
            client.account()
        self.assertIn('cookies', str(caught.exception))
        with self.assertRaises(NotLoggedInError):
            client.modhash()

    def test_logged_in_without_a_modhash(self):
        self.reddit.modhash = None
        with self.assertRaises(NotLoggedInError) as caught:
            self.client.modhash()
        self.assertIn('modhash', str(caught.exception))

    # -- writing -----------------------------------------------------------

    def test_writes_a_page(self):
        self.client.write_page('test', 'usernotes', '{"ver": 6}', reason='add a note')
        self.assertEqual(self.reddit.page('test'), '{"ver": 6}')
        request = self.reddit.requests_to('/r/test/api/wiki/edit', method='POST')[0]
        self.assertEqual(request.form, {'content': '{"ver": 6}', 'page': 'usernotes',
                                        'reason': 'add a note', 'uh': 'modhash-abc'})
        self.assertEqual(request.headers['content-type'],
                         'application/x-www-form-urlencoded')

    def test_writing_without_a_reason_omits_it(self):
        self.client.write_page('test', 'usernotes', '{}')
        self.assertNotIn('reason', self.reddit.requests_to('/r/test/api/wiki/edit')[0].form)

    def test_a_write_without_cookies_fails_before_sending_a_modhash(self):
        client = make_client(self.reddit, cookies=None)
        with self.assertRaises(NotLoggedInError):
            client.write_page('test', 'usernotes', '{}')

    def test_the_page_size_limit_is_reported_as_toolbox_reports_it(self):
        self.reddit.max_page_bytes = 10
        with self.assertRaises(PageFullError) as caught:
            self.client.write_page('test', 'usernotes', 'x' * 50)
        self.assertIn('usernotes full', str(caught.exception))
        self.assertEqual(caught.exception.status, 413)

    def test_a_rejection_embedded_in_a_200_is_still_an_error(self):
        self.reddit.fail(200, path='/r/test/api/wiki/edit',
                         body={'json': {'errors': [['RATELIMIT', 'too fast', 'uh']]}})
        with self.assertRaises(ApiError) as caught:
            self.client.write_page('test', 'usernotes', '{}')
        self.assertIn('RATELIMIT', str(caught.exception))

    def test_page_settings_go_in_the_query_string(self):
        self.client.set_page_settings('test', 'usernotes')
        request = self.reddit.requests_to('/r/test/wiki/settings/', method='POST')[0]
        self.assertEqual(request.form, {})
        self.assertEqual(request.query, {'page': 'usernotes', 'listed': 'true',
                                         'permlevel': '2', 'uh': 'modhash-abc'})
        self.assertTrue(self.reddit.listed)
        self.assertEqual(self.reddit.permlevel, 2)

    # -- failure mapping ---------------------------------------------------

    def test_rate_limit(self):
        self.reddit.fail(429, path='/r/test/wiki/usernotes.json', times=5,
                         body={'reason': 'RATELIMIT', 'message': 'you are doing that too much'},
                         headers={'Retry-After': '30'})
        with self.assertRaises(RateLimitedError) as caught:
            self.client.read_page('test')
        self.assertEqual(caught.exception.retry_after, 30.0)
        self.assertEqual(caught.exception.status, 429)
        self.assertIn('rate limiting', str(caught.exception))
        self.assertIn('30s', str(caught.exception))

    def test_a_short_rate_limit_is_waited_out(self):
        self.reddit.pages[('test', 'usernotes')] = '{"ver": 6}'
        self.reddit.fail(429, path='/r/test/wiki/usernotes.json',
                         headers={'Retry-After': '0.05'})
        started = time.monotonic()
        self.assertEqual(self.client.read_page('test'), '{"ver": 6}')
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_retry_after_beyond_the_limit_is_reported_rather_than_waited_out(self):
        # A CLI should say "reddit says wait 10 minutes", not hang.
        self.reddit.fail(429, path='/r/test/wiki/usernotes.json',
                         headers={'Retry-After': '600'})
        self.reddit.pages[('test', 'usernotes')] = '{"ver": 6}'
        started = time.monotonic()
        with self.assertRaises(RateLimitedError):
            self.client.read_page('test')
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_read_is_retried_after_a_server_error(self):
        self.reddit.pages[('test', 'usernotes')] = '{"ver": 6}'
        self.reddit.fail(500, path='/r/test/wiki/usernotes.json', times=2)
        client = make_client(self.reddit, max_retries=3)
        self.assertEqual(client.read_page('test'), '{"ver": 6}')
        self.assertEqual(len(self.reddit.requests_to('/r/test/wiki/usernotes.json')), 3)

    def test_a_read_gives_up_after_max_retries(self):
        self.reddit.fail(503, path='/r/test/wiki/usernotes.json', times=10)
        with self.assertRaises(NetworkError):
            self.client.read_page('test')
        self.assertEqual(len(self.reddit.requests_to('/r/test/wiki/usernotes.json')), 3)

    def test_a_write_is_not_repeated_after_a_server_error(self):
        # Repeating it could add the note twice; better to report and let the
        # caller look.
        self.reddit.fail(500, path='/r/test/api/wiki/edit', times=3)
        with self.assertRaises(NetworkError) as caught:
            self.client.write_page('test', 'usernotes', '{}')
        self.assertIn('may or may not have been applied', str(caught.exception))
        self.assertEqual(len(self.reddit.requests_to('/r/test/api/wiki/edit')), 1)

    def test_a_rate_limited_write_is_retried(self):
        # Reddit answers 429 without applying the write, so one retry is safe.
        self.reddit.fail(429, path='/r/test/api/wiki/edit',
                         headers={'Retry-After': '0'})
        self.client.write_page('test', 'usernotes', '{"ver": 6}')
        self.assertEqual(self.reddit.page('test'), '{"ver": 6}')
        self.assertEqual(len(self.reddit.requests_to('/r/test/api/wiki/edit')), 2)

    def test_a_stale_modhash_reads_as_not_logged_in(self):
        self.client.modhash()                   # fetched and cached
        self.reddit.modhash = 'a-newer-modhash'  # as if the session were refreshed
        with self.assertRaises(NotLoggedInError):
            self.client.write_page('test', 'usernotes', '{}')

    def test_a_non_json_response_is_an_error(self):
        self.reddit.fail(200, path='/api/me.json', body=b'<html>nope</html>')
        with self.assertRaises(ApiError) as caught:
            self.client.account()
        self.assertIn('not JSON', str(caught.exception))

    def test_a_forbidden_subreddit(self):
        self.reddit.fail(403, path='/r/private/wiki/usernotes.json', times=3,
                         body={'reason': 'FORBIDDEN', 'message': 'Forbidden'})
        with self.assertRaises(ForbiddenError) as caught:
            self.client.read_page('private')
        self.assertEqual(caught.exception.status, 403)
        self.assertIn('moderates', str(caught.exception))

    def test_unexpected_statuses_carry_reddit_s_reason(self):
        self.reddit.fail(400, path='/r/test/wiki/usernotes.json', times=3,
                         body={'reason': 'BAD_SOMETHING', 'message': 'no'})
        with self.assertRaises(ApiError) as caught:
            self.client.read_page('test')
        self.assertEqual(caught.exception.reason, 'BAD_SOMETHING')
        self.assertEqual(str(caught.exception),
                         'reddit refused the request (HTTP 400): no')


class TransportTests(unittest.TestCase):
    """Failure handling that needs no server."""

    def test_a_refused_connection_is_a_network_error(self):
        # Port 1 on loopback: nothing is listening, and nothing is sent.
        client = RedditWikiClient('a=b', base_url='http://127.0.0.1:1', min_interval=0,
                                  retry_backoff=0, max_retries=1, timeout=1)
        with self.assertRaises(NetworkError):
            client.read_page('test')

    def test_pacing_keeps_requests_apart(self):
        reddit = FakeReddit(cookie=None)
        reddit.start()
        self.addCleanup(reddit.stop)
        reddit.pages[('test', 'usernotes')] = '{}'
        client = RedditWikiClient(None, base_url=reddit.base_url, min_interval=0.25)
        started = time.monotonic()
        client.read_page('test')
        client.read_page('test')
        self.assertGreaterEqual(time.monotonic() - started, 0.24)

    def test_a_configured_opener_is_used(self):
        calls = []

        class Opener:
            def open(self, request, timeout=None):
                calls.append((request.get_method(), request.full_url, timeout))
                raise urllib.error.URLError('no')

            def close(self):
                calls.append('closed')

        client = RedditWikiClient(None, base_url='https://www.reddit.com',
                                  opener=Opener(), max_retries=1, min_interval=0,
                                  retry_backoff=0)
        with self.assertRaises(NetworkError):
            client.read_page('test')
        self.assertEqual(calls[0][0], 'GET')
        self.assertEqual(calls[0][1],
                         'https://www.reddit.com/r/test/wiki/usernotes.json?raw_json=1')
        client.close()
        self.assertEqual(calls[-1], 'closed')

    def test_bad_construction_arguments(self):
        with self.assertRaises(ValueError):
            RedditWikiClient(None, base_url='')
        with self.assertRaises(ValueError):
            RedditWikiClient(None, max_retries=0)
        for logger in (object(), 'a logger', type('L', (), {'debug': print})()):
            with self.subTest(logger=logger):
                with self.assertRaises(ValueError):
                    RedditWikiClient(None, logger=logger)

    def test_cookies_are_never_logged(self):
        # A logger that records everything, to prove the modhash and the
        # cookie values never reach it.
        class RecordingLogger:
            def __init__(self):
                self.messages = []

            def _record(self, message, *args):
                self.messages.append(message)

            debug = info = warning = error = _record

        reddit = FakeReddit(cookie='reddit_session=secret-value')
        reddit.start()
        self.addCleanup(reddit.stop)
        reddit.pages[('test', 'usernotes')] = '{"ver": 6}'
        logger = RecordingLogger()
        client = make_client(reddit, cookies='reddit_session=secret-value', logger=logger)
        client.read_page('test')
        client.write_page('test', 'usernotes', '{"ver": 6}')
        client.set_page_settings('test', 'usernotes')
        client.account()
        transcript = '\n'.join(logger.messages)
        self.assertNotIn('secret-value', transcript)
        self.assertNotIn('modhash-abc', transcript)
        self.assertTrue(any(message.startswith('GET ') and
                            '/r/test/wiki/usernotes.json' in message
                            for message in logger.messages))
        # The modhash rides in the query string of the settings call, and must
        # not survive into the log.
        self.assertTrue(any('uh=redacted' in message for message in logger.messages))


if __name__ == '__main__':
    unittest.main()
