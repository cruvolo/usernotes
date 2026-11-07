"""Command line tools for the reddit Moderator Toolbox "usernotes" store.

Usernotes are kept by the toolbox browser extension in a subreddit wiki page
(``https://www.reddit.com/r/<subreddit>/wiki/usernotes``) as compressed, base64
encoded JSON.  This package reads and writes that store outside of a browser.

Modules:

`usernotes.cookies`
    Retrieve the browser cookies needed to authenticate to Reddit.  Vendored
    from, and behaviourally compatible with, ``yt-dlp --cookies-from-browser``.

The command line front end itself is not written yet; `usernotes.cookies`
documents the interface it is expected to use.
"""

__version__ = '0.1.0'

__all__ = ['__version__']
