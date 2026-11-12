"""The lyric library: retrieval, quoting and the two-stage model selection.

Public surface:

    library = Library("lyrics.sqlite3")
    library.search("夜里散步")                      # deterministic retrieval
    library.search(source=fragment["source"], direction="after")
    library.step(history, settings=Settings(model=call_model))

Retrieval never calls a model.  ``step`` calls the model twice per user turn:
once to turn the conversation into search terms, once to choose among the
candidates those terms retrieved.

Everything the runtime needs is in the SQLite file: the phrases, an FTS5 index
over their unigrams, bigrams and trigrams, document frequencies and the
precomputed vector lengths.  Python keeps only the song list and SQLite's page
cache, so memory does not grow with the number of lyrics.
"""

import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from math import log, sqrt
from pathlib import Path

from .prompts import (PLAN_BODY, PLAN_FORMAT, PLAN_SCHEMA, SELECT_BODY,
                      SELECT_FORMAT, SELECT_SCHEMA)
from .text import NGRAM_SIZES, normalized, ngrams

Model = Callable[[str, str, float, dict], Mapping | str]
DIRECTIONS = ("at", "before", "after", "around")
MAX_QUERIES = 4
MAX_TERMS = 3
MAX_CANDIDATES = 16
MAX_CHARS = 140
MIN_TERM = 2
MAX_TERM = 96
HISTORY_TURNS = 6
HISTORY_CHARS = 350
DEFAULT_POOL = 40
DEFAULT_WEIGHTS = (1.0, 1.5, 2.0)
FALLBACK_MIN = 8
SQL_CHUNK = 900


class ModelError(Exception):
    """A model call failed.  The library never hides one."""


class MissingIndexError(RuntimeError):
    """The database has no retrieval index; rebuild it first."""


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
    # Replace the tunable part of each system prompt.  The fixed output-format
    # block and the JSON schema are always appended by the library.
    plan_prompt: str | None = None
    select_prompt: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A scored phrase span, before its line-level metadata is fetched."""

    song_id: int
    start: int
    end: int
    title: str
    author: str
    text: str
    phrases: tuple[str, ...]


class Library:
    def __init__(self, db_path, *, cache_size=32, pool=DEFAULT_POOL,
                 weights=DEFAULT_WEIGHTS):
        self.path = Path(db_path).resolve()
        self.pool = max(4, int(pool))
        self.weights = tuple(float(weight) for weight in weights)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro",
                                   uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA cache_size=-{max(1, int(cache_size)) * 1024}")
        with self._lock:
            names = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE name IN ('phrases','lyric_fts','grams')")}
            if names != {"phrases", "lyric_fts", "grams"}:
                raise MissingIndexError(
                    f"{self.path} has no retrieval index; run "
                    f"`python -m botaloid_lite.build {self.path}` first")
            self._songs = {row["id"]: dict(row) for row in self._db.execute(
                "SELECT id,title,author,duration_ms FROM songs")}
            self._phrase_count = self._db.execute(
                "SELECT count(*) FROM phrases").fetchone()[0]

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
            passage = self._navigate(source, direction, count)
            return [passage] if passage is not None else []
        allowed = self._matching(author, title)
        if not allowed:
            return []
        filtered = bool(author or title)
        groups = _groups(queries, query)
        if groups:
            candidates = self._retrieve(groups, allowed, filtered,
                                        {normalized(text) for text in exclude}, phrases)
        elif filtered:
            candidates = self._openings(allowed, limit, phrases)
        else:
            return []
        if refine is not None:
            chosen = self._apply(refine(_options(candidates)), candidates, limit, phrases)
            return [passage for passage in
                    (self._passage(candidate, count) for candidate, count in chosen)
                    if passage is not None]
        return [passage for passage in
                (self._passage(candidate) for candidate in candidates[:limit])
                if passage is not None]

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
        # Do not answer a follow-up with the lines we just quoted.
        exclude = {normalized(line) for turn in turns[:-1]
                   if turn.get("role") == "assistant"
                   for line in str(turn.get("text") or "").splitlines() if normalized(line)}
        plan = self._plan(turns, message, settings)
        candidates = self._quotes_for_plan(plan, message, settings, exclude)
        chosen = self._select(candidates, turns, message, settings) if candidates else []
        return turns + [_turn([passage for passage in
                               (self._passage(candidate, count) for candidate, count in chosen)
                               if passage is not None])]

    # --------------------------------------------------------------- retrieval

    def _fts(self, match, limit):
        if not match:
            return []
        unigram, bigram, trigram = self.weights
        sql = (f"SELECT rowid FROM lyric_fts WHERE lyric_fts MATCH ? "
               f"ORDER BY bm25(lyric_fts, {unigram}, {bigram}, {trigram}) LIMIT ?")
        with self._lock:
            return [row[0] for row in self._db.execute(sql, (match, limit))]

    def _group_ids(self, group):
        primary, fallback = [], []
        for term in group:
            key = normalized(term)
            if not key:
                continue
            for size in (3, 2):
                primary.extend(ngrams(key, size))
            fallback.extend(ngrams(key, 1))
        ids = self._fts(_match_text(primary), self.pool)
        # Unigrams are only a safety net: on their own they flood the result
        # set with common characters.
        if len(ids) < FALLBACK_MIN:
            ids.extend(self._fts(_match_text(fallback), self.pool))
        return list(dict.fromkeys(ids))

    def _retrieve(self, groups, allowed, filtered, exclude, phrases):
        pools = [self._group_ids(group) for group in groups]
        ids, seen = [], set()
        for layer in zip_longest(*pools):
            for rowid in layer:
                if rowid is not None and rowid not in seen:
                    seen.add(rowid)
                    ids.append(rowid)
        rows = self._rows(ids)
        candidates, keys, per_song = [], set(), Counter()
        for rowid in ids:
            row = rows.get(rowid)
            if row is None:
                continue
            if filtered and row["song_id"] not in allowed:
                continue
            key = row["text_key"]
            if not key or key in exclude or key in keys:
                continue
            if per_song[row["song_id"]] >= 2:
                continue
            keys.add(key)
            per_song[row["song_id"]] += 1
            candidates.append(row)
        scored = self._rank(candidates, [term for group in groups for term in group])
        scored.sort(key=lambda item: (-item[1], item[0]["id"]))
        return [self._extend(row, phrases) for row, _ in scored[:MAX_CANDIDATES]]

    def _rows(self, ids):
        rows = {}
        with self._lock:
            for chunk in _chunks(ids, SQL_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                for row in self._db.execute(
                        "SELECT id,song_id,idx,stanza,text,translation,text_key,"
                        "start_ms,end_ms,line_first,line_last,norm "
                        f"FROM phrases WHERE id IN ({placeholders})", chunk):
                    rows[row["id"]] = row
        return rows

    def _idf(self, grams):
        grams = list(grams)
        frequencies = {}
        with self._lock:
            for chunk in _chunks(grams, SQL_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                for gram, df in self._db.execute(
                        f"SELECT gram, df FROM grams WHERE gram IN ({placeholders})", chunk):
                    frequencies[gram] = df
        total = self._phrase_count
        return {gram: log((1 + total) / (1 + frequencies.get(gram, 1))) + 1 for gram in grams}

    def _rank(self, rows, terms):
        query = Counter()
        for term in terms:
            key = normalized(term)
            for size in NGRAM_SIZES:
                query.update(ngrams(key, size))
        if not query:
            return []
        idf = self._idf(query)
        weighted = {gram: tf * idf[gram] for gram, tf in query.items()}
        norm_q = sqrt(sum(value * value for value in weighted.values())) or 1.0
        literal = [term for term in terms if len(term) >= MIN_TERM]
        scored = []
        for row in rows:
            doc = normalized(row["text"]) + normalized(row["translation"] or "")
            counts = Counter()
            for size in NGRAM_SIZES:
                counts.update(ngrams(doc, size))
            dot = sum(value * counts[gram] * idf[gram]
                      for gram, value in weighted.items() if gram in counts)
            norm = row["norm"] or 0.0
            score = dot / (norm_q * norm) if norm else 0.0
            if any(term in row["text_key"] for term in literal):
                score += 0.18
            scored.append((row, score))
        return scored

    def _extend(self, row, limit):
        with self._lock:
            window = self._db.execute(
                "SELECT idx,stanza,text,translation,text_key,start_ms,end_ms,"
                "line_first,line_last FROM phrases WHERE song_id=? AND idx>=? "
                "ORDER BY idx LIMIT ?", (row["song_id"], row["idx"], limit)).fetchall()
        span = [window[0]]
        length, keys = len(window[0]["text"]), {window[0]["text_key"]}
        for nxt in window[1:]:
            if (nxt["idx"] != span[-1]["idx"] + 1 or nxt["stanza"] != window[0]["stanza"]
                    or length + len(nxt["text"]) > MAX_CHARS
                    or nxt["text_key"] in keys
                    or nxt["text"].lstrip().startswith(("－", "—", "―"))):
                break
            span.append(nxt)
            length += len(nxt["text"])
            keys.add(nxt["text_key"])
        song = self._songs[row["song_id"]]
        return Candidate(
            song_id=row["song_id"], start=span[0]["idx"], end=span[-1]["idx"],
            title=song["title"], author=song["author"],
            text="\n".join(phrase["text"] for phrase in span),
            phrases=tuple(phrase["text"] for phrase in span))

    def _openings(self, allowed, limit, phrases):
        result = []
        for song_id in sorted(allowed):
            with self._lock:
                rows = self._db.execute(
                    "SELECT idx,text,translation FROM phrases WHERE song_id=? "
                    "ORDER BY idx LIMIT ?", (song_id, phrases)).fetchall()
            if not rows:
                continue
            song = self._songs[song_id]
            result.append(Candidate(
                song_id=song_id, start=rows[0]["idx"], end=rows[-1]["idx"],
                title=song["title"], author=song["author"],
                text="\n".join(row["text"] for row in rows),
                phrases=tuple(row["text"] for row in rows)))
            if len(result) >= limit:
                break
        return result

    def _navigate(self, source, direction, count):
        try:
            song_id = int(source["song_id"])
            start, end = int(source["start"]), int(source["end"])
        except (KeyError, TypeError, ValueError):
            return None
        with self._lock:
            total = self._db.execute(
                "SELECT count(*) FROM phrases WHERE song_id=?", (song_id,)).fetchone()[0]
        if not total:
            return None
        count = min(max(1, count), 8)
        if direction == "before":
            first, last = start - count, start - 1
        elif direction == "around":
            first, last = start - count, end + count
        elif direction == "at":
            first, last = start, end
        else:
            first, last = end + 1, end + count
        first, last = max(0, first), min(total - 1, last)
        if first > last:
            return None
        candidate = Candidate(song_id=song_id, start=first, end=last, title="",
                              author="", text="", phrases=())
        return self._passage(candidate)

    def _passage(self, candidate, count=None):
        end = candidate.end if count is None else min(candidate.end,
                                                      candidate.start + max(1, count) - 1)
        with self._lock:
            rows = self._db.execute(
                "SELECT idx,text,translation,start_ms,end_ms,line_first,line_last "
                "FROM phrases WHERE song_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
                (candidate.song_id, candidate.start, end)).fetchall()
            if not rows:
                return None
            lines = self._db.execute(
                "SELECT position,text,translation,start_ms,end_ms FROM lyric_lines "
                "WHERE song_id=? AND position BETWEEN ? AND ? ORDER BY position",
                (candidate.song_id, rows[0]["line_first"], rows[-1]["line_last"])).fetchall()
        song = self._songs.get(candidate.song_id, {})
        return Passage(
            song_id=candidate.song_id,
            title=song.get("title", candidate.title),
            author=song.get("author", candidate.author),
            start=rows[0]["idx"], end=rows[-1]["idx"],
            text="\n".join(row["text"] for row in rows),
            translation="\n".join(row["translation"] for row in rows if row["translation"]) or None,
            lines=tuple(LineSpan(line["position"], line["text"], line["translation"],
                                 line["start_ms"], line["end_ms"]) for line in lines))

    def _matching(self, author, title):
        author_key, title_key = normalized(author or ""), normalized(title or "")
        pool = {song_id for song_id, song in self._songs.items()
                if (not author_key or author_key in normalized(song["author"]))
                and (not title_key or title_key in normalized(song["title"]))}
        if title_key:
            exact = {song_id for song_id in pool
                     if normalized(self._songs[song_id]["title"]) == title_key}
            return exact or pool
        return pool

    # ------------------------------------------------------------------- model

    def _plan(self, turns, message, settings):
        if settings.model is None:
            return {}
        system = _system_prompt(settings.plan_prompt, PLAN_BODY, PLAN_FORMAT)
        plan = self._call(system, _history_prompt(turns, message),
                          settings.plan_temperature, PLAN_SCHEMA, settings)
        return {"queries": plan.get("queries") if isinstance(plan.get("queries"), list) else [],
                "author": _text(plan.get("author")), "title": _text(plan.get("title"))}

    def _quotes_for_plan(self, plan, message, settings, exclude=()):
        groups = _groups(plan.get("queries"), message)
        allowed = self._matching(plan.get("author"), plan.get("title"))
        if not allowed:
            return []
        named = plan.get("author") or plan.get("title")
        if not groups:
            if not named:
                return []
            return self._openings(allowed, settings.results, settings.phrases)
        return self._retrieve(groups, allowed, bool(named), exclude, settings.phrases)

    def _select(self, candidates, turns, message, settings):
        if settings.model is None:
            return [(candidate, settings.phrases)
                    for candidate in candidates[:settings.results]]
        system = _system_prompt(settings.select_prompt, SELECT_BODY, SELECT_FORMAT)
        choices = self._call(system, _candidates_prompt(turns, message, _options(candidates)),
                             settings.select_temperature, SELECT_SCHEMA, settings)
        return self._apply(choices, candidates, settings.results, settings.phrases)

    def _apply(self, choices, candidates, results, phrases):
        if isinstance(choices, Mapping):
            choices = choices.get("choices")
        result, used, titles = [], set(), set()
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, Mapping):
                continue
            candidate_id, count = choice.get("id"), choice.get("count", 1)
            if type(candidate_id) is not int or not 1 <= candidate_id <= len(candidates):
                continue
            if type(count) is not int:
                continue
            candidate = candidates[candidate_id - 1]
            title = normalized(candidate.title)
            if candidate.song_id in used or title in titles:
                continue
            result.append((candidate, max(1, min(count, phrases))))
            used.add(candidate.song_id)
            titles.add(title)
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


def _system_prompt(custom, body, format_block):
    if custom is None:
        return body + "\n\n" + format_block
    if not isinstance(custom, str):
        raise ValueError("custom prompts must be strings or None")
    custom = custom.strip()
    return format_block if not custom else custom + "\n\n" + format_block


def _match_text(tokens):
    unique = list(dict.fromkeys(token for token in tokens if token))
    if not unique:
        return ""
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in unique)


def _chunks(items, size):
    chunk = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


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


def _options(candidates):
    return [{"id": i, "title": candidate.title, "author": candidate.author,
             "phrases": list(candidate.phrases)}
            for i, candidate in enumerate(candidates, 1)]


def _turn(passages):
    fragments = [passage.to_dict() for passage in passages]
    if not fragments:
        return {"role": "assistant", "text": "", "fragments": [],
                "source": None, "status": "no_match"}
    return {"role": "assistant", "text": "\n".join(p.text for p in passages),
            "fragments": fragments, "source": fragments[-1]["source"], "status": "selected"}
