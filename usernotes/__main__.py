"""``python -m usernotes``, for running the tool without installing it."""

import sys

from .cli import main

if __name__ == '__main__':
    sys.exit(main())
