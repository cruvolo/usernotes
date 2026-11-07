# (c)2026 Chris Ruvolo. All rights reserved.
# License: AGPL v3.0 only

"""Every exception this package raises, in one place.

The hierarchy is deliberately shallow: one base class so callers can catch
everything from a single ``except``, then one family per layer of the stack.

``UsernotesError``
    base class of everything below

Reading and writing the *wiki page text* (no network) - see `usernotes.format`:

``FormatError``
    the page text is not a usernotes store
``InvalidPageError``
    ... it is damaged, truncated or not JSON at all
``UnsupportedSchemaError``
    ... it is a schema version this tool does not understand

Talking to Reddit - see `usernotes.api`:

``ApiError``
    a request failed, or Reddit refused it
``NetworkError``
    ... because it could not be completed (timeout, connection, 5xx)
``RateLimitedError``
    ... because we were sending requests too fast
``NotLoggedInError``
    ... because the cookies are missing, expired or not for Reddit
``ForbiddenError``
    ... because the logged-in account may not do it
``NoWikiPageError``
    ... because the page is missing, empty or the wiki is disabled
``PageFullError``
    ... because the wiki page is too big (Reddit answers HTTP 413)

Editing the store in memory - see `usernotes.store`:

``StaleStoreError``
    the page changed under us, so saving would destroy someone else's work
``UserNotFoundError`` / ``NoteNotFoundError``
    a requested user or note is not in the store
``UnknownNoteTypeError``
    a note type was named that the subreddit does not define
"""

__all__ = [
    'ApiError',
    'ForbiddenError',
    'FormatError',
    'InvalidConfigError',
    'InvalidPageError',
    'NetworkError',
    'NoWikiPageError',
    'NotLoggedInError',
    'NoteNotFoundError',
    'PageFullError',
    'RateLimitedError',
    'StaleStoreError',
    'UnknownNoteTypeError',
    'UnsupportedSchemaError',
    'UserNotFoundError',
    'UsernotesError',
]


class UsernotesError(Exception):
    """Base class for every error raised by this package."""


class FormatError(UsernotesError):
    """The wiki page text is not a usernotes store."""


class InvalidPageError(FormatError):
    """The usernotes page is damaged, empty or not JSON.

    Reddit's wiki page for a subreddit that has never used usernotes is
    missing or empty rather than damaged, which is why callers usually treat
    this as "there are no notes yet" - see `usernotes.api.NoWikiPageError` for
    the transport-level version of the same situation.
    """


class InvalidConfigError(FormatError):
    """The subreddit's toolbox config is present but not usable.

    Raised when reading ``usernoteColors``, so it covers the note type table
    rather than the notes themselves.
    """


class UnsupportedSchemaError(FormatError):
    """The page is a usernotes schema version this tool cannot read.

    Toolbox reads v4-v6 and writes v6; so does this package.  Older versions
    (v3 and below) stored the note map as an array and are not converted, and
    a *newer* version means a newer toolbox wrote something we would not
    understand well enough to write back safely.
    """


class ApiError(UsernotesError):
    """A request to Reddit failed.

    :param message: human-readable description.
    :param status: HTTP status code, when there was one.
    :param reason: Reddit's own error code, e.g. ``'PAGE_NOT_CREATED'`` or
        ``'USER_REQUIRED'``, when the response carried one.
    :param url: the URL that failed (never contains credentials; the modhash
        travels in the request body or query and is not part of this URL for
        the API calls this package makes, but do not log it blindly either).
    """

    def __init__(self, message, *, status=None, reason=None, url=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.url = url


class NetworkError(ApiError):
    """The request could not be completed: DNS, connection, timeout, or 5xx."""


class RateLimitedError(NetworkError):
    """Reddit is throttling us (HTTP 429, or an exhausted rate-limit budget).

    :param retry_after: seconds Reddit asked us to wait, when it said.
    """

    def __init__(self, message, *, retry_after=None, **kwargs):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class NotLoggedInError(ApiError):
    """The cookies do not identify a logged-in Reddit account.

    Usually means the jar is empty, expired, or was read from a browser
    profile the user is not logged in with.
    """


class ForbiddenError(ApiError):
    """The logged-in account is not allowed to do this.

    The common case for usernotes is a non-moderator: the page exists but the
    account may not read it, so ``reason`` is typically ``'FORBIDDEN'``.
    """


class NoWikiPageError(ApiError):
    """The wiki page does not exist, is empty, or the wiki is disabled.

    ``reason`` is Reddit's ``'PAGE_NOT_CREATED'`` or ``'WIKI_DISABLED'``, or
    ``None`` when the page was readable but empty.
    """


class PageFullError(ApiError):
    """The wiki page is too large for Reddit to accept (HTTP 413).

    Toolbox shows this as "usernotes full"; the store has to be pruned or
    split before it can be saved again.
    """


class StaleStoreError(UsernotesError):
    """The wiki page changed after it was read.

    Saving now would discard whatever another moderator added in the
    meantime, so the store refuses unless forced.
    """


class UserNotFoundError(UsernotesError):
    """No notes are stored for the requested user."""


class NoteNotFoundError(UsernotesError):
    """The requested note is not stored for that user."""


class UnknownNoteTypeError(UsernotesError):
    """A note type was named that the subreddit's config does not define.

    :param value: what the caller asked for.
    :param known: the keys that would have worked.
    """

    def __init__(self, message, *, value=None, known=()):
        super().__init__(message)
        self.value = value
        self.known = tuple(known)
