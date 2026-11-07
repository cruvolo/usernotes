"""The package version, in one place.

Kept apart from ``usernotes/__init__.py`` so that modules which need the
version - the default ``User-Agent`` in `usernotes.api`, for instance - can
import it without importing the package's public API back into itself.
"""

__all__ = ['__version__']

__version__ = '0.1.0'
