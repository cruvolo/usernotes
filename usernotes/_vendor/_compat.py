"""Local stand-ins for the ``yt_dlp`` internals used by the vendored modules.

`usernotes._vendor.browser_cookies` and `usernotes._vendor.aes` are adapted
copies of yt-dlp modules; upstream they import a logger, a subprocess helper,
optional third-party packages and a pile of small utility functions from
``yt_dlp``'s own namespace.  This module supplies equivalents so that the
vendored code runs standalone, with only the standard library plus (optionally)
``secretstorage``.

Names here deliberately mirror the yt-dlp originals (``Popen.run``,
``expand_path``, ``try_call``, ...) so that the vendored files stay diffable
against a newer yt-dlp checkout.

Not vendored: this module is ours, written for usernotes.
"""

import os
import re
import subprocess
import sys
import urllib.parse

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    import sqlite3
except ImportError:  # Python built without sqlite support
    # https://github.com/yt-dlp/yt-dlp/issues/544
    sqlite3 = None


secretstorage = None
try:
    import secretstorage
    _SECRETSTORAGE_UNAVAILABLE_REASON = None
except ImportError:
    _SECRETSTORAGE_UNAVAILABLE_REASON = (
        'as the `secretstorage` module is not installed. '
        'Please install it by running `python3 -m pip install secretstorage`')
except Exception as _err:  # pragma: no cover - depends on the local keyring/D-Bus setup
    _SECRETSTORAGE_UNAVAILABLE_REASON = (
        f'as the `secretstorage` module could not be initialized. {_err}')


def _load_cryptodome_aes():
    """Return a pycryptodome AES module, or ``None`` when unavailable.

    Stand-in for ``yt_dlp.dependencies.Cryptodome`` (only its ``AES`` attribute
    is used by the vendored code).  AES is optional: without it the vendored
    pure-python implementation is used, which is slower but dependency-free.
    """
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        try:
            from Crypto.Cipher import AES
        except (ImportError, SyntaxError):
            # Old pycrypto raises SyntaxError on modern Python
            return None
    return AES


class _Cryptodome:
    """Namespace object standing in for ``yt_dlp.dependencies.Cryptodome``."""

    AES = _load_cryptodome_aes()


Cryptodome = _Cryptodome()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Logger:
    """A minimal stderr logger implementing the protocol the vendored code uses.

    The vendored cookie code only needs ``debug``, ``info``, ``warning`` and
    ``error``, each taking a single message.  ``warning`` additionally accepts
    ``only_once`` to suppress repeats of the same message.

    :param stream: where to write; defaults to ``sys.stderr``.
    :param level: minimum level to emit - ``'debug'``, ``'info'``, ``'warning'``
        or ``'error'``.  Messages below this level are dropped.
    """

    LEVELS = {'debug': 10, 'info': 20, 'warning': 30, 'error': 40}

    def __init__(self, stream=None, level='warning'):
        if level not in self.LEVELS:
            raise ValueError(f'unknown log level: {level!r}')
        self._stream = stream
        self._level = level
        self._seen_once = set()

    @property
    def level(self):
        return self._level

    def _emit(self, level, message):
        if self.LEVELS[level] < self.LEVELS[self._level]:
            return
        stream = self._stream if self._stream is not None else sys.stderr
        if stream is None:
            return
        print(f'{level}: {message}', file=stream)
        stream.flush()

    def debug(self, message):
        self._emit('debug', message)

    def info(self, message):
        self._emit('info', message)

    def warning(self, message, only_once=False):
        if only_once:
            if message in self._seen_once:
                return
            self._seen_once.add(message)
        self._emit('warning', message)

    def error(self, message):
        self._emit('error', message)


class NullLogger(Logger):
    """A logger that discards everything.

    Use when the caller wants the result of cookie extraction but never any
    diagnostics - for example inside a library that reports failures itself.
    """

    def _emit(self, level, message):
        return


NULL_LOGGER = NullLogger()


class QuietMultilinePrinter:
    """No-op stand-in for ``yt_dlp.minicurses.QuietMultilinePrinter``.

    The vendored code wraps its (potentially long) extraction loops in a
    progress bar.  usernotes has no progress bar: extraction of a few hundred
    cookies is fast, and printing progress would only add noise to a CLI whose
    real output is the usernotes themselves.  A logger may still supply its own
    ``progress_bar()`` method returning a context manager with a ``print``
    method if it wants one.
    """

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def print(self, *args, **kwargs):
        """Discard a progress message."""

    def print_at_line(self, text, pos):
        """Discard a progress message."""

    def end(self):
        """Discard any state."""


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

class Popen(subprocess.Popen):
    """Minimal stand-in for ``yt_dlp.utils.Popen``.

    Only ``Popen.run`` is used by the vendored code: it runs a command to
    completion and returns ``(stdout, stderr, returncode)``.  It is used to talk
    to the platform credential stores (``security``, ``kwallet-query``,
    ``dbus-send``).

    Simplification versus upstream: no shell-escaping of ``.bat``/``.cmd``
    arguments (we only ever run well-known binaries).
    """

    if sys.platform == 'win32':
        _startupinfo = subprocess.STARTUPINFO()
        _startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    else:
        _startupinfo = None

    def __init__(self, args, *remaining, text=False, **kwargs):
        if text is True:
            kwargs['text'] = True
            kwargs.setdefault('encoding', 'utf-8')
            kwargs.setdefault('errors', 'replace')
        if self._startupinfo is not None:
            # Hide the console window when spawning helper processes on Windows
            kwargs.setdefault('startupinfo', self._startupinfo)
        super().__init__(args, *remaining, **kwargs)

    def communicate_or_kill(self, *args, **kwargs):
        try:
            return self.communicate(*args, **kwargs)
        except BaseException:  # Including KeyboardInterrupt
            self.kill()
            self.wait()
            raise

    @classmethod
    def run(cls, *args, timeout=None, input=None, **kwargs):
        """Run *args* to completion; return ``(stdout, stderr, returncode)``."""
        if input is not None and kwargs.get('stdin') is None:
            kwargs['stdin'] = subprocess.PIPE
        with cls(*args, **kwargs) as proc:
            default = '' if proc.text_mode else b''
            stdout, stderr = proc.communicate_or_kill(input=input, timeout=timeout)
            return stdout or default, stderr or default, proc.returncode


# ---------------------------------------------------------------------------
# Odds and ends from yt_dlp.utils
# ---------------------------------------------------------------------------

def compat_ord(c):
    """``ord()`` for a one-character string, pass through an int."""
    return c if isinstance(c, int) else ord(c)


def compat_expanduser(path):
    """``os.path.expanduser`` with ``%HOME%`` honoured on Windows.

    Python 3.8+ ignores ``%HOME%`` on Windows, which would break
    ``%USERPROFILE%``-less setups.  See https://github.com/yt-dlp/yt-dlp/issues/792
    """
    if os.name not in ('nt', 'ce'):
        return os.path.expanduser(path)
    home = os.environ.get('HOME')
    if not home:
        return os.path.expanduser(path)
    elif not path.startswith('~'):
        return path
    i = path.replace('\\', '/', 1).find('/')  # ~user
    if i < 0:
        i = len(path)
    userhome = os.path.join(os.path.dirname(home), path[1:i]) if i > 1 else home
    return userhome + path[i:]


def expand_path(s):
    """Expand shell variables and ``~`` in a path."""
    return os.path.expandvars(compat_expanduser(s))


def is_path_like(f):
    """Whether *f* can be used as a filesystem path."""
    return isinstance(f, (str, bytes, os.PathLike))


def str_or_none(v, default=None):
    """``str(v)``, or *default* when *v* is ``None``."""
    return default if v is None else str(v)


def try_call(*funcs, expected_type=None, args=(), kwargs=None):
    """Return the first successful call result of *funcs*, else ``None``."""
    kwargs = kwargs or {}
    for f in funcs:
        try:
            val = f(*args, **kwargs)
        except (AttributeError, KeyError, TypeError, IndexError, ValueError, ZeroDivisionError):
            pass
        else:
            if expected_type is None or isinstance(val, expected_type):
                return val


def error_to_str(err):
    """Render an exception as ``'ExceptionType: message'``."""
    return f'{type(err).__name__}: {err}'


def write_string(s, out=None, encoding=None):
    """Write *s* to *out* (default stderr), coping with binary streams."""
    assert isinstance(s, str)
    out = out or sys.stderr
    if not out:
        return

    enc, buffer = None, out
    if 'b' in (getattr(out, 'mode', None) or ''):
        enc = encoding or sys.getdefaultencoding()
    elif hasattr(out, 'buffer'):
        buffer = out.buffer
        enc = encoding or getattr(out, 'encoding', None) or 'utf-8'

    buffer.write(s.encode(enc, 'ignore') if enc else s)
    out.flush()


def sanitize_url(url, *, scheme='http'):
    """Fix up a user-supplied URL (missing scheme, common typos)."""
    # Prepend protocol-less URLs with the scheme, and fix common typos
    if url is None:
        return None
    elif url.startswith('//'):
        return f'{scheme}:{url}'
    COMMON_TYPOS = (
        (r'^httpss://', r'https://'),
        (r'^rmtp([es]?)://', r'rtmp\1://'),
    )
    for mistake, fixup in COMMON_TYPOS:
        if re.match(mistake, url):
            return re.sub(mistake, fixup, url)
    return url


def escape_rfc3986(s):
    """Escape non-ASCII characters as suggested by RFC 3986."""
    return urllib.parse.quote(s, b"%/;:@&=+$,!~*'()?#[]")


def remove_dot_segments(path):
    """Resolve ``.``/``..`` path segments (RFC 3986 5.2.4)."""
    output = []
    segments = path.split('/')
    for s in segments:
        if s == '.':
            continue
        elif s == '..':
            if output:
                output.pop()
        else:
            output.append(s)
    if not segments[0] and (not output or output[0]):
        output.insert(0, '')
    if segments[-1] in ('.', '..'):
        output.append('')
    return '/'.join(output)


def normalize_url(url):
    """Normalize URL as suggested by RFC 3986."""
    url_parsed = urllib.parse.urlparse(url)
    return url_parsed._replace(
        netloc=url_parsed.netloc.encode('idna').decode('ascii'),
        path=escape_rfc3986(remove_dot_segments(url_parsed.path)),
        params=escape_rfc3986(url_parsed.params),
        query=escape_rfc3986(url_parsed.query),
        fragment=escape_rfc3986(url_parsed.fragment),
    ).geturl()
