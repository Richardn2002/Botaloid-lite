"""Media links for lyric metadata.

The abstraction lives in :mod:`botaloid_lite.media.base`; the VocaDB querier is
registered when this package is imported, so ``Resolver()`` works with no setup.
"""

from .base import (USER_AGENT, LyricMeta, MediaLink, Querier, QuerierError, Resolver,
                   available, json_fetch, normalized, register, resolve)
from .vocadb import VocadbQuerier

if "vocadb" not in available():
    register(VocadbQuerier())

__all__ = ["USER_AGENT", "LyricMeta", "MediaLink", "Querier", "QuerierError",
           "Resolver", "VocadbQuerier", "available", "json_fetch", "normalized",
           "register", "resolve"]
