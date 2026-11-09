"""Normalisation, phrase segmentation and exact lyric matching.

The library stores one row per lyric line.  A *phrase* is what a user would
call "a line to quote": one sentence, or one clause, never a fragment that
starts mid-word.  Retrieval, quoting and continuation all work on phrases,
so those boundaries are the unit everything else agrees on.
"""

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

SENTENCE_END = re.compile(r'[。！？!?]+[」』”’）)]*|(?<![.\d])\.(?=\s|$)')
CLAUSE_GAP = re.compile(r'(?<=[ぁ-んァ-ヶ])　+')
ATTACHED_CLAUSE = re.compile(r"^(?:けど|かも|とは|なんて|って|から|ので|のに|ほど)")
CLAUSE_END = re.compile(r'(?:ない|たい|だった|です|ます|でしょう|だよ|だね)$')
OPEN_END = re.compile(r'(?:[、，,:：]|けれど|けど|ながら|そして|なのに|を|[一-龯ァ-ヶ][がのへはに])$')
WORD_CONTINUES = re.compile(r'^[ぁぃぅぇぉゃゅょっァィゥェォャュョッー]')


def normalized(text):
    """Matching key: width-, case- and punctuation-insensitive."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Line:
    id: int
    song_id: int
    position: int
    stanza: int
    text: str
    translation: str | None
    start_ms: int | None
    end_ms: int | None


@dataclass(frozen=True)
class Phrase:
    """One quotable piece of lyric, possibly merged from soft line breaks."""

    text: str
    lines: tuple[Line, ...]

    @cached_property
    def key(self):
        return normalized(self.text)

    @property
    def start_ms(self):
        return next((line.start_ms for line in self.lines if line.start_ms is not None), None)

    @property
    def end_ms(self):
        return next((line.end_ms for line in reversed(self.lines) if line.end_ms is not None), None)


@dataclass(frozen=True)
class Track:
    lines: list[Line]

    @cached_property
    def key(self):
        return "".join(normalized(line.text) for line in self.lines)

    @cached_property
    def phrases(self):
        result = []
        for line in self.lines:
            pieces = split_line(line.text)
            if not pieces:
                continue
            if result:
                previous = result[-1]
                last = previous.lines[-1]
                if (last.position + 1 == line.position and last.stanza == line.stanza
                        and (OPEN_END.search(previous.text) or
                             (WORD_CONTINUES.match(pieces[0]) and not SENTENCE_END.search(previous.text)))):
                    # A soft database line break can fall inside one phrase.
                    separator = " " if previous.text[-1].isascii() or pieces[0][0].isascii() else ""
                    result[-1] = Phrase(previous.text + separator + pieces[0], (*previous.lines, line))
                    pieces = pieces[1:]
            result.extend(Phrase(piece, (line,)) for piece in pieces)
        return result

    @cached_property
    def boundaries(self):
        offsets = [0]
        for phrase in self.phrases:
            offsets.append(offsets[-1] + len(phrase.key))
        return offsets

    @cached_property
    def by_line(self):
        positions = defaultdict(list)
        for i, phrase in enumerate(self.phrases):
            for line in phrase.lines:
                positions[line.id].append(i)
        return positions

    def matches(self, key):
        """Yield (first_phrase, next_phrase, aligned) for every occurrence of key."""
        ends = self.boundaries
        start = self.key.find(key)
        while start >= 0:
            stop = start + len(key)
            boundary = bisect_left(ends, stop)
            if boundary < len(ends) and ends[boundary] == stop:
                first = bisect_right(ends, start) - 1
                yield first, boundary, ends[first] == start
            start = self.key.find(key, start + 1)

    def locate(self, key):
        start = self.key.find(key)
        if start >= 0:
            return bisect_right(self.boundaries, start) - 1, bisect_left(self.boundaries, start + len(key))
        return None


def split_line(text):
    pieces = []
    start = 0
    for end in SENTENCE_END.finditer(text):
        pieces.append(text[start:end.end()])
        start = end.end()
    pieces.append(text[start:])
    result = []
    for piece in pieces:
        start = 0
        for gap in CLAUSE_GAP.finditer(piece):
            if CLAUSE_END.search(piece[start:gap.start()]) and not ATTACHED_CLAUSE.match(piece[gap.end():]):
                result.append(piece[start:gap.start()].strip())
                start = gap.end()
        rest = piece[start:].strip()
        if normalized(rest):
            result.append(rest)
        elif rest and result:
            result[-1] += rest
    return [piece for piece in result if normalized(piece)]


def continuation(tracks, message, source=None):
    """The next phrase when `message` is a verbatim lyric passage.

    Returns (song_id, phrase_index) or None.  The passage must be whole
    phrases, and every occurrence of it in the library must be followed by
    the same phrase -- otherwise the library cannot tell which song the user
    meant and stays quiet.
    """
    key = normalized(message)
    if len(key) < (2 if source else 4):
        return None
    source_song = source["song_id"] if source else None
    hits = []
    for song_id, track in tracks.items():
        if len(key) < 4 and song_id != source_song:
            continue
        if key not in track.key:
            continue
        for first, end, aligned in track.matches(key):
            if not aligned:
                continue
            following = track.phrases[end].key if end < len(track.phrases) else None
            hits.append((song_id, first, end, following))
    if not hits or len({hit[3] for hit in hits}) != 1:
        return None
    if source:
        forward = [hit for hit in hits if hit[0] == source_song and hit[1] == source["end"] + 1]
        if forward:
            return forward[0][0], forward[0][2]
    song_id, _, end, _ = max(hits, key=lambda hit: (hit[2] - hit[1], -hit[0]))
    return song_id, end
