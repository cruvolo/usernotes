"""The logger contract the modules in this package accept.

Every public entry point that can emit progress or warnings takes a ``logger``
argument and defaults to a `Logger` writing warnings and errors to stderr,
which is what a command line tool wants.  Callers are free to pass their own
object as long as it has the four methods checked by `resolve_logger`, which
lets a front end route everything through its own logging setup.

Nothing here ever logs credentials: see the security notes in
`usernotes.cookies`.
"""

from __future__ import annotations

from ._vendor._compat import Logger, NULL_LOGGER, NullLogger  # noqa: F401  (public facade)

__all__ = [
    'Logger',
    'NULL_LOGGER',
    'NullLogger',
    'resolve_logger',
]

_LEVELS = ('debug', 'info', 'warning', 'error')


def resolve_logger(logger=None):
    """Return *logger*, or a default one writing warnings to stderr.

    :param logger: ``None`` for the default, or an object with ``debug``,
        ``info``, ``warning`` and ``error`` methods taking one message each
        (``warning`` may additionally be called as ``warning(msg, only_once)``,
        as `Logger` supports).  A logger may also provide a ``progress_bar()``
        method returning a context manager with a ``print`` method.
    :raises ValueError: if *logger* does not provide those methods, which
        would otherwise surface much later as an ``AttributeError`` in the
        middle of a request.
    """
    if logger is None:
        return Logger()
    for name in _LEVELS:
        if not callable(getattr(logger, name, None)):
            raise ValueError(
                f'invalid logger {logger!r}: no {name}() method '
                '(a logger must provide debug(), info(), warning() and error())')
    return logger
