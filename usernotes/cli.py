"""The ``usernotes`` command line tool.

This is the front end over the rest of the package: it turns arguments into a
`usernotes.store.UsernotesStore`, prints what was asked for, and reports
failures in a way a shell can act on.

Three commands are implemented.  ``read`` prints one user's notes the way
toolbox's "User Notes" dialog shows them - see `usernotes.display`; ``add``
writes a note and then prints those notes again, read back from the wiki;
``flags`` lists the note types the subreddit defines, because nothing else
would tell a moderator what ``add --flag`` accepts.  The library underneath
them can already edit a subreddit's notes and back them up, so ``backup``,
``restore`` and ``search`` are a matter of more commands here.

Three things are worth knowing before reading the code:

* **Adding is a read-modify-write of the whole page.**  The format has no
  smaller granularity, so `add` loads the page, adds to the copy and writes it
  back as a new revision - and the store refuses to write if the page changed
  while it was being edited, because that would discard another moderator's
  note.  Nothing is written until the arguments, the flag and the message have
  all been checked, and the listing printed afterwards is read back from the
  wiki rather than from memory, so it is also the proof that the write landed.

* **Cookies are a parameter everywhere in this package, and this is the layer
  that gets them.**  ``--cookies-from-browser`` (yt-dlp's grammar, see
  `usernotes.cookies`) and ``--cookies FILE`` are both optional; without
  either, the request goes out anonymously, which can only read a subreddit
  whose usernotes page is public - and toolbox's are moderator-only.
  Extraction happens before the first request, and a jar is never printed or
  logged, only the *names* of the cookies that were found.
* **Exit statuses are the interface for scripts.**  ``0`` for a listing that
  was printed or a note that was written, ``1`` for a request that failed, a
  store that could not be read, or a write that was refused (the flag does not
  exist, the page changed while the note was being added, the page is full),
  ``2`` for a usage error (argparse's own).  A user with no notes is not an
  error: the listing says so and the status stays ``0``, which is what toolbox
  does with an empty dialog.
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
    render_note_types,
    render_user_notes,
)
from .errors import ForbiddenError, NotLoggedInError, UsernotesError
from .format import note_type
from .model import normalize_username
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
            'Read and add to the notes the reddit Moderator Toolbox keeps for a\n'
            'subreddit.  The notes live on the subreddit\'s wiki page, and only its\n'
            'moderators may read or write them, so a session is usually needed: give\n'
            'one with --cookies-from-browser or --cookies.'),
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

    commands = parser.add_subparsers(dest='command', metavar='COMMAND', required=True)
    _read_command(commands)
    _add_command(commands)
    _flags_command(commands)
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
            '  usernotes read toolbox alice --cookies-from-browser firefox\n'
            '  usernotes read r/toolbox u/alice --cookies cookies.txt\n'
            '  usernotes read toolbox alice --json | jq .notes\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_connection_options()])
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
            '  usernotes add toolbox alice "spam watch, third time this week" \\\n'
            '      --flag spamwatch --cookies-from-browser firefox\n'
            '  usernotes add toolbox alice "banned for abuse" --flag ban \\\n'
            '      --link https://www.reddit.com/r/toolbox/comments/abc123/\n'
            '  usernotes flags toolbox            # what --flag can be\n'
            '\n'
            'Without --flag the note keeps the flag of the user\'s most recent\n'
            'flagged note, so an established flag does not have to be typed out\n'
            'again every time; --flag none writes a note with no flag at all.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_connection_options()])
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
            '  usernotes flags toolbox --cookies-from-browser firefox\n'
            '  usernotes flags toolbox --json | jq -r \'.flags[].key\'\n'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[_connection_options()])
    _subreddit_argument(parser)
    _output_arguments(parser, times=False, json_help=(
        'print the flags as JSON instead of the listing, for scripts: '
        'subreddit and flags[] (key, text, color)'))
    parser.set_defaults(run=flags)
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


def _connection_options():
    """The options every command needs to reach reddit, as a parent parser.

    Shared rather than repeated so that the commands to come read them the same
    way, and so that argparse rejects a bad browser specification or subreddit
    before anything is sent.
    """
    parser = argparse.ArgumentParser(add_help=False)
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
    return parser


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
