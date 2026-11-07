# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Command line tools for the reddit Moderator Toolbox "usernotes" store.

Usernotes are kept by the toolbox browser extension in a subreddit wiki page
(``https://www.reddit.com/r/<subreddit>/wiki/usernotes``) as compressed, base64
encoded JSON.  This package reads, edits and writes that store outside of a
browser, and backs it up.

Modules:

`usernotes.store`
    The usual entry point: `~usernotes.store.UsernotesStore` loads a
    subreddit's notes, edits them, and saves them back as a wiki revision.
`usernotes.format`
    The storage format itself - the page JSON, its zlib/base64 blob, the
    constants pools, link squashing, and the ``Note`` and ``NoteType`` types.
    Pure: no network, no Reddit.
`usernotes.model`
    The in-memory notes collection: case-insensitive usernames, newest-first
    ordering, adding and removing notes.
`usernotes.api`
    Reddit's wiki API over `urllib` with a caller-supplied cookie.
`usernotes.cookies`
    Retrieve the cookies needed to authenticate to Reddit, vendored from and
    compatible with ``yt-dlp --cookies-from-browser``.
`usernotes.errors`
    Every exception the package raises.
`usernotes.cli`
    The ``usernotes`` command line tool over all of the above, reached either
    as ``usernotes``, ``python -m usernotes`` or `usernotes.cli.main`.
`usernotes.display`
    Turning one user's notes into the text a terminal is shown, coloured the
    way toolbox colours them in the browser.

Cookies are always a parameter, never something this package fetches on its
own: pass whatever `usernotes.cookies.load_cookie_jar` returns to
``UsernotesStore(cookies=...)``.  `usernotes.cli` is the one place that goes
and gets them, from ``--cookies-from-browser`` or ``--cookies FILE``.

The commands implemented so far are ``usernotes read SUBREDDIT USERNAME``,
which prints one user's notes; ``usernotes add SUBREDDIT USERNAME TEXT``, which
writes a note and prints those notes again, read back from the wiki; and
``usernotes flags SUBREDDIT``, which lists the note types - the flags - the
subreddit defines.  The library underneath them can already back a store up, so
``backup``, ``restore`` and ``search`` are a matter of more commands in
`usernotes.cli`.

A first look at a subreddit's notes, from a shell and from python::

    $ usernotes read toolbox alice --cookies-from-browser firefox
    $ usernotes add toolbox alice "banned for abuse" --flag ban

    from usernotes import cookies
    from usernotes.store import UsernotesStore

    with UsernotesStore('toolbox', cookies=cookies.load_cookie_jar(from_browser='firefox')) as store:
        store.load()
        for note in store.notes('alice'):
            print(note.datetime(), note.type, note.text)
"""

from ._version import __version__
from .api import RedditWikiClient, User, cookie_jar
from .errors import UsernotesError
from .format import (
    DEFAULT_NOTE_TYPES,
    NOTES_PAGE,
    NOTES_SCHEMA,
    Note,
    NoteType,
    Page,
    build_page,
    parse_page,
)
from .model import Notes, UserNotes, normalize_username
from .store import Backup, UsernotesStore, normalize_subreddit

__all__ = [
    'Backup',
    'DEFAULT_NOTE_TYPES',
    'NOTES_PAGE',
    'NOTES_SCHEMA',
    'Note',
    'NoteType',
    'Notes',
    'Page',
    'RedditWikiClient',
    'User',
    'UsernotesError',
    'UsernotesStore',
    'UserNotes',
    '__version__',
    'build_page',
    'cookie_jar',
    'normalize_subreddit',
    'normalize_username',
    'parse_page',
]
