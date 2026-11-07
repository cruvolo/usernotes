# usernotes

Command line tools for the reddit Moderator Toolbox **usernotes** store.

Usernotes are the notes a subreddit's moderators keep about users.  Toolbox
stores them on the subreddit's wiki page
(`https://www.reddit.com/r/<subreddit>/wiki/usernotes`) as compressed, base64
encoded JSON, and this package reads, edits and backs that store up outside a
browser.  It works on the same wiki page toolbox does, so a note added on
either side shows up in both.

The whole set is implemented: `usernotes read`, `add`, `flags`, `search`,
`remove`, `backup` and `restore`.

## Requirements

* Python 3.11 or newer.
* No third-party runtime libraries on Windows or macOS - the standard library
  is all that is needed.  On Linux, `secretstorage` is installed with the
  package: it is what reads a Chromium cookie store's key out of the desktop
  keyring over D-Bus, and without it those cookies cannot be decrypted.  It
  comes with `cryptography` and `jeepney` of its own.
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
usernotes [-v|-q] [connection options] COMMAND [options] ...
```

| Command | What it does |
|---|---|
| `usernotes read SUBREDDIT USERNAME` | Print a user's notes. |
| `usernotes add SUBREDDIT USERNAME TEXT` | Add a note, then print their notes again. |
| `usernotes flags SUBREDDIT` | List the flags a note can carry. |
| `usernotes search SUBREDDIT PATTERN` | Find notes by user, text, flag or time. |
| `usernotes remove SUBREDDIT USERNAME` | Delete one of a user's notes. |
| `usernotes backup SUBREDDIT` | Save the whole usernotes page to a file. |
| `usernotes restore SUBREDDIT FILE` | Put such a file back as a new wiki revision. |

Names are matched ignoring case and the `u/` prefix, and so are subreddit names
(`toolbox` and `/r/toolbox` are the same).  Every command reads or writes the
same wiki page, and a session is nearly always needed for that, so the options
that say where to get one - and which reddit to talk to - are **global**: like
`-v` and `-q`, they go before the command rather than after it.  See
[Cookies](#cookies) below.

### Reading

`usernotes read` prints every note `/r/SUBREDDIT` holds for `USERNAME`, newest
first, laid out the way toolbox's "User Notes" dialog shows them: the moderator
who wrote each note and when, the note type in the colour the subreddit gives
it, the note itself, and the link it is about.  The author is printed as
`/u/name`, so a terminal that linkifies reddit usernames makes it clickable.

```sh
# cookies from the browser you are logged in to reddit with.  They are
# global options, like -v: they go before the command, not after it.
usernotes --cookies-from-browser firefox read toolbox alice

# cookies exported to a Netscape cookies.txt file
usernotes --cookies cookies.txt read r/toolbox u/alice

# JSON, for a script
usernotes --cookies-from-browser firefox read toolbox alice --json \
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
it comes back as a clickable URL in the listing.  `--url` is the same option,
for when the thing it names is a URL; give either one.

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

### Searching

`usernotes search` prints every note whose **user**, **text**, **flag** or
**timestamp** matches, with the user it belongs to.  Matching is by substring,
anywhere in a field, and ignores case - which is what toolbox's own search
boxes do - and `--regex` reads the pattern as a regular expression instead:

```sh
usernotes search toolbox spam

# a regular expression, and a case-sensitive one
usernotes search toolbox '^spam' --regex
usernotes search toolbox Spam --case-sensitive

# notes from a day, or a flag's own name
usernotes search toolbox 2026-11-04
usernotes search toolbox 'Spam Watch'
```

```
Usernotes matching 'spam' - /r/toolbox
2 notes for 2 users

User           Note
alice          [Spam Watch] spam watch, second time - by /u/creesch - 4 days ago (2026-11-03 06:12:45 UTC)
               https://www.reddit.com/r/toolbox/comments/abc123/
bobthespammer  [Ban] banned for spam - by /u/othermod - 3 days ago (2026-11-04 06:11:20 UTC)
```

Nothing matching is not an error: it says so and the exit status stays 0.

### Removing

`usernotes remove` deletes one of a user's notes.  The note is named one of
three ways, and exactly one has to be given:

```sh
usernotes remove toolbox alice --newest
usernotes remove toolbox alice --oldest

# the time, copied straight out of a listing: the part in brackets
usernotes remove toolbox alice --timestamp "2026-11-05 06:11:20 UTC"
```

`--timestamp` takes a time as `usernotes read` (or `search`) prints it, so the
whole workflow is: read the notes, copy `2026-11-05 06:11:20 UTC` out of the
middle of `3 days ago (2026-11-05 06:11:20 UTC)`, and name that note with it.
A time with no zone is UTC, and the ISO 8601 spellings work too.  A note's
timestamp is its identity - it is what toolbox itself identifies a note by - so
the time names one note exactly; if a paste matches nothing, the error lists the
times the user does have, in that same form.

The note is written out of the page as a new revision, and the user's remaining
notes are printed afterwards, read back from the wiki:

```
[Spam Watch] spam watch, second time - by /u/creesch - 4 days ago (2026-11-03 06:12:45 UTC)

User Notes - /u/alice
/r/toolbox - 1 note
...
```

Reddit keeps the page's revision history, so a removal can be undone there, or
with `usernotes restore` from a backup.  A user whose last note goes is dropped
from the store, as toolbox drops them.

### Backing up

`usernotes backup` writes the whole page to a file, exactly as reddit returns
it.  Nothing is re-encoded or re-serialised, so a page this tool cannot read is
still backed up whole, and a page kept at an older schema version is backed up
as it stands.  The file is what `restore` writes back.

```sh
# usernotes_<subreddit>_<UTC timestamp>.json in the current directory
usernotes --cookies-from-browser firefox backup toolbox

# ... or wherever you want it
usernotes backup toolbox --directory ~/usernotes-backups
usernotes backup toolbox --output before-the-cleanup.json
```

One line says what was written:

```
usernotes_toolbox_20261107T061743Z.json: 345 notes for 12 users (41233 bytes)
```

### Restoring

`usernotes restore` writes a backup file over the page as a new wiki revision.
It is the one command here that can take notes away, because it replaces the
page instead of editing it; it is meant for the day someone wipes the notes, or
for putting a page straight after a bad bulk edit.

```sh
# say what it would do, and write nothing
usernotes restore toolbox usernotes_toolbox_20261107T061743Z.json --dry-run

# do it
usernotes --cookies-from-browser firefox restore toolbox \
  usernotes_toolbox_20261107T061743Z.json
```

What keeps it honest:

* the file has to decode as a usernotes page; one that does not is refused
  rather than written over the notes.  `--force` puts it back anyway, for
  restoring a backup of a page that was already damaged;
* the page is read first, so the file is compared with what it would replace: a
  file holding *fewer* notes than the page does is reported before anything is
  written;
* a page that already holds exactly that file is left alone, so re-running a
  restore does not leave an empty revision in the page history;
* reddit keeps the page's revision history, so a restore is undone the way any
  other wiki edit is - run `usernotes backup` first if the notes as they stand
  are worth keeping.

A file backed up from one subreddit can be restored into another: it holds a
page, not a name.

### Options

`global` options go before the command - `usernotes --cookies FILE read toolbox
alice` - and the rest go after it, where the command's own `--help` lists them.

| Option | Commands | What it does |
|---|---|---|
| `--cookies-from-browser SPEC` | global | Take the reddit session cookies from a browser: `BROWSER[+KEYRING][:PROFILE][::CONTAINER]`, yt-dlp's grammar.  Browsers: `brave`, `chrome`, `chromium`, `edge`, `firefox`, `opera`, `safari`, `vivaldi`, `whale`; keyrings (Linux): `basictext`, `gnomekeyring`, `kwallet`, `kwallet5`, `kwallet6`. |
| `--cookies FILE` | global | Take them from a Netscape `cookies.txt` instead, for when the browser's store cannot be read.  Both may be given; the file wins. |
| `--base-url URL` | global | The reddit origin to talk to; `https://old.reddit.com` works too. |
| `--flag FLAG` | `add` | The flag to set, by name or key as `usernotes flags SUBREDDIT` lists it.  Defaults to the flag on the user's most recent flagged note; `none` sets no flag. |
| `--link URL`, `--url URL` | `add` | What the note is about: an absolute URL, or a path such as `/r/toolbox/comments/abc123/`. |
| `-E`, `--regex` | `search` | Read PATTERN as a regular expression, matched anywhere in a field. |
| `--case-sensitive` | `search` | Match case, where the default ignores it. |
| `--newest` / `--oldest` | `remove` | Delete the user's newest, or oldest, note. |
| `--timestamp TIME` | `remove` | Delete the note written at this time, as `usernotes read` prints it, e.g. `"2026-11-05 06:11:20 UTC"`. |
| `-o FILE`, `--output FILE` | `backup` | The file to write; the default is `usernotes_<subreddit>_<UTC timestamp>.json` in the current directory. |
| `-d DIR`, `--directory DIR` | `backup` | Where to put that generated filename; created if it does not exist. |
| `--force` | `restore` | Restore a file that does not decode as a usernotes page. |
| `--dry-run` | `restore` | Say what would be written, and write nothing. |
| `--json` | all | Print one JSON document instead of the listing.  For `read` and `add`: `subreddit`, `page`, `schema`, `exists`, `name`, `aliases`, `notes[]` (`text`, `time`, `time_iso`, `mod`, `type`, `link`) and `warnings[]`.  For `flags`: `subreddit` and `flags[]` (`key`, `text`, `color`).  For `search`: `subreddit`, `pattern`, `regex`, `count` and `matches[]` (each a note with a `user`).  For `remove`: the `read` document plus the note that went, under `removed`.  For `backup`: `path`, `subreddit`, `taken_at`, `size`, `notes`, `users` and `warnings`.  For `restore`: `path`, `subreddit`, `schema`, `size`, `notes`, `users`, `replaced_notes`, `replaced_users`, `written`, `dry_run` and `warnings`. |
| `--color WHEN` | all | `auto` (default: colour when stdout is a terminal), `always`, `never`. |
| `--local` | `read`, `add`, `search`, `remove` | Show note times in the local time zone instead of UTC. |
| `-v` / `-q` | all | More or less logging on stderr: `-v` for what is happening, `-vv` for every request, `-q` for errors only. |

### Cookies

Reading or writing a subreddit's usernotes normally needs a moderator's
session, which is why the tool takes cookies from a browser profile you are
already logged in to reddit with - the same thing `yt-dlp --cookies-from-browser`
does.  A note is recorded against that account.  Without either option the
request goes out anonymously, which only works for a subreddit whose usernotes
page is public, and cannot write.  If the browser's cookie store
cannot be read (a locked keyring, a different user account, Safari), export the
cookies with a browser extension and pass them with `--cookies FILE`.  Either
way they go *before* the command, as the global options they are:

```sh
usernotes --cookies-from-browser firefox read toolbox alice
usernotes --cookies ~/cookies.txt search toolbox spam
```

Cookies are credentials: the tool never prints or logs them, only the *names*
of the cookies it found.

A Chromium-family browser on Linux encrypts what it stores with a key held in
the desktop keyring, and which keyring that is has to be guessed from the
desktop environment - so on a desktop it does not recognise, the guess can be
wrong and the reddit session cookie goes missing, leaving every request
anonymous.  The tool says so before it makes one, and naming the keyring settles
it: `usernotes --cookies-from-browser chromium+gnomekeyring ...`.  A store with
no keyring-encrypted cookies in it is read without any keyring access at all,
and a keyring that cannot be read costs only those cookies, not the store.

### Exit status

| Status | Meaning |
|---|---|
| `0` | A listing was printed, a search ran (nothing matching counts), a note was written or deleted, or a file was backed up or restored. |
| `1` | Reddit refused, or a store or file could not be read or written (bad cookies, not a moderator, the page is not usernotes, a flag that does not exist, a note with that timestamp, a pattern that is not a regular expression, the page changed while the note was being added, a file that is not a usernotes page, ...). |
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
