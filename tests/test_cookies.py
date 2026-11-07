"""Tests for :mod:`usernotes.cookies` and the vendored code behind it.

The suite is deliberately non-destructive: it builds throwaway browser profiles
(Chromium ``Cookies`` and Firefox ``cookies.sqlite`` databases) in temporary
directories and reads only those.  No real browser profile, no network, no
Reddit, no wiki page.

Run from the repository root::

    python3 -m unittest discover -v
"""

import contextlib
import hashlib
import http.cookiejar
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

from usernotes import cookies
from usernotes._vendor import aes as vendored_aes
from usernotes._vendor import browser_cookies as vendored_cookies

REDDIT_URL = 'https://www.reddit.com'

# Chrome's "basic" Linux key.  Values encrypted with it are what Chromium on
# Linux writes when the desktop keyring is unavailable (``v10`` cookies), so a
# test can produce real ones without a keyring.
CHROME_V10_KEY = hashlib.pbkdf2_hmac('sha1', b'peanuts', b'saltysalt', 1, 16)
CHROME_V10_IV = b' ' * 16

# The secret the browser keeps in the desktop keyring for its ``v11`` cookies.
# A stand-in: no test reads a real keyring, and none of these values leaves the
# temporary profile it was written for.
CHROME_KEYRING_SECRET = b'c2VjcmV0LWZyb20tdGhlLWtleXJpbmc='
CHROME_V11_IV = b' ' * 16


def pkcs7_pad(data):
    pad = 16 - len(data) % 16
    return data + bytes([pad]) * pad


def chrome_encrypt(value, *, hash_prefix=False):
    """Encrypt *value* the way Chromium's Linux "basic" store does."""
    plaintext = value.encode()
    if hash_prefix:
        # Chromium >= meta version 24 prepends a SHA-256 of the (host, name)
        plaintext = hashlib.sha256(b'reddit_session').digest() + plaintext
    return b'v10' + vendored_aes.aes_cbc_encrypt_bytes(
        pkcs7_pad(plaintext), CHROME_V10_KEY, CHROME_V10_IV)


def chrome_v11_encrypt(value, secret=CHROME_KEYRING_SECRET):
    """Encrypt *value* the way Chromium does when the desktop keyring has a key.

    The key is derived from the secret the browser stores in the keyring, which
    is why a store like this cannot be read without asking the keyring for it.
    """
    key = hashlib.pbkdf2_hmac('sha1', secret, b'saltysalt', 1, 16)
    return b'v11' + vendored_aes.aes_cbc_encrypt_bytes(
        pkcs7_pad(value.encode()), key, CHROME_V11_IV)


@contextlib.contextmanager
def report_from_mate():
    """The desktop the bug report came from: MATE, started by lightdm.

    ``XDG_CURRENT_DESKTOP=MATE`` is a name chromium's own detection does not
    know, and ``DESKTOP_SESSION=lightdm-xsession`` is not ``mate``, so the
    desktop is *other* - which the vendored code reads as basic-text, no key.
    """
    with unittest.mock.patch.dict(os.environ, {
            'XDG_CURRENT_DESKTOP': 'MATE',
            'DESKTOP_SESSION': 'lightdm-xsession',
    }, clear=True):
        yield


@contextlib.contextmanager
def secret_service_stub(secret=CHROME_KEYRING_SECRET):
    """Stand in for reading the browser's key out of the freedesktop keyring.

    Yields the mock, so a test can assert whether the key was asked for at all.
    """
    with unittest.mock.patch.object(
            vendored_cookies, '_get_gnome_keyring_password',
            return_value=secret) as lookup:
        yield lookup


def write_chrome_profile(root, rows, meta_version=23):
    """Create ``<root>/Cookies`` with the schema Chromium uses."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, 'Cookies')
    conn = sqlite3.connect(path)
    try:
        conn.execute('CREATE TABLE meta (key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR)')
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(meta_version),))
        conn.execute('''
            CREATE TABLE cookies (
                host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB,
                path TEXT, expires_utc INTEGER, is_secure INTEGER)''')
        for row in rows:
            conn.execute('INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?)', row)
        conn.commit()
    finally:
        conn.close()
    return path


def write_firefox_profile(root, rows, schema_version=15, containers=None):
    """Create ``<root>/cookies.sqlite`` with the schema Firefox uses."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, 'cookies.sqlite')
    conn = sqlite3.connect(path)
    try:
        conn.execute('''
            CREATE TABLE moz_cookies (
                id INTEGER PRIMARY KEY, originAttributes TEXT DEFAULT '',
                name TEXT, value TEXT, host TEXT, path TEXT,
                expiry INTEGER, isSecure INTEGER)''')
        conn.execute(f'PRAGMA user_version = {schema_version:d}')
        for row in rows:
            conn.execute(
                'INSERT INTO moz_cookies (originAttributes, name, value, host, path, expiry, isSecure) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)', row)
        conn.commit()
    finally:
        conn.close()
    if containers is not None:
        with open(os.path.join(root, 'containers.json'), 'w', encoding='utf-8') as f:
            json.dump({'identities': containers}, f)
    return path


def cookie_map(jar):
    """``{name: Cookie}`` for the Reddit URL."""
    return {cookie.name: cookie for cookie in jar.get_cookies_for_url(REDDIT_URL)}


def read_file(path):
    with open(path, 'rb') as f:
        return f.read()


def write_netscape_file(path, *lines):
    """Write *lines* as a Netscape cookies.txt.

    The magic header line is not decoration: ``http.cookiejar`` refuses a file
    without it.
    """
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# Netscape HTTP Cookie File\n')
        for line in lines:
            f.write(line + '\n')
    return path


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='usernotes-test-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmpdir, *parts)


class BrowserSpecTest(unittest.TestCase):
    def test_plain_browser(self):
        spec = cookies.parse_browser_spec('firefox')
        self.assertEqual(spec.browser, 'firefox')
        self.assertIsNone(spec.profile)
        self.assertIsNone(spec.keyring)
        self.assertIsNone(spec.container)

    def test_browser_is_case_insensitive(self):
        self.assertEqual(cookies.parse_browser_spec('FireFox').browser, 'firefox')

    def test_keyring_is_normalized_to_upper(self):
        spec = cookies.parse_browser_spec('chromium+GnomeKeyring')
        self.assertEqual(spec.keyring, 'GNOMEKEYRING')
        self.assertEqual(cookies.parse_browser_spec('chromium+GNOMEKEYRING'), spec)

    def test_profile_and_container(self):
        spec = cookies.parse_browser_spec('firefox:Profile 2::work')
        self.assertEqual(spec.profile, 'Profile 2')
        self.assertEqual(spec.container, 'work')

    def test_profile_path(self):
        spec = cookies.parse_browser_spec('chrome:/tmp/some profile')
        self.assertEqual(spec.profile, '/tmp/some profile')

    def test_none_container(self):
        self.assertEqual(cookies.parse_browser_spec('firefox::none').container, 'none')

    def test_round_trip_through_str(self):
        for value in ('firefox', 'chrome+basictext:Default', 'firefox:p::work'):
            self.assertEqual(str(cookies.parse_browser_spec(value)), value)

    def test_unsupported_browser(self):
        with self.assertRaises(ValueError) as ctx:
            cookies.parse_browser_spec('netscape')
        self.assertIn('unsupported browser', str(ctx.exception))
        self.assertIn('firefox', str(ctx.exception))

    def test_unsupported_keyring(self):
        with self.assertRaises(ValueError) as ctx:
            cookies.parse_browser_spec('chrome+nosuchring')
        self.assertIn('unsupported keyring', str(ctx.exception))

    def test_empty_value(self):
        with self.assertRaises(ValueError):
            cookies.parse_browser_spec('')

    def test_direct_construction_validates(self):
        with self.assertRaises(ValueError):
            cookies.BrowserSpec('netscape')
        with self.assertRaises(ValueError):
            cookies.BrowserSpec('chrome', keyring='nosuchring')

    def test_as_tuple(self):
        spec = cookies.BrowserSpec('chrome', profile='Default', keyring='basictext', container=None)
        self.assertEqual(spec.as_tuple(), ('chrome', 'Default', 'BASICTEXT', None))

    def test_supported_names_are_advertised(self):
        self.assertIn('firefox', cookies.SUPPORTED_BROWSERS)
        self.assertIn('gnomekeyring', cookies.SUPPORTED_KEYRINGS)


class LoggerTest(unittest.TestCase):
    def test_default_level_hides_debug_and_info(self):
        stream = io.StringIO()
        logger = cookies.Logger(stream=stream)
        logger.debug('d')
        logger.info('i')
        logger.warning('w')
        logger.error('e')
        self.assertEqual(stream.getvalue(), 'warning: w\nerror: e\n')

    def test_debug_level_shows_everything(self):
        stream = io.StringIO()
        logger = cookies.Logger(stream=stream, level='debug')
        logger.debug('d')
        self.assertEqual(stream.getvalue(), 'debug: d\n')

    def test_only_once_suppresses_repeats(self):
        stream = io.StringIO()
        logger = cookies.Logger(stream=stream)
        logger.warning('same', only_once=True)
        logger.warning('same', only_once=True)
        logger.warning('same')
        self.assertEqual(stream.getvalue(), 'warning: same\nwarning: same\n')

    def test_unknown_level(self):
        with self.assertRaises(ValueError):
            cookies.Logger(level='nope')

    def test_null_logger_is_silent(self):
        logger = cookies.NULL_LOGGER
        logger.debug('d')
        logger.info('i')
        logger.warning('w', only_once=True)
        logger.error('e')

    def test_invalid_logger_is_rejected(self):
        with self.assertRaises(ValueError):
            cookies.load_cookie_header(REDDIT_URL, cookie_file='unused.txt', logger=object())


class NetscapeCookieFileTest(TempDirTestCase):
    """The ``--cookies FILE`` source, which needs no browser at all."""

    def write_cookies_txt(self, *lines):
        return write_netscape_file(self.path('cookies.txt'), *lines)

    def test_session_cookie_is_used(self):
        path = self.write_cookies_txt(
            '.reddit.com\tTRUE\t/\tFALSE\t0\treddit_session\ts3cret')
        jar = cookies.load_cookie_jar(cookie_file=path, logger=cookies.NULL_LOGGER)
        cookie = cookie_map(jar)['reddit_session']
        # `expires=0` is a session cookie, and must not be dropped as expired
        self.assertIsNone(cookie.expires)
        self.assertTrue(cookie.discard)
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')

    def test_pathlib_path_is_accepted(self):
        import pathlib
        path = pathlib.Path(self.write_cookies_txt(
            '.reddit.com\tTRUE\t/\tFALSE\t1999999999\treddit_session\tx'))
        header = cookies.load_cookie_header(REDDIT_URL, cookie_file=path, logger=cookies.NULL_LOGGER)
        self.assertEqual(header, 'reddit_session=x')

    def test_missing_file_is_an_error(self):
        with self.assertRaises(cookies.CookieLoadError) as ctx:
            cookies.load_cookie_jar(cookie_file=self.path('nope.txt'), logger=cookies.NULL_LOGGER)
        self.assertIn('not readable', str(ctx.exception))

    def test_json_file_gets_a_helpful_error(self):
        path = self.path('cookies.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('[{"domain": ".reddit.com"}]')
        with self.assertRaises(cookies.CookieLoadError) as ctx:
            cookies.load_cookie_jar(cookie_file=path, logger=cookies.NULL_LOGGER)
        self.assertIn('Netscape formatted', str(ctx.exception))

    def test_save_and_reload_round_trip(self):
        source = self.write_cookies_txt(
            '.reddit.com\tTRUE\t/\tFALSE\t0\treddit_session\ts3cret')
        jar = cookies.load_cookie_jar(cookie_file=source, logger=cookies.NULL_LOGGER)
        dump = self.path('dump.txt')
        jar.save(dump)
        with open(dump, encoding='utf-8') as f:
            self.assertIn('reddit_session\ts3cret', f.read())
        reloaded = cookies.load_cookie_jar(cookie_file=dump, logger=cookies.NULL_LOGGER)
        self.assertEqual(reloaded.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')

    def test_no_source_is_a_caller_error(self):
        with self.assertRaises(ValueError):
            cookies.load_cookie_jar()

    def test_cookie_names_never_leaks_values(self):
        path = self.write_cookies_txt(
            '.reddit.com\tTRUE\t/\tFALSE\t0\treddit_session\ts3cret',
            '.reddit.com\tTRUE\t/\tFALSE\t0\ttoken_v2\ttok')
        jar = cookies.load_cookie_jar(cookie_file=path, logger=cookies.NULL_LOGGER)
        names = cookies.cookie_names(jar)
        self.assertEqual(names, ('reddit_session', 'token_v2'))
        self.assertNotIn('s3cret', str(names))


@unittest.skipUnless(sys.platform.startswith('linux'), 'Chromium key derivation differs per platform')
class ChromiumCookieTest(TempDirTestCase):
    """Chromium extraction, using values encrypted with Chrome's "basic" key."""

    def load(self, profile, logger=cookies.NULL_LOGGER):
        spec = cookies.BrowserSpec('chrome', profile=profile, keyring='basictext')
        return cookies.load_cookie_jar(from_browser=spec, logger=logger)

    def test_v10_cookie_is_decrypted(self):
        profile = self.path('chrome-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_encrypt('s3cret'), '/', 0, 1),
        ])
        jar = self.load(profile)
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')
        self.assertIsNone(cookie_map(jar)['reddit_session'].expires)

    def test_hash_prefix_is_stripped_on_newer_schemas(self):
        profile = self.path('chrome-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_encrypt('s3cret', hash_prefix=True), '/', 0, 1),
        ], meta_version=24)
        self.assertEqual(self.load(profile).get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')

    def test_plaintext_cookie_is_used_as_is(self):
        profile = self.path('chrome-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', 'plain-value', b'', '/', 0, 1),
        ])
        self.assertEqual(self.load(profile).get_cookie_header(REDDIT_URL), 'reddit_session=plain-value')

    def test_undecryptable_cookie_is_skipped_not_fatal(self):
        profile = self.path('chrome-profile')
        write_chrome_profile(profile, [
            # v11 needs a keyring key we do not have (basictext provides none)
            ('.reddit.com', 'reddit_session', '', b'v11' + os.urandom(32), '/', 0, 1),
            ('.reddit.com', 'token_v2', '', chrome_encrypt('tok'), '/', 0, 1),
        ])
        stream = io.StringIO()
        jar = self.load(profile, logger=cookies.Logger(stream=stream))
        self.assertEqual(cookie_map(jar).keys(), {'token_v2'})
        self.assertIn('cannot decrypt v11 cookies', stream.getvalue())

    def test_non_reddit_cookies_are_kept_but_not_sent_to_reddit(self):
        profile = self.path('chrome-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_encrypt('s3cret'), '/', 0, 1),
            ('.example.com', 'other', '', chrome_encrypt('nope'), '/', 0, 1),
        ])
        jar = self.load(profile)
        self.assertIn('other', {cookie.name for cookie in jar})
        self.assertEqual(cookie_map(jar).keys(), {'reddit_session'})

    def test_missing_database_is_an_error(self):
        with self.assertRaises(cookies.CookieLoadError) as ctx:
            self.load(self.path('no-such-profile'))
        self.assertIn('could not find chrome cookies database', str(ctx.exception))

    def test_profile_is_not_written_to(self):
        profile = self.path('chrome-profile')
        path = write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_encrypt('s3cret'), '/', 0, 1),
        ])
        before = os.stat(path).st_mtime_ns, read_file(path)
        self.load(profile)
        after = os.stat(path).st_mtime_ns, read_file(path)
        self.assertEqual(before, after)
        # The live database is opened through a temporary copy, which is removed
        self.assertEqual(sorted(os.listdir(profile)), ['Cookies'])


class ChromiumKeyringTest(TempDirTestCase):
    """The v11 cookies, whose key comes from the desktop keyring.

    No test here touches a real keyring: the secret service lookup is stubbed
    out, and the environment is the one from the bug report - MATE started from
    lightdm, which is a desktop neither chromium nor the vendored detection has
    a name for, so the keyring is guessed to be basic-text and the guessed
    answer has to be questioned when a v11 cookie turns up.
    """

    def load(self, profile, keyring=None, logger=cookies.NULL_LOGGER, secret=CHROME_KEYRING_SECRET):
        spec = cookies.BrowserSpec('chromium', profile=profile, keyring=keyring)
        with report_from_mate(), secret_service_stub(secret) as lookup:
            self.lookup = lookup
            return cookies.load_cookie_jar(from_browser=spec, logger=logger)

    def test_v11_cookie_is_decrypted_from_the_secret_service(self):
        profile = self.path('chromium-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_v11_encrypt('s3cret'), '/', 0, 1),
        ])
        jar = self.load(profile)
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')
        self.lookup.assert_called_once()

    def test_v11_cookie_is_skipped_when_the_secret_service_has_nothing(self):
        profile = self.path('chromium-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_v11_encrypt('s3cret'), '/', 0, 1),
            ('.reddit.com', 'token_v2', '', chrome_encrypt('tok'), '/', 0, 1),
        ])
        stream = io.StringIO()
        jar = self.load(profile, secret=b'', logger=cookies.Logger(stream=stream))
        # v10 still works: only the v11 cookie is given up on, once.
        self.assertEqual(cookie_map(jar).keys(), {'token_v2'})
        self.assertIn('cannot decrypt v11 cookies', stream.getvalue())

    def test_a_store_without_v11_cookies_never_asks_for_a_key(self):
        profile = self.path('chromium-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_encrypt('s3cret'), '/', 0, 1),
        ])
        # The keyring is only consulted for a cookie that needs it, so a
        # basic-text store cannot pop up a keyring prompt or need secretstorage.
        self.assertEqual(self.load(profile).get_cookie_header(REDDIT_URL),
                         'reddit_session=s3cret')
        self.lookup.assert_not_called()

    def test_an_explicit_keyring_is_not_second_guessed(self):
        profile = self.path('chromium-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_v11_encrypt('s3cret'), '/', 0, 1),
        ])
        jar = self.load(profile, keyring='basictext')
        self.assertEqual(cookie_map(jar), {})
        self.lookup.assert_not_called()

    def test_naming_the_keyring_reads_it(self):
        # `chromium+gnomekeyring`: the workaround for this case, and what the
        # command line tells a user to pass when it cannot work it out.
        profile = self.path('chromium-profile')
        write_chrome_profile(profile, [
            ('.reddit.com', 'reddit_session', '', chrome_v11_encrypt('s3cret'), '/', 0, 1),
        ])
        jar = self.load(profile, keyring='gnomekeyring')
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')


class FirefoxCookieTest(TempDirTestCase):
    def load(self, profile, container=None):
        spec = cookies.BrowserSpec('firefox', profile=profile, container=container)
        return cookies.load_cookie_jar(from_browser=spec, logger=cookies.NULL_LOGGER)

    def test_plaintext_cookie_is_read(self):
        profile = self.path('ff-profile')
        write_firefox_profile(profile, [
            ('', 'reddit_session', 's3cret', '.reddit.com', '/', 1999999999, 1),
        ])
        jar = self.load(profile)
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=s3cret')
        self.assertEqual(cookie_map(jar)['reddit_session'].expires, 1999999999)

    def test_schema_16_expiry_is_milliseconds(self):
        profile = self.path('ff-profile')
        write_firefox_profile(profile, [
            ('', 'reddit_session', 's3cret', '.reddit.com', '/', 1999999999000, 1),
        ], schema_version=16)
        self.assertEqual(cookie_map(self.load(profile))['reddit_session'].expires, 1999999999)

    def test_container_filtering(self):
        profile = self.path('ff-profile')
        write_firefox_profile(profile, [
            ('^userContextId=2', 'reddit_session', 'work-value', '.reddit.com', '/', 1999999999, 1),
            ('', 'reddit_session', 'default-value', '.reddit.com', '/', 1999999999, 1),
        ], containers=[{'userContextId': 2, 'name': 'work', 'l10nID': 'userContextWork.label'}])

        jar = self.load(profile, container='work')
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=work-value')

        # A container can also be matched by its l10n id.  The vendored code
        # compares against the *whole* l10n id string (upstream behaviour), so
        # the localized name is normally what a user types.
        jar = self.load(profile, container='userContextWork.label')
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=work-value')

        # ... and the cookies outside every container are reachable too
        jar = self.load(profile, container='none')
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=default-value')

    def test_unknown_container_is_an_error(self):
        profile = self.path('ff-profile')
        write_firefox_profile(profile, [], containers=[])
        with self.assertRaises(cookies.CookieLoadError) as ctx:
            self.load(profile, container='nope')
        self.assertIn('could not find firefox container', str(ctx.exception))

    def test_cookie_file_wins_over_browser(self):
        profile = self.path('ff-profile')
        write_firefox_profile(profile, [
            ('', 'reddit_session', 'from-browser', '.reddit.com', '/', 1999999999, 1),
        ])
        cookie_file = write_netscape_file(
            self.path('cookies.txt'),
            '.reddit.com\tTRUE\t/\tFALSE\t1999999999\treddit_session\tfrom-file')
        jar = cookies.load_cookie_jar(
            from_browser=cookies.BrowserSpec('firefox', profile=profile),
            cookie_file=cookie_file, logger=cookies.NULL_LOGGER)
        self.assertEqual(jar.get_cookie_header(REDDIT_URL), 'reddit_session=from-file')
        self.assertEqual(len(list(jar)), 1)


class FacadeTest(TempDirTestCase):
    def test_unsupported_browser_in_spec_string(self):
        with self.assertRaises(ValueError):
            cookies.load_cookie_jar(from_browser='netscape')

    def test_invalid_spec_object(self):
        with self.assertRaises(ValueError):
            cookies.load_cookie_jar(from_browser=1234)

    def test_invalid_cookie_file_type(self):
        with self.assertRaises(ValueError):
            cookies.load_cookie_jar(cookie_file=1234, logger=cookies.NULL_LOGGER)

    def test_fatal_error_is_a_load_error(self):
        self.assertTrue(issubclass(cookies.FatalCookieLoadError, cookies.CookieLoadError))

    def test_header_is_none_when_no_cookie_matches(self):
        cookie_file = write_netscape_file(
            self.path('cookies.txt'), '.example.com\tTRUE\t/\tFALSE\t0\tother\tx')
        self.assertIsNone(
            cookies.load_cookie_header(REDDIT_URL, cookie_file=cookie_file, logger=cookies.NULL_LOGGER))

    def test_jar_is_usable_as_a_cookiejar(self):
        cookie_file = write_netscape_file(
            self.path('cookies.txt'), '.reddit.com\tTRUE\t/\tFALSE\t0\treddit_session\ts3cret')
        jar = cookies.load_cookie_jar(cookie_file=cookie_file, logger=cookies.NULL_LOGGER)
        # `http.cookiejar.CookieJar` API, so urllib's HTTPCookieProcessor works
        self.assertIsInstance(jar, http.cookiejar.CookieJar)


if __name__ == '__main__':
    unittest.main()
