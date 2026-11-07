# usernotes

Command line tools for the reddit Moderator Toolbox **usernotes** store.

Usernotes are the notes a subreddit's moderators keep about users.  Toolbox
stores them on the subreddit's wiki page
(`https://www.reddit.com/r/<subreddit>/wiki/usernotes`) as compressed, base64
encoded JSON, and this package reads, edits and backs that store up outside a
browser.  It works on the same wiki page toolbox does, so a note added on
either side shows up in both.

Reading and adding are implemented: `usernotes read`, `usernotes add` and
`usernotes flags`.  Backing up, restoring and searching are still to come; the
library underneath them (`usernotes.store`) already does the work.

## Requirements

* Python 3.11 or newer.
* No third-party runtime libraries - the standard library is all that is
  needed.  Reading Chromium cookies from the GNOME keyring on Linux is the one
  optional extra: `python3 -m pip install ".[gnome-keyring]"`.
* A reddit account that moderates the subreddit, unless its usernotes page has
  been left publicly readable.

## Build and install

The console script and the library come from one install.  From the directory
holding `pyproject.toml`:

```sh
python3 -m pip install .
usernotes --version
```

For working on it instead, install it in place:

```sh
python3 -m pip install -e .
```

Nothing has to be installed to try it: run from that same directory and the
source tree is used directly.

```sh
python3 -m usernotes --version
```

## Use

```
usernotes [-v|-q] COMMAND [options] ...
```

| Command | What it does |
|---|---|
| `usernotes read SUBREDDIT USERNAME` | Print a user's notes. |
| `usernotes add SUBREDDIT USERNAME TEXT` | Add a note, then print their notes again. |
| `usernotes flags SUBREDDIT` | List the flags a note can carry. |

Names are matched ignoring case and the `u/` prefix, and so are subreddit names
(`toolbox` and `/r/toolbox` are the same).  Reading a subreddit's notes normally
needs a moderator's session, which is what the cookie options are for - see
[Cookies](#cookies) below.

### Reading

`usernotes read` prints every note `/r/SUBREDDIT` holds for `USERNAME`, newest
first, laid out the way toolbox's "User Notes" dialog shows them: the moderator
who wrote each note and when, the note type in the colour the subreddit gives
it, the note itself, and the link it is about.  The author is printed as
`/u/name`, so a terminal that linkifies reddit usernames makes it clickable.

```sh
# cookies from the browser you are logged in to reddit with
usernotes read toolbox alice --cookies-from-browser firefox

# cookies exported to a Netscape cookies.txt file
usernotes read r/toolbox u/alice --cookies cookies.txt

# JSON, for a script
usernotes read toolbox alice --cookies-from-browser firefox --json \
  | jq '.notes[] | "\(.time_iso) \(.mod): \(.text)"'
```

On a terminal the output is coloured: a note takes the colour the subreddit
configures for its type, tag and text both, and the author and date are grey.
(The link under a note keeps a colour of its own instead - it is shown in blue,
so that it still reads as a URL.)

```
User Notes - /u/alice
/r/toolbox - 3 notes

Author                                        Note
/u/creesch                                    [Spam Watch] spam watch, second time this week
about a minute ago (2026-11-07 06:12:45 UTC)  https://www.reddit.com/r/toolbox/comments/abc123/-/def456/
/u/othermod                                   [Ban] repeated abuse
2 days ago (2026-11-05 06:11:20 UTC)
unknown                                       imported from the old store
about a year ago (2025-10-03 05:04:15 UTC)
```

A user with no notes is not an error: the listing says so and the exit status
stays 0.  Piping the output somewhere, or `--color never`, turns the colour
off; `NO_COLOR` and `TERM=dumb` are honoured too.

### Adding

`usernotes add` writes a note and then prints the user's notes again.  The
listing afterwards is read back from the wiki, not from memory, so what you see
is what was stored: the new note at the top, with the flag it was stored with and
the moderator it was recorded against.

```sh
usernotes add toolbox alice "spam watch, third time this week" --flag spamwatch

usernotes add toolbox alice "banned for abuse" --flag ban \
  --link https://www.reddit.com/r/toolbox/comments/abc123/
```

`--flag` takes a flag's name or its key, either one, and neither is case
sensitive; `usernotes flags SUBREDDIT` lists what a subreddit has.  Leave
`--flag` out and the note keeps the flag of the user's most recent flagged note
- a note written without a flag does not hide an older one - so an established
flag does not have to be typed out again every time.  `--flag none` writes a note
with no flag at all.

`--link` is what the note is about: an absolute URL, or a path such as
`/r/toolbox/comments/abc123/`.  It is stored the way toolbox stores a link, so
it comes back as a clickable URL in the listing.

The note is recorded against the account whose cookies were used, and the whole
wiki page is rewritten, because the format has no smaller unit.  If another
moderator saved notes in the meantime, the write is refused rather than
overwriting their work: run the command again.

### Flags

`usernotes flags` prints the note flags a subreddit defines - its own, from the
toolbox config page, or toolbox's defaults where it has configured none - each
in the colour it appears in and with the key `--flag` accepts:

```
Usernote flags - /r/toolbox

Flag              Key
Good Contributor  gooduser
Spam Watch        spamwatch
Spam Warning      spamwarn
Abuse Warning     abusewarn
Ban               ban
Permanent Ban     permban
Bot Ban           botban
```

An `add` with a flag the subreddit does not define fails with that list in the
message, so a typo is caught before anything is written.

### Options

| Option | Commands | What it does |
|---|---|---|
| `--cookies-from-browser SPEC` | all | Take the reddit session cookies from a browser: `BROWSER[+KEYRING][:PROFILE][::CONTAINER]`, yt-dlp's grammar.  Browsers: `brave`, `chrome`, `chromium`, `edge`, `firefox`, `opera`, `safari`, `vivaldi`, `whale`; keyrings (Linux): `basictext`, `gnomekeyring`, `kwallet`, `kwallet5`, `kwallet6`. |
| `--cookies FILE` | all | Take them from a Netscape `cookies.txt` instead, for when the browser's store cannot be read.  Both may be given; the file wins. |
| `--base-url URL` | all | The reddit origin to talk to; `https://old.reddit.com` works too. |
| `--flag FLAG` | `add` | The flag to set, by name or key as `usernotes flags SUBREDDIT` lists it.  Defaults to the flag on the user's most recent flagged note; `none` sets no flag. |
| `--link URL` | `add` | What the note is about: an absolute URL, or a path such as `/r/toolbox/comments/abc123/`. |
| `--json` | all | Print one JSON document instead of the listing.  For `read` and `add`: `subreddit`, `page`, `schema`, `exists`, `name`, `aliases`, `notes[]` (`text`, `time`, `time_iso`, `mod`, `type`, `link`) and `warnings[]`.  For `flags`: `subreddit` and `flags[]` (`key`, `text`, `color`). |
| `--color WHEN` | all | `auto` (default: colour when stdout is a terminal), `always`, `never`. |
| `--local` | `read`, `add` | Show note times in the local time zone instead of UTC. |
| `-v` / `-q` | all | More or less logging on stderr: `-v` for what is happening, `-vv` for every request, `-q` for errors only. |

### Cookies

Reading or writing a subreddit's usernotes normally needs a moderator's
session, which is why the tool takes cookies from a browser profile you are
already logged in to reddit with - the same thing `yt-dlp --cookies-from-browser`
does.  A note is recorded against that account.  Without either option the
request goes out anonymously, which only works for a subreddit whose usernotes
page is public, and cannot write.  If the browser's cookie store
cannot be read (a locked keyring, a different user account, Safari), export the
cookies with a browser extension and pass them with `--cookies FILE`.

Cookies are credentials: the tool never prints or logs them, only the *names*
of the cookies it found.

### Exit status

| Status | Meaning |
|---|---|
| `0` | A listing was printed - an empty one counts - or a note was written. |
| `1` | Reddit refused, or the store could not be read or written (bad cookies, not a moderator, the page is not usernotes, a flag that does not exist, the page changed while the note was being added, ...). |
| `2` | A usage error, reported by the argument parser before anything is sent. |

## Development

```sh
python3 -m unittest discover -v          # the whole suite
python3 -m unittest tests.test_cli -v    # one module
```

The tests never touch reddit: a throwaway HTTP server on `127.0.0.1` stands in
for the wiki API, so they are safe to run anywhere.

The code is a library with a thin front end over it:

| Module | What it is |
|---|---|
| `usernotes/cli.py` | The command line tool: arguments, cookies, exit statuses. |
| `usernotes/display.py` | Turning notes into terminal text, in toolbox's colours. |
| `usernotes/store.py` | `UsernotesStore`: load a subreddit's notes, edit them, save, back up. |
| `usernotes/format.py` | The on-wiki storage format (page JSON, its zlib/base64 blob, note types). |
| `usernotes/model.py` | The in-memory notes collection (usernames, ordering). |
| `usernotes/api.py` | Reddit's wiki API over `urllib`, with a caller-supplied cookie. |
| `usernotes/cookies.py` | Getting those cookies out of a browser (vendored from yt-dlp). |
| `usernotes/errors.py` | Every exception the package raises. |

## License

Code derived from a third party in `usernotes/_vendor/` comes from the
[`yt-dlp` project](https://github.com/yt-dlp/yt-dlp/), and is public domain.

Everything else is under the terms of the AGPL 3.0.  See the [`COPYING`
file](COPYING).
