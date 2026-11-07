# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Talk to Reddit's wiki API with a cookie the caller already has.

This module is the network half of the package: it reads and writes wiki
pages, fetches the modhash a write needs, and turns Reddit's answers into the
exceptions in `usernotes.errors`.  It knows nothing about the usernotes
format - `usernotes.format` does that, and `usernotes.store` puts the two
together.

Cookies are a *parameter*, never something this module goes looking for.  The
caller decides where they come from, typically
``usernotes.cookies.load_cookie_jar()``::

    from usernotes import cookies
    from usernotes.api import RedditWikiClient

    client = RedditWikiClient(cookies.load_cookie_jar(from_browser='firefox'))
    print(client.account().name)
    print(client.read_page('toolbox', 'usernotes')[:80])

Accepted forms of the ``cookies`` parameter, all normalised by `cookie_jar`:

* an `http.cookiejar.CookieJar`, including the one `usernotes.cookies`
  returns (used as-is, so nothing is copied or re-parsed);
* a ``Mapping`` of cookie name to value, e.g. ``{'reddit_session': '...'}``;
* a ``Cookie:`` header value, e.g. ``'reddit_session=...; token=...'``;
* the *content* of a Netscape ``cookies.txt`` file;
* ``None`` for an anonymous client.

Reddit specifics that are deliberate
-------------------------------------

``raw_json=1``
    Reddit HTML-escapes ``&``, ``<`` and ``>`` in its JSON responses unless
    asked not to.  Every request here sets ``raw_json=1``, so note text is
    read back exactly as it was written.  (Toolbox does not, and papers over
    the result by HTML-decoding note text on read - which is why it corrupts
    notes containing a literal entity.  See `usernotes.format`.)

``uh``
    Writes need the account's modhash, which is fetched from ``/api/me.json``
    once and cached per client.  The modhash is a credential: it is never
    logged, and error messages never carry it.
"""

from __future__ import annotations

import dataclasses
import http.client
import http.cookiejar
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from ._logging import resolve_logger
from ._version import __version__
# The vendored jar type, for parity with what `usernotes.cookies` hands back:
# a caller should be able to pass a jar from either place to the same code, so
# both have `get_cookie_header` and `get_cookies_for_url`.  (Only the *type* is
# used here; nothing in this module goes looking for cookies.)
from ._vendor.browser_cookies import CookieJar
from .format import DEFAULT_BASE_URL, NOTES_PAGE
from .errors import (
    ApiError,
    ForbiddenError,
    InvalidPageError,
    NetworkError,
    NoWikiPageError,
    NotLoggedInError,
    PageFullError,
    RateLimitedError,
)

__all__ = [
    'DEFAULT_BASE_URL',
    'DEFAULT_USER_AGENT',
    'RedditWikiClient',
    'User',
    'cookie_jar',
]

#: User-Agent sent when the caller does not set one.  Reddit rejects requests
#: from a default urllib agent, and asks that agents identify themselves.
DEFAULT_USER_AGENT = (f'usernotes-cli/{__version__} '
                      '(reddit Moderator Toolbox usernotes tool)')

#: HTTP statuses worth retrying a *read* on.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Longest a retry will wait for Reddit's ``Retry-After`` before giving up and
#: reporting the rate limit to the user instead.  A couple of seconds is worth
#: absorbing silently; the ten minutes Reddit sometimes asks for is not, and a
#: command line tool that hangs with no output is worse than one that says
#: "try again in 600s" and exits.
_MAX_RETRY_DELAY = 5.0

#: Query parameters whose values are credentials, never to be logged.
_REDACTED_PARAMS = frozenset({'uh'})

#: Longest a retry backoff will grow to, however many attempts are allowed.
_MAX_BACKOFF = 8.0

_IP_RE = re.compile(r'[\d.]+')


@dataclasses.dataclass(frozen=True)
class User:
    """The Reddit account the cookies belong to.

    :param name: the username, as Reddit spells it.
    :param modhash: the ``uh`` value writes need, or ``None`` if Reddit did not
        send one.  A credential: do not log it.
    :param id: the account's fullname (``t2_...``), when Reddit sent one.
    """

    name: str
    modhash: str | None = None
    id: str | None = None


def cookie_jar(cookies, *, domain=None, secure=True):
    """Normalise any accepted cookie form into an `http.cookiejar.CookieJar`.

    :param cookies: see the module docstring for the accepted forms.
    :param domain: cookie domain to attach to cookies given as name/value
        pairs or as a header, since those carry no domain of their own.
        Defaults to ``.reddit.com``, which covers ``www`` and ``old``.
        Use ``secure=False`` for a plain-``http`` ``base_url`` (a test server),
        or the cookies will be withheld from the request.
    :param secure: whether the cookies should be marked "https only".
    :returns: a jar.  A jar passed in is returned unchanged.
    :raises ValueError: if *cookies* is not one of the accepted forms, or a
        ``Cookie:`` header or ``cookies.txt`` content does not parse.  Bad user
        input, not a network failure: the message says what is wrong.
    """
    if cookies is None:
        return CookieJar()
    if isinstance(cookies, http.cookiejar.CookieJar):
        return cookies
    if isinstance(cookies, (bytes, bytearray)):
        cookies = cookies.decode('utf-8', 'strict')

    domain = _fixup_domain(domain) if domain else _cookie_domain(DEFAULT_BASE_URL)

    if isinstance(cookies, str):
        return _jar_from_netscape(cookies) if _looks_like_netscape(cookies) \
            else _jar_from_header(cookies, domain=domain, secure=secure)

    if isinstance(cookies, dict) or hasattr(cookies, 'items'):
        try:
            pairs = list(cookies.items())
        except Exception as err:  # a Mapping-alike that will not cooperate
            raise ValueError(f'invalid cookies: {cookies!r}') from err
    elif hasattr(cookies, '__iter__'):
        pairs = list(cookies)
    else:
        raise ValueError(
            f'unsupported cookie source: {cookies!r}.  Pass a CookieJar, a '
            'mapping of names to values, a "name=value; name=value" header, '
            'or the text of a Netscape cookies.txt file.')

    jar = CookieJar()
    for pair in pairs:
        name, value = _unpack_pair(pair)
        jar.set_cookie(_make_cookie(name, value, domain=domain, secure=secure))
    return jar


class RedditWikiClient:
    """A small, polite client for Reddit's cookie-authenticated wiki API.

    :param cookies: anything `cookie_jar` accepts, or ``None`` for an
        anonymous client (fine for reading public wiki pages; writes will fail
        with `NotLoggedInError`).
    :param base_url: origin to talk to, without a trailing slash.  The default
        is ``https://www.reddit.com``; ``https://old.reddit.com`` works too.
    :param user_agent: ``User-Agent`` header to send.  Reddit blocks default
        urllib agents, so only override this with something equally honest.
    :param logger: see `usernotes._logging.resolve_logger`.  Never sees a
        credential: requests are logged as method, path and status only.
    :param timeout: socket timeout for a single request, in seconds.
    :param min_interval: minimum seconds between requests, to stay well inside
        Reddit's rate limits (toolbox waits 750ms after each wiki write).
        ``0`` disables the pacing.
    :param max_retries: how many times to try a request that failed in a way
        that is safe to retry - reads only, plus writes that Reddit answered
        with HTTP 429 (which it sends without applying them).
    :param retry_backoff: seconds to wait before the first retry; the wait
        doubles each attempt, to a few seconds.  ``0`` retries immediately,
        which is what tests want.
    :param cookie_domain: domain for cookies supplied as plain name/value
        pairs.  ``None`` derives it from *base_url*.
    :param opener: a pre-built ``urllib`` opener to send requests with, for
        callers who need their own handlers (proxies, retries, tests).  It must
        have an ``open(request, timeout=...)`` returning a response object.

    Instances are safe to share between threads; requests are paced under a
    lock so the rate limit holds across them.
    """

    def __init__(self, cookies=None, *, base_url=DEFAULT_BASE_URL, user_agent=None,
                 logger=None, timeout=30.0, min_interval=1.0, max_retries=3,
                 retry_backoff=1.0, cookie_domain=None, opener=None):
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f'invalid base_url: {base_url!r}')
        if max_retries < 1:
            raise ValueError(f'max_retries must be at least 1, not {max_retries}')
        if retry_backoff < 0:
            raise ValueError(f'retry_backoff cannot be negative: {retry_backoff!r}')

        self.base_url = base_url.rstrip('/')
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.log = resolve_logger(logger)

        self.cookies = cookie_jar(
            cookies,
            domain=cookie_domain or _cookie_domain(self.base_url),
            secure=urllib.parse.urlsplit(self.base_url).scheme == 'https')
        self._opener = opener if opener is not None else urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies))
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._account = None

    def __repr__(self):
        return f'<RedditWikiClient {self.base_url}>'

    # -- account ------------------------------------------------------------

    def account(self, *, refresh=False):
        """The signed-in account, fetching it once and caching it.

        :param refresh: ask Reddit again even if it is already known.
        :raises NotLoggedInError: if the cookies do not identify an account.
        """
        if self._account is None or refresh:
            body = self._get_json('/api/me.json', params={'raw_json': 1})
            data = body.get('data') if isinstance(body, dict) else None
            if not isinstance(data, dict) or not data.get('name'):
                raise NotLoggedInError(
                    'reddit does not recognise these cookies as a logged-in '
                    'account; log in to reddit in the browser the cookies came '
                    'from, or export fresh ones with --cookies FILE')
            self._account = User(name=data['name'], modhash=data.get('modhash'),
                                 id=data.get('id'))
            self.log.debug(f'signed in as u/{self._account.name}')
        return self._account

    def modhash(self, *, refresh=False):
        """The account's modhash (``uh``), which every write needs.

        :raises NotLoggedInError: if there is no account, or Reddit did not
            send a modhash with it.
        """
        account = self.account(refresh=refresh)
        if not account.modhash:
            raise NotLoggedInError(
                f'reddit did not send a modhash for u/{account.name}; '
                'cannot write to the wiki with this session')
        return account.modhash

    # -- wiki pages ---------------------------------------------------------

    def read_page(self, subreddit, page=NOTES_PAGE, *, missing_ok=False):
        """Read a wiki page's raw text.

        :param missing_ok: return ``None`` instead of raising when the page is
            missing or empty.  "Missing" is a normal state for usernotes: a
            subreddit that has never used them has no page.
        :returns: the page text (``data.content_md``), or ``None``.
        :raises NoWikiPageError: if the page is missing, empty, or the wiki is
            disabled, and *missing_ok* is false.
        :raises ForbiddenError: if the account may not read the page - for
            usernotes, the usual sign that it is not a moderator there.
        """
        path = f'/r/{subreddit}/wiki/{page}.json'
        try:
            body = self._get_json(path, params={'raw_json': 1})
        except NoWikiPageError:
            if missing_ok:
                self.log.debug(f'/r/{subreddit}/wiki/{page}: no page')
                return None
            raise
        data = body.get('data') if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ApiError(f'unexpected response shape from {path}', url=path)
        content = data.get('content_md')
        if not content:
            if missing_ok:
                return None
            raise NoWikiPageError(f'/r/{subreddit}/wiki/{page} is empty', url=path)
        self.log.debug(f'read /r/{subreddit}/wiki/{page} ({len(content)} bytes)')
        return content

    def read_json_page(self, subreddit, page=NOTES_PAGE, *, missing_ok=False):
        """Read a wiki page and parse it as JSON.

        :returns: the parsed page, or ``None`` when *missing_ok* and there is
            no page.
        :raises InvalidPageError: if the page text is not valid JSON.
        """
        content = self.read_page(subreddit, page, missing_ok=missing_ok)
        if content is None:
            return None
        try:
            return json.loads(content)
        except ValueError as err:
            raise InvalidPageError(
                f'/r/{subreddit}/wiki/{page} is not valid JSON: {err}') from err

    def page_exists(self, subreddit, page=NOTES_PAGE):
        """Whether the page exists and has content."""
        return self.read_page(subreddit, page, missing_ok=True) is not None

    def write_page(self, subreddit, page, content, *, reason=None, modhash=None):
        """Replace a wiki page's text.

        :param content: the new page text, written as given.  Callers that
            store JSON must minify it themselves (`usernotes.format.build_page`
            does): the wiki page has a hard size limit.
        :param reason: revision note.  Reddit shows it in the page history, so
            it is worth writing something a moderator would understand.
        :param modhash: an already-fetched modhash, to avoid a round trip.
        :raises PageFullError: if Reddit refuses the page as too large
            (HTTP 413) - toolbox calls this "usernotes full".
        :raises NotLoggedInError: if there is no usable session.
        """
        data = {'content': content, 'page': page, 'uh': modhash or self.modhash()}
        if reason:
            data['reason'] = reason
        self._post(f'/r/{subreddit}/api/wiki/edit', data=data)
        self.log.info(f'wrote /r/{subreddit}/wiki/{page} ({len(content)} bytes)')

    def set_page_settings(self, subreddit, page, *, listed=True, permlevel=2, modhash=None):
        """Re-apply a wiki page's visibility settings.

        Toolbox does this after every usernotes write: the page must stay
        *listed* and *mod only* (``permlevel=2``), or the subreddit's notes
        become readable by anyone.  Reddit does not reset the settings by
        itself, but neither does it guarantee them, and toolbox re-applies
        them "super extra double-secret secure" - so this does too.

        The parameters travel in the query string because that is where
        toolbox puts them for this endpoint, which is not a real API method
        and does not return JSON.

        :param listed: whether the page appears in the subreddit's wiki index.
        :param permlevel: who may read the page: 0 public, 1 subreddit
            members, 2 moderators only.
        """
        params = {
            'page': page,
            'listed': 'true' if listed else 'false',
            'permlevel': str(permlevel),
            'uh': modhash or self.modhash(),
        }
        self._request('POST', f'/r/{subreddit}/wiki/settings/', params=params)
        self.log.debug(f'page settings re-applied to /r/{subreddit}/wiki/{page}')

    def close(self):
        """Release the connection pool, if the opener has one."""
        close = getattr(self._opener, 'close', None)
        if callable(close):
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -- request plumbing ---------------------------------------------------

    def _get_json(self, path, params=None):
        status, raw = self._request('GET', path, params=params)
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as err:
            raise ApiError(
                f'reddit returned a response that is not JSON ({err})',
                status=status, url=self._safe_url(path, params)) from err

    def _post(self, path, *, data=None, params=None):
        status, raw = self._request('POST', path, data=data, params=params)
        self._check_write_response(status, raw, self._safe_url(path, params))
        return status, raw

    def _url(self, path, params=None):
        url = f'{self.base_url}{path}'
        if params:
            # doseq so a caller can pass repeated parameters if it ever needs to.
            url += '?' + urllib.parse.urlencode(params, doseq=True)
        return url

    def _safe_url(self, path, params=None):
        """The URL for a log line or an error message, with ``uh`` hidden."""
        return _redact(self._url(path, params))

    def _request(self, method, path, *, params=None, data=None):
        """Send one request, pacing and retrying it as the method allows.

        :returns: ``(status, body bytes)``.
        :raises ApiError: and its subclasses, for every failure mode; the
            exception carries Reddit's own ``reason`` when there was one.
        """
        url = self._url(path, params)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode('utf-8')
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header('User-Agent', self.user_agent)
        request.add_header('Accept', 'application/json')
        if body is not None:
            request.add_header('Content-Type', 'application/x-www-form-urlencoded')

        # Only a read is safe to repeat.  A write that Reddit answered with 429
        # was rejected before it was applied, so that one is safe too; a write
        # that failed with a timeout or a 5xx may have landed, and repeating it
        # would create a second note.
        retryable_post_statuses = frozenset({429})

        for attempt in range(1, self.max_retries + 1):
            self._wait_turn()
            logged = _redact(url)
            self.log.debug(f'{method} {logged}')
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    self.log.debug(f'{method} {logged} -> {response.status}')
                    return response.status, raw
            except urllib.error.HTTPError as err:
                raw = err.read()
                delay = _retry_after(err.headers)
                retryable = err.code in _RETRY_STATUSES and (
                    method == 'GET' or err.code in retryable_post_statuses)
                last_attempt = attempt >= self.max_retries
                if retryable and not last_attempt and (delay is None or delay <= _MAX_RETRY_DELAY):
                    wait = delay if delay is not None else self._backoff(attempt)
                    self.log.debug(f'{method} {logged} -> {err.code}, retrying in {wait:.1f}s')
                    time.sleep(wait)
                    continue
                self.log.debug(f'{method} {logged} -> {err.code}')
                raise _error_for(err.code, raw, logged, retry_after=delay,
                                 method=method) from err
            except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
                if method == 'GET' and attempt < self.max_retries:
                    wait = self._backoff(attempt)
                    self.log.debug(f'{method} {logged} failed ({err}), retrying in {wait:.1f}s')
                    time.sleep(wait)
                    continue
                hint = '' if method == 'GET' else (
                    ' - the request may or may not have been applied, so check '
                    'the page before repeating it')
                raise NetworkError(
                    f'{method} {logged} failed: {_describe_transport_error(err)}{hint}',
                    url=logged) from err

        raise AssertionError('unreachable: the retry loop always returns or raises')

    def _check_write_response(self, status, raw, url):
        """Raise if a 2xx write response still reports a failure.

        Reddit can answer a write with HTTP 200 and an error embedded in the
        body, so the status alone is not enough.
        """
        if not raw or not raw.strip():
            return
        try:
            body = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return      # not JSON (the wiki settings endpoint returns HTML)
        if not isinstance(body, dict):
            return
        payload = body.get('json')
        errors = payload.get('errors') if isinstance(payload, dict) else None
        if errors:
            raise ApiError(f'reddit rejected the request: {_describe_errors(errors)}',
                           status=status, url=url)
        if body.get('error'):
            raise ApiError(f'reddit rejected the request: {_describe_body(body)}',
                           status=status, url=url)

    def _backoff(self, attempt):
        """Seconds to wait before retry number *attempt* (1 for the first)."""
        return min(self.retry_backoff * 2 ** (attempt - 1), _MAX_BACKOFF)

    def _wait_turn(self):
        with self._lock:
            if self._last_request and self.min_interval > 0:
                wait = self.min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            self._last_request = time.monotonic()


def _error_for(status, raw, url, *, retry_after=None, method='GET'):
    """Map an HTTP failure onto the exception that describes it best."""
    reason, message = _reddit_error(raw)
    detail = f': {message}' if message else (f' ({reason})' if reason else '')
    # A write that failed with a server error may still have been applied -
    # the response is what went missing - so say so rather than let the caller
    # assume nothing happened and repeat it.
    ambiguous = (' - the request may or may not have been applied, so check the '
                 'page before repeating it' if method != 'GET' and status >= 500 else '')

    if status == 413:
        return PageFullError(
            f'the wiki page is too large for reddit to accept (HTTP 413, which '
            f'toolbox calls "usernotes full"){detail}',
            status=status, reason=reason, url=url)
    if status == 429:
        wait = f'; reddit asked us to wait {retry_after:g}s' if retry_after else ''
        return RateLimitedError(
            f'reddit is rate limiting this client (HTTP 429){wait}{detail}',
            status=status, reason=reason, url=url, retry_after=retry_after)
    if reason in ('PAGE_NOT_CREATED', 'WIKI_DISABLED'):
        return NoWikiPageError(
            f'the wiki page does not exist or the wiki is disabled '
            f'({reason}){detail}', status=status, reason=reason, url=url)
    if status == 404 and reason in (None, 'PAGE_NOT_CREATED'):
        return NoWikiPageError(f'no such wiki page{detail}',
                               status=status, reason=reason, url=url)
    if status == 401 or reason == 'USER_REQUIRED':
        return NotLoggedInError(
            f'reddit refused the request as unauthenticated{detail}',
            status=status, reason=reason, url=url)
    if status == 403:
        return ForbiddenError(
            f'reddit refused the request as forbidden{detail}.  Reading or '
            'writing a subreddit\'s usernotes needs an account that moderates '
            'it', status=status, reason=reason, url=url)
    if status >= 500:
        return NetworkError(f'reddit failed the request (HTTP {status}){detail}{ambiguous}',
                            status=status, reason=reason, url=url)
    return ApiError(f'reddit refused the request (HTTP {status}){detail}',
                    status=status, reason=reason, url=url)


def _reddit_error(raw):
    """Pull ``(reason, message)`` out of a Reddit error body."""
    if not raw:
        return None, None
    try:
        body = json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    reason = body.get('reason')
    parts = []
    if body.get('message'):
        parts.append(str(body['message']))
    errors = (body.get('json') or {}).get('errors') if isinstance(body.get('json'), dict) else None
    if errors:
        parts.append(_describe_errors(errors))
    if not parts:
        parts.append(_describe_body(body))
    return reason, '; '.join(part for part in parts if part)


def _describe_errors(errors):
    """Render Reddit's ``json.errors`` list, e.g. ``[['RATELIMIT', '...', '']]``."""
    rendered = []
    for error in errors:
        if isinstance(error, (list, tuple)):
            rendered.append(': '.join(str(part) for part in error if part))
        else:
            rendered.append(str(error))
    return '; '.join(rendered)


def _describe_body(body):
    """A short, safe description of a JSON body, never including its values.

    Reddit's error bodies are small and structured; anything unrecognised is
    described by its keys so an unexpected response cannot dump a huge page
    into an error message.
    """
    keys = ', '.join(sorted(str(key) for key in body))
    return f'body with keys: {keys}' if keys else 'empty body'


def _redact(url):
    """Hide credential parameters in a URL, for logs and error messages.

    The modhash travels in the query string for the wiki settings endpoint
    (that is where toolbox puts it), and it is as good as a password for the
    length of the session: nobody should find it in a log file or a traceback.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    query = [(name, 'redacted' if name in _REDACTED_PARAMS else value)
             for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _describe_transport_error(err):
    if isinstance(err, urllib.error.URLError) and err.reason is not None:
        return f'{err.reason}'
    return f'{err}'


def _retry_after(headers):
    """Seconds to wait, from ``Retry-After`` or ``X-Ratelimit-Reset``."""
    if headers is None:
        return None
    value = headers.get('Retry-After')
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass    # an HTTP-date; Reddit sends seconds, so ignore the rest
    value = headers.get('X-Ratelimit-Reset')
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    return None


def _cookie_domain(url):
    """The cookie domain for *url*, for cookies that carry none of their own.

    Cookies given as a mapping or a header have no domain, so one has to be
    invented from the URL.  A subdomain gets its parent (``www.reddit.com`` ->
    ``.reddit.com``) so that ``www`` and ``old`` share one session; an IP
    address is used as it is, and a single-label host is adjusted for the
    standard library's policy (see `_fixup_domain`).
    """
    host = urllib.parse.urlsplit(url).hostname or 'www.reddit.com'
    if _IP_RE.fullmatch(host) or host.count('.') < 2:
        return _fixup_domain(host)
    return '.' + '.'.join(host.split('.')[-2:])


def _fixup_domain(domain):
    """Turn a domain into one ``http.cookiejar`` will actually match.

    A host with no dot in it is compared by the standard library policy
    against ``<host>.local`` (see ``eff_request_host``), so a cookie written
    for a bare ``localhost`` is never sent anywhere.  ``.localhost.local`` is
    the domain that policy does match, which matters to anyone pointing this
    client at a local test server.
    """
    if _IP_RE.fullmatch(domain) or '.' in domain:
        return domain
    return f'.{domain}.local'


def _looks_like_netscape(text):
    stripped = text.lstrip()
    if stripped.startswith('#'):
        return True
    # A header string has no newlines; a cookie file has one line per cookie,
    # and those lines are tab separated.
    return '\n' in text and any('\t' in line for line in text.splitlines() if line.strip())


def _jar_from_header(header, *, domain, secure):
    """Parse a ``Cookie:`` header value into a jar."""
    text = header.strip()
    prefix = 'cookie:'
    if text.lower().startswith(prefix):
        text = text[len(prefix):].strip()
    jar = CookieJar()
    for chunk in text.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, value = chunk.partition('=')
        name = name.strip()
        if not sep or not name:
            raise ValueError(
                f'invalid cookie header: {chunk!r} is not "name=value".  Pass '
                'the whole header value, e.g. "reddit_session=abc; token=def"')
        jar.set_cookie(_make_cookie(name, value.strip(), domain=domain, secure=secure))
    if not len(jar):
        raise ValueError('invalid cookie header: no cookies in it')
    return jar


def _jar_from_netscape(text):
    """Parse the content of a Netscape ``cookies.txt`` file into a jar."""
    jar = CookieJar()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.lstrip().startswith(('#', '$')) or not line.strip():
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            raise ValueError(
                f'invalid cookies.txt content: line {number} has {len(parts)} '
                'tab-separated fields, expected 7')
        domain, domain_specified, path, secure, expires, name = parts[:6]
        # A value should not contain a tab, but if one does it is still the
        # value rather than another field.
        value = '\t'.join(parts[6:])
        if name == '':
            name, value = value, None
        # curl and the browsers write a session cookie as `0`; an expiry of
        # zero would otherwise read as "expired in 1970" and never be sent.
        expires = None if expires in ('', '0') else int(expires)
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=(domain_specified == 'TRUE'),
            domain_initial_dot=domain.startswith('.'), path=path, path_specified=False,
            secure=(secure == 'TRUE'),
            expires=expires,
            discard=expires is None, comment=None, comment_url=None, rest={},
            rfc2109=False))
    if not len(jar):
        raise ValueError('invalid cookies.txt content: no cookies in it')
    return jar


def _make_cookie(name, value, *, domain, secure):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=domain.startswith('.'),
        path='/', path_specified=True, secure=secure, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False)


def _unpack_pair(pair):
    try:
        name, value = pair
    except (TypeError, ValueError) as err:
        raise ValueError(f'invalid cookie entry: {pair!r} (expected a name and a value)') from err
    if not isinstance(name, str) or not name:
        raise ValueError(f'invalid cookie name: {name!r}')
    if value is None:
        value = ''
    elif not isinstance(value, str):
        value = str(value)
    return name, value
