"""The on-wiki usernotes storage format: decode and encode the wiki page.

This module is pure: it turns the text of ``/r/<sub>/wiki/usernotes`` into
`Note` objects and back again, and never touches the network.  Everything it
does is specified in ``usernotes-storage.md`` at the repository root, which is
the reference for the format; read it before changing anything here.

The page
--------

A page is one JSON document holding all notes for a subreddit::

    {"ver": 6,
     "constants": {"users": ["creesch"], "warnings": ["abusewarn", null]},
     "blob": "<base64 of the zlib-deflated note map>"}

``blob`` is the whole username -> notes map, zlib-compressed and base64
encoded; ``constants`` are two intern tables that map the small integers in
each note (``m``, ``w``) to the moderator name and note type key they mean.
Version 5 stores that map inline under ``users`` instead of in ``blob``, and
version 4 is the same but with millisecond timestamps; version 6 (which
toolbox and this package write) is the compressed form.  See
`compress_blob`, `decompress_blob` and `parse_page` / `build_page`.

Divergences from toolbox
------------------------

A faithful reimplementation should be readable by toolbox and behave the way
its users expect, but three details of toolbox's behaviour are bugs or lossy,
so they are deliberately not copied:

* **Links that are not squashed forms.**  Toolbox recognises only
  ``l,<id>[,<id>]``, ``m,<id>`` and ``https://mod.reddit.com/...``, and
  reduces anything else to ``""`` on read - so a store containing a legacy
  full URL loses it on the next save.  `unsquash_link` and `squash_link` keep
  any absolute ``http(s)`` URL as it is instead, which is what toolbox's own
  documentation recommends for legacy stores.
* **Note text is not HTML-decoded on read.**  Toolbox runs note text through
  ``htmlDecode()`` when it reads a page and writes it back verbatim, so a note
  whose text contains a literal entity (``&amp;``) reads back as ``&`` and is
  corrupted on the next save.  `parse_page` keeps the stored text exactly, so
  a read/modify/write round trip never changes it.  Use `html_decode` in the
  display layer if toolbox-parity output is wanted.
* **Newer schemas are refused, not mis-read.**  Toolbox's version dispatch
  falls through for ``ver > 6`` and hands back the raw undecoded page;
  `parse_page` raises `UnsupportedSchemaError` instead, so a store written by
  a future toolbox can never be corrupted by an old tool.

There is one more thing toolbox does that is dropped here for a different
reason: it sanitises note text with DOMPurify on read (``purifyObject``).  A
command line tool has no renderer to protect, and sanitising on read would
silently rewrite moderator text on the next save, so note text is treated as
data here.  Escaping for display is the display layer's business.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import html
import json
import re
import zlib

from .errors import InvalidConfigError, InvalidPageError, UnsupportedSchemaError

__all__ = [
    'DEFAULT_BASE_URL',
    'DEFAULT_NOTE_TYPES',
    'DEFAULT_ZLIB_LEVEL',
    'NOTES_DEPRECATED_SCHEMA',
    'NOTES_MAX_SCHEMA',
    'NOTES_MIN_SCHEMA',
    'NOTES_PAGE',
    'NOTES_SCHEMA',
    'Note',
    'NoteType',
    'Page',
    'build_page',
    'compress_blob',
    'decompress_blob',
    'html_decode',
    'note_type',
    'note_types_from_config',
    'parse_page',
    'squash_link',
    'time_from_stored',
    'time_to_stored',
    'unsquash_link',
]

#: The wiki page a subreddit's usernotes live on.
NOTES_PAGE = 'usernotes'

#: Schema version toolbox writes today; the version this package writes.
NOTES_SCHEMA = 6

#: Oldest schema version that can be read (toolbox's ``notesMinSchema``).
NOTES_MIN_SCHEMA = 4

#: Schema version toolbox warns about as deprecated.
NOTES_DEPRECATED_SCHEMA = 4

#: Newest schema version that can be read (toolbox's ``notesMaxSchema``).
NOTES_MAX_SCHEMA = 6

#: Default origin for expanding squashed links to absolute URLs.
DEFAULT_BASE_URL = 'https://www.reddit.com'

#: zlib level toolbox's pako uses (``Z_DEFAULT_COMPRESSION``).
DEFAULT_ZLIB_LEVEL = 6

#: Longest stored timestamp that is still seconds rather than milliseconds.
#: Toolbox decides with ``time.toString().length <= 10``.
_MAX_SECONDS_DIGITS = 10

# Link shapes toolbox squashes (`usernotes.js:1491-1543`).  The comment form is
# deliberately loose: an optional slug may or may not carry a comment id.
_COMMENTS_LINK_RE = re.compile(r'/comments/(\w+)/(?:[^/]+/(?:(\w+))?)?')
_MODMAIL_LINK_RE = re.compile(r'/messages/(\w+)')
_ABSOLUTE_URL_RE = re.compile(r'https?://', re.IGNORECASE)

#: Note type keys are validated as ``^[\w-]+$`` by toolbox's config module.
_TYPE_KEY_RE = re.compile(r'[\w-]+')


@dataclasses.dataclass(order=True)
class NoteType:
    """A note type as the subreddit's config defines it.

    :param key: what goes in the note's ``w`` (via the constants pool), e.g.
        ``'spamwatch'``.  Always ``^[\\w-]+$``; that is what toolbox's config
        validation accepts.
    :param text: how it is displayed, e.g. ``'Spam Watch'``.
    :param color: toolbox's CSS colour name, e.g. ``'fuchsia'``.  Kept so a
        terminal front end can pick a 256-colour approximation.
    """

    key: str
    text: str
    color: str = ''

    def __post_init__(self):
        if not isinstance(self.key, str) or not _TYPE_KEY_RE.fullmatch(self.key):
            raise ValueError(f'invalid note type key: {self.key!r}')
        if not isinstance(self.text, str) or not self.text:
            raise ValueError(f'invalid note type text: {self.text!r}')
        if not isinstance(self.color, str):
            raise ValueError(f'invalid note type color: {self.color!r}')

    @classmethod
    def from_config(cls, value):
        """Build from one ``usernoteColors`` entry (a dict).

        :raises ValueError: if *value* is not a dict with a usable ``key``.
        """
        if not isinstance(value, dict):
            raise ValueError(f'note type is not an object: {value!r}')
        key = value.get('key')
        # toolbox falls back to the key when text is missing, and allows an
        # entry with no colour.
        return cls(key=key, text=value.get('text') or key, color=value.get('color') or '')

    def as_dict(self):
        """``{'key', 'text', 'color'}``, as the config stores it."""
        return {'key': self.key, 'text': self.text, 'color': self.color}


#: Note types used when the subreddit has no ``usernoteColors`` in its config,
#: verbatim from toolbox's ``tbcore.js`` defaults.
DEFAULT_NOTE_TYPES = (
    NoteType(key='gooduser', text='Good Contributor', color='green'),
    NoteType(key='spamwatch', text='Spam Watch', color='fuchsia'),
    NoteType(key='spamwarn', text='Spam Warning', color='purple'),
    NoteType(key='abusewarn', text='Abuse Warning', color='orange'),
    NoteType(key='ban', text='Ban', color='red'),
    NoteType(key='permban', text='Permanent Ban', color='darkred'),
    NoteType(key='botban', text='Bot Ban', color='black'),
)


@dataclasses.dataclass
class Note:
    """One usernote, decoded from the store.

    Times are **milliseconds** since the Unix epoch, as toolbox holds them in
    memory; ``0`` means "no time", which is what toolbox writes for a note
    whose timestamp it lost (``deflateTime(undefined)``).  Note that the
    *stored* form is seconds (schema v5/v6) or milliseconds (v4), so a note
    only survives a round trip to second precision - see `time_from_stored`.

    :param text: the note itself, exactly as stored (the ``n`` field).
    :param time: milliseconds since the epoch, or ``0`` when unknown (``t``).
    :param mod: moderator who wrote the note (``m``, via ``constants.users``).
        ``None`` when the store does not say, or says something out of range.
    :param type: note type *key* (``w``, via ``constants.warnings``), e.g.
        ``'spamwatch'``.  ``None`` when the note has no type.
    :param link: absolute URL the note is about (``l``, unsquashed), or ``''``.
    """

    text: str = ''
    time: int = 0
    mod: str | None = None
    type: str | None = None
    link: str = ''

    @property
    def has_time(self):
        """Whether this note carries a timestamp at all."""
        return bool(self.time)

    @property
    def seconds(self):
        """Creation time as whole seconds since the epoch (``0`` if unknown).

        Whole seconds are what the *stored* format holds, and what toolbox
        uses to identify a note; it is lossy for notes added with
        millisecond precision within the same second.
        """
        return int(self.time / 1000)

    def datetime(self, tz=datetime.timezone.utc):
        """Creation time as an aware `datetime.datetime`, or ``None``."""
        if not self.time:
            return None
        return datetime.datetime.fromtimestamp(self.time / 1000, tz)

    def display_text(self):
        """Note text as toolbox would show it (see `html_decode`).

        Only for display: never write the result back into the store.
        """
        return html_decode(self.text)

    def as_dict(self):
        """A JSON-friendly view of the note, for the CLI's ``--json`` output.

        Keys are the CLI's, not the store's: ``text``, ``time`` (milliseconds),
        ``time_iso`` (``None`` when unknown), ``mod``, ``type`` and ``link``
        (``None`` when there is none).
        """
        moment = self.datetime()
        return {
            'text': self.text,
            'time': self.time,
            'time_iso': None if moment is None else moment.isoformat(),
            'mod': self.mod,
            'type': self.type,
            'link': self.link or None,
        }

    def copy(self, **changes):
        """A shallow copy, with any fields in *changes* replaced."""
        return dataclasses.replace(self, **changes)


@dataclasses.dataclass(frozen=True)
class Page:
    """A decoded wiki page.

    :param ver: the schema version the page was written with.
    :param users: username -> notes, in stored order.  Usernames are not
        canonicalised here (that is `usernotes.model`'s job), and a user with
        no notes is kept with an empty tuple rather than dropped.
    :param warnings: human-readable descriptions of anything damaged or
        unexpected that was found and worked around, empty for a clean page.
        Worth showing to the user: it means the store has been written by
        something other than a stock toolbox.
    """

    ver: int
    users: dict[str, tuple[Note, ...]]
    warnings: tuple[str, ...] = ()

    def note_count(self):
        """Total number of notes on the page."""
        return sum(len(notes) for notes in self.users.values())


def parse_page(content_md, subreddit='', *, strict=False, base_url=DEFAULT_BASE_URL):
    """Decode the text of a usernotes wiki page.

    :param content_md: the page text (``data.content_md`` from the wiki API).
    :param subreddit: the subreddit the page belongs to, used to expand
        squashed links to absolute URLs.
    :param strict: raise `InvalidPageError` on the first damaged note instead
        of skipping it and recording a warning.
    :param base_url: origin used when expanding squashed links.
    :returns: a `Page`.
    :raises InvalidPageError: if the text is empty, not JSON, not a usernotes
        page, or (with ``strict``) damaged beyond use.
    :raises UnsupportedSchemaError: if ``ver`` is outside v4-v6.
    """
    problems = []

    def problem(message):
        if strict:
            raise InvalidPageError(message)
        problems.append(message)

    if not isinstance(content_md, str):
        raise InvalidPageError(f'wiki page content is {type(content_md).__name__}, not text')
    if not content_md.strip():
        raise InvalidPageError('wiki page is empty')

    try:
        doc = json.loads(content_md)
    except ValueError as err:
        raise InvalidPageError(f'wiki page is not valid JSON: {err}') from err
    if not isinstance(doc, dict):
        raise InvalidPageError(f'wiki page is {type(doc).__name__}, not a JSON object')

    ver = doc.get('ver')
    if isinstance(ver, bool) or not isinstance(ver, int):
        raise InvalidPageError('wiki page has no "ver" schema version')
    if ver < NOTES_MIN_SCHEMA:
        raise UnsupportedSchemaError(
            f'usernotes schema v{ver} is too old to be understood '
            f'(the oldest readable is v{NOTES_MIN_SCHEMA})')
    if ver > NOTES_MAX_SCHEMA:
        raise UnsupportedSchemaError(
            f'usernotes schema v{ver} is newer than this tool understands '
            f'(the newest readable is v{NOTES_MAX_SCHEMA})')

    users_raw = decompress_blob(doc.get('blob')) if ver == 6 else doc.get('users')
    if not isinstance(users_raw, dict):
        what = '"blob"' if ver == 6 else '"users"'
        raise InvalidPageError(f'wiki page is schema v{ver} but has no {what} object')

    constants = doc.get('constants')
    if not isinstance(constants, dict):
        if constants is not None:
            problem(f'page "constants" is {type(constants).__name__}, not an object; '
                    'note authors and types will be missing')
        constants = {}

    users = {}
    for name, user in users_raw.items():
        notes = ()
        if not isinstance(user, dict):
            problem(f'user {name!r}: entry is {type(user).__name__}, not an object; '
                    'no notes read from it')
        else:
            if user.get('u') is not None:
                # Only v3 and older pooled the username like this; seeing the
                # field means a foreign writer.  Not fatal, just odd.
                problem(f'user {name!r}: entry carries an unexpected "u" field')
            notes_raw = user.get('ns')
            if notes_raw is None:
                problem(f'user {name!r}: entry has no "ns" list; treated as no notes')
                notes_raw = []
            if not isinstance(notes_raw, list):
                problem(f'user {name!r}: "ns" is {type(notes_raw).__name__}, not a list; '
                        'no notes read from it')
            else:
                parsed = []
                for index, raw in enumerate(notes_raw):
                    if not isinstance(raw, dict):
                        problem(f'user {name!r}: note {index} is {type(raw).__name__}, '
                                'not an object; skipped')
                        continue
                    parsed.append(_parse_note(ver, constants, raw, subreddit, index,
                                              name, problem, base_url))
                notes = tuple(parsed)
        # Kept even when nothing could be read from it, with the problem
        # recorded: an entry that is there but unreadable is worth showing.
        users[name] = notes

    return Page(ver=ver, users=users, warnings=tuple(problems))


def build_page(users, *, ver=NOTES_SCHEMA, base_url=DEFAULT_BASE_URL):
    """Encode notes into the text of a usernotes wiki page.

    The ``constants`` pools are rebuilt from scratch, in first-seen order,
    exactly as toolbox does on every save - the pools a page was read with are
    never reused, because toolbox does not preserve them either and their
    indices are only meaningful for the revision they came from.

    :param users: username -> iterable of `Note`, in the order to store them
        (newest first is what toolbox does; `usernotes.model.Notes` sorts that
        way).  Empty note lists are written as an empty ``ns`` array, and
        dropped by `usernotes.store.UsernotesStore` before it gets here.
    :param ver: schema version to write, 5 or 6.  v6 (the default) compresses
        the note map into ``blob``; v5 writes it inline.  v4 and older are
        refused: writing them would mean *downgrading* a store, which toolbox
        also refuses to do.
    :param base_url: origin used when squashing links.
    :returns: the page text, minified - toolbox writes
        ``JSON.stringify`` output and notes that "not indenting saves precious
        bytes"; the wiki page limit is why the format is compressed at all.
    :raises ValueError: if *ver* is not 5 or 6, or a note holds a value the
        format cannot represent.
    """
    if ver not in (5, 6):
        raise ValueError(f'can only write schema v5 or v6, not v{ver}')

    deflated = {}
    users_pool = []
    warnings_pool = []
    for name, notes in users.items():
        slots = []
        for note in notes:
            slots.append({
                # Key order matters for byte-for-byte parity with toolbox's
                # JSON.stringify, so keep toolbox's field order here.
                'n': _as_text(note.text),
                't': time_to_stored(ver, note.time),
                'm': _intern(users_pool, _as_key(note.mod)),
                'l': squash_link(note.link, base_url=base_url),
                'w': _intern(warnings_pool, _as_key(note.type)),
            })
        deflated[name] = {'ns': slots}

    constants = {'users': users_pool, 'warnings': warnings_pool}
    if ver == 6:
        # toolbox's compressBlob(): drop the users object, add the blob, so
        # the written key order is ver, constants, blob.
        return _minified({'ver': ver, 'constants': constants, 'blob': compress_blob(deflated)})
    return _minified({'ver': ver, 'users': deflated, 'constants': constants})


def compress_blob(users):
    """Compress a note map into a page's ``blob`` string.

    ``base64(zlib_deflate(utf8(JSON.stringify(users))))`` - toolbox's
    ``TBHelpers.zlibDeflate()``.  The parameters are not arbitrary: zlib
    (RFC 1950, not raw deflate and not gzip) at the default level 6, standard
    base64 with padding and no line breaks, and JSON with no spaces and with
    non-ASCII characters left raw (``JSON.stringify`` does not escape them).
    With all of that, Python's zlib reproduces pako's output byte for byte.

    :param users: the decoded note map (``{name: {'ns': [...]}}``).
    :returns: the blob, as an ASCII string.
    """
    raw = json.dumps(users, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return base64.b64encode(zlib.compress(raw, DEFAULT_ZLIB_LEVEL)).decode('ascii')


def decompress_blob(blob):
    """Decode a page's ``blob`` string back into the note map.

    The exact reverse of `compress_blob`.

    :raises InvalidPageError: if *blob* is not base64 zlib-wrapped UTF-8 JSON.
    """
    if not isinstance(blob, str) or not blob.strip():
        raise InvalidPageError('blob is missing or empty')
    try:
        raw = zlib.decompress(base64.b64decode(blob, validate=False))
    except (binascii.Error, zlib.error, ValueError) as err:
        raise InvalidPageError(f'blob is not base64-encoded zlib data: {err}') from err
    try:
        users = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as err:
        raise InvalidPageError(f'blob does not hold UTF-8 JSON: {err}') from err
    if not isinstance(users, dict):
        raise InvalidPageError(f'blob holds {type(users).__name__}, not a note map')
    return users


def time_from_stored(ver, time):
    """Convert a stored ``t`` to milliseconds since the epoch.

    v5 and v6 store seconds, v4 stores milliseconds, and toolbox tells them
    apart by digit count (``toString().length <= 10``) rather than by version
    alone, so a v6 note that was somehow written with milliseconds is still
    read correctly.
    """
    if not isinstance(time, (int, float)) or isinstance(time, bool):
        return 0
    if ver >= 5 and len(str(int(time))) <= _MAX_SECONDS_DIGITS:
        return int(time * 1000)
    return int(time)


def time_to_stored(ver, time):
    """Convert milliseconds since the epoch to the stored ``t`` for *ver*.

    ``None`` and ``0`` become ``0`` ("no time"), matching toolbox's
    ``deflateTime(undefined)``; for v5 and v6 millisecond precision is
    truncated, not rounded, as toolbox's ``Math.trunc`` does.
    """
    if time is None:
        return 0
    if not isinstance(time, (int, float)) or isinstance(time, bool):
        raise ValueError(f'note time is {type(time).__name__}, not a number: {time!r}')
    time = int(time)
    if ver >= 5 and len(str(time)) > _MAX_SECONDS_DIGITS:
        return int(time / 1000)      # truncate towards zero, like Math.trunc
    return time


def squash_link(permalink, *, base_url=DEFAULT_BASE_URL):
    """Reduce a permalink to the form stored in a note's ``l`` field.

    ``/r/<sub>/comments/<id>/`` becomes ``l,<id>``, a comment link becomes
    ``l,<id>,<id>``, an old modmail thread becomes ``m,<id>``, a new modmail
    URL is stored whole, and anything else that is an absolute ``http(s)`` URL
    is stored whole too (toolbox writes ``''`` there and loses it on the next
    read; see the module docstring).

    :param permalink: absolute URL or relative path, or ``''``/``None``.
    :returns: the ``l`` value, ``''`` when there is no usable link.
    """
    if not permalink:
        return ''
    if not isinstance(permalink, str):
        raise ValueError(f'link is {type(permalink).__name__}, not text: {permalink!r}')

    comments = _COMMENTS_LINK_RE.search(permalink)
    if comments:
        squashed = f'l,{comments.group(1)}'
        if comments.group(2) is not None:
            squashed += f',{comments.group(2)}'
        return squashed
    modmail = _MODMAIL_LINK_RE.search(permalink)
    if modmail:
        return f'm,{modmail.group(1)}'
    if _is_absolute_url(permalink):
        return permalink
    return ''


def unsquash_link(subreddit, link, *, base_url=DEFAULT_BASE_URL):
    """Expand a stored ``l`` field into an absolute URL.

    :param subreddit: the subreddit the notes belong to.
    :param link: the stored ``l`` value.
    :returns: an absolute URL, or ``''`` when there is nothing usable.  A
        stored value that is already an absolute URL is returned unchanged, so
        legacy links survive a read/modify/write round trip.
    """
    if not link or not isinstance(link, str):
        return ''
    if _is_absolute_url(link):
        return link

    params = link.split(',')
    if params[0] == 'l' and len(params) > 1 and params[1]:
        url = f'{base_url}/r/{subreddit}/comments/{params[1]}/'
        if len(params) > 2 and params[2]:
            url += f'-/{params[2]}/'
        return url
    if params[0] == 'm' and len(params) > 1 and params[1]:
        return f'{base_url}/r/{subreddit}/message/messages/{params[1]}'
    return ''


def html_decode(text):
    """Decode HTML entities, the way toolbox does when it reads note text.

    Toolbox shows ``&amp;`` in a stored note as ``&``; this reproduces that,
    for display only.  Never feed the result back into the store: the stored
    text is what other moderators and toolbox itself will read.

    .. note::

       This is `html.unescape`, which decodes a few sequences without a
       trailing semicolon that a browser's HTML parser would not.  Notes that
       contain entities are vanishingly rare and this is display-only.
    """
    if not isinstance(text, str):
        return text
    return html.unescape(text)


def note_types_from_config(usernote_colors):
    """Build the note type table from a toolbox config's ``usernoteColors``.

    :param usernote_colors: the config value: a list of ``{key, text, color}``
        objects.  ``None`` or an empty list means "not configured".
    :returns: a tuple of `NoteType`, or `DEFAULT_NOTE_TYPES` when the config
        does not define any - which is what toolbox falls back to.
    :raises InvalidConfigError: if the value is present but unusable.
    """
    if usernote_colors is None or usernote_colors == []:
        return DEFAULT_NOTE_TYPES
    if not isinstance(usernote_colors, list):
        raise InvalidConfigError(
            f'usernoteColors is {type(usernote_colors).__name__}, not a list')
    types = []
    seen = set()
    for entry in usernote_colors:
        try:
            note_type = NoteType.from_config(entry)
        except ValueError as err:
            raise InvalidConfigError(f'invalid usernoteColors entry: {err}') from err
        if note_type.key in seen:
            raise InvalidConfigError(f'duplicate usernoteColors key: {note_type.key!r}')
        seen.add(note_type.key)
        types.append(note_type)
    return tuple(types) or DEFAULT_NOTE_TYPES


def note_type(value, types=DEFAULT_NOTE_TYPES):
    """Find a note type by key, display text, or loose spelling of either.

    ``'spamwatch'``, ``'Spam Watch'`` and ``'spam watch'`` all find the default
    Spam Watch type; comparison ignores case, spaces, punctuation and
    underscores.

    :param value: a `NoteType` (returned unchanged), or the key or display
        text to look up.
    :param types: the table to search, normally the subreddit's own.
    :returns: the matching `NoteType`, or ``None``.
    :raises ValueError: if *value* is not text.
    """
    if isinstance(value, NoteType):
        return value
    if not isinstance(value, str):
        raise ValueError(f'note type is {type(value).__name__}, not text: {value!r}')
    wanted = _fold(value)
    for note_type_ in types:
        if wanted in (_fold(note_type_.key), _fold(note_type_.text)):
            return note_type_
    return None


# -- internals ---------------------------------------------------------------

def _is_absolute_url(value):
    return bool(_ABSOLUTE_URL_RE.match(value))


def _fold(value):
    return re.sub(r'[^a-z0-9]', '', value.casefold())


def _minified(doc):
    return json.dumps(doc, separators=(',', ':'), ensure_ascii=False)


def _as_text(value):
    """Note text, which the format always stores as a string.

    ``None`` is refused rather than written as ``''``: a note whose text is
    missing is a bug in the caller, and writing an empty note over it would
    hide it.
    """
    if not isinstance(value, str):
        raise ValueError(f'note text is {type(value).__name__}, not text: {value!r}')
    return value


def _as_key(value):
    """A constants-pool entry: a string, or ``None`` for "unset"."""
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f'constants pool value is {type(value).__name__}, not text: {value!r}')


def _intern(pool, value):
    """toolbox's ``_constManager.create()``: the index of *value*, adding it.

    toolbox's version compares with ``indexOf``, so two separate ``undefined``s
    become two ``null`` entries in the pool; here ``None`` matches ``None`` and
    is stored once.  The indices are equivalent either way.
    """
    try:
        return pool.index(value)
    except ValueError:
        pool.append(value)
        return len(pool) - 1


def _pool_lookup(constants, pool_name, index):
    """toolbox's ``_constManager.get()``, as ``(value, resolved)``.

    Mirrors JavaScript semantics: a missing pool or an index that is not a
    usable position yields ``undefined``, which toolbox renders as "no
    moderator" / type ``none``.  An index that *does* resolve to a pool entry
    holding ``null`` is a legitimate "no type" - that is what a note created
    without a type stores - so it is reported as resolved with no value; the
    difference matters because only the first is worth warning about.
    """
    pool = constants.get(pool_name)
    if not isinstance(pool, list):
        return None, False
    if index is None:
        return None, True       # the note simply has no such field
    if isinstance(index, bool) or not isinstance(index, int):
        return None, False
    if index < 0 or index >= len(pool):
        # JavaScript would say undefined here too (`pool[-1]`, `pool[99]`),
        # only Python would happily answer with the last element.
        return None, False
    value = pool[index]
    return (value if isinstance(value, str) else None), True


def _parse_note(ver, constants, raw, subreddit, index, name, problem, base_url):
    """Decode one stored note object, reporting damage through *problem*."""
    where = f'user {name!r}: note {index}'

    text = raw.get('n')
    if text is None:
        text = ''
    elif not isinstance(text, str):
        problem(f'{where}: text is {type(text).__name__}, not text; stored as empty')
        text = ''

    time = raw.get('t')
    if time is None:
        time = 0
    stored_time = time_from_stored(ver, time)
    if stored_time == 0 and time not in (0, None):
        problem(f'{where}: time is not a number ({time!r}); treated as unknown')

    mod = _checked_lookup(constants, 'users', raw.get('m'), where, 'moderator', problem)
    note_type_key = _checked_lookup(constants, 'warnings', raw.get('w'), where, 'type', problem)

    link = raw.get('l')
    if link is None:
        link = ''
    elif not isinstance(link, str):
        problem(f'{where}: link is {type(link).__name__}, not text; dropped')
        link = ''
    else:
        link = unsquash_link(subreddit, link, base_url=base_url)

    return Note(text=text, time=stored_time, mod=mod, type=note_type_key, link=link)


def _checked_lookup(constants, pool_name, index, where, label, problem):
    value, resolved = _pool_lookup(constants, pool_name, index)
    if not resolved:
        problem(f'{where}: {label} index {index!r} does not resolve to a '
                f'{pool_name} entry; treated as unset')
    return value
