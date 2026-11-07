# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Find the notes a search key matches, for ``usernotes search``.

Toolbox's usernotes manager has two search boxes - one for the user, one for
the note contents - and both match without regard to case, as a substring of
what is on the screen (``usernotes.js:1142-1168``).  A command line has room
for one pattern and for more fields, so this module matches a single key
against everything that identifies a note: the user it belongs to, the note
text, its flag, and its timestamp.

Nothing here reads or writes anything: a search is a predicate over notes
somebody else has already loaded.
"""

from __future__ import annotations

import re

from .display import format_time, safe_text
from .format import note_type as find_note_type

__all__ = ['fields', 'matcher', 'search']


def fields(username, note, note_types=()):
    """The strings a search is matched against, for one note.

    Four fields, and where a field has more than one spelling, all of them -
    the point of a search is to find a note by whatever its moderator happens
    to remember about it:

    * the username, as the store spells it and as reddit writes it
      (``/u/name``);
    * the note text, exactly as the listing shows it: HTML entities decoded
      and whitespace collapsed, so a search for what is on the screen finds
      it, and the control characters a listing never prints are not there to
      be matched either;
    * the flag, both as the key the store holds (``spamwatch``) and as the
      name the subreddit shows for it (``Spam Watch``);
    * the timestamp, in the two forms a moderator might have in hand: as the
      listing prints it (``2026-11-04 06:12:45 UTC``) and as ISO 8601
      (``2026-11-04T06:12:45+00:00``).  A note with no time has neither.

    :param username: the canonical username the note is stored under.
    :param note: a `usernotes.format.Note`.
    :param note_types: the subreddit's `usernotes.format.NoteType` table, so
        that a flag can be matched by name as well as by key.
    :returns: a tuple of strings.
    """
    values = [safe_text(username), f'/u/{safe_text(username)}']

    text = safe_text(note.display_text())
    if text:
        values.append(text)

    if note.type:
        values.append(safe_text(note.type))
        known = find_note_type(note.type, note_types)
        if known is not None:
            values.append(safe_text(known.text))

    if note.has_time:
        moment = note.datetime()
        values.append(format_time(moment, zone=True))
        values.append(moment.isoformat())

    return tuple(values)


def matcher(pattern, *, regex=False, ignore_case=True):
    """A predicate over the strings of one note, for `search`.

    Built apart from `search` so that a command line front end can make one -
    and so find out that a regular expression does not compile - before it goes
    to the network for any notes to match against.

    :param pattern: what to look for.
    :param regex: treat *pattern* as a regular expression, matched anywhere in
        a field (``re.search``), rather than as a substring of it.
    :param ignore_case: match without regard to case, which is what toolbox's
        search boxes do.
    :raises ValueError: if *pattern* is not text, or is not a regular
        expression and *regex* was asked for.  The message is re's own, which
        says what is wrong with it.
    """
    if not isinstance(pattern, str):
        raise ValueError(f'search pattern is {type(pattern).__name__}, not text: '
                         f'{pattern!r}')
    if regex:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as err:
            raise ValueError(f'not a regular expression (--regex): {err}') from err
        return lambda values: any(compiled.search(value) for value in values)
    if ignore_case:
        needle = pattern.casefold()
        return lambda values: any(needle in value.casefold() for value in values)
    return lambda values: any(pattern in value for value in values)


def search(users, matches, note_types=()):
    """The notes that *matches* accepts, in the order they were given.

    :param users: ``(username, notes)`` pairs, as
        `usernotes.model.Notes.items` returns them - the stored spelling of
        each username, with that user's notes newest first.
    :param matches: a predicate from `matcher`.
    :param note_types: as `fields`.
    :returns: a list of ``(username, note)`` pairs, which is what a listing
        needs to show whose note each match is.
    """
    return [(username, note)
            for username, notes in users
            for note in notes
            if matches(fields(username, note, note_types))]
