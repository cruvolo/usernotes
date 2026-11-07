# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""The ``usernotes`` command line tool.

This is the front end over the rest of the package: it turns arguments into a
`usernotes.store.UsernotesStore`, prints what was asked for, and reports
failures in a way a shell can act on.

Five commands are implemented.  ``read`` prints one user's notes the way
toolbox's "User Notes" dialog shows them - see `usernotes.display`; ``add``
writes a note and then prints those notes again, read back from the wiki;
``flags`` lists the note types the subreddit defines, because nothing else
would tell a moderator what ``add --flag`` accepts; ``backup`` saves the whole
wiki page to a file; and ``restore`` puts such a file back.  ``search`` is a
matter of one more command here.

Four things are worth knowing before reading the code:

* **Adding is a read-modify-write of the whole page.**  The format has no
  smaller granularity, so `add` loads the page, adds to the copy and writes it
  back as a new revision - and the store refuses to write if the page changed
  while it was being edited, because that would discard another moderator's
  note.  Nothing is written until the arguments, the flag and the message have
  all been checked, and the listing printed afterwards is read back from the
  wiki rather than from memory, so it is also the proof that the write landed.

* **Restoring replaces the page.**  Unlike ``add``, which edits the page it
  read, `restore` writes a backup file over whatever is there now, and that is
  the one way to take notes away - so the file has to decode as a usernotes
  page unless ``--force`` says otherwise, the page is read first so that what
  is about to be replaced can be reported, and ``--dry-run`` says what would
  happen without writing.  Reddit keeps the page's revision history, so a
  restore is undone the way any other wiki edit is; ``backup`` first if the
  notes as they stand are worth keeping.

* **Cookies are a parameter everywhere in this package, and this is the layer
  that gets them.**  ``--cookies-from-browser`` (yt-dlp's grammar, see
  `usernotes.cookies`) and ``--cookies FILE`` are both optional; without
  either, the request goes out anonymously, which can only read a subreddit
  whose usernotes page is public - and toolbox's are moderator-only.
  Extraction happens before the first request, and a jar is never printed or
  logged, only the *names* of the cookies that were found.

  They are global options, with ``--base-url``, because every command reads or
  writes the same wiki page and so every command needs them: they are given
  before the command - ``usernotes --cookies FILE read toolbox alice`` - and
  a command's own options never collide with them.
* **Exit statuses are the interface for scripts.**  ``0`` for a listing that
  was printed, a note that was written or a file that was backed up or
  restored; ``1`` for a request that failed, a store that could not be read, a
  file that could not be read or written, or a write that was refused (the flag
  does not exist, the page changed while the note was being added, the page is
  full); ``2`` for a usage error (argparse's own).  A user with no notes is not
  an error: the listing says so and the status stays ``0``, which is what
  toolbox does with an empty dialog.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

from ._logging import Logger
from ._version import __version__
from .api import DEFAULT_BASE_URL
from .cookies import (
    BROWSER_SPEC_METAVAR,
    CookieLoadError,
    cookie_names,
    load_cookie_jar,
    parse_browser_spec,
)
from .display import (
    COLOR_MODES,
    Style,
    color_enabled,
    parse_time,
    render_note,
    render_note_types,
    render_search_results,
    render_user_notes,
)
from .errors import ForbiddenError, NotLoggedInError, UsernotesError
from .format import note_type
from .model import normalize_username
from .search import matcher, search as search_notes
from .store import UsernotesStore, normalize_subreddit

__all__ = [
    'EXIT_ERROR',
    'EXIT_INTERRUPTED',
    'EXIT_OK',
    'build_parser',
    'main',
]

#: What the tool calls itself in messages, however it was invoked.
PROG = 'usernotes'

#: The listing was printed (an empty one is still a listing).
EXIT_OK = 0

#: Reddit refused, the store could not be read, or the cookies could not be.
EXIT_ERROR = 1

#: Ctrl-C, conventionally 128 + SIGINT.
EXIT_INTERRUPTED = 130

#: Verbosity levels, indexed by how many ``-v`` were given.
_LOG_LEVELS = ('warning', 'info', 'debug')

#: ``--flag`` values that mean "no flag", for a user whose notes carry one and
#: a note that should not.  ``none`` is what toolbox's own dialog calls the
#: untyped selection.
_NO_FLAG = ('none', '-')

#: The shapes of link that survive a save: the squashed ``l,<id>`` an older
#: store holds, and anything `usernotes.format.squash_link` can reduce.  A link
#: that is neither is squashed to nothing on the way to the wiki, so it is
#: refused here rather than silently dropped (see `_link`).
_SQUASHED_LINK_RE = re.compile(r'[lm],')
_ABSOLUTE_LINK_RE = re.compile(r'https?://', re.IGNORECASE)


def main(argv=None, *, client_options=None):
    """Run the tool and return its exit status.

    :param argv: the arguments without the program name; ``sys.argv[1:]`` by
        default.
    :param client_options: extra `usernotes.api.RedditWikiClient` options -
        ``min_interval``, ``timeout``, ``base_url`` - used to build the client.
        The tests point it at a local server and turn the request pacing off
        with it; a caller embedding the tool can do the same.
    :returns: `EXIT_OK`, `EXIT_ERROR` or `EXIT_INTERRUPTED`.  A usage error
        never gets here: argparse reports it and raises `SystemExit` itself.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = Logger(level=_log_level(args))

    try:
        return args.run(args, logger, client_options)
    except KeyboardInterrupt:
        _complain('interrupted')
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # Something downstream (`| head`) stopped reading.  Say nothing - the
        # reader is gone - and make sure the interpreter's final flush does
        # not report the same broken pipe all over again.
        _hush_stdout()
        return EXIT_ERROR
    except CookieLoadError as err:
        _complain(f'could not load the browser cookies: {err}')
        _complain('if the browser store cannot be read, export the cookies to a '
                  'file and pass --cookies FILE instead')
        return EXIT_ERROR
    except OSError as err:
        # A backup that cannot be written, or a restore file that cannot be
        # read.  Requests to reddit come back as `usernotes.errors`, never as
        # these, so anything here is the filesystem talking.
        where = f'{err.filename}: ' if getattr(err, 'filename', None) else ''
        _complain(f'{where}{err.strerror or err}')
        return EXIT_ERROR
    except UsernotesError as err:
        _complain(str(err))
        if _no_cookies(args) and isinstance(err, (NotLoggedInError, ForbiddenError)):
            _complain('hint: usernotes are moderator-only, so this needs the '
                      'cookies of an account that moderates '
                      f'/r/{getattr(args, "subreddit", "this subreddit")}; pass '
                      '--cookies-from-browser BROWSER, or --cookies FILE with '
                      'cookies exported from a browser')
        return EXIT_ERROR


# -- the commands ------------------------------------------------------------

def read(args, logger, client_options):
    """``usernotes read``: print one user's notes."""
    with _store(args, logger, client_options) as store:
        store.load()
        _show(store, args, args.username)
    return EXIT_OK


def add(args, logger, client_options):
    """``usernotes add``: write a note, then print the user's notes again."""
    with _store(args, logger, client_options) as store:
        store.load()
        store.add(args.username, args.text, type=_flag(args, store, logger),
                  link=args.link)
        store.save()
        # The notes are read back from the wiki rather than printed from
        # memory: a listing that was re-read is also the proof that the note
        # landed, and shows what reddit actually holds - the flag the note was
        # stored with, the author it was recorded as.
        store.reload()
        _show(store, args, args.username)
    return EXIT_OK


def flags(args, logger, client_options):
    """``usernotes flags``: list the flags a subreddit's notes can carry."""
    with _store(args, logger, client_options) as store:
        note_types = store.note_types()
        if args.json:
            print(json.dumps(
                {'subreddit': store.subreddit,
                 'flags': [type_.as_dict() for type_ in note_types]},
                indent=2, ensure_ascii=False))
            return EXIT_OK
        print(render_note_types(note_types, subreddit=store.subreddit,
                                style=Style(color_enabled(args.color))), end='')
    return EXIT_OK


def backup(args, logger, client_options):
    """``usernotes backup``: save the whole wiki page to a file."""
    with _store(args, logger, client_options) as store:
        result = store.backup(args.output, directory=args.directory)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
            return EXIT_OK
        print(result.summary())
    return EXIT_OK


def search(args, logger, client_options):
    """``usernotes search``: find notes by user, text, flag or time."""
    try:
        matches = matcher(args.pattern, regex=args.regex,
                          ignore_case=not args.case_sensitive)
    except ValueError as err:
        # A pattern that will not compile is worth saying so about before any
        # notes are fetched to match it against.
        _complain(str(err))
        return EXIT_ERROR

    with _store(args, logger, client_options) as store:
        store.load()
        # The type table is a second request, and only worth making when there
        # are notes whose flags it could name, or match.
        note_types = store.note_types() if store.note_count() else ()
        found = search_notes(store.users.items(), matches, note_types=note_types)
        if args.json:
            print(json.dumps(
                {'subreddit': store.subreddit,
                 'page': store.page,
                 'pattern': args.pattern,
                 'regex': args.regex,
                 'count': len(found),
                 'matches': [{'user': username, **note.as_dict()}
                             for username, note in found]},
                indent=2, ensure_ascii=False))
            return EXIT_OK
        print(render_search_results(
            args.pattern, found,
            subreddit=store.subreddit,
            note_types=note_types if found else (),
            style=Style(color_enabled(args.color)),
            tz=None if args.local else datetime.timezone.utc,
            has_page=store.exists), end='')
    return EXIT_OK


def remove(args, logger, client_options):
    """``usernotes remove``: delete one of a user's notes."""
    with _store(args, logger, client_options) as store:
        store.load()
        removed = store.remove(args.username, _note_to_remove(args, store).time)
        store.save()
        # As with `add`, what is shown afterwards is read back from the wiki:
        # it is the remaining notes as reddit now holds them, which is also the
        # proof that the note went.
        store.reload()
        user = store.find(args.username)
        notes = () if user is None else tuple(user.notes)
        name = args.username if user is None else user.name

        if args.json:
            document = _as_json(store, name, user, notes)
            document['removed'] = removed.as_dict()
            print(json.dumps(document, indent=2, ensure_ascii=False))
            return EXIT_OK

        print(render_note(removed, note_types=store.note_types(),
                          style=Style(color_enabled(args.color)),
                          tz=None if args.local else datetime.timezone.utc))
        print()
        _show(store, args, args.username)
    return EXIT_OK


def restore(args, logger, client_options):
    """``usernotes restore``: write a backup file back to the wiki."""
    with _store(args, logger, client_options) as store:
        result = store.restore(args.path, force=args.force, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
            return EXIT_OK
        print(result.summary())
    return EXIT_OK


# -- arguments ---------------------------------------------------------------

def _note_text(value):
    """An argparse ``type`` for the note itself.

    Surrounding whitespace goes - toolbox's edit dialog trims it too - and a
    note that is left empty is a usage error here rather than a refusal from
    the store later: the format has no way to store an empty note, so there is
    nothing to send for one.
    """
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError('the note is empty')
    return text


def _link(value):
    """An argparse ``type`` for ``--link``, checked before anything is sent.

    Only link shapes the storage format keeps are accepted: an absolute URL, a
    reddit path, or the squashed ``l,<id>`` an older store holds.  Anything
    else - a bare word, a title - is squashed to nothing on the way to the
    wiki, so the note would be written without the link it was given and the
    moderator would have no way to tell; it is a usage error instead.
    """
    link = value.strip()
    if not link:
        return ''       # --link '' is "no link", which is the default anyway
    if (_SQUASHED_LINK_RE.match(link) or _ABSOLUTE_LINK_RE.match(link)
            or link.startswith('/')):
        return link
    raise argparse.ArgumentTypeError(
        f'not a reddit link: {value!r} (expected an absolute URL, a path such as '
        '/r/toolbox/comments/abc123/, or a squashed l,<id> from an older store)')


def _pattern(value):
    """An argparse ``type`` for a search pattern.

    Whitespace around it goes - a pattern is typed, not copied, and a stray
    leading space would make a search quietly miss everything - but a pattern
    that is only whitespace is refused rather than searched for, since it would
    match every note there is.
    """
    pattern = value.strip()
    if not pattern:
        raise argparse.ArgumentTypeError('the pattern is empty')
    return pattern


def _readable_file(value):
    """An argparse ``type`` for a file that has to be there to be read.

    A file that is not there is a mistake in the command line, and saying so
    before anything is sent to reddit beats a request that is only made to
    come back with "no such file", or a traceback: whatever the file is
    restored from is the one thing about a restore that cannot be undone.
    """
    if not os.path.isfile(value):
        raise argparse.ArgumentTypeError(f'no such file: {value}')
    return value


def _checked(normalizer):
    """An argparse ``type`` that keeps a ValueError's message.

    Given a ``type=`` callable, argparse reports a plain `ValueError` as
    "invalid normalize_subreddit value: ...", which says nothing about what
    was wrong or what would have worked - and these messages exist precisely
    to say that ("not a subreddit name: 'foo' (expected 3-21 characters ...)";
    "unsupported browser: ... Supported browsers are: ...").  An
    `argparse.ArgumentTypeError` is reported as it stands, so that is what
    these are re-raised as.  Either way the mistake is a usage error, reported
    before anything is sent to reddit.
    """
    def parse(value):
        try:
            return normalizer(value)
        except ValueError as err:
            raise argparse.ArgumentTypeError(str(err)) from err
    return parse


def build_parser():
    """The argument parser, for callers that want to see or extend the CLI."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Read, edit and back up the notes the reddit Moderator Toolbox keeps\n'
            'for a subreddit.  The notes live on the subreddit\'s wiki page, and\n'
            'only its moderators may read or write them, so a session is usually\n'
            'needed: give one with --cookies-from-browser or --cookies, before the\n'
            'command.'),
        epilog=(
            'usernotes is a companion to the toolbox extension, not a replacement\n'
            'for it: it reads and writes the same wiki page, and changes made there\n'
            'appear in both.'))
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument('-v', '--verbose', action='count', default=0,
                           help='log more: -v for what is happening, -vv for '
                                'every request')
    verbosity.add_argument('-q', '--quiet', action='store_true',
                           help='log errors only')
    _connection_options(parser)

    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)
    _read_command(commands)
    _add_command(commands)
    _flags_command(commands)
    _search_command(commands)
    _remove_command(commands)
    _backup_command(commands)
    _restore_command(commands)
    return parser


def _read_command(commands):
    parser = commands.add_parser(
        'read',
        help="print one user's usernotes",
        description=(
            'Print every note /r/SUBREDDIT holds for USERNAME, newest first, the\n'
            'way toolbox\'s "User Notes" dialog shows them: the moderator who wrote\n'
            'each note and when, the note type in the colour the subreddit gives it,\n'
            'the note itself, and the link it is about.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox read toolbox alice\n'
            '  usernotes --cookies cookies.txt read r/toolbox u/alice\n'
            '  usernotes read toolbox alice --json | jq .notes\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subject_arguments(parser)
    _output_arguments(parser, times=True)
    parser.set_defaults(run=read)
    return parser


def _add_command(commands):
    parser = commands.add_parser(
        'add',
        help="add a note to a user's usernotes",
        description=(
            'Add a note for USERNAME to /r/SUBREDDIT, then print their notes again,\n'
            'read back from the wiki: the new note is shown as it was stored, with\n'
            'the flag it was stored with and the moderator it was recorded against.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox add toolbox alice \\\n'
            '      "spam watch, third time this week" --flag spamwatch\n'
            '  usernotes add toolbox alice "banned for abuse" --flag ban \\\n'
            '      --link https://www.reddit.com/r/toolbox/comments/abc123/\n'
            '  usernotes flags toolbox            # what --flag can be\n'
            '\n'
            'Without --flag the note keeps the flag of the user\'s most recent\n'
            'flagged note, so an established flag does not have to be typed out\n'
            'again every time; --flag none writes a note with no flag at all.'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subject_arguments(parser)
    parser.add_argument('text', metavar='TEXT', type=_note_text,
                        help='the note itself, in quotes if it has spaces')
    parser.add_argument('--flag', metavar='FLAG', default=None,
                        help="the flag to set, by name or key as `usernotes flags "
                             "SUBREDDIT` lists it; defaults to the user's most "
                             "recent flag, and 'none' sets no flag")
    parser.add_argument('--link', metavar='URL', type=_link, default='',
                        help='what the note is about: an absolute URL, or a path '
                             'such as /r/toolbox/comments/abc123/')
    _output_arguments(parser, times=True)
    parser.set_defaults(run=add)
    return parser


def _flags_command(commands):
    parser = commands.add_parser(
        'flags',
        help='list the flags a subreddit can put on a note',
        description=(
            'Print the note flags /r/SUBREDDIT defines: its own, from the toolbox\n'
            'config page, or toolbox\'s defaults where it has not configured any.\n'
            'Each is shown with the colour it appears in, and with both the name\n'
            'and the key that `usernotes add --flag` accepts.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox flags toolbox\n'
            '  usernotes flags toolbox --json | jq -r \'.flags[].key\'\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subreddit_argument(parser)
    _output_arguments(parser, times=False, json_help=(
        'print the flags as JSON instead of the listing, for scripts: '
        'subreddit and flags[] (key, text, color)'))
    parser.set_defaults(run=flags)
    return parser


def _search_command(commands):
    parser = commands.add_parser(
        'search',
        help="search a subreddit's usernotes",
        description=(
            'Print every note on /r/SUBREDDIT that PATTERN matches, each with the\n'
            'user it belongs to: the username, the note text, its flag and its\n'
            'timestamp are all searched, and a note is shown if any of them\n'
            'matches.  Matching ignores case and finds the pattern anywhere in a\n'
            'field - which is what toolbox\'s own search boxes do - and --regex\n'
            'makes PATTERN a regular expression instead of a substring.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox search toolbox spam\n'
            '  usernotes search toolbox "^spam watch" --regex\n'
            '  usernotes search toolbox 2026-11-04        # notes from that day\n'
            '  usernotes search toolbox alice --json | jq -r \'.matches[].type\'\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subreddit_argument(parser)
    parser.add_argument('pattern', metavar='PATTERN', type=_pattern,
                        help='what to look for: a substring, or a regular '
                             'expression with --regex (surrounding whitespace is '
                             'ignored)')
    parser.add_argument('-E', '--regex', action='store_true',
                        help='read PATTERN as a regular expression, matched '
                             'anywhere in a field')
    parser.add_argument('--case-sensitive', action='store_true',
                        help='match case, where the default ignores it')
    _output_arguments(parser, times=True, json_help=(
        'print the matches as JSON instead of the listing, for scripts: '
        'subreddit, page, pattern, regex, count and matches[] (each with the '
        'user and the note fields)'))
    parser.set_defaults(run=search)
    return parser


def _remove_command(commands):
    parser = commands.add_parser(
        'remove',
        help="delete a note from a user's usernotes",
        description=(
            "Delete one of USERNAME's notes on /r/SUBREDDIT - the one named by\n"
            '--newest, by --oldest, or by --timestamp - and then print the notes\n'
            'they have left, read back from the wiki.  The page is written as a\n'
            'new revision: reddit keeps the history, so the note can be put back\n'
            'there, or with `usernotes restore` from a backup.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox remove toolbox alice --newest\n'
            '  usernotes --cookies cookies.txt remove toolbox alice --oldest\n'
            '  usernotes remove toolbox alice --timestamp "2026-11-05 06:11:20 UTC"\n'
            '\n'
            '--timestamp takes a time as `usernotes read` prints it, so one can be\n'
            'copied out of the middle of a listing - "3 days ago (2026-11-05\n'
            '06:11:20 UTC)" - and name the note it belongs to.'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subject_arguments(parser)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument('--newest', action='store_true',
                       help="the user's newest note")
    which.add_argument('--oldest', action='store_true',
                       help="the user's oldest note")
    which.add_argument('--timestamp', metavar='TIME', type=_checked(parse_time),
                       help='the note written at this time, as `usernotes read` '
                            'prints it, e.g. "2026-11-05 06:11:20 UTC"')
    _output_arguments(parser, times=True, json_help=(
        'print the removed note and the notes left as JSON, for scripts: the '
        'same document as `read --json`, with the note that was deleted under '
        '"removed"'))
    parser.set_defaults(run=remove)
    return parser


def _backup_command(commands):
    parser = commands.add_parser(
        'backup',
        help="save a subreddit's usernotes to a file",
        description=(
            'Write the whole /r/SUBREDDIT usernotes page to a file, exactly as\n'
            'reddit returns it: never re-encoded, so a page this tool cannot read\n'
            'is still backed up whole, and the file is what `usernotes restore`\n'
            'writes back.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox backup toolbox\n'
            '  usernotes backup toolbox --directory ~/usernotes-backups\n'
            '  usernotes backup toolbox --output before-the-cleanup.json\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subreddit_argument(parser)
    where = parser.add_mutually_exclusive_group()
    where.add_argument('-o', '--output', metavar='FILE', default=None,
                       help='the file to write; the default is '
                            'usernotes_<subreddit>_<UTC timestamp>.json in the '
                            'current directory')
    where.add_argument('-d', '--directory', metavar='DIR', default=None,
                       help='where to put that generated filename; created if it '
                            'does not exist')
    _output_arguments(parser, times=False, json_help=(
        'print what was written as JSON instead of the one-line summary: path, '
        'subreddit, taken_at, size, notes, users and warnings'))
    parser.set_defaults(run=backup)
    return parser


def _restore_command(commands):
    parser = commands.add_parser(
        'restore',
        help="write a backup file back to a subreddit's usernotes",
        description=(
            "Replace the /r/SUBREDDIT usernotes page with the contents of FILE, as\n"
            'a new wiki revision.  The page is read first, so what is about to be\n'
            'replaced is known and a file that holds fewer notes than the page does\n'
            'is reported before anything is written; a file that does not decode as\n'
            'a usernotes page is refused unless --force is given.  Reddit keeps the\n'
            'page history, so a restore can be undone like any other edit - run\n'
            '`usernotes backup` first if the notes as they stand are worth keeping.'),
        epilog=(
            'examples:\n'
            '  usernotes --cookies-from-browser firefox restore toolbox \\\n'
            '      usernotes_toolbox_20261107T061743Z.json\n'
            '  usernotes restore toolbox backup.json --dry-run   # look first\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _subreddit_argument(parser)
    parser.add_argument('path', metavar='FILE', type=_readable_file,
                        help='the file to restore, as `usernotes backup` wrote it')
    parser.add_argument('--force', action='store_true',
                        help='restore a file that does not decode as a usernotes '
                             'page, for putting back a backup of a page that was '
                             'already damaged')
    parser.add_argument('--dry-run', dest='dry_run', action='store_true',
                        help='say what would be written and write nothing')
    _output_arguments(parser, times=False, json_help=(
        'print what was restored as JSON instead of the one-line summary: path, '
        'subreddit, schema, size, notes, users, replaced_notes, replaced_users, '
        'written, dry_run and warnings'))
    parser.set_defaults(run=restore)
    return parser


def _subject_arguments(parser):
    """``SUBREDDIT`` and ``USERNAME``, which the per-user commands start with."""
    _subreddit_argument(parser)
    parser.add_argument('username', metavar='USERNAME',
                        type=_checked(normalize_username),
                        help='the user, e.g. alice, u/alice or /u/alice')


def _subreddit_argument(parser):
    parser.add_argument('subreddit', metavar='SUBREDDIT',
                        type=_checked(normalize_subreddit),
                        help='the subreddit, e.g. toolbox or /r/toolbox')


def _output_arguments(parser, *, times, json_help=(
        'print the notes as JSON instead of the listing, for scripts: '
        'subreddit, page, schema, exists, name, aliases, notes[] and warnings[]')):
    """How the work is shown: ``--json``, ``--color``, and ``--local``.

    :param times: whether this command shows note times, and so takes
        ``--local``.
    """
    parser.add_argument('--json', action='store_true', help=json_help)
    parser.add_argument('--color', choices=COLOR_MODES, default='auto', metavar='WHEN',
                        help='colour the output: auto (the default) colours it when '
                             'stdout is a terminal, always, or never')
    if times:
        parser.add_argument('--local', action='store_true',
                            help='show note times in the local time zone rather '
                                 'than UTC')


def _connection_options(parser):
    """Add the options that say how to reach reddit, to the top level parser.

    These are global, like ``-v`` and ``-q``: every command reads or writes
    the subreddit's wiki page, so every command needs a session, and there is
    no command that would not take them.  Passing them before the command
    says so, and saves repeating them in each command's help.

    A bad browser specification is rejected here, while the arguments are
    being read, so that it is a usage error rather than a failure halfway
    through a request.
    """
    parser.add_argument(
        '--cookies-from-browser', metavar=BROWSER_SPEC_METAVAR,
        type=_checked(parse_browser_spec), default=None,
        help='take the reddit session cookies from a browser, as yt-dlp does; '
             'the browser must be logged in to reddit')
    parser.add_argument(
        '--cookies', metavar='FILE', default=None,
        help='take the reddit session cookies from a Netscape cookies.txt file, '
             'for when the browser store cannot be read')
    parser.add_argument(
        '--base-url', metavar='URL', default=DEFAULT_BASE_URL,
        help=f'the reddit origin to talk to (default: {DEFAULT_BASE_URL}; '
             'https://old.reddit.com works too)')


# -- helpers -----------------------------------------------------------------

def _load_cookies(args, logger, base_url):
    """The session cookies, or ``None`` for an anonymous request.

    :param base_url: the origin the request will go to, so that a jar which
        holds nothing for it - the wrong browser profile is the usual cause -
        is reported before the request is made rather than as a puzzling
        refusal from reddit.
    :raises usernotes.cookies.CookieLoadError: if a source was given but could
        not be read, which main() reports and turns into `EXIT_ERROR`.
    """
    if args.cookies_from_browser is None and args.cookies is None:
        logger.info('no cookies given: reading reddit without a session')
        return None
    jar = load_cookie_jar(args.cookies_from_browser, args.cookies, logger=logger)
    if not cookie_names(jar, base_url):
        logger.warning(f'none of the cookies that were loaded apply to {base_url}; '
                       'the request will be anonymous')
    return jar


def _store(args, logger, client_options):
    """The store a command works on: client built, cookies loaded."""
    options = _client_options(args, client_options)
    return UsernotesStore(args.subreddit, logger=logger, **options,
                          cookies=_load_cookies(args, logger, options['base_url']))


def _show(store, args, username):
    """Print one user's notes: the toolbox-style listing, or JSON."""
    user = store.find(username)
    notes = () if user is None else tuple(user.notes)
    name = username if user is None else user.name
    if args.json:
        print(json.dumps(_as_json(store, name, user, notes), indent=2,
                         ensure_ascii=False))
        return
    print(render_user_notes(
        name, notes,
        subreddit=store.subreddit,
        # Only worth a request (the subreddit's toolbox config) if there is a
        # note to name and colour.
        note_types=store.note_types() if notes else (),
        style=Style(color_enabled(args.color)),
        tz=None if args.local else datetime.timezone.utc,
        has_page=store.exists), end='')


def _flag(args, store, logger):
    """The flag for a note being added: the one named, or the user's last one.

    Without ``--flag`` the flag is the one on the most recent note of theirs
    that carries one - not necessarily their newest note, because a note
    written without a flag should not hide an older one.  The point is that a
    moderator who has flagged this user before does not type the flag out
    again every time they add to the same story.

    A flag the subreddit no longer defines is not carried over: `add` would
    refuse it, so the note goes in without a flag and says so, rather than
    failing on something the moderator did not ask for.
    """
    if args.flag is not None:
        value = args.flag.strip()
        return None if value.lower() in _NO_FLAG else value

    recent = next((note.type for note in store.notes(args.username) if note.type),
                  None)
    if recent is None:
        return None
    if note_type(recent, store.note_types()) is None:
        logger.warning(f'the most recent flag on u/{args.username}, {recent!r}, is '
                       f'not one /r/{store.subreddit} defines any more; the note '
                       'is being added without a flag')
        return None
    logger.info(f'carrying over the flag {recent!r} from an earlier note on '
                f'u/{args.username}')
    return recent


def _note_to_remove(args, store):
    """The note `remove` was asked for, by whichever selector was given.

    A note's identity is its time: the stored format has nothing else to go on,
    and toolbox identifies notes the same way (``Math.trunc(note.time / 1000)``
    is its note id), so a timestamp names exactly one note.  ``--newest`` and
    ``--oldest`` are for a moderator who has not read a time off a listing yet;
    they follow the order the store holds, which is newest first, so a note
    whose time the store lost sorts last and is what ``--oldest`` finds.

    :raises usernotes.errors.UserNotFoundError: if the user has no notes.
    :raises usernotes.errors.NoteNotFoundError: if no note has that timestamp.
    """
    user = store.require(args.username)
    if args.newest:
        return user.notes[0]
    if args.oldest:
        return user.notes[-1]
    return user.require(_milliseconds(args.timestamp))


def _milliseconds(moment):
    """An aware `datetime` in the milliseconds `usernotes.format.Note` holds."""
    return int(moment.timestamp() * 1000)


def _client_options(args, client_options):
    """Options to build the reddit client with.

    The command line's ``--base-url``, with anything a caller embedding the
    tool passed to `main` layered over it.
    """
    options = dict(client_options or {})
    options.setdefault('base_url', args.base_url)
    return options


def _as_json(store, name, user, notes):
    """The document ``--json`` prints.

    The notes are `usernotes.format.Note.as_dict` as they stand, and the rest
    describes the store they came out of, so a script can tell "this user has
    no notes" from "this subreddit has never used usernotes" without parsing
    the listing.
    """
    return {
        'subreddit': store.subreddit,
        'page': store.page,
        'schema': store.ver,
        'exists': store.exists,
        'name': name,
        'aliases': [] if user is None else list(user.aliases),
        'notes': [note.as_dict() for note in notes],
        'warnings': list(store.warnings),
    }


def _log_level(args):
    return 'error' if args.quiet else _LOG_LEVELS[min(args.verbose, len(_LOG_LEVELS) - 1)]


def _no_cookies(args):
    return args.cookies_from_browser is None and args.cookies is None


def _complain(message):
    print(f'{PROG}: {message}', file=sys.stderr)


def _hush_stdout():
    """Point stdout at the void, so exiting cannot fail on the same broken pipe."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (AttributeError, OSError, ValueError):
        pass        # a stream with no file descriptor of its own, e.g. in a test
