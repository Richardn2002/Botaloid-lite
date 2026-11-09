"""The lyric library: retrieval, quoting and the two-stage model selection.

Public surface:

    library = Library("lyrics.sqlite3")
    library.search("夜里散步")                      # deterministic retrieval
    library.search(source=fragment["source"], direction="after")
    library.step(history, settings=Settings(model=call_model))

`search` never calls a model.  `step` calls the model twice per user turn --
once to turn the conversation into search terms, once to choose among the
candidates those terms retrieved.
"""

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from .prompts import PLAN, PLAN_SCHEMA, SELECT, SELECT_SCHEMA
from .text import Line, Track, normalized

Model = Callable[[str, str, float, dict], Mapping | str]
DIRECTIONS = ("at", "before", "after", "around")
MAX_QUERIES = 4
MAX_TERMS = 3
MAX_CANDIDATES = 16
POOL = 120
MAX_CHARS = 140
MIN_TERM = 2
MAX_TERM = 96
HISTORY_TURNS = 6
HISTORY_CHARS = 350


class ModelError(Exception):
    """A model call failed.  The library never hides one."""


@dataclass(frozen=True)
class LineSpan:
    position: int
    text: str
    translation: str | None
    start_ms: int | None
    end_ms: int | None

    def to_dict(self):
        return {"position": self.position, "text": self.text,
                "translation": self.translation,
                "start_ms": self.start_ms, "end_ms": self.end_ms}


@dataclass(frozen=True)
class Passage:
    """One quotable span of lyric, with everything a caller needs to show it."""

    song_id: int
    title: str
    author: str
    start: int          # phrase index, inclusive
    end: int            # phrase index, inclusive
    text: str
    translation: str | None
    lines: tuple[LineSpan, ...]

    @property
    def position(self):
        return self.lines[0].position

    @property
    def start_ms(self):
        return next((line.start_ms for line in self.lines if line.start_ms is not None), None)

    @property
    def end_ms(self):
        return next((line.end_ms for line in reversed(self.lines) if line.end_ms is not None), None)

    def to_dict(self):
        return {
            "text": self.text,
            "translation": self.translation,
            "title": self.title,
            "author": self.author,
            "song_id": self.song_id,
            "position": self.position,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            # Opaque locator: pass it back as `source` to navigate.
            "source": {"song_id": self.song_id, "start": self.start, "end": self.end},
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class Settings:
    """Everything `step` needs, supplied per call instead of persisted."""

    model: Model | None = None
    plan_temperature: float = 0.2
    select_temperature: float = 0.55
    results: int = 3
    phrases: int = 4


class Library:
    def __init__(self, db_path, *, max_features=100_000):
        self.path = Path(db_path).resolve()
        self._lock = threading.RLock()
        self._max_features = max_features
        self._vectorizer = None
        self._matrix = None
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro",
                                  uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._songs = {row["id"]: dict(row) for row in self._db.execute(
                "SELECT id,title,author,duration_ms FROM songs")}
            self._lines = [Line(row["id"], row["song_id"], row["position"], row["stanza"],
                                row["text"], row["translation"], row["start_ms"], row["end_ms"])
                           for row in self._db.execute(
                "SELECT id,song_id,position,stanza,text,translation,start_ms,end_ms "
                "FROM lyric_lines ORDER BY song_id,position")]
            grouped = {}
            for line in self._lines:
                grouped.setdefault(line.song_id, []).append(line)
            self._tracks = {song_id: Track(group) for song_id, group in grouped.items()}

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_error):
        self.close()

    # ------------------------------------------------------------------ search

    def search(self, query=None, *, queries=None, author=None, title=None,
               source=None, direction="after", count=1, limit=5, phrases=4,
               exclude=(), refine=None):
        """Return passages, best first, without calling a model.

        `query`    free text; `queries` optional extra search terms.
        `author`/`title` restrict the library (substring, normalized).
        `source`   navigate from a passage instead of searching; `direction`
                   is "at", "before", "after" or "around", `count` is how many
                   extra phrases to include.
        `exclude`  lyric texts that must not be returned (e.g. recent replies).
        `refine`   optional callable(options) -> {"choices": [...]}, the same
                   contract as the SELECT stage, for semantic re-ranking.
        """
        limit, phrases, count = max(1, int(limit)), _clamp(phrases, 1, 8), max(1, int(count))
        if source is not None:
            if direction not in DIRECTIONS:
                raise ValueError("direction must be one of " + ", ".join(DIRECTIONS))
            return self._navigate(source, direction, count)
        groups = _groups(queries, query)
        allowed = _matching(self._songs, author, title)
        if not allowed:
            return []
        mask = allowed if (author or title) else None
        if groups:
            quotes = self._quotes(self._candidates(groups, exclude, mask), groups, phrases)
            quotes = [quote for quote in quotes if quote.song_id in allowed]
        elif author or title:
            quotes = self._openings(allowed, limit, phrases)
        else:
            return []
        if refine is not None:
            return self._apply(refine(_options(self._tracks, quotes)), quotes, limit, phrases)
        return quotes[:limit]

    # -------------------------------------------------------------------- step

    def step(self, history, *, settings=None):
        """Append the assistant's reply to `history` and return the new list.

        `history` is a list of {"role": "user"|"assistant", "text": str}
        dicts whose last turn is the user message being answered.  Assistant
        turns returned by this method also carry `fragments`, `source` and
        `status`; keep them so the caller can navigate with search(source=...).
        """
        settings = settings or Settings()
        turns = [dict(turn) for turn in history]
        if not turns or turns[-1].get("role") != "user":
            raise ValueError("history must end with a user turn")
        message = str(turns[-1].get("text") or "")
        plan = self._plan(turns, message, settings)
        quotes = self._quotes_for_plan(plan, message, settings)
        selected = self._select(quotes, turns, message, settings) if quotes else []
        return turns + [_turn(selected)]

    # --------------------------------------------------------------- internals

    def _ensure_index(self):
        if self._vectorizer is not None:
            return True
        docs = [normalized(line.text) + " " + normalized(line.translation or "")
                for line in self._lines]
        if not any(doc.strip() for doc in docs):
            return False
        vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 3),
                                     dtype=np.float32, max_features=self._max_features)
        self._vectorizer = vectorizer
        self._matrix = vectorizer.fit_transform(docs)
        return True

    def _candidates(self, groups, exclude=(), allowed=None):
        if not groups or not self._ensure_index():
            return []
        with self._lock:
            vectors = self._vectorizer.transform([term for group in groups for term in group])
            scores = (vectors @ self._matrix.T).toarray()
            if allowed is not None:
                active = np.fromiter((line.song_id in allowed for line in self._lines),
                                     dtype=bool, count=len(self._lines))
                scores[:, ~active] = 0
        excluded = {normalized(text) for text in exclude}
        take = min(POOL, len(self._lines))
        pools, offset = [], 0
        for group in groups:
            group_scores = scores[offset:offset + len(group)].max(axis=0)
            offset += len(group)
            weights = {}
            for i in np.argpartition(group_scores, -take)[-take:]:
                if group_scores[i] <= 0:
                    continue
                line = self._lines[i]
                text = normalized(line.text)
                if not text or text in excluded:
                    continue
                weight = float(group_scores[i])
                if any(term in text for term in group):
                    weight += 0.18
                weights[i] = weight
            pools.append(sorted(weights, key=lambda i: (-weights[i], i)))
        seen, per_song, chosen = set(), {}, []
        # Interleave the searches so one direction cannot take every slot.
        for i in (i for layer in zip_longest(*pools) for i in layer if i is not None):
            line = self._lines[i]
            text = normalized(line.text)
            if text in seen or per_song.get(line.song_id, 0) >= 2:
                continue
            seen.add(text)
            per_song[line.song_id] = per_song.get(line.song_id, 0) + 1
            chosen.append(line)
            if len(chosen) >= MAX_CANDIDATES:
                break
        return chosen

    def _quotes(self, lines, groups, phrases):
        terms = [term for group in groups for term in group]
        quotes, seen = [], set()
        for line in lines:
            track = self._tracks.get(line.song_id)
            span = self._span(track, line, terms, phrases) if track else None
            if not span or (line.song_id, span[0]) in seen:
                continue
            seen.add((line.song_id, span[0]))
            passage = self._passage(line.song_id, *span)
            if passage is not None:
                quotes.append(passage)
        return quotes

    def _span(self, track, line, terms, phrases):
        starts = [i for i in track.by_line.get(line.id, [])
                  if len(track.phrases[i].text) <= 180]
        if not starts:
            return None
        start = max(starts, key=lambda i: max(
            (len(term) for term in terms if term in track.phrases[i].key), default=0))
        first = track.phrases[start]
        end, length, keys = start, len(first.text), {first.key}
        for phrase in track.phrases[start + 1:start + phrases]:
            if (phrase.lines[0].stanza != first.lines[0].stanza
                    or length + len(phrase.text) > MAX_CHARS
                    or phrase.key in keys
                    or phrase.text.lstrip().startswith(("－", "—", "―"))):
                break
            end, length = end + 1, length + len(phrase.text)
            keys.add(phrase.key)
        return start, end

    def _passage(self, song_id, start, end):
        track, song = self._tracks.get(song_id), self._songs.get(song_id)
        if track is None or song is None:
            return None
        chunk = track.phrases[start:end + 1]
        if not chunk:
            return None
        lines = list(dict.fromkeys(line for phrase in chunk for line in phrase.lines))
        return Passage(
            song_id=song_id, title=song["title"], author=song["author"],
            start=start, end=end,
            text="\n".join(phrase.text for phrase in chunk),
            translation="\n".join(line.translation for line in lines if line.translation) or None,
            lines=tuple(LineSpan(line.position, line.text, line.translation,
                                 line.start_ms, line.end_ms) for line in lines),
        )

    def _openings(self, allowed, limit, phrases):
        result = []
        for song_id in sorted(allowed):
            track = self._tracks.get(song_id)
            if not track or not track.phrases:
                continue
            passage = self._passage(song_id, 0, min(phrases, len(track.phrases)) - 1)
            if passage is not None:
                result.append(passage)
            if len(result) >= limit:
                break
        return result

    def _navigate(self, source, direction, count):
        try:
            song_id = int(source["song_id"])
            start, end = int(source["start"]), int(source["end"])
        except (KeyError, TypeError, ValueError):
            return []
        track = self._tracks.get(song_id)
        if track is None or not track.phrases:
            return []
        count = min(count, 8)
        if direction == "before":
            first, last = start - count, start - 1
        elif direction == "around":
            first, last = start - count, end + count
        elif direction == "at":
            first, last = start, end
        else:
            first, last = end + 1, end + count
        first, last = max(0, first), min(len(track.phrases) - 1, last)
        if first > last:
            return []
        passage = self._passage(song_id, first, last)
        return [passage] if passage is not None else []

    def _plan(self, turns, message, settings):
        if settings.model is None:
            return {}
        plan = self._call(PLAN, _history_prompt(turns, message),
                          settings.plan_temperature, PLAN_SCHEMA, settings)
        return {"queries": plan.get("queries") if isinstance(plan.get("queries"), list) else [],
                "author": _text(plan.get("author")), "title": _text(plan.get("title"))}

    def _quotes_for_plan(self, plan, message, settings):
        groups = _groups(plan.get("queries"), message)
        allowed = _matching(self._songs, plan.get("author"), plan.get("title"))
        if not allowed:
            return []
        named = plan.get("author") or plan.get("title")
        if not groups:
            return self._openings(allowed, settings.results, settings.phrases) if named else []
        return self._quotes(self._candidates(groups, allowed=allowed if named else None),
                            groups, settings.phrases)

    def _select(self, quotes, turns, message, settings):
        if settings.model is None:
            return quotes[:settings.results]
        options = _options(self._tracks, quotes)
        choices = self._call(SELECT, _candidates_prompt(turns, message, options),
                             settings.select_temperature, SELECT_SCHEMA, settings)
        return self._apply(choices, quotes, settings.results, settings.phrases)

    def _apply(self, choices, quotes, results, phrases):
        if isinstance(choices, Mapping):
            choices = choices.get("choices")
        result, used = [], set()
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, Mapping):
                continue
            candidate_id, count = choice.get("id"), choice.get("count", 1)
            if type(candidate_id) is not int or not 1 <= candidate_id <= len(quotes):
                continue
            if type(count) is not int:
                continue
            quote = quotes[candidate_id - 1]
            if quote.song_id in used:
                continue
            end = min(quote.end, quote.start + max(1, min(count, phrases)) - 1)
            passage = self._passage(quote.song_id, quote.start, end)
            if passage is not None:
                result.append(passage)
                used.add(quote.song_id)
            if len(result) >= results:
                break
        return result

    @staticmethod
    def _call(system, prompt, temperature, schema, settings):
        try:
            answer = settings.model(system, prompt, temperature, schema)
        except ModelError:
            raise
        except Exception as error:                     # a broken adapter is not fatal
            raise ModelError(str(error)) from error
        if isinstance(answer, str):
            try:
                answer = json.loads(answer)
            except ValueError:
                return {}
        return dict(answer) if isinstance(answer, Mapping) else {}


def _clamp(value, low, high):
    return max(low, min(int(value), high))


def _text(value):
    return value.strip()[:100] if isinstance(value, str) else ""


def _groups(queries, query):
    groups, seen = [], set()
    source = [queries] if isinstance(queries, str) else list(queries or [])
    for group in source[:MAX_QUERIES]:
        values = [group] if isinstance(group, str) else list(group)
        terms = []
        for value in values[:MAX_TERMS]:
            if not isinstance(value, str):
                continue
            term = normalized(value[:MAX_TERM])
            if len(term) >= MIN_TERM and term not in seen:
                terms.append(term)
                seen.add(term)
        if terms:
            groups.append(terms)
    raw = normalized((query or "")[:220])
    if raw and raw not in seen:
        groups.append([raw])
    return groups


def _matching(songs, author, title):
    author_key, title_key = normalized(author or ""), normalized(title or "")
    pool = {song_id for song_id, song in songs.items()
            if (not author_key or author_key in normalized(song["author"]))
            and (not title_key or title_key in normalized(song["title"]))}
    if title_key:
        exact = {song_id for song_id in pool
                 if normalized(songs[song_id]["title"]) == title_key}
        return exact or pool
    return pool


def _history_prompt(turns, message):
    lines = ["前文（只用于参考）："]
    for turn in turns[:-1][-HISTORY_TURNS:]:
        role = "用户：" if turn.get("role") == "user" else "机器人："
        lines.append(role + str(turn.get("text") or "")[:HISTORY_CHARS])
    lines.append("【当前用户】\n" + message + "\n【当前用户结束】")
    return "\n".join(lines)


def _candidates_prompt(turns, message, options):
    lines = ["前文（只用于参考）："]
    for turn in turns[:-1][-HISTORY_TURNS:]:
        role = "用户：" if turn.get("role") == "user" else "机器人："
        lines.append(role + str(turn.get("text") or "")[:HISTORY_CHARS])
    lines.append("候选歌词：")
    for item in options:
        phrases = " / ".join(f"{i}) {text}" for i, text in enumerate(item["phrases"], 1))
        lines.append(f'候选 {item["id"]}（《{item["title"]}》 / {item["author"]}）：{phrases}')
    lines.append("【当前用户】\n" + message + "\n【当前用户结束】")
    return "\n".join(lines)


def _options(tracks, quotes):
    result = []
    for i, quote in enumerate(quotes, 1):
        track = tracks.get(quote.song_id)
        chunk = track.phrases[quote.start:quote.end + 1] if track else []
        result.append({"id": i, "title": quote.title, "author": quote.author,
                       "phrases": [phrase.text for phrase in chunk] or [quote.text]})
    return result


def _turn(passages):
    fragments = [passage.to_dict() for passage in passages]
    if not fragments:
        return {"role": "assistant", "text": "", "fragments": [],
                "source": None, "status": "no_match"}
    return {"role": "assistant", "text": "\n".join(p.text for p in passages),
            "fragments": fragments, "source": fragments[-1]["source"], "status": "selected"}
