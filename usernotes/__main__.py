# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""``python -m usernotes``, for running the tool without installing it."""

import sys

from .cli import main

if __name__ == '__main__':
    sys.exit(main())
