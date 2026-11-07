"""Third-party code vendored into usernotes.

Modules in this package are copied from other projects and adapted to run
standalone (no dependency on the project they came from).  Each module carries
a header comment naming its upstream source and listing the adaptations made.

Currently vendored, both from `yt-dlp <https://github.com/yt-dlp/yt-dlp>`_ at
commit ``412e7f0a4b0588fe884a02b344ac66a06538475b`` (version ``2026.10.26``):

``aes.py``
    Pure-python AES-CBC/AES-GCM primitives, from ``yt_dlp/aes.py``.

``browser_cookies.py``
    Browser cookie extraction, from ``yt_dlp/cookies.py``.

``_compat.py``
    Local stand-ins for the handful of ``yt_dlp`` internals those two modules
    import (logging, subprocess helper, small string/path helpers).  This file
    is ours, not vendored.

Licensing: yt-dlp is released into the public domain under the Unlicense, so
copying and adapting it is unencumbered.  See
``../yt-dlp/LICENSE`` in the reference checkout.  The Unlicense places no
conditions on reuse, but keep this provenance note so the origin of the code
(and the need to refresh it when browsers change) stays visible.

The public, stable API of this project lives in :mod:`usernotes.cookies`; the
names in this package are implementation details and may change when the
vendored code is refreshed.
"""
