"""Normalisation and phrase segmentation.

The library stores one row per lyric line.  A *phrase* is what a user would
call "a line to quote": one sentence, or one clause, never a fragment that
starts mid-word.  Retrieval, quoting and navigation all work on phrases, so
those boundaries are the unit everything else agrees on.
"""

import re
import unicodedata
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
    def by_line(self):
        positions = defaultdict(list)
        for i, phrase in enumerate(self.phrases):
            for line in phrase.lines:
                positions[line.id].append(i)
        return positions


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
