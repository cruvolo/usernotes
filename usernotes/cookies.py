# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Retrieve the browser cookies needed to talk to Reddit as a moderator.

The usernotes wiki page can only be read (and written) by a moderator of the
subreddit, so this project needs an authenticated Reddit session.  The least
surprising way to get one is the same as the moderator toolbox's: use the
cookies from the browser the user is already logged in to Reddit with.

This module locates the browser's own on-disk cookie store (Firefox's
``cookies.sqlite``, a Chromium-family ``Cookies`` SQLite database, or Safari's
``Cookies.binarycookies``), decrypts the values where the OS requires it, and
hands back a cookie jar.  It is built on code vendored from `yt-dlp`, so it
behaves like ``yt-dlp --cookies-from-browser`` does - see
`usernotes._vendor.browser_cookies` and ``browser-cookies.md``.

Nothing here talks to Reddit: no network access, no cookies written to disk.
Extraction reads a *live* user profile (read-only, via a copy of the database),
so the jar is sensitive data - see `Security`_ below.

Quick start
-----------

.. code-block:: python

    from usernotes import cookies

    jar = cookies.load_cookie_jar(from_browser='firefox')
    jar.get_cookie_header('https://www.reddit.com')     # 'reddit_session=...; ...'

For a one-off request:

.. code-block:: python

    header = cookies.load_cookie_header('https://www.reddit.com', from_browser='chrome')

Cookies alone are not enough to use Reddit's wiki API: the write path also
needs a ``uh`` (modhash) query parameter, which toolbox reads from
``/api/me.json`` as ``data.modhash``.  Fetch that over the authenticated session
after loading cookies here.

Interface for the command line tool
-----------------------------------

============================== ==========================================
CLI option                     Use
============================== ==========================================
``--cookies-from-browser``     `parse_browser_spec` on the raw option
                               value, then pass the result (or the raw
                               string) as ``from_browser=`` to
                               `load_cookie_jar`.
``--cookies FILE``             ``cookie_file=`` on `load_cookie_jar`.
both                           Pass both; the file is loaded second, so the
                               file wins for cookies present in both.
``-v`` / ``--verbose``         Build a `Logger` with ``level='info'`` /
                               ``'debug'``; the default emits warnings and
                               errors only.
============================== ==========================================

Error handling the CLI is expected to implement:

* `parse_browser_spec` raises `ValueError` for malformed or unsupported user
  input - report it as a usage error (argparse does this for you via
  ``type=``).
* `load_cookie_jar` and `load_cookie_header` raise `CookieLoadError`, whose
  message already includes the underlying reason, for anything that went wrong
  while extracting - report it to stderr and exit non-zero.
  `FatalCookieLoadError` marks the subset that retrying cannot fix.
* An empty jar is not an error here, but it means the request will be
  unauthenticated.  `cookie_names` returns the names (never the values) of the
  cookies that apply to a URL, which is enough to detect that case and tell the
  user to log in / pick another browser.

Security
--------

Cookies are bearer credentials - anyone holding them *is* the logged-in
moderator.  Therefore: never print or log a jar or a ``Cookie:`` header (not
even at debug level), never write them to git, and do not keep them around
longer than the request needs.  Only cookie *names* are safe to display.

Cross-platform notes
--------------------

* Chromium-family browsers on Linux/macOS encrypt cookie values with a key held
  in the desktop keyring (GNOME keyring, KWallet, or Chrome's ``basic`` store).
  Reading it may prompt for keychain access on macOS and fail on a locked
  keyring on Linux; the CLI should point the user at ``--cookies FILE`` as an
  alternative.  GNOME keyring support needs the optional ``secretstorage``
  package.
* On Windows, DPAPI decryption only works when running as the same user account
  as the browser.
* Firefox needs no decryption anywhere, and is the most reliable source.

API reference
-------------

.. data:: SUPPORTED_BROWSERS

   Frozenset of browser names accepted in a browser specification.

.. data:: SUPPORTED_KEYRINGS

   Frozenset of Linux keyring names accepted after ``+`` in a browser
   specification.

.. data:: BROWSER_SPEC_METAVAR

   The grammar of a browser specification, for use as an argparse metavar.
"""

import dataclasses
import os
import re

from ._logging import Logger, NULL_LOGGER, NullLogger, resolve_logger  # noqa: F401  (public facade)
from ._vendor import browser_cookies as _bc

__all__ = [
    'BROWSER_SPEC_METAVAR',
    'BrowserSpec',
    'CookieJar',
    'CookieLoadError',
    'FatalCookieLoadError',
    'Logger',
    'NULL_LOGGER',
    'NullLogger',
    'REDDIT_ORIGIN',
    'SUPPORTED_BROWSERS',
    'SUPPORTED_KEYRINGS',
    'cookie_names',
    'load_cookie_header',
    'load_cookie_jar',
    'parse_browser_spec',
]

#: Browser names accepted in a browser specification, e.g. ``'firefox'``.
SUPPORTED_BROWSERS = frozenset(_bc.SUPPORTED_BROWSERS)

#: Linux keyring names accepted after ``+`` in a browser specification.
#: Lower-case, as the user types them (``chromium+gnomekeyring``).
SUPPORTED_KEYRINGS = frozenset(name.lower() for name in _bc.SUPPORTED_KEYRINGS)

#: Grammar of the ``--cookies-from-browser`` value, for argparse metavars.
BROWSER_SPEC_METAVAR = 'BROWSER[+KEYRING][:PROFILE][::CONTAINER]'

#: Origin used when asking a jar for Reddit's cookies.
REDDIT_ORIGIN = 'https://www.reddit.com'

#: The cookie jar type returned by `load_cookie_jar`.  It is an
#: `http.cookiejar.CookieJar`, so it can be handed straight to
#: ``urllib.request.HTTPCookieProcessor`` or used with ``requests`` via
#: ``HTTPCookieProcessor``-style adapters.
CookieJar = _bc.CookieJar


class CookieLoadError(Exception):
    """Cookies could not be loaded.

    The message includes the underlying reason, so it is suitable for showing
    to the user as-is.  ``__cause__`` carries the original exception.
    """


class FatalCookieLoadError(CookieLoadError):
    """Cookies could not be loaded and retrying will not help.

    Raised when the failure is not a matter of the user passing different
    arguments: an undecryptable cookie store, or a database that cannot be
    read at all.  Callers that would otherwise suggest an alternative cookie
    source should not bother.
    """


@dataclasses.dataclass(frozen=True)
class BrowserSpec:
    """A parsed ``--cookies-from-browser`` value.

    The grammar is yt-dlp's::

        BROWSER[+KEYRING][:PROFILE][::CONTAINER]

    * ``BROWSER`` - one of `SUPPORTED_BROWSERS`, case-insensitive.
    * ``KEYRING`` - Linux only, one of `SUPPORTED_KEYRINGS`, case-insensitive.
    * ``PROFILE`` - profile name as the browser knows it, or a path to a
      profile directory.  Default: the most recently used profile.
    * ``CONTAINER`` - Firefox only: a container name, or the literal ``none``
      for cookies outside any container.  Default: all containers.

    Use `parse_browser_spec` to build one from user input; constructing
    directly is fine too, and raises `ValueError` for unsupported values.

    :raises ValueError: if *browser* or *keyring* is not supported.
    """

    browser: str
    profile: str | None = None
    keyring: str | None = None
    container: str | None = None

    def __post_init__(self):
        browser = self.browser.lower()
        if browser not in SUPPORTED_BROWSERS:
            raise ValueError(
                f'unsupported browser: "{self.browser}". '
                f'Supported browsers are: {", ".join(sorted(SUPPORTED_BROWSERS))}')
        object.__setattr__(self, 'browser', browser)

        if self.keyring is not None:
            keyring = self.keyring.upper()
            if keyring not in _bc.SUPPORTED_KEYRINGS:
                raise ValueError(
                    f'unsupported keyring: "{self.keyring}". '
                    f'Supported keyrings are: {", ".join(sorted(SUPPORTED_KEYRINGS))}')
            object.__setattr__(self, 'keyring', keyring)

    def __str__(self):
        """Render back to the ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` form.

        Safe to log: a specification names a browser and a profile, never a
        cookie value.
        """
        spec = self.browser
        if self.keyring:
            spec += f'+{self.keyring.lower()}'
        if self.profile:
            spec += f':{self.profile}'
        if self.container:
            spec += f'::{self.container}'
        return spec

    def as_tuple(self):
        """``(browser, profile, keyring, container)``, for the vendored loader."""
        return (self.browser, self.profile, self.keyring, self.container)


def parse_browser_spec(value):
    """Parse a ``--cookies-from-browser`` value into a `BrowserSpec`.

    :param value: the raw option value, e.g. ``'firefox'``,
        ``'chrome+gnomekeyring:Default'`` or ``'firefox::work'``.  Whitespace
        around the separators is ignored.  A ``~`` in a profile path is
        expanded when the cookies are loaded, not here, so `BrowserSpec.profile`
        holds what the user typed.
    :raises ValueError: if the value does not parse, or names a browser or
        keyring that is not supported.  The message lists what is supported,
        so it can be shown to the user directly.
    """
    if not isinstance(value, str):
        raise ValueError(f'invalid browser specification: {value!r}')
    match = _BROWSER_SPEC_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            f'invalid browser specification: "{value}". '
            f'Expected {BROWSER_SPEC_METAVAR}')
    browser, keyring, profile, container = match.group('browser', 'keyring', 'profile', 'container')
    return BrowserSpec(browser=browser, profile=profile, keyring=keyring, container=container)


def load_cookie_jar(from_browser=None, cookie_file=None, *, logger=None):
    """Load cookies from a browser and/or a Netscape-format cookie file.

    :param from_browser: a `BrowserSpec`, or the raw
        ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` string to parse with
        `parse_browser_spec`.  ``None`` to skip.
    :param cookie_file: path to a Netscape ``cookies.txt`` file, e.g. one
        exported by a browser extension.  ``None`` to skip.  Useful on machines
        where the browser's cookie store cannot be read, and for testing.
    :param logger: object with ``debug``, ``info``, ``warning`` and ``error``
        methods taking one message (``warning`` also takes ``only_once``); see
        `Logger`.  Defaults to a `Logger` writing warnings and errors to
        stderr, which is what a CLI wants.  A logger may additionally provide a
        ``progress_bar()`` method returning a context manager with a ``print``
        method, if it wants progress output.
    :returns: a `CookieJar` with everything found.  The file, when both sources
        are given, is loaded second and therefore wins for cookies that exist
        in both.
    :raises ValueError: if *from_browser* is a malformed specification, or
        neither source is given.  Both mean the caller has a bug or bad user
        input, not that cookie extraction failed.
    :raises CookieLoadError: if a source could not be read.
    """
    if from_browser is None and cookie_file is None:
        raise ValueError('no cookie source given: expected from_browser or cookie_file')
    if cookie_file is not None and not isinstance(cookie_file, (str, bytes, os.PathLike)):
        raise ValueError(f'invalid cookie file: {cookie_file!r}')
    if isinstance(from_browser, str):
        from_browser = parse_browser_spec(from_browser)
    elif from_browser is not None and not isinstance(from_browser, BrowserSpec):
        raise ValueError(f'invalid browser specification: {from_browser!r}')

    logger = resolve_logger(logger)
    try:
        return _bc.load_cookies(
            None if cookie_file is None else os.fspath(cookie_file),
            None if from_browser is None else from_browser.as_tuple(),
            logger)
    except _bc.FatalCookieError as err:
        raise FatalCookieLoadError(str(err)) from err
    except Exception as err:
        raise CookieLoadError(str(err)) from err


def load_cookie_header(url, from_browser=None, cookie_file=None, *, logger=None):
    """Load cookies and return the ``Cookie:`` header value for *url*.

    Convenience wrapper around `load_cookie_jar` for a single request.  Prefer
    loading the jar once and calling ``CookieJar.get_cookie_header`` per URL
    when more than one request is made - extraction reads the browser's
    database each time.

    :returns: the header value, or ``None`` when no cookie applies to *url*.
    :raises ValueError: as `load_cookie_jar`.
    :raises CookieLoadError: as `load_cookie_jar`.

    The returned string is a credential: do not log it or put it in an error
    message.
    """
    return load_cookie_jar(from_browser, cookie_file, logger=logger).get_cookie_header(url)


def cookie_names(jar, url=REDDIT_ORIGIN):
    """Names of the cookies in *jar* that apply to *url*, sorted.

    Values are never returned, so this is safe to print.  A CLI can use it to
    tell "the browser has no Reddit cookies, log in there or use --cookies
    FILE" apart from "we sent cookies and Reddit still says we are not logged
    in".
    """
    return tuple(sorted({cookie.name for cookie in jar.get_cookies_for_url(url)}))


# Grammar from yt-dlp, which accepts `BROWSER[+KEYRING][:PROFILE][::CONTAINER]`.
_BROWSER_SPEC_RE = re.compile(r'''(?x)
    (?P<browser>[^+:]+)
    (?:\s*\+\s*(?P<keyring>[^:]+))?
    (?:\s*:\s*(?!:)(?P<profile>.+?))?
    (?:\s*::\s*(?P<container>.+))?
''')
