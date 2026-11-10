"""Normalisation, phrase segmentation and n-gram tokenisation.

A *phrase* is what a user would call "a line to quote": one sentence, or one
clause, never a fragment that starts mid-word.  Phrases are computed once,
offline, and stored in the library; everything at query time works on the
integers that identify them.

This module is pure standard library and is shared by the offline builder and
the runtime, so the two always tokenise and segment identically.
"""

import re
import unicodedata

SENTENCE_END = re.compile(r'[。！？!?]+[」』”’）)]*|(?<![.\d])\.(?=\s|$)')
CLAUSE_GAP = re.compile(r'(?<=[ぁ-んァ-ヶ])　+')
ATTACHED_CLAUSE = re.compile(r"^(?:けど|かも|とは|なんて|って|から|ので|のに|ほど)")
CLAUSE_END = re.compile(r'(?:ない|たい|だった|です|ます|でしょう|だよ|だね)$')
OPEN_END = re.compile(r'(?:[、，,:：]|けれど|けど|ながら|そして|なのに|を|[一-龯ァ-ヶ][がのへはに])$')
WORD_CONTINUES = re.compile(r'^[ぁぃぅぇぉゃゅょっァィゥェォャュョッー]')
NON_WORD = re.compile(r"[\W_]+")

NGRAM_SIZES = (1, 2, 3)


def normalized(text):
    """Matching key: width-, case- and punctuation-insensitive."""
    return NON_WORD.sub("", unicodedata.normalize("NFKC", str(text)).casefold())


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


def phrases(rows):
    """Group lyric rows into phrases.

    `rows` yields ``(position, stanza, text)`` in song order.  Returns
    ``(line_first, line_last, text, stanza)`` tuples, where the line range is
    inclusive and 1-based.

    Lines are split into sentences and clauses; then adjacent rows are merged
    again when the line break falls inside a clause, which is common in
    Japanese lyrics.
    """
    result = []
    for position, stanza, text in rows:
        pieces = split_line(text)
        if not pieces:
            continue
        if result:
            first, last, previous, previous_stanza = result[-1]
            if (last + 1 == position and previous_stanza == stanza
                    and (OPEN_END.search(previous) or
                         (WORD_CONTINUES.match(pieces[0]) and not SENTENCE_END.search(previous)))):
                separator = " " if previous[-1].isascii() or pieces[0][0].isascii() else ""
                result[-1] = [first, position, previous + separator + pieces[0], previous_stanza]
                pieces = pieces[1:]
        result.extend([position, position, piece, stanza] for piece in pieces)
    return [tuple(item) for item in result]


def ngrams(text, size):
    """Character n-grams of a normalised string."""
    return [text[i:i + size] for i in range(len(text) - size + 1)]
