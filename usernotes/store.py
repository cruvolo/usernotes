"""Read and write a subreddit's usernotes as one editable store.

This is the module a command line front end wants::

    from usernotes import cookies
    from usernotes.store import UsernotesStore

    store = UsernotesStore('toolbox', cookies=cookies.load_cookie_jar(from_browser='firefox'))
    store.load()

    for note in store.notes('alice'):
        print(note.time, note.type, note.text)

    store.add('alice', 'spam watch, second time this week', type='spam watch',
              link='https://www.reddit.com/r/toolbox/comments/abc123/-/def456/')
    store.save()

Cookies are a parameter, and getting them is the caller's job - see
`usernotes.cookies`, whose `~usernotes.cookies.load_cookie_jar` result is what
``cookies=`` expects (a plain `http.cookiejar.CookieJar`, a mapping of names to
values, a ``Cookie:`` header, or the text of a ``cookies.txt`` all work too).

How a save works
----------------

The store is a copy of the whole wiki page: `load` reads it, the editing
methods change the copy, and `save` writes the copy back as a new revision.
That is the only granularity the format has - toolbox does the same.

Three things are therefore done deliberately on save, because the alternative
is losing a moderator's work:

* **The page is re-read first.**  If it changed since `load` - another
  moderator added a note in the meantime, or someone edited the wiki by hand -
  saving would silently discard that, so `StaleStoreError` is raised instead.
  ``check_stale=False`` overrides it.
* **A page that does not exist is created.**  A subreddit that has never used
  usernotes has no wiki page at all; `load` then yields an empty store and
  `save` writes the first revision, which is what toolbox does when a
  moderator adds the first note.
* **Note types and moderator names are resolved before writing.**  The stored
  integers are indices into per-revision pools, so `add` turns a type like
  ``'spam watch'`` into the subreddit's type key (reading the subreddit's
  config page once, if needed) and records the author as the signed-in
  account's username.

The save reason follows toolbox's wording (``create new note on user alice``)
with this tool's name appended, so a page's revision history stays readable.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import time

from ._logging import resolve_logger
from .api import DEFAULT_BASE_URL, RedditWikiClient
from .errors import (
    ApiError,
    FormatError,
    InvalidConfigError,
    NoWikiPageError,
    StaleStoreError,
    UnknownNoteTypeError,
)
from .format import (
    DEFAULT_NOTE_TYPES,
    NOTES_PAGE,
    NOTES_SCHEMA,
    Note,
    NoteType,
    Page,
    build_page,
    note_type,
    note_types_from_config,
    parse_page,
    unsquash_link,
)
from .model import Notes, UserNotes, normalize_username

__all__ = [
    'Backup',
    'UsernotesStore',
    'normalize_subreddit',
]

#: Subreddit names are 3-21 characters of letters, digits and underscores.
_SUBREDDIT_RE = re.compile(r'[A-Za-z0-9_]{3,21}')

#: Wiki tools that rewrite a page with indentation are told not to; keeping the
#: revision reason short keeps it readable in the page history.
_MAX_REASON = 200

#: How many damaged-page warnings to log at warning level before summarising.
_MAX_LOGGED_WARNINGS = 5

#: Stands in for "this argument was not given", so that `UsernotesStore.update`
#: can tell "leave it alone" apart from "clear it".
_UNSET = object()


@dataclasses.dataclass(frozen=True)
class Backup:
    """What `UsernotesStore.backup` wrote.

    :param path: the file that was written.  It holds the wiki page's text
        byte for byte - never a re-serialisation of it, so a page that cannot
        be decoded is still backed up.
    :param subreddit: the subreddit it came from.
    :param taken_at: when it was written (UTC).
    :param size: how many bytes were written.
    :param notes: how many notes the page decoded to, or ``None`` when the
        page could not be decoded.
    :param users: how many users the page decoded to, or ``None``.
    :param warnings: anything odd found while decoding, empty for a clean
        page.  Non-empty warnings with ``notes`` of ``None`` means the backup
        is a copy of a page this tool cannot read - still worth keeping.
    """

    path: str
    subreddit: str
    taken_at: datetime.datetime
    size: int
    notes: int | None
    users: int | None
    warnings: tuple[str, ...] = ()

    def summary(self):
        """One line describing the backup, for a CLI to print."""
        if self.notes is None:
            return (f'{self.path}: {self.size} bytes of {self.subreddit} usernotes '
                    f'that could not be decoded')
        return (f'{self.path}: {self.notes} notes for {self.users} users '
                f'({self.size} bytes)')


def normalize_subreddit(value):
    """Clean up a subreddit name as typed on the command line.

    Accepts ``toolbox``, ``r/toolbox`` and ``/r/toolbox``, and rejects
    anything that is not a subreddit name, so a mistyped subreddit fails here
    rather than as a confusing 404 from Reddit.

    :raises ValueError: if *value* is not text, or not a plausible subreddit.
    """
    if not isinstance(value, str):
        raise ValueError(f'subreddit is {type(value).__name__}, not text: {value!r}')
    name = value.strip()
    if name.lower().startswith('/r/'):
        name = name[3:]
    elif name.lower().startswith('r/'):
        name = name[2:]
    name = name.strip().strip('/')
    if not _SUBREDDIT_RE.fullmatch(name):
        raise ValueError(
            f'not a subreddit name: {value!r} '
            '(expected 3-21 characters of letters, digits or "_")')
    return name


class UsernotesStore:
    """One subreddit's usernotes: load them, edit them, write them back.

    :param subreddit: the subreddit, e.g. ``'toolbox'`` or ``'/r/toolbox'``.
    :param cookies: anything `usernotes.api.cookie_jar` accepts - the jar from
        `usernotes.cookies.load_cookie_jar` is the usual one.  ``None`` gives
        an anonymous store, which can read public wiki pages and nothing else.
    :param client: a ready-made `usernotes.api.RedditWikiClient` to use
        instead of building one.  Useful for sharing a session between stores,
        or for a client with unusual settings.  Mutually exclusive with
        *cookies* and *client_options*.
    :param page: the wiki page name, for a subreddit that keeps its notes
        somewhere other than ``usernotes``.
    :param logger: see `usernotes._logging.resolve_logger`.
    :param client_options: any other `usernotes.api.RedditWikiClient` argument
        (``timeout``, ``min_interval``, ``base_url``, ...), used only when this
        store builds its own client.

    Nothing is read until it is needed: constructing a store makes no
    requests, `load` (and every method that needs the notes) does.
    """

    def __init__(self, subreddit, *, cookies=None, client=None, page=NOTES_PAGE,
                 logger=None, **client_options):
        self.subreddit = normalize_subreddit(subreddit)
        self.page = page
        self.log = resolve_logger(logger)

        if client is not None and (cookies is not None or client_options):
            raise ValueError(
                'pass either client= or cookies= (with client options), not both')
        if client is None:
            client = RedditWikiClient(cookies, logger=logger, **client_options)
        self.client = client

        self._users = Notes()
        self._raw = None
        self._exists = False
        self._loaded = False
        self._ver = NOTES_SCHEMA
        self._warnings = ()
        self._changes = []
        self._note_types = None

    def __repr__(self):
        state = ('not loaded' if not self._loaded
                 else f'{len(self._users)} user(s), {self._users.note_count()} note(s)')
        return f'<UsernotesStore /r/{self.subreddit}: {state}>'

    def __len__(self):
        """Number of users with notes in the store."""
        self.load()
        return len(self._users)

    def __iter__(self):
        """Iterate over the usernames, in stored order."""
        self.load()
        return iter(self._users)

    def __contains__(self, username):
        self.load()
        return self._users.find(username) is not None

    # -- reading ------------------------------------------------------------

    @property
    def loaded(self):
        """Whether the wiki page has been read yet."""
        return self._loaded

    @property
    def exists(self):
        """Whether the subreddit has a usernotes page at all.

        False for a subreddit that has never used usernotes, which is not an
        error: the first `save` creates the page.
        """
        return self._exists

    @property
    def dirty(self):
        """Whether anything has been changed since the page was read."""
        return bool(self._changes)

    @property
    def ver(self):
        """Schema version the page was read as, or was last written as."""
        return self._ver

    @property
    def warnings(self):
        """Oddities found in the page when it was read, if any."""
        return self._warnings

    @property
    def users(self):
        """The notes themselves, as a `usernotes.model.Notes` mapping."""
        self.load()
        return self._users

    def pending_changes(self):
        """Descriptions of the unsaved edits, in the order they were made."""
        return tuple(self._changes)

    def load(self, *, force=False):
        """Read the usernotes page, once.

        :param force: read it again even if it has already been read, throwing
            away unsaved edits.  A CLI should refuse to do that without
            asking; `reload` is the explicit spelling.
        :returns: this store, so calls can be chained.
        :raises usernotes.errors.FormatError: if the page exists but is not a
            usernotes store this tool can read.
        """
        if self._loaded and not force:
            return self
        raw = self.client.read_page(self.subreddit, self.page, missing_ok=True)
        page = Page(ver=NOTES_SCHEMA, users={}) if raw is None else parse_page(
            raw, self.subreddit, base_url=self.client.base_url)
        self._raw = raw
        self._exists = raw is not None
        self._ver = page.ver
        self._users = Notes(page.users)
        self._warnings = page.warnings
        self._changes = []
        self._loaded = True
        self._log_warnings(page.warnings)
        if not self._exists:
            self.log.debug(f'/r/{self.subreddit} has no usernotes page')
        elif page.ver != NOTES_SCHEMA:
            self.log.warning(
                f'/r/{self.subreddit} usernotes are schema v{page.ver}; saving '
                f'will upgrade them to v{NOTES_SCHEMA}')
        return self

    def reload(self):
        """Read the page again, discarding unsaved edits."""
        if self._changes:
            self.log.warning(
                f're-reading /r/{self.subreddit} usernotes discards '
                f'{len(self._changes)} unsaved change(s)')
        return self.load(force=True)

    def read_all(self):
        """Every user's notes: ``{username: (Note, ...)}``, newest first.

        :raises usernotes.errors.FormatError: if the page cannot be read.
        """
        self.load()
        return self._users.as_dict()

    def usernames(self):
        """The usernames with notes, in stored order."""
        self.load()
        return self._users.usernames()

    def note_count(self):
        """Total number of notes; a damaged page's skipped notes are not counted."""
        self.load()
        return self._users.note_count()

    def find(self, username):
        """The `usernotes.model.UserNotes` for *username*, or ``None``.

        Matching ignores case and a leading ``u/``.
        """
        self.load()
        return self._users.find(username)

    def notes(self, username):
        """The notes for *username*, newest first; empty when there are none."""
        user = self.find(username)
        return () if user is None else tuple(user.notes)

    def require(self, username):
        """The `usernotes.model.UserNotes` for *username*.

        :raises usernotes.errors.UserNotFoundError: if the store has no notes
            for that user.
        """
        self.load()
        return self._users.require(username)

    def note_types(self, *, refresh=False):
        """The subreddit's note types, as ``usernoteColors`` defines them.

        Read from the subreddit's ``toolbox`` config page on first use and
        cached; a subreddit that has not configured any gets toolbox's
        defaults.  Any problem reading the config is a warning, not an error:
        the defaults are always a usable answer.

        :param refresh: read the config page again.
        """
        if self._note_types is not None and not refresh:
            return self._note_types

        config = None
        try:
            config = self.client.read_json_page(self.subreddit, 'toolbox', missing_ok=True)
        except FormatError as err:
            self.log.warning(f'/r/{self.subreddit} toolbox config is not JSON ({err}); '
                             'using the default note types')
        except NoWikiPageError:
            config = None
        except ApiError as err:
            self.log.warning(f'could not read the note types for /r/{self.subreddit} '
                             f'({err}); using the default note types')

        colors = config.get('usernoteColors') if isinstance(config, dict) else None
        try:
            self._note_types = note_types_from_config(colors)
        except InvalidConfigError as err:
            self.log.warning(f'/r/{self.subreddit} usernoteColors are unusable ({err}); '
                             'using the default note types')
            self._note_types = DEFAULT_NOTE_TYPES
        if not colors:
            self.log.debug(f'/r/{self.subreddit} defines no usernoteColors; '
                           'using the default note types')
        return self._note_types

    # -- editing ------------------------------------------------------------

    def add(self, username, text, *, type=None, link='', mod=None, time=None,
            note_types=None):
        """Add a note for *username* and return it.

        :param username: the user the note is about.  ``u/alice`` and
            ``alice`` are the same user; notes are stored under the spelling
            already in the store, or under this one for a new user.
        :param text: the note itself.  Surrounding whitespace is trimmed (as
            toolbox's edit dialog does) and an empty note is refused.
        :param type: a note type - its key (``'spamwatch'``), its display text
            (``'Spam Watch'``) or a loose spelling of either (``'spam watch'``).
            ``None`` for an untyped note.
        :param link: the thing the note is about: an absolute URL, or a
            squashed value (``l,<id>``) from an older store.  ``''`` for none.
        :param mod: who to record as the author.  Defaults to the signed-in
            account, which is one request (cached).
        :param time: creation time in milliseconds since the epoch.  Defaults
            to now; only tests and importers should pass it.
        :param note_types: the type table to resolve *type* against, to skip
            reading the subreddit config.
        :raises ValueError: if the username or text is unusable.
        :raises usernotes.errors.UnknownNoteTypeError: if *type* is not one the
            subreddit defines.
        :raises usernotes.errors.NotLoggedInError: if *mod* has to be looked up
            and the cookies are not usable.
        """
        self.load()
        name = normalize_username(username)
        note = Note(
            text=_clean_text(text),
            time=_now_ms() if time is None else _check_time(time),
            mod=self._author(mod),
            type=self._note_type_key(type, note_types),
            link=self._link(link),
        )
        # Toolbox prepends (unshift), and its revision reasons read like this.
        self._users.add(name, note)
        self._changes.append(f'create new note on user {name}')
        return note

    def update(self, username, time, *, text=_UNSET, type=_UNSET, link=_UNSET,
               mod=_UNSET):
        """Change one note in place and return it.

        Only the fields given are touched; to *clear* one, pass ``None`` for
        *type* or *mod*, or ``''`` for *link*.  Note text cannot be cleared -
        an empty note is not a thing toolbox can store - so deleting the note
        is `remove`'s job.

        :param time: the note to change, by its timestamp (milliseconds, as
            `Note.time` gives it) or its whole seconds.  A second is not a
            precise enough key to be sure which note was meant when several
            share one: the stored format has nothing finer, so the first of
            them in stored order is taken, which is what toolbox does, and the
            store says so in a warning.
        :param type: as `add`; ``None`` to leave the note with no type.
        :param mod: the author to record; ``None`` to record none.
        :raises usernotes.errors.UserNotFoundError: if the user has no notes.
        :raises usernotes.errors.NoteNotFoundError: if there is no such note.
        :raises ValueError: if no field was given, or *text* is empty.
        :raises usernotes.errors.UnknownNoteTypeError: if *type* is not one the
            subreddit defines.
        """
        user = self.require(username)
        if text is _UNSET and type is _UNSET and link is _UNSET and mod is _UNSET:
            raise ValueError('update() needs at least one of text=, type=, link=, mod=')
        _warn_ambiguous(user, time, 'changing', self.log)
        note = user.require(time)

        if text is not _UNSET:
            note.text = _clean_text(text)
        if type is not _UNSET:
            note.type = self._note_type_key(type, None)
        if link is not _UNSET:
            note.link = self._link(link)
        if mod is not _UNSET:
            note.mod = None if mod is None else normalize_username(mod)
        self._changes.append(f'update note {note.time} on user {user.name}')
        return note

    def remove(self, username, time):
        """Delete one note and return it.

        A user whose last note is deleted is dropped from the store entirely,
        as toolbox does.

        :param time: as `update`.
        :raises usernotes.errors.UserNotFoundError: if the user has no notes.
        :raises usernotes.errors.NoteNotFoundError: if there is no such note.
        """
        user = self.require(username)
        _warn_ambiguous(user, time, 'deleting', self.log)
        note = self._users.remove_note(username, time)
        self._changes.append(f'delete note {note.time} on user {user.name}')
        return note

    def remove_user(self, username):
        """Delete every note stored for *username*.

        :returns: how many notes were removed.
        :raises usernotes.errors.UserNotFoundError: if there are none.
        """
        self.load()
        name = normalize_username(username)
        removed = self._users.remove_user(name)
        self._changes.append(f'delete all {removed} note(s) on user {name}')
        return removed

    # -- writing ------------------------------------------------------------

    def save(self, reason=None, *, force=False, check_stale=True, update_settings=True):
        """Write the store back to the wiki as a new revision.

        :param reason: revision note for the page history.  Defaults to a
            description of the pending edits, worded like toolbox's
            (``create new note on user alice``), with this tool's name
            appended.  The whole store is rewritten either way, because that
            is the only granularity the format has.
        :param force: write even when nothing has changed.  Without it, a save
            with no pending edits is a no-op, so an accidental second write
            does not leave an empty revision in the page history.
        :param check_stale: re-read the page first and refuse to overwrite a
            version that changed since `load`.  Turning this off means a
            moderator's concurrent change is lost; it exists for scripts that
            know they are the only writer.
        :param update_settings: re-apply the page's "listed, moderators only"
            settings after writing, as toolbox does.  A failure here is
            reported as a warning: the notes themselves were saved.
        :returns: whether anything was written.
        :raises usernotes.errors.StaleStoreError: if the page changed and
            *check_stale* is on.
        :raises usernotes.errors.PageFullError: if Reddit refuses the page as
            too large - the store needs pruning before it can be saved.
        """
        self.load()
        if not self._changes and not force:
            self.log.debug(f'/r/{self.subreddit} usernotes: nothing to save')
            return False
        if check_stale:
            self._check_stale()

        # A user entry with no notes carries nothing and only makes the page
        # bigger, which matters: the wiki page has a hard size limit.
        dropped = self._users.prune()
        if dropped:
            self.log.debug(f'dropped {dropped} user entr{"y" if dropped == 1 else "ies"} '
                           'with no notes')

        content = build_page(self._users.as_dict(), ver=NOTES_SCHEMA,
                             base_url=self.client.base_url)
        self.client.write_page(self.subreddit, self.page, content,
                               reason=_with_provenance(self._reason(reason)))
        if update_settings:
            try:
                self.client.set_page_settings(self.subreddit, self.page)
            except ApiError as err:
                self.log.warning(
                    f'the usernotes were saved, but re-applying the page '
                    f'settings failed ({err}); check that /r/{self.subreddit}/wiki/'
                    f'{self.page} is still listed and moderators-only')

        self._raw = content
        self._exists = True
        self._ver = NOTES_SCHEMA
        self._changes = []
        self._warnings = ()
        return True

    def backup(self, path=None, *, directory=None):
        """Write a copy of the wiki page to a file, for safekeeping.

        The file holds the page's text exactly as Reddit returned it, never a
        re-serialisation: a backup that cannot be decoded is still a backup,
        and re-encoding it through this tool's own reader would be the one way
        to lose something.

        :param path: the file to write.  Defaults to
            ``usernotes_<subreddit>_<UTC timestamp>.json`` inside *directory*
            (or the current directory).
        :param directory: where to put a generated filename.  Created if it
            does not exist.
        :returns: a `Backup` describing what was written.
        :raises usernotes.errors.NoWikiPageError: if there is no page to back
            up - an empty file would be a misleading backup.
        :raises OSError: if the file cannot be written.
        """
        raw = self.client.read_page(self.subreddit, self.page, missing_ok=False)
        taken_at = datetime.datetime.now(datetime.timezone.utc)
        if path is None:
            name = f'usernotes_{self.subreddit}_{taken_at:%Y%m%dT%H%M%SZ}.json'
            path = os.path.join(directory, name) if directory else name
        path = os.fspath(path)

        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        _write_atomic(path, raw)
        size = len(raw.encode('utf-8'))

        notes = users = None
        warnings = ()
        try:
            page = parse_page(raw, self.subreddit, base_url=self.client.base_url)
        except FormatError as err:
            warnings = (f'the page could not be decoded: {err}',)
            self.log.warning(f'backed up /r/{self.subreddit} usernotes, but they '
                             f'could not be decoded ({err})')
        else:
            notes = page.note_count()
            users = len(page.users)
            warnings = page.warnings
            self._log_warnings(warnings)
        return Backup(path=path, subreddit=self.subreddit, taken_at=taken_at,
                      size=size, notes=notes, users=users, warnings=warnings)

    # -- internals ----------------------------------------------------------

    def _check_stale(self):
        current = self.client.read_page(self.subreddit, self.page, missing_ok=True)
        if _comparable(current) != _comparable(self._raw):
            raise StaleStoreError(
                f'/r/{self.subreddit}/wiki/{self.page} changed since it was read '
                '(another moderator saved notes, or it was edited on the wiki). '
                'Re-read with reload() and apply the change again, or pass '
                'check_stale=False to overwrite that revision')

    def _reason(self, reason):
        if reason:
            return reason
        if not self._changes:
            return f'rewrote usernotes for /r/{self.subreddit} unchanged'
        return '; '.join(self._changes)

    def _author(self, mod):
        if mod is None:
            return self.client.account().name
        return normalize_username(mod)

    def _note_type_key(self, value, note_types):
        if value is None:
            return None
        table = self.note_types() if note_types is None else note_types
        found = note_type(value, table)
        if found is None:
            raise UnknownNoteTypeError(
                f'{value!r} is not a note type on /r/{self.subreddit}; '
                f'known types are {", ".join(t.key for t in table)}',
                value=value, known=tuple(t.key for t in table))
        return found.key

    def _link(self, link):
        if not link:
            return ''
        if not isinstance(link, str):
            raise ValueError(f'link is {type(link).__name__}, not text: {link!r}')
        # Accept a squashed value (`l,abc,def`) as well as a URL, so a caller
        # can pass back what it read out of an older store.
        if re.match(r'^[lm],', link):
            expanded = unsquash_link(self.subreddit, link, base_url=self.client.base_url)
            if not expanded:
                raise ValueError(f'link is not a usable squashed link: {link!r}')
            return expanded
        return link.strip()

    def _log_warnings(self, warnings):
        for warning in warnings[:_MAX_LOGGED_WARNINGS]:
            self.log.warning(f'/r/{self.subreddit} usernotes: {warning}')
        if len(warnings) > _MAX_LOGGED_WARNINGS:
            self.log.warning(f'/r/{self.subreddit} usernotes: ...and '
                             f'{len(warnings) - _MAX_LOGGED_WARNINGS} more problem(s)')


def _comparable(text):
    """The form of a page used to decide whether it has changed.

    Line endings and surrounding whitespace are ignored.  A wiki edit that
    only re-wraps the page changes nothing about the notes, and reddit is
    within its rights to normalise them; treating that as "another moderator
    got there first" would block a save for no reason.  Anything else - one
    byte of the notes themselves - is a change, and is not written over.
    """
    if text is None:
        return None
    return text.replace('\r\n', '\n').strip()


def _warn_ambiguous(user, time, action, log):
    """Say so when a timestamp does not pick out exactly one note.

    The stored format keeps whole seconds and has no note id, so two notes
    added in the same second are indistinguishable even to toolbox.  Silence
    would be worse than the ambiguity: the caller is about to change or delete
    one of them.
    """
    matches = user.find_all(time)
    if len(matches) > 1:
        log.warning(
            f'user {user.name!r} has {len(matches)} notes with the same timestamp '
            f'({time}); {action} the first of them, which is what toolbox does')


def _clean_text(text):
    if not isinstance(text, str):
        raise ValueError(f'note text is {type(text).__name__}, not text: {text!r}')
    text = text.strip()
    if not text:
        raise ValueError('note text is empty')
    return text


def _check_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'note time is {type(value).__name__}, not a number: {value!r}')
    if value < 0:
        raise ValueError(f'note time is negative: {value!r}')
    return int(value)


def _now_ms():
    return int(time.time() * 1000)


def _with_provenance(reason):
    """Mark the revision as this tool's, the way toolbox marks its own."""
    reason = f'{reason} via usernotes-cli'
    return reason if len(reason) <= _MAX_REASON else reason[:_MAX_REASON - 3] + '...'


def _write_atomic(path, text):
    """Write *text* to *path* without ever leaving a half-written file.

    Backups may overwrite an earlier one, and a machine that dies mid-write
    must not destroy it; also write in binary so no platform translates the
    line endings of the page text.
    """
    data = text.encode('utf-8')
    temporary = f'{path}.tmp'
    try:
        with open(temporary, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
