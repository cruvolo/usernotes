# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""The notes a subreddit holds, as an editable in-memory collection.

This is the layer between the wire format (`usernotes.format`) and the wiki
(`usernotes.store`): it answers "what notes does u/alice have", "add this
note", "change that one".  It holds no network code, so it can be exercised on
its own.

Two behaviours here are copied from toolbox because stores in the wild depend
on them:

* **Usernames are matched case-insensitively.**  Reddit usernames are
  case-insensitive, and a few third-party writers have stored a lower-cased
  key - so a lookup for ``Alice`` also finds an entry stored as ``alice``.  The
  first spelling seen is the canonical one, and entries merged under it are
  written back under the canonical name only, which is what toolbox does on
  save (``usernotes.js:73-98`` and ``:675-679``).
* **Notes are kept newest first.**  Toolbox prepends new notes
  (``unshift``), so ``ns[0]`` is normally the newest; here the order is made
  explicit by sorting on the timestamp, which also copes with pages written by
  other tools and with notes whose time is unknown.
"""

from __future__ import annotations

import re

from .errors import NoteNotFoundError, UserNotFoundError
from .format import Note, describe_time

__all__ = [
    'Notes',
    'UserNotes',
    'normalize_username',
]

#: Reddit usernames are 3-20 characters of letters, digits, ``_`` and ``-``.
#: Legacy stores can hold odd keys, so the minimum length is not enforced.
_USERNAME_RE = re.compile(r'[A-Za-z0-9_-]{1,20}')


def normalize_username(value):
    """Clean up a username as typed on the command line.

    Accepts what moderators actually type - ``u/alice``, ``/u/alice``,
    ``alice`` with stray whitespace - and rejects anything Reddit would not
    accept as a username, so a typo fails here rather than as an empty
    ``find()`` result or a wiki page written under a nonsense key.

    :raises ValueError: if *value* is not text, or not a plausible username.
    """
    if not isinstance(value, str):
        raise ValueError(f'username is {type(value).__name__}, not text: {value!r}')
    name = value.strip()
    for prefix in ('/u/', 'u/'):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.strip()
    if not _USERNAME_RE.fullmatch(name):
        raise ValueError(
            f'not a reddit username: {value!r} '
            '(expected up to 20 characters of letters, digits, "_" or "-")')
    return name


class UserNotes:
    """One user's notes, newest first.

    :param name: the username as stored (the canonical spelling).
    :param notes: the notes, in any order; sorted here.
    :param aliases: other keys this user's notes were found under, e.g. the
        lower-cased duplicate a third-party writer left behind.  They are
        dropped when the page is written back.
    """

    def __init__(self, name, notes=(), *, aliases=()):
        self.name = name
        self.aliases = tuple(aliases)
        self.notes = list(notes)
        self.sort()

    def __repr__(self):
        return f'<UserNotes {self.name!r}: {len(self.notes)} note(s)>'

    def __len__(self):
        return len(self.notes)

    def __iter__(self):
        return iter(self.notes)

    def __bool__(self):
        return bool(self.notes)

    def __contains__(self, note):
        return note in self.notes

    def sort(self):
        """Order the notes newest first.

        The sort is stable, so notes sharing a timestamp (the stored format
        only keeps whole seconds) stay in the order they were found, and a
        note added out of order does not shuffle its neighbours.
        """
        self.notes.sort(key=lambda note: note.time, reverse=True)
        return self

    def times(self):
        """The timestamps of this user's notes, newest first."""
        return tuple(note.time for note in self.notes)

    def find_all(self, time):
        """Every note matching *time*.

        *time* is milliseconds since the epoch; a whole-second value also
        matches, since that is the precision the stored format has and what
        toolbox identifies notes by.

        :returns: a tuple of notes, newest first, empty when none match.
        """
        seconds = int(time / 1000)
        return tuple(note for note in self.notes
                     if note.time == time or (seconds and note.seconds == seconds))

    def find(self, time):
        """The note matching *time*, or ``None``.

        When several notes match because the timestamp is only second-accurate,
        the newest one wins, which is the one toolbox would act on (it walks
        the notes newest first and stops at the first match).  Notes sharing
        exactly the same timestamp are indistinguishable in the stored format;
        the first of those in stored order is taken.

        Use `find_all` to see the whole set, and check whether there is more
        than one before acting on it.
        """
        matches = self.find_all(time)
        return matches[0] if matches else None

    def require(self, time):
        """The note matching *time*.

        :raises NoteNotFoundError: if there is none, with the timestamps that
            would have worked in the message.
        """
        note = self.find(time)
        if note is None:
            raise NoteNotFoundError(
                f'no note with time {time} on user {self.name!r}; '
                f'that user has {_describe_times(self.notes)}')
        return note

    def add(self, note):
        """Add *note*, keeping the newest-first order (toolbox unshifts)."""
        self.notes.insert(0, note)
        return self.sort()

    def remove(self, time):
        """Remove and return the note matching *time*.

        :raises NoteNotFoundError: if there is no such note.
        """
        note = self.require(time)
        self.notes.remove(note)
        return note

    def as_dict(self):
        """``{'name', 'aliases', 'notes'}``, for JSON output."""
        return {
            'name': self.name,
            'aliases': list(self.aliases),
            'notes': [note.as_dict() for note in self.notes],
        }


class Notes:
    """Every user's notes in one subreddit, keyed by username.

    Behaves like a read-only mapping of username to `UserNotes`, with the
    mutating operations (`add`, `remove_note`, `remove_user`) keeping the
    canonical-name and ordering rules described in the module docstring.

    :param users: username -> iterable of `Note`, e.g. a parsed page's
        ``Page.users``.  Keys that differ only by case are merged, first seen
        wins the spelling.
    """

    def __init__(self, users=None):
        self._users: dict[str, UserNotes] = {}
        #: casefolded alias -> canonical key, for case-insensitive lookups and
        #: for spotting the duplicate keys foreign writers leave behind.
        self._index: dict[str, str] = {}
        for name, notes in (users or {}).items():
            self._ingest(name, notes)

    def __repr__(self):
        return f'<Notes {len(self._users)} user(s), {self.note_count()} note(s)>'

    def __len__(self):
        return len(self._users)

    def __iter__(self):
        return iter(self._users)

    def __contains__(self, username):
        return self.find(username) is not None

    def _ingest(self, name, notes):
        canonical = self._index.get(_key(name))
        if canonical is None:
            self._users[name] = UserNotes(name, notes)
            self._index[_key(name)] = name
        else:
            user = self._users[canonical]
            user.notes.extend(notes)
            user.aliases = user.aliases + (name,)
            user.sort()

    def find(self, username):
        """The `UserNotes` for *username*, or ``None``.

        Matching ignores case and a leading ``u/``.
        """
        canonical = self._index.get(_key(normalize_username(username)))
        return None if canonical is None else self._users[canonical]

    def require(self, username):
        """The `UserNotes` for *username*.

        :raises UserNotFoundError: if the store has no notes for that user.
        """
        user = self.find(username)
        if user is None:
            raise UserNotFoundError(f'no notes stored for user {username!r}')
        return user

    def add(self, username, note):
        """Store *note* for *username* and return it.

        A new user entry is created under the spelling given here; for a user
        who already has notes, theirs is used, so notes never end up split
        across two spellings of the same name.
        """
        user = self.find(username)
        if user is None:
            user = UserNotes(normalize_username(username))
            self._users[user.name] = user
            self._index[_key(user.name)] = user.name
        user.add(note)
        return note

    def remove_note(self, username, time):
        """Remove the note matching *time* from *username*.

        The user entry goes too when it was their last note, which is what
        toolbox does and what keeps the store from filling up with empty
        shells.

        :returns: the removed `Note`.
        :raises UserNotFoundError: if the store has no notes for that user.
        :raises NoteNotFoundError: if that user has no note at that time.
        """
        user = self.require(username)
        note = user.remove(time)
        if not user.notes:
            self._drop(user)
        return note

    def remove_user(self, username):
        """Remove every note stored for *username*.

        :returns: how many notes were removed.
        :raises UserNotFoundError: if the store has no notes for that user.
        """
        user = self.require(username)
        removed = len(user.notes)
        self._drop(user)
        return removed

    def _drop(self, user):
        self._users.pop(user.name, None)
        self._index.pop(_key(user.name), None)
        for alias in user.aliases:
            self._index.pop(_key(alias), None)
        user.notes.clear()

    def prune(self):
        """Drop user entries that hold no notes.

        Toolbox removes a user whose last note is deleted, but a store written
        by something else can still hold an empty ``ns`` array.  Nothing is
        lost by dropping those, and leaving them in grows the page for no
        reason - the wiki has a hard size limit.

        :returns: how many entries were dropped.
        """
        empty = [user for user in self._users.values() if not user.notes]
        for user in empty:
            self._drop(user)
        return len(empty)

    def usernames(self):
        """The stored usernames, in the order they were read."""
        return tuple(self._users)

    def items(self):
        """``(username, UserNotes)`` pairs, in the order they were read."""
        return tuple(self._users.items())

    def note_count(self):
        """Total number of notes across all users."""
        return sum(len(user) for user in self._users.values())

    def as_dict(self):
        """``{username: tuple[Note, ...]}``, ready for `usernotes.format.build_page`."""
        return {name: tuple(user.notes) for name, user in self._users.items()}

    def as_json(self):
        """The whole store as JSON-friendly data, for the CLI's ``--json``."""
        return {name: user.as_dict() for name, user in self._users.items()}


def _key(username):
    return username.casefold()


def _describe_times(notes):
    """The times a user does have, for a message about one they have not.

    Rendered as a listing renders them (see `usernotes.format.describe_time`),
    because the numbers the store works in are nothing a moderator can match
    against a note they are looking at.
    """
    if not notes:
        return 'no notes'
    return 'times ' + ', '.join(describe_time(note.time) for note in notes)
