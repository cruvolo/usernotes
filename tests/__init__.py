"""Tests for the usernotes test suite.

Run from the repository root::

    python3 -m unittest discover

Every test builds its own throwaway browser profile in a temporary directory.
The suite never reads a real browser profile, never touches the network, and
never writes to Reddit or a subreddit wiki - the only datastore involved is a
``cookies.sqlite``/``Cookies`` file the test itself created in a temp dir.
"""
