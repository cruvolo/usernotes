"""A miniature Reddit wiki server, for tests.

Tests must never touch the real Reddit - the notes on it are real moderators'
data.  This runs a throwaway HTTP server on ``127.0.0.1`` that answers the
handful of endpoints `usernotes.api` uses, so the client's URL building, form
encoding, cookie handling, status mapping and retry logic are all exercised
for real, over a socket, without leaving the machine.

Typical use::

    class TestSomething(unittest.TestCase):
        def setUp(self):
            self.reddit = FakeReddit(pages={('toolbox', 'usernotes'): '{"ver":6,...}'})
            self.addCleanup(self.reddit.stop)
            client = RedditWikiClient('reddit_session=abc', base_url=self.reddit.base_url)

Everything the server saw is kept in `FakeReddit.requests`, so a test can
assert on the query parameters, the form body, and which cookies arrived.
"""

from __future__ import annotations

import collections
import http.server
import json
import re
import threading
import urllib.parse

#: Endpoint patterns, matching `usernotes.api`'s calls.
_WIKI_PAGE_RE = re.compile(r'/r/(?P<sub>[^/]+)/wiki/(?P<page>[^/]+)\.json')
_WIKI_EDIT_RE = re.compile(r'/r/(?P<sub>[^/]+)/api/wiki/edit')
_WIKI_SETTINGS_RE = re.compile(r'/r/(?P<sub>[^/]+)/wiki/settings/')

Record = collections.namedtuple(
    'Record', ('method', 'path', 'query', 'form', 'headers'))

#: A queued one-shot response: `FakeReddit.fail` appends these.
Failure = collections.namedtuple('Failure', ('method', 'path', 'status', 'body', 'headers'))


class FakeReddit:
    """A throwaway Reddit wiki API.

    :param user: username ``/api/me.json`` reports.
    :param modhash: modhash it reports, and requires for writes.
    :param pages: ``{(subreddit, page): text}`` of existing wiki pages.
    :param cookie: if set, e.g. ``'reddit_session=abc'``, every request must
        carry exactly this ``Cookie`` header or it is refused as
        unauthenticated.  This is how the tests prove the client really sends
        the cookies it was given.
    :param disabled_wikis: subreddits whose wiki is disabled, so reads answer
        ``WIKI_DISABLED``.
    :param max_page_bytes: reject a wiki edit whose content is larger than
        this, with HTTP 413 - the size limit the real wiki has.
    """

    def __init__(self, *, user='moduser', modhash='modhash-abc', pages=None,
                 cookie=None, disabled_wikis=(), max_page_bytes=None,
                 listed=True, permlevel=2):
        self.user = user
        self.modhash = modhash
        self.pages = dict(pages or {})
        self.cookie = cookie
        self.disabled_wikis = set(disabled_wikis)
        self.max_page_bytes = max_page_bytes
        self.listed = listed
        self.permlevel = permlevel

        self.requests: list[Record] = []
        self.failures: collections.deque[Failure] = collections.deque()
        self.account_status = 200
        self.write_status = None            # force a status for wiki edits
        self.settings_status = 200
        self._server = None
        self._thread = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        """Start serving in a background thread; returns the base URL."""
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                self._dispatch('GET')

            def do_POST(self):
                self._dispatch('POST')

            def log_message(self, *args):
                pass        # keep the test output clean

            def _dispatch(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length).decode('utf-8') if length else ''
                record = Record(
                    method=method,
                    path=parsed.path,
                    query=dict(urllib.parse.parse_qsl(parsed.query)),
                    form=dict(urllib.parse.parse_qsl(body)),
                    headers={key.lower(): value for key, value in self.headers.items()},
                )
                fake.requests.append(record)
                queued = fake._next_failure(record)
                if queued is None:
                    status, payload, content_type = fake.handle(record)
                    headers = {}
                else:
                    status, payload, content_type, headers = queued
                if isinstance(payload, bytes):
                    data = payload
                else:
                    data = json.dumps(payload).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.fake = self
        # A short poll interval: BaseServer.shutdown() waits for the loop to
        # notice, and the default half second would be paid by every test.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self):
        """Shut the server down; safe to call twice."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    @property
    def base_url(self):
        host, port = self._server.server_address[:2]
        return f'http://{host}:{port}'

    # -- what the tests ask of it ------------------------------------------

    def fail(self, status, *, path=None, method=None, times=1, body=None, headers=None):
        """Answer the next matching request(s) with *status* instead of normally.

        Used to provoke the failures that are awkward to arrange otherwise:
        rate limits, 5xx, a bad modhash.
        """
        for _ in range(times):
            self.failures.append(Failure(method=method, path=path, status=status,
                                         body=body, headers=headers or {}))

    def page(self, subreddit, page='usernotes'):
        """The current content of a wiki page, or ``None``."""
        return self.pages.get((subreddit, page))

    def parse_page(self, subreddit, page='usernotes'):
        """The current content of a wiki page, parsed as JSON."""
        content = self.page(subreddit, page)
        return None if content is None else json.loads(content)

    def requests_to(self, path, *, method=None):
        """The recorded requests for *path* (exact match), in order."""
        return [record for record in self.requests
                if record.path == path and (method is None or record.method == method)]

    # -- the API itself -----------------------------------------------------

    def handle(self, record):
        """Answer one request: ``(status, payload, content-type)``."""
        if record.path == '/api/me.json':
            return self._me(record)

        match = _WIKI_EDIT_RE.fullmatch(record.path)
        if match is not None:
            return self._edit(match['sub'], record)

        match = _WIKI_SETTINGS_RE.fullmatch(record.path)
        if match is not None:
            return self._settings(match['sub'], record)

        match = _WIKI_PAGE_RE.fullmatch(record.path)
        if match is not None:
            return self._read(match['sub'], match['page'], record)

        return 404, {'reason': 'NOT_FOUND', 'message': 'not a reddit endpoint'}, 'application/json'

    def _me(self, record):
        if self.account_status != 200:
            return self.account_status, {'reason': 'UNKNOWN'}, 'application/json'
        if not self._authenticated(record):
            # Reddit answers an anonymous /api/me.json with an empty object.
            return 200, {}, 'application/json'
        return 200, {'kind': 't2', 'data': {
            'name': self.user, 'modhash': self.modhash, 'id': 't2_test'}}, 'application/json'

    def _read(self, subreddit, page, record):
        if subreddit in self.disabled_wikis:
            return 403, {'reason': 'WIKI_DISABLED', 'message': 'Forbidden'}, 'application/json'
        if not self._authenticated(record):
            return 403, {'reason': 'USER_REQUIRED', 'message': 'please log in'}, 'application/json'
        content = self.page(subreddit, page)
        if content is None:
            return 404, {'reason': 'PAGE_NOT_CREATED', 'message': 'Forbidden'}, 'application/json'
        return 200, {'kind': 'wikipage', 'data': {
            'content_md': content, 'may_revise': True, 'revision_id': 'rev-1',
        }}, 'application/json'

    def _edit(self, subreddit, record):
        if not self._authenticated(record):
            return 403, {'reason': 'USER_REQUIRED', 'message': 'please log in'}, 'application/json'
        if record.form.get('uh') != self.modhash:
            return 403, {'reason': 'USER_REQUIRED', 'message': 'bad modhash'}, 'application/json'
        if self.write_status is not None:
            return self.write_status, {'reason': 'UNKNOWN'}, 'application/json'
        content = record.form.get('content', '')
        if self.max_page_bytes is not None and len(content.encode('utf-8')) > self.max_page_bytes:
            return 413, b'<html>page too long</html>', 'text/html'
        self.pages[(subreddit, record.form.get('page', 'usernotes'))] = content
        return 200, {}, 'application/json'

    def _settings(self, subreddit, record):
        if not self._authenticated(record):
            return 403, {'reason': 'USER_REQUIRED', 'message': 'please log in'}, 'application/json'
        if self.settings_status != 200:
            return self.settings_status, b'<html>error</html>', 'text/html'
        # The page settings apply to the page named in the query string, which
        # is where toolbox puts them and where `usernotes.api` puts them too.
        if 'page' not in record.query:
            return 400, {'reason': 'NO_PAGE', 'message': 'page is required'}, 'application/json'
        self.listed = record.query.get('listed') == 'true'
        self.permlevel = int(record.query.get('permlevel', 0))
        return 200, b'{}', 'application/json'

    def _authenticated(self, record):
        if self.cookie is None:
            return True
        return self.cookie in (record.headers.get('cookie') or '')

    def _next_failure(self, record):
        """The queued failure for *record*, as a 4-tuple, or ``None``.

        Called by the request handler before `handle`, so that a queued
        response can carry its own headers (``Retry-After``, which is how
        Reddit says how long to wait).
        """
        for index, failure in enumerate(self.failures):
            if failure.method is not None and failure.method != record.method:
                continue
            if failure.path is not None and failure.path != record.path:
                continue
            del self.failures[index]
            body = failure.body if failure.body is not None else {'reason': 'QUEUED_FAILURE'}
            content_type = 'text/html' if isinstance(body, bytes) else 'application/json'
            return failure.status, body, content_type, failure.headers
        return None
