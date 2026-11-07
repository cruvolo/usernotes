# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

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

Backups, and putting them back
------------------------------

`backup` writes the page's text to a file exactly as Reddit returned it, and
`restore` writes such a file back as a new revision - also exactly, never
through this tool's own writer.  So a page that this tool cannot decode is
still backed up, and still restorable, as it is; re-encoding either way would
be the one way to lose something.

Restoring is the one operation here that can take notes away, because it
replaces the page rather than editing it, so it checks what it is about to
replace first - see `restore`.  Reddit keeps every revision, so a restore can
be undone from the page history like any other edit.
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
    'Restore',
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

    def as_dict(self):
        """A JSON-friendly view of the backup, for a CLI's ``--json``."""
        return {
            'path': self.path,
            'subreddit': self.subreddit,
            'taken_at': self.taken_at.isoformat(),
            'size': self.size,
            'notes': self.notes,
            'users': self.users,
            'warnings': list(self.warnings),
        }


@dataclasses.dataclass(frozen=True)
class Restore:
    """What `UsernotesStore.restore` wrote, or would have written.

    :param path: the file that was restored.
    :param subreddit: the subreddit whose notes it was written to.  Not
        necessarily the one the backup was taken from: the file holds a page,
        not a name, so a backup can be put into another subreddit.
    :param size: how many bytes the file holds.
    :param ver: the schema version the file decoded as, or ``None`` when it
        could not be decoded - a restore that was forced.
    :param notes: how many notes the file decoded to, or ``None``.
    :param users: how many users the file decoded to, or ``None``.
    :param replaced_notes: how many notes the page held before the restore, or
        ``None`` when there was no page, or it could not be decoded either.
    :param replaced_users: as *replaced_notes*, for users.
    :param warnings: anything odd found while decoding the file, empty for a
        clean one.
    :param written: whether the page was written.  False for a dry run, and
        for a page that already held the file.
    :param dry_run: whether this was a dry run, which is what tells the two
        ways of not writing apart.
    """

    path: str
    subreddit: str
    size: int
    ver: int | None = None
    notes: int | None = None
    users: int | None = None
    replaced_notes: int | None = None
    replaced_users: int | None = None
    warnings: tuple[str, ...] = ()
    written: bool = False
    dry_run: bool = False

    def summary(self):
        """One line describing the restore, for a CLI to print."""
        if self.notes is None:
            counts = f'{self.size} bytes that could not be decoded'
        else:
            counts = f'{self.notes} notes for {self.users} users ({self.size} bytes)'
        if self.written:
            return f'{self.path}: restored {counts} to /r/{self.subreddit}'
        why = 'dry run' if self.dry_run else 'the page already holds it'
        return f'{self.path}: {counts} not written to /r/{self.subreddit} ({why})'

    def as_dict(self):
        """A JSON-friendly view of the restore, for a CLI's ``--json``."""
        return {
            'path': self.path,
            'subreddit': self.subreddit,
            'schema': self.ver,
            'size': self.size,
            'notes': self.notes,
            'users': self.users,
            'replaced_notes': self.replaced_notes,
            'replaced_users': self.replaced_users,
            'written': self.written,
            'dry_run': self.dry_run,
            'warnings': list(self.warnings),
        }


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
        self._owns_client = client is None
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

    def close(self):
        """Release the client's connections, if this store built the client.

        A store constructed with ``cookies=`` owns its client and closes it; a
        store given ``client=`` does not close it, because the caller is
        sharing that client with something else.
        """
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

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

    def restore(self, path, *, force=False, reason=None, dry_run=False,
                update_settings=True):
        """Write a backup file back to the wiki, as a new revision.

        This is `backup` in reverse, and the one thing here that can *lose*
        notes: whatever the page holds now is replaced by what the file holds.
        The file's text goes to the wiki byte for byte, never through this
        tool's own writer, so what comes back is what was backed up - including
        the parts of it this tool cannot read (see *force*).

        Four things are done first, because the alternative is a moderator
        noticing afterwards that a month of notes is gone:

        * the file has to be there, be UTF-8 text, and not be empty;
        * it has to decode as a usernotes page unless *force* says otherwise -
          writing something that is not a store over a store would take the
          notes with it;
        * the page is read, so that what is about to be replaced is known, and
          a file holding *fewer* notes than the page does is reported as a
          warning before anything is written;
        * a page that already holds exactly this file is not written at all, so
          re-running a restore does not leave an empty revision behind.

        Reddit keeps the page's revision history, so a restore is undone the
        same way any other wiki edit is; `backup` first if the notes as they
        stand are worth keeping.

        :param path: the file to restore, as `backup` wrote it.
        :param force: write a file that does not decode as a usernotes page,
            for putting back a backup of a page that was already damaged.  The
            notes in it will not be readable afterwards either, until whatever
            damaged them is undone.
        :param reason: revision note for the page history.  Defaults to the
            file's name, with this tool's name appended, as `save` does.
        :param dry_run: do everything except the write, so that a moderator can
            see what a restore would do first.
        :param update_settings: re-apply the page's "listed, moderators only"
            settings afterwards, as `save` does.
        :returns: a `Restore` describing what was, or would have been, written.
        :raises OSError: if the file cannot be read.
        :raises usernotes.errors.FormatError: if the file is not UTF-8 text, is
            empty, or does not decode and *force* was not given.
        :raises usernotes.errors.PageFullError: if Reddit refuses the page as
            too large.
        """
        path = os.fspath(path)
        content = _read_backup(path)
        size = len(content.encode('utf-8'))

        page = None
        warnings = ()
        try:
            page = parse_page(content, self.subreddit, base_url=self.client.base_url)
        except FormatError as err:
            if not force:
                raise FormatError(
                    f'{path} is not a usernotes page ({err}); refusing to write it '
                    f'over /r/{self.subreddit}/wiki/{self.page} - pass force=True '
                    'to restore it anyway') from err
            warnings = (f'the file could not be decoded: {err}',)
            self.log.warning(f'restoring {path}, which could not be decoded ({err})')

        notes = users = ver = None
        if page is not None:
            notes, users, ver = page.note_count(), len(page.users), page.ver
            warnings = page.warnings
            self._log_warnings(warnings)

        current = self.client.read_page(self.subreddit, self.page, missing_ok=True)
        replaced_notes = replaced_users = None
        if current is not None:
            try:
                replaced = parse_page(current, self.subreddit,
                                      base_url=self.client.base_url)
            except FormatError:
                pass        # a page that cannot be decoded counts as unknown
            else:
                replaced_notes = replaced.note_count()
                replaced_users = len(replaced.users)

        if _comparable(current) == _comparable(content):
            self.log.info(f'/r/{self.subreddit}/wiki/{self.page} already holds '
                          f'{path}; nothing to write')
            written = False
        elif dry_run:
            self.log.info(f'{path} would be written to '
                          f'/r/{self.subreddit}/wiki/{self.page}')
            written = False
        else:
            if self._changes:
                self.log.warning(
                    f'{path} is being restored over {len(self._changes)} unsaved '
                    f'change(s) to /r/{self.subreddit}; they will not be written')
            if notes is not None and replaced_notes is not None and notes < replaced_notes:
                self.log.warning(
                    f'{path} holds {notes} note(s) and /r/{self.subreddit}/wiki/'
                    f'{self.page} holds {replaced_notes}: restoring takes '
                    f'{replaced_notes - notes} of them away')
            self.client.write_page(
                self.subreddit, self.page, content,
                reason=_with_provenance(self._restore_reason(reason, path)))
            if update_settings:
                try:
                    self.client.set_page_settings(self.subreddit, self.page)
                except ApiError as err:
                    self.log.warning(
                        f'the usernotes were restored, but re-applying the page '
                        f'settings failed ({err}); check that /r/{self.subreddit}/'
                        f'wiki/{self.page} is still listed and moderators-only')
            written = True

        if written:
            # The store holds what the wiki holds now, as it does after `save`.
            self._raw = content
            self._exists = True
            self._changes = []
            self._warnings = warnings
            self._loaded = True
            self._users = Notes(page.users) if page is not None else Notes()
            self._ver = ver if ver is not None else NOTES_SCHEMA
        return Restore(path=path, subreddit=self.subreddit, size=size, ver=ver,
                       notes=notes, users=users, replaced_notes=replaced_notes,
                       replaced_users=replaced_users, warnings=warnings,
                       written=written, dry_run=dry_run)

    # -- internals ----------------------------------------------------------

    def _restore_reason(self, reason, path):
        if reason:
            return reason
        return (f'restore usernotes for /r/{self.subreddit} '
                f'from {os.path.basename(path)}')

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


def _read_backup(path):
    """The text of a backup file, for `UsernotesStore.restore`.

    Read as bytes and decoded here rather than with ``open(path)`` so that a
    file which is not UTF-8 text fails as a format problem - the caller has
    asked for the file to be written to a wiki page, and a page is text - and
    not as a UnicodeDecodeError from somewhere deeper.

    :raises OSError: if the file cannot be read.
    :raises usernotes.errors.FormatError: if it is empty, or not UTF-8.
    """
    with open(path, 'rb') as handle:
        data = handle.read()
    if not data.strip():
        raise FormatError(f'{path} is empty; there is nothing to restore')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as err:
        raise FormatError(f'{path} is not UTF-8 text ({err})') from err


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
