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
  is all that is needed.
* On Linux, `secretstorage` is installed with the package: it is what reads a
  Chromium cookie store's key out of the desktop keyring over D-Bus, and
  without it those cookies cannot be decrypted.  It comes with `cryptography`
  and `jeepney` of its own.  On Debian-based systems install the
  `python3-secretstorage` package.
* A reddit account that moderates the subreddit, unless its usernotes page has
  been left publicly readable.

## Build and install

On Debian-based Linux systems, install the secretstorage dependency:

```sh
sudo apt install python3-secretstorage
```

The console script and the library come from one install.  From the directory
holding `pyproject.toml`:

```sh
python3 -m pip install .
usernotes --version
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

See [command.md](command.md) for details about each command.  Or run `usernotes
COMMAND --help`.

### Cookies

Reading or writing a subreddit's usernotes normally needs a moderator's
session, which is why the tool takes cookies from a browser profile you are
already logged in to reddit with - the same thing `yt-dlp --cookies-from-browser`
does.  A note is recorded against that account.  Without either option the
request goes out anonymously, which only works for a subreddit whose usernotes
page is public (which is unexpected), and cannot write.  If the browser's
cookie store cannot be read (a locked keyring, a different user account,
Safari), export the cookies with a browser extension and pass them with
`--cookies FILE`.  Either way they go *before* the command, as the global
options they are:

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
| `usernotes/cookies.py` | Getting those cookies out of a browser (borrowed from yt-dlp). |
| `usernotes/errors.py` | Every exception the package raises. |

## License

Code derived from a third party in `usernotes/_vendor/` comes from the
[`yt-dlp` project](https://github.com/yt-dlp/yt-dlp/), and is public domain.

Everything else is under the terms of the AGPL 3.0.  See the [`COPYING`
file](COPYING).
