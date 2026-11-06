# CLAUDE.md

## Usernotes

This is a set of command line tools to work with the reddit toolbox `usernotes`
browser extension component.  It runs independently of toolbox.  It is written
in python and reasonable attempts are made at making it cross-platform portable
within the python ecosystem.

The reddit moderator toolbox has a way to store information about a user, and
share it between all the moderators.  Usernotes are written to the subreddit's
wiki, which is used as a shared data repository.  The usernotes are written to
the subreddit's wiki at the following URL:
`https://www.reddit.com/r/<subreddit>/wiki/usernotes` The data is written in a
json format with base64 encoding and some compression. See
`usernotes-storage.md` for details.

## Reference 

These programs must make reasonable attempts to match existing functionality in
toolbox.  To do so, the user's browser cookies may be needed.  See
`browser-cookies.md`.

As such, the repositories of reddit toolbox and yt-dlp are available in the
following locations relative to this file:

* `../reddit-moderator-toolbox`
* `../yt-dlp`

Treat these directories as read-only.

Read components of `reddit-moderator-toolbox` to investigate usernotes
functionality.

Read components of `yt-dlp` to investigate how to retrieve cookies from the
user's browsers.

You may take code from both these projects to use within this project as needed.

## Goals

1. Create a command line interface to read usernotes for a particular user and
   display them.  Try to match the existing functionality of usernotes.
   Retrieve the user's full usernote history, and display it.  When output is a
   tty, assume xterm-256-color is available and match colors as needed.

   Parameters include: subreddit, username, --cookies-from-browser (matching
   yt-dlp functionality)

   Output should match the UI of toolbox, including date, link, the actual
   usernote, etc.

2. Create a command line interface to add to a user's usernotes on a particular
   subreddit.  Parameters include: subreddit, username, --cookies-from-browser
   (match yt-dlp), the usernote message, and whether any of the flags should be
   set ("spam watch", "spam warning", "ban", "permanent ban", etc.).

3. Create a command line interface to back up a subreddit's usernotes.

## Code

Use standard python3 where possible.  Use necessary libraries, and make
infrastructure to retrieve dependencies as needed.

Use best practices.

Don't make mistakes.

## Git use

Local git may be used as needed.  It's better to make smaller, more
understandable, incremental commits than larger commits.  There is no upstream
or origin repo.

Do not ever commit secret data like cookies or keys to git!  For example, do
not commit the `docker-claude-agent.sh` script to git, as it contains an
important secret, an API key.

NEVER REWRITE GIT HISTORY!  If something needs to be undone, do it without
rewriting history.

## Docker use

This claude instance is running unprivileged inside a docker container.  If you
need to install any additional packages, add them to the Dockerfile and let the
user know.

## Current state

This repository is brand new.  No code exists.  Use best practices when
creating the codebase.

## Cookies

When you need cookies to test in a non-destructive manner, let the user know
how to retrieve them for you.

## Testing

ALL TESTING MUST BE NON-DESTRUCTIVE!!!  Assume you are working with a
production datastore and production reddit instance.

