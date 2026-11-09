"""VocaDB querier: find a song's YouTube PV and start it at the lyric.

Uses the public VocaDB API (https://wiki.vocadb.net/docs/public-api).  One
lookup performs at most two requests -- first the title without its trailing
brackets, then the title as-is -- and caches responses, as the API asks.
"""

import re
import urllib.parse
from collections.abc import Mapping

from .base import LyricMeta, MediaLink, QuerierError, json_fetch, normalized

API = "https://vocadb.net/api"
TRAILING_BRACKETS = re.compile(r"\s*[\(（\[【][^\)）\]】]*[\)）\]】]\s*$")
AUTHOR_SEPARATORS = re.compile(r"[/／,，、&＆]")
YOUTUBE_SERVICES = {"youtube"}


class VocadbQuerier:
    """Map a lyric to a YouTube link on the matching VocaDB song entry."""

    name = "vocadb"

    def __init__(self, *, base_url=API, fetch=json_fetch, timeout=15.0,
                 max_results=10, lang="Japanese", cache_size=128):
        self.base_url = base_url.rstrip("/")
        self.fetch = fetch
        self.timeout = timeout
        self.max_results = max(1, int(max_results))
        self.lang = lang
        self.cache_size = max(0, int(cache_size))
        self._cache = {}

    def find(self, meta) -> MediaLink | None:
        meta = LyricMeta.coerce(meta)
        if not normalized(meta.title):
            return None
        for query in _queries(meta.title):
            link = _best(self._search(query), meta)
            if link is not None:
                return link
        return None

    def _search(self, query):
        params = {
            "query": query,
            "fields": "PVs,Artists",
            "nameMatchMode": "Auto",
            # Popularity order surfaces the canonical entry; the ranking below
            # then decides based on title and artist.
            "sort": "FavoritedTimes",
            "maxResults": str(self.max_results),
            "lang": self.lang,
        }
        url = self.base_url + "/songs?" + urllib.parse.urlencode(params)
        data = self._get(url)
        items = data.get("items")
        if not isinstance(items, list):
            raise QuerierError("VocaDB response has no items array")
        return [item for item in items if isinstance(item, Mapping)]

    def _get(self, url):
        if url in self._cache:
            return self._cache[url]
        try:
            data = self.fetch(url, timeout=self.timeout)
        except QuerierError:
            raise
        except Exception as error:
            raise QuerierError(f"VocaDB request failed: {error}") from error
        if not isinstance(data, Mapping):
            raise QuerierError("VocaDB returned a non-object response")
        if self.cache_size:
            if len(self._cache) >= self.cache_size:
                self._cache.clear()
            self._cache[url] = data
        return data


def _queries(title):
    full = str(title).strip()
    queries = [full]
    core = _core(full)
    if normalized(core) and normalized(core) != normalized(full):
        queries.insert(0, core)
    return [query for query in dict.fromkeys(queries) if normalized(query)]


def _core(title):
    result = str(title).strip()
    for _ in range(3):
        stripped = TRAILING_BRACKETS.sub("", result).strip()
        if not stripped or stripped == result:
            break
        result = stripped
    return result


def _author_parts(author):
    parts = []
    for part in AUTHOR_SEPARATORS.split(str(author or "")):
        key = normalized(part)
        if len(key) >= 2:
            parts.append(key)
    return parts


def _name_score(name, title):
    if not name or not title:
        return 0
    if name == title:
        return 3
    short, long = sorted((name, title), key=len)
    if len(short) < 2:
        return 0
    if long.startswith(short) or long.endswith(short):
        return 2
    if short in long and len(short) * 2 >= len(long):
        return 1
    return 0


def _score(item, meta):
    names = {normalized(item.get("name")), normalized(item.get("defaultName"))} - {""}
    titles = {normalized(meta.title), normalized(_core(meta.title))} - {""}
    name_score = max((_name_score(name, title) for name in names for title in titles),
                     default=0)
    if not name_score:
        return 0
    artist = normalized(item.get("artistString") or "")
    hits = sum(1 for part in _author_parts(meta.author) if part in artist)
    if name_score < 2 and hits < 2:
        return 0
    original = 10 if str(item.get("songType", "")).casefold() == "original" else 0
    return name_score * 10 + min(hits, 3) * 5 + original


def _pick_pv(item):
    pvs = [pv for pv in item.get("pvs") or [] if isinstance(pv, Mapping)
           and str(pv.get("service", "")).casefold() in YOUTUBE_SERVICES
           and pv.get("pvId") and not pv.get("disabled")]
    if not pvs:
        return None
    # The official upload is normally the earliest non-disabled YouTube PV.
    pvs.sort(key=lambda pv: (0 if str(pv.get("pvType", "")).casefold() == "original" else 1,
                             str(pv.get("publishDate") or "9999")))
    return pvs[0]


def _best(items, meta):
    scored = [(score, item) for item in items
              if (score := _score(item, meta)) > 0]
    for _, item in sorted(scored, key=lambda pair: (-pair[0], pair[1].get("id") or 0)):
        pv = _pick_pv(item)
        if pv is not None:
            return _link(item, pv, meta)
    return None


def _link(item, pv, meta):
    seconds = meta.start_seconds
    url = "https://www.youtube.com/watch?v=" + urllib.parse.quote(str(pv["pvId"]))
    if seconds:
        url += f"&t={seconds}"
    return MediaLink(url=url, provider="vocadb", service="youtube",
                     start_seconds=seconds, video_id=str(pv["pvId"]),
                     title=item.get("name"))
