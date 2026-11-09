"""Media lookup: turn lyric metadata into a playable link.

A *querier* is one service that knows how to map lyric metadata (title,
author, position in the song) to a media URL.  Queriers are independent of
the lyric library: they accept a mapping, a ``LyricMeta`` or any object with
``title``/``author``/``start_ms`` attributes, and return a ``MediaLink`` or
``None``.

Adding a service means implementing ``find`` and registering an instance:

    from botaloid_lite.media import register

    class MyQuerier:
        name = "my-service"
        def find(self, meta):
            ...
            return MediaLink(...)

    register(MyQuerier())

Callers then select it by name or pass instances directly:

    Resolver(["vocadb"]).find(passage)
    Resolver([MyQuerier()]).find(passage)

This module imports nothing from the rest of the package and needs only the
standard library.
"""

import json
import re
import unicodedata
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

USER_AGENT = "botaloid-lite/0.2 (media resolver)"


def json_fetch(url: str, timeout: float = 15.0) -> Mapping:
    """Default transport: GET a URL and decode its JSON body."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def normalized(text) -> str:
    """Comparison key: width-, case- and punctuation-insensitive."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text)).casefold())


@dataclass(frozen=True)
class LyricMeta:
    """What a querier needs to know.  Nothing here is library-specific."""

    title: str
    author: str = ""
    start_ms: int | None = None

    @classmethod
    def coerce(cls, value) -> "LyricMeta":
        """Accept a LyricMeta, a mapping (``Passage.to_dict()``) or an object."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(title=value)
        if isinstance(value, Mapping):
            get = value.get
        else:
            get = lambda key, default=None: getattr(value, key, default)  # noqa: E731
        title = get("title") or get("name") or ""
        author = get("author") or get("artist") or ""
        start_ms = get("start_ms")
        try:
            start_ms = None if start_ms is None else max(0, int(start_ms))
        except (TypeError, ValueError):
            start_ms = None
        return cls(title=str(title), author=str(author), start_ms=start_ms)

    @property
    def start_seconds(self) -> int | None:
        return None if self.start_ms is None else self.start_ms // 1000


@dataclass(frozen=True)
class MediaLink:
    url: str
    provider: str
    service: str
    start_seconds: int | None = None
    video_id: str | None = None
    title: str | None = None

    def to_dict(self):
        return {"url": self.url, "provider": self.provider, "service": self.service,
                "start_seconds": self.start_seconds, "video_id": self.video_id,
                "title": self.title}


class QuerierError(RuntimeError):
    """A querier could not complete its lookup (network, bad response)."""


@runtime_checkable
class Querier(Protocol):
    name: str

    def find(self, meta: LyricMeta) -> MediaLink | None:
        ...


REGISTRY: dict[str, Querier] = {}


def register(querier: Querier) -> Querier:
    """Make a querier available by name to ``Resolver()``."""
    REGISTRY[querier.name] = querier
    return querier


def available() -> list[str]:
    return list(REGISTRY)


class Resolver:
    """Try queriers in order and return the first link that exists."""

    def __init__(self, queriers: Iterable[Querier | str] | None = None):
        if queriers is None:
            self.queriers = list(REGISTRY.values())
            return
        self.queriers = []
        for item in queriers:
            if isinstance(item, str):
                if item not in REGISTRY:
                    raise ValueError(f"unknown querier: {item!r}")
                item = REGISTRY[item]
            self.queriers.append(item)

    @property
    def providers(self) -> list[str]:
        return [querier.name for querier in self.queriers]

    def find(self, meta) -> MediaLink | None:
        """First successful link, or None.  A failing querier is skipped."""
        lyric = LyricMeta.coerce(meta)
        for querier in self.queriers:
            try:
                link = querier.find(lyric)
            except Exception:                        # one broken provider is not fatal
                continue
            if link is not None:
                return link
        return None

    def find_all(self, meta) -> list[MediaLink]:
        lyric = LyricMeta.coerce(meta)
        links = []
        for querier in self.queriers:
            try:
                link = querier.find(lyric)
            except Exception:
                continue
            if link is not None:
                links.append(link)
        return links


def resolve(meta, queriers: Iterable[Querier | str] | None = None) -> MediaLink | None:
    """Shorthand for ``Resolver(queriers).find(meta)``."""
    return Resolver(queriers).find(meta)


__all__ = [
    "LyricMeta", "MediaLink", "Querier", "QuerierError", "Resolver", "available",
    "json_fetch", "normalized", "register", "resolve", "USER_AGENT",
]
