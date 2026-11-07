# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Render one user's notes as text, the way toolbox shows them in the browser.

Toolbox's "User Notes" dialog (``extension/data/modules/usernotes.js:329-506``)
is a two column table: the *author* column holds the moderator who wrote the
note and, under them, when they wrote it - the date being a link to whatever
the note is about - and the *note* column holds the type in the subreddit's
colour followed by the note text itself.  This module writes the same listing
for a terminal, which is what ``usernotes read`` prints.

What is kept, and what is not
-----------------------------

* The relative time is toolbox's ("3 years ago" is ``jquery.timeago``'s
  wording, reproduced here), and the absolute time is next to it in
  parentheses.  Toolbox keeps the absolute time in a tooltip; a terminal has
  no hover, so both are printed.
* Note types are coloured the way the subreddit's ``usernoteColors`` says,
  approximated in xterm-256 colours - see `color_index`.
* The author and the date are grey, as toolbox's usernotes manager shows them
  (``.mod, .date { color: #A8A8A8 }``).  The author is spelled ``/u/name``
  rather than ``name``, which is how reddit spells a username everywhere else
  and what a terminal that linkifies reddit usernames - rxvt-unicode's matcher
  and the many others that copied it - needs to see to make one clickable.
* The link under a note is printed rather than hidden behind the date: it is
  the one field of a note that cannot be read out of a coloured terminal cell.

Three things are deliberately *not* toolbox's behaviour:

* A coloured note takes its type's colour on the text as well as on the tag.
  Toolbox colours the tag only, which is easy to miss at the start of a long
  line; a whole line in the ban colour is not.  A note with no type, or with
  one the config does not define, has no colour to take and stays plain.
* A note whose type the subreddit's config no longer defines is shown by its
  stored key (``[spamwatch]``), uncoloured.  Toolbox renders nothing at all
  there, which for a command line tool would mean silently dropping stored
  data.
* Every string that comes out of the wiki page is passed through `safe_text`
  first.  Note text is other people's data, and a note (or a moderator name)
  holding terminal control characters - deliberately or through a damaged
  page - would otherwise be able to rewrite the screen, or move the cursor and
  forge the rest of the listing.  Toolbox sanitises note text with DOMPurify
  for the same reason; there is no renderer to protect here, so the display
  layer escapes instead of the reader (see `usernotes.format`, which keeps the
  stored text exactly as it is).

Three more things are rendered here.  A search result is the per-user listing
with the user added as its first column and each note on one line, since what
a search turns up is notes across users rather than one user's history.  A
single note on one line looks like toolbox's usernotes manager -
``[Type] the note - by /u/mod - 3 days ago`` - with the absolute time added,
which is what ``usernotes remove`` shows for the note it took away.  And
`parse_time` reads that absolute time back, so a time copied out of a listing
can name a note on the command line.

Colour is off unless asked for; `color_enabled` decides, and `Style` is what
does the wrapping, so nothing in this module has to check twice.
"""

from __future__ import annotations

import dataclasses
import datetime
import math
import os
import re
import sys

from .format import note_type as find_note_type

__all__ = [
    'COLOR_MODES',
    'Style',
    'color_enabled',
    'color_index',
    'format_time',
    'parse_time',
    'relative_time',
    'render_note',
    'render_note_types',
    'render_search_results',
    'render_user_notes',
    'safe_text',
]

#: Values ``--color`` accepts.
COLOR_MODES = ('auto', 'always', 'never')

#: xterm-256 index toolbox's grey (``#A8A8A8``) is exactly.
MUTED = 248

#: xterm-256 index a link is shown in.
LINK = 33

#: Characters never printed, whatever a page stores.  C0 and C1 controls would
#: drive the terminal rather than appear in it, an unpaired surrogate cannot be
#: encoded at all, and the bidi controls can reorder a line so that a note
#: reads as something it is not.  ``\\t``, ``\\n`` and ``\\r`` are absent
#: because :func:`safe_text` folds them into spaces, as a browser would.
_UNSAFE = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f'
    '\ud800-\udfff'
    '‎‏‪-‮⁦-⁩]')

#: xterm-256 index for the CSS colour names a ``usernoteColors`` is likely to
#: use.  A few are not the nearest colour to the CSS one on purpose: toolbox's
#: palette is picked for a light browser page, and its ``black`` (Bot Ban, in
#: its own defaults) would be invisible on a dark terminal, so it is shown as
#: dark grey instead.  Reaching the table is what matters - the tag has to be
#: tellable from the neighbouring one - so where a name has to be nudged to
#: stay legible, it is nudged.
_COLOR_NAMES = {
    'black': 240, 'silver': 250, 'gray': 244, 'grey': 244, 'white': 255,
    'darkgray': 240, 'darkgrey': 240, 'lightgray': 250, 'lightgrey': 250,
    'slategray': 103, 'slategrey': 103, 'dimgray': 240, 'dimgrey': 240,
    'maroon': 88, 'darkred': 124, 'red': 196, 'crimson': 161, 'tomato': 203,
    'orangered': 202, 'orange': 208, 'darkorange': 208, 'gold': 220,
    'yellow': 226, 'khaki': 222, 'olive': 100, 'beige': 230, 'tan': 180,
    'brown': 130, 'chocolate': 166, 'salmon': 209,
    'green': 34, 'darkgreen': 22, 'forestgreen': 28, 'seagreen': 29,
    'lime': 46, 'lightgreen': 120, 'springgreen': 48, 'teal': 30,
    'turquoise': 45, 'aqua': 51, 'cyan': 51, 'skyblue': 117,
    'blue': 33, 'navy': 18, 'darkblue': 18, 'lightblue': 117, 'steelblue': 67,
    'slateblue': 104, 'indigo': 54,
    'purple': 93, 'darkviolet': 129, 'violet': 177, 'orchid': 170,
    'fuchsia': 201, 'magenta': 201, 'plum': 183, 'pink': 211, 'hotpink': 205,
}

#: The 6x6x6 colour cube's component values, and the index its first cell has.
_CUBE_STEPS = (0, 95, 135, 175, 215, 255)
_CUBE_FIRST = 16

#: The 24 step grey ramp: values ``8 + 10 * i``, starting at index 232.  The
#: cube cannot express the greys between its steps, which is why this exists.
_GREY_FIRST = 232
_GREY_STEP = 10

_HEX_RE = re.compile(r'#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})\Z')
_RGB_RE = re.compile(
    r'rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+\s*)?\)\Z')

#: CSS colour values that mean "no colour of its own".
_NO_COLOR = frozenset({'transparent', 'currentcolor', 'inherit', 'initial',
                       'unset', 'none', 'revert'})


def color_enabled(mode='auto', stream=None):
    """Whether to emit ANSI colour, for a ``--color`` value.

    ``'auto'`` (the default) colours the output when it is going to a
    terminal, and honours the two conventional ways of saying otherwise:
    ``NO_COLOR`` in the environment (https://no-color.org/) and a ``TERM`` of
    ``dumb``.

    :param mode: one of `COLOR_MODES`.
    :param stream: the stream that will be written to; defaults to
        ``sys.stdout``.
    :raises ValueError: if *mode* is not one of `COLOR_MODES`.
    """
    if mode not in COLOR_MODES:
        raise ValueError(f'unknown colour mode: {mode!r} '
                         f'(expected one of {", ".join(COLOR_MODES)})')
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('TERM', '').strip().lower() == 'dumb':
        return False
    stream = sys.stdout if stream is None else stream
    isatty = getattr(stream, 'isatty', None)
    return bool(isatty is not None and isatty())


class Style:
    """Wraps text in ANSI escapes, or hands it back untouched.

    :param enabled: whether to emit escapes at all.  Everything here is a
        no-op when it is false, so callers can style unconditionally and let
        ``--color`` and the stream decide.
    """

    def __init__(self, enabled=False):
        self.enabled = bool(enabled)

    def __repr__(self):
        return f'<Style {"on" if self.enabled else "off"}>'

    def __bool__(self):
        return self.enabled

    def _wrap(self, code, text):
        if not self.enabled or not text:
            return text
        return f'\x1b[{code}m{text}\x1b[0m'

    def bold(self, text):
        """*text* in bold."""
        return self._wrap('1', text)

    def color(self, index, text):
        """*text* in xterm-256 colour *index* (``None`` leaves it alone)."""
        if index is None:
            return text
        return self._wrap(f'38;5;{index}', text)


def color_index(css_color):
    """The xterm-256 index closest to a CSS colour, or ``None``.

    Toolbox writes a note type's colour straight into a ``style`` attribute, so
    a subreddit can configure any CSS colour: a name, ``#rgb``, ``#rrggbb`` or
    ``rgb()``/``rgba()``.  Names are looked up in a table (see `_COLOR_NAMES`),
    and everything else is matched to the nearest of the terminal's own
    colours, with the 24 step grey ramp considered as well as the colour cube -
    without it, a grey comes out as the nearest cube step, which is not grey.
    A colour that cannot be understood is no colour, never an error: the
    listing is still perfectly readable uncoloured.

    :param css_color: the configured colour, or ``None``/``''``.
    :returns: an index for ``Style.color``, or ``None``.
    """
    if not isinstance(css_color, str):
        return None
    value = css_color.strip().lower()
    if not value or value in _NO_COLOR:
        return None
    if value in _COLOR_NAMES:
        return _COLOR_NAMES[value]

    match = _HEX_RE.match(value)
    if match is not None:
        digits = value[1:]
        if len(digits) == 3:
            digits = ''.join(2 * digit for digit in digits)
        return _nearest_index(int(digits[0:2], 16), int(digits[2:4], 16),
                              int(digits[4:6], 16))

    match = _RGB_RE.match(value)
    if match is not None:
        return _nearest_index(*(min(255, int(part)) for part in match.groups()))
    return None


def relative_time(when, now=None):
    """How long ago *when* was, in toolbox's words.

    Toolbox shows note times through ``jquery.timeago``
    (``extension/data/tbui.js:2076``), so the wording here is its: "less than a
    minute ago", "3 hours ago", "about a month ago", "2 years ago".  One
    divergence: a time in the future, which ``timeago`` would call "ago" like
    any other, reads "in 2 days" here.

    :param when: an aware `datetime.datetime`.
    :param now: what "now" is, for tests.  Defaults to the current time.
    """
    now = datetime.datetime.now(when.tzinfo) if now is None else now
    seconds = (now - when).total_seconds()
    phrase = _timeago_phrase(abs(seconds))
    return f'{phrase} ago' if seconds >= 0 else f'in {phrase}'


def format_time(when, tz=datetime.timezone.utc, *, zone=False):
    """An absolute timestamp, for next to the relative one.

    :param when: an aware `datetime.datetime`.
    :param tz: the zone to show it in; ``None`` for the machine's own, which
        is what ``--local`` asks for.
    :param zone: whether to append the zone's abbreviation, e.g. ``UTC``.  The
        listing asks for it, because a run of notes can straddle a daylight
        saving change and the abbreviation is what tells those two apart.
    """
    moment = when.astimezone(tz)
    text = moment.strftime('%Y-%m-%d %H:%M:%S')
    if zone and moment.tzname():
        text = f'{text} {moment.tzname()}'
    return text


def parse_time(value):
    """Read a timestamp back; the way in for what `format_time` writes out.

    A listing prints each note's time as ``2026-11-04 06:12:45 UTC``, and this
    turns that - copied straight out of the output - back into a time, which is
    how a note is named on the command line: ``usernotes remove --timestamp``.
    A time with no zone is UTC, because that is what a listing shows unless it
    was asked for local times; ``2026-11-04T06:12:45+00:00`` and the other ISO
    8601 spellings `datetime.datetime.fromisoformat` takes are read too.

    :param value: the timestamp, as text.
    :returns: an aware `datetime.datetime`.
    :raises ValueError: if *value* is not a timestamp, or names a zone that
        cannot be resolved: a local zone name would mean two different
        instants depending on the machine reading it, so those are refused
        rather than guessed at.
    """
    if not isinstance(value, str):
        raise ValueError(f'timestamp is {type(value).__name__}, not text: {value!r}')
    text = value.strip()
    if not text:
        raise ValueError('timestamp is empty')

    words = text.split()
    if len(words) > 1 and words[-1].casefold() in ('utc', 'gmt'):
        text = ' '.join(words[:-1])         # the zone our own output names
    elif len(words) > 1 and words[-1].isalpha():
        raise ValueError(
            f'cannot read the zone {words[-1]!r} in {value!r}; give the time in '
            'UTC, as `usernotes read` prints it (it shows local times only when '
            'asked for them with --local)')

    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as err:
        raise ValueError(
            f'not a timestamp: {value!r} (expected something like '
            f'"2026-11-04 06:12:45 UTC", as `usernotes read` prints it)') from err
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def safe_text(value):
    """*value* as text that is safe to print, never ``None``.

    Terminal control characters are dropped, whitespace is collapsed to single
    spaces (a browser renders a note's newlines and tabs that way, so this is
    the listing toolbox would have shown), and characters that cannot be
    printed at all - unpaired surrogates from a damaged page, the bidi
    overrides that could make a note read as its opposite - go with them.

    This is for *display only*.  The stored text is what other moderators and
    toolbox itself will read, so it is never rewritten: see the note in
    `usernotes.format` about not decoding HTML on read either.
    """
    if not isinstance(value, str):
        return ''
    return ' '.join(_UNSAFE.sub('', value).split())


@dataclasses.dataclass(frozen=True)
class Cell:
    """One cell of the listing.

    :param plain: the text as it would read uncoloured, which is what the
        column is measured against: ANSI escapes take no space on screen but
        are characters in the string.
    :param styled: the same text with whatever escapes it should carry.
    """

    plain: str = ''
    styled: str = ''


def render_user_notes(username, notes, *, subreddit, note_types=(), style=None,
                      now=None, tz=datetime.timezone.utc, has_page=True):
    """Render one user's notes the way toolbox's "User Notes" dialog shows them.

    :param username: the user the notes belong to, spelled as it should be
        shown.  ``/u/`` is added here.
    :param notes: the notes, newest first, as
        `usernotes.store.UsernotesStore.notes` returns them.
    :param subreddit: the subreddit they were read from.
    :param note_types: the subreddit's `usernotes.format.NoteType` table, for
        naming and colouring each note's type.
    :param style: a `Style`; colour is off when this is not given.
    :param now: what "now" is for the relative times, for tests.
    :param tz: the zone absolute times are shown in; ``None`` for the local.
    :param has_page: whether the subreddit has a usernotes page at all, so
        that an empty listing says which empty case it is.
    :returns: the text to print, newline terminated.
    """
    style = Style() if style is None else style
    name = safe_text(username)
    subreddit = safe_text(subreddit)
    notes = list(notes)

    lines = [style.bold(f'User Notes - /u/{name}')]
    if not has_page:
        lines.append(style.color(MUTED, f'/r/{subreddit} - no usernotes page yet'))
        return '\n'.join(lines) + '\n'
    if not notes:
        lines.append(style.color(MUTED, f'/r/{subreddit} - no notes for /u/{name}'))
        return '\n'.join(lines) + '\n'
    lines.append(style.color(MUTED, f'/r/{subreddit} - {_count(len(notes), "note")}'))
    lines.append('')

    now = datetime.datetime.now(datetime.timezone.utc) if now is None else now
    rows = []
    for note in notes:
        rows.extend(_note_rows(note, note_types, style, now, tz))

    lines.extend(_table(('Author', 'Note'), rows))
    return '\n'.join(lines) + '\n'


def render_note_types(note_types, *, subreddit, style=None):
    """Render a subreddit's note types - its flags - as a list to pick from.

    This is what ``usernotes flags`` prints, so that a moderator can see what
    ``usernotes add --flag`` accepts without going to the subreddit's toolbox
    config page: the flag as it is written, the colour it will be shown in, and
    the key or name to pass (either works, and neither is case sensitive).

    :param note_types: the subreddit's `usernotes.format.NoteType` table, as
        `usernotes.store.UsernotesStore.note_types` returns it.
    :param subreddit: the subreddit they belong to.
    :param style: a `Style`; colour is off when this is not given.
    :returns: the text to print, newline terminated.
    """
    style = Style() if style is None else style
    subreddit = safe_text(subreddit)
    types = list(note_types)
    lines = [style.bold(f'Usernote flags - /r/{subreddit}')]
    if not types:
        lines.append(style.color(MUTED, f'/r/{subreddit} - no flags'))
        return '\n'.join(lines) + '\n'

    lines.append('')
    rows = []
    for note_type in types:
        name = safe_text(note_type.text)
        key = safe_text(note_type.key)
        rows.append((Cell(name, style.color(color_index(note_type.color), name)),
                     Cell(key, style.color(MUTED, key))))
    lines.extend(_table(('Flag', 'Key'), rows))
    return '\n'.join(lines) + '\n'


def render_note(note, *, note_types=(), style=None, now=None,
                tz=datetime.timezone.utc):
    """Render one note as a single line, with no trailing newline.

    ``[Ban] repeated abuse - by /u/creesch - 3 days ago (2026-11-05 06:11:20
    UTC)``: the shape toolbox's usernotes manager gives a note, with the
    absolute time after the relative one, so that it can be read off and used
    to name the note again - see `parse_time`.  This is what ``usernotes
    remove`` prints for the note it took away.

    :param note: the `usernotes.format.Note`.
    :param note_types: the subreddit's `usernotes.format.NoteType` table, for
        naming and colouring the note's flag.
    :param style: a `Style`; colour is off when this is not given.
    :param now: what "now" is for the relative time, for tests.
    :param tz: the zone the absolute time is shown in; ``None`` for the local.
    :returns: one line of text.
    """
    style = Style() if style is None else style
    now = datetime.datetime.now(datetime.timezone.utc) if now is None else now
    return _note_line(note, note_types, style, now, tz).styled


def render_search_results(pattern, matches, *, subreddit, note_types=(), style=None,
                          now=None, tz=datetime.timezone.utc, has_page=True):
    """Render the notes a search found, with the user each belongs to.

    A listing like `render_user_notes`, with the user as an extra first column
    and each note on one line (see `render_note`): what a search turns up is
    scattered across users, so whose note it is has to be on every line, and
    keeping the note text next to the user leaves the author and time trailing
    it where a column of dates would push the text off a narrow terminal.

    :param pattern: what was searched for; the title says so, since a listing
        of matches is only readable next to what it matched.
    :param matches: ``(username, note)`` pairs, as `usernotes.search.search`
        returns them, in the order they are to be shown.
    :param subreddit: the subreddit they were read from.
    :param note_types: the subreddit's `usernotes.format.NoteType` table.
    :param style: a `Style`; colour is off when this is not given.
    :param now: what "now" is for the relative times, for tests.
    :param tz: the zone absolute times are shown in; ``None`` for the local.
    :param has_page: whether the subreddit has a usernotes page at all, so
        that "nothing matched" and "there are no notes" can be told apart.
    :returns: the text to print, newline terminated.
    """
    style = Style() if style is None else style
    subreddit = safe_text(subreddit)
    matches = list(matches)

    lines = [style.bold(f'Usernotes matching {safe_text(pattern)!r} - /r/{subreddit}')]
    if not has_page:
        lines.append(style.color(MUTED, f'/r/{subreddit} - no usernotes page yet'))
        return '\n'.join(lines) + '\n'
    if not matches:
        lines.append(style.color(MUTED, 'no notes match'))
        return '\n'.join(lines) + '\n'

    users = len({username for username, _ in matches})
    lines.append(style.color(
        MUTED, f'{_count(len(matches), "note")} for {_count(users, "user")}'))
    lines.append('')

    now = datetime.datetime.now(datetime.timezone.utc) if now is None else now
    rows = []
    for username, note in matches:
        name = safe_text(username)
        rows.append((Cell(name, style.bold(name)),
                     _note_line(note, note_types, style, now, tz)))
        link = safe_text(note.link)
        if link:
            rows.append((Cell(), Cell(link, style.color(LINK, link))))
    lines.extend(_table(('User', 'Note'), rows))
    return '\n'.join(lines) + '\n'


# -- internals ---------------------------------------------------------------

def _table(headers, rows):
    """Lay `Cell`s out in columns, with the last column left unpadded.

    Every listing here is a grid of cells, and ANSI escapes take no space on
    screen while being characters in the string: the widths come from each
    cell's ``plain`` side, and the padding is added to the styled one, so a
    coloured listing lines up exactly as the plain one does.

    :param headers: the column names, plain text.
    :param rows: one tuple of `Cell` per row, one per column.
    :returns: a list of lines.
    """
    widths = [max([len(name)] + [len(row[index].plain) for row in rows])
              for index, name in enumerate(headers)]
    lines = ['  '.join(f'{name:<{width}}'
                       for name, width in zip(headers, widths)).rstrip()]
    for row in rows:
        lines.append('  '.join(
            f'{cell.styled}{" " * (width - len(cell.plain))}'
            for cell, width in zip(row, widths)).rstrip())
    return lines


def _count(number, thing):
    """``1 note`` / ``3 notes``, for the line above a listing."""
    return f'{number} {thing}{"" if number == 1 else "s"}'


def _date_text(note, now, tz):
    """When a note was written: the relative time, then the absolute one.

    The absolute time is there because a terminal has no tooltip to keep it in
    (toolbox shows its listings with ``jquery.timeago`` and the exact time in
    the ``title``), and because it is the string that names the note on the
    command line - see `parse_time`.
    """
    if not note.has_time:
        return 'unknown date'
    when = note.datetime()          # aware, always UTC: `tz` is for display
    return f'{relative_time(when, now)} ({format_time(when, tz, zone=True)})'


def _note_line(note, note_types, style, now, tz):
    """One note as a single line of `Cell`: flag, text, author, time.

    Toolbox's usernotes manager writes a note this way (``usernotes.js:1051``),
    with the type in its colour, the note itself, and then who wrote it and
    when after a dash each.  The flag and the text take the type's colour, as
    they do in the per-user listing.
    """
    tag, color = _type_tag(note.type, note_types)
    text = safe_text(note.display_text())
    if tag and text:
        body_plain = f'{tag} {text}'
        body_styled = f'{style.color(color, tag)} {style.color(color, text)}'
    else:
        body_plain = tag or text
        body_styled = style.color(color, body_plain)

    author = safe_text(note.mod)
    author = f'/u/{author}' if author else 'unknown'
    date = _date_text(note, now, tz)
    return Cell(
        plain=f'{body_plain} - by {author} - {date}',
        styled=f'{body_styled} - {style.color(MUTED, f"by {author}")} - '
               f'{style.color(MUTED, date)}')


def _note_rows(note, note_types, style, now, tz):
    """The row (or two) toolbox uses for one note.

    The author column carries the moderator who wrote the note and, under
    them, when they wrote it; the note column carries the type in its colour
    followed by the note text, and under that the link the note is about.

    The author goes in as ``/u/name`` rather than ``name``: that is how a
    username is spelled anywhere else on reddit, and it is what a terminal
    that links reddit users (rxvt-unicode's matcher, and the many terminals
    with the same built in) needs to recognise to make the name clickable.
    """
    author = safe_text(note.mod)
    mod = f'/u/{author}' if author else 'unknown'
    date = _date_text(note, now, tz)

    tag, color = _type_tag(note.type, note_types)
    text = safe_text(note.display_text())
    if tag and text:
        # The note text takes the type's colour as well: a wall of notes is
        # easier to read when the ban is red and the spam watch is not, and
        # the tag alone is easy to miss at the start of a long line.  A note
        # with no type, or one whose type the config does not define, has no
        # colour to take (`_type_tag` answers ``None``), so it stays plain.
        note_plain = f'{tag} {text}'
        note_styled = f'{style.color(color, tag)} {style.color(color, text)}'
    else:
        note_plain = note_styled = style.color(color, tag or text)

    link = safe_text(note.link)
    return [
        (Cell(mod, style.color(MUTED, mod)),
         Cell(note_plain, note_styled)),
        (Cell(date, style.color(MUTED, date)),
         Cell(link, style.color(LINK, link))),
    ]


def _type_tag(key, note_types):
    """``(text, colour index)`` for a note's type, as toolbox shows it."""
    if not key:
        return '', None
    known = find_note_type(key, note_types)
    if known is None:
        return f'[{safe_text(key)}]', None
    return f'[{safe_text(known.text)}]', color_index(known.color)


def _timeago_phrase(seconds):
    """jquery.timeago's wording for a distance in seconds, without its "ago"."""
    minutes = seconds / 60
    hours = minutes / 60
    days = hours / 24
    years = days / 365
    if seconds < 45:
        return 'less than a minute'
    if seconds < 90:
        return 'about a minute'
    if minutes < 45:
        return f'{_js_round(minutes):d} minutes'
    if minutes < 90:
        return 'about an hour'
    if hours < 24:
        return f'{_js_round(hours):d} hours'
    if hours < 42:
        return 'a day'
    if days < 30:
        return f'{_js_round(days):d} days'
    if days < 45:
        return 'about a month'
    if days < 365:
        return f'{_js_round(days / 30):d} months'
    if years < 1.5:
        return 'about a year'
    return f'{math.floor(years):d} years'


def _js_round(value):
    """``Math.round``: halves round up, where Python's `round` goes to even."""
    return math.floor(value + 0.5)


def _nearest_index(red, green, blue):
    """The xterm-256 index closest to an RGB colour."""
    target = (red, green, blue)
    cube = tuple(min(range(len(_CUBE_STEPS)),
                     key=lambda i: abs(_CUBE_STEPS[i] - value))
                 for value in target)
    cube_rgb = tuple(_CUBE_STEPS[i] for i in cube)
    cube_index = _CUBE_FIRST + 36 * cube[0] + 6 * cube[1] + cube[2]

    average = sum(target) / 3
    grey = min(range(24), key=lambda i: abs((8 + _GREY_STEP * i) - average))
    grey_rgb = (8 + _GREY_STEP * grey,) * 3

    if _distance(grey_rgb, target) < _distance(cube_rgb, target):
        return _GREY_FIRST + grey
    return cube_index


def _distance(left, right):
    """Squared euclidean distance; the square root would not change the order."""
    return sum((a - b) ** 2 for a, b in zip(left, right))
