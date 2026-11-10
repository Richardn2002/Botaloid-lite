"""Build the derived retrieval tables in a lyrics SQLite file.

Everything the runtime needs lives in the database: the phrases, the FTS5
index over their unigrams, bigrams and trigrams, the document frequencies and
the precomputed vector norms.  Nothing is kept in Python memory afterwards.

Run ``python -m botaloid_lite.build data/lyrics.sqlite3`` to (re)build an
existing file, or let ``tools/prepare.py`` call it after importing.

The build is deliberately offline-friendly: it assumes a machine with a few
hundred megabytes of free RAM, not the machine that serves requests.
"""

import sqlite3
import time
from collections import Counter
from math import log, sqrt
from pathlib import Path

from .text import NGRAM_SIZES, normalized, ngrams, phrases

SCHEMA = """
DROP TABLE IF EXISTS phrases;
DROP TABLE IF EXISTS grams;
DROP TABLE IF EXISTS lyric_fts;
CREATE TABLE phrases (
    id INTEGER PRIMARY KEY,
    song_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    stanza INTEGER NOT NULL,
    line_first INTEGER NOT NULL,
    line_last INTEGER NOT NULL,
    text TEXT NOT NULL,
    translation TEXT,
    text_key TEXT NOT NULL,
    start_ms INTEGER,
    end_ms INTEGER,
    norm REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX phrases_song ON phrases(song_id, idx);
CREATE VIRTUAL TABLE lyric_fts USING fts5(
    u, b, t, content='', tokenize='unicode61'
);
CREATE TABLE grams (gram TEXT PRIMARY KEY, df INTEGER NOT NULL);
"""

INSERT_PHRASE = ("INSERT INTO phrases(id,song_id,idx,stanza,line_first,line_last,"
                 "text,translation,text_key,start_ms,end_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)")


def token_columns(text, translation):
    """The three FTS columns (unigrams, bigrams, trigrams) for one phrase."""
    doc = normalized(text) + normalized(translation or "")
    return tuple(" ".join(ngrams(doc, size)) for size in NGRAM_SIZES)


def build_index(db_path, *, progress=None):
    """(Re)build phrases, FTS, document frequencies and norms in `db_path`."""
    path = Path(db_path)
    db = sqlite3.connect(path)
    started = time.perf_counter()
    try:
        db.execute("PRAGMA journal_mode=OFF")
        db.execute("PRAGMA synchronous=OFF")
        db.executescript(SCHEMA)
        count = _fill_phrases(db)
        _fill_fts(db)
        _fill_grams(db)
        _fill_norms(db, count, progress)
        db.execute("ANALYZE")
        db.commit()
    finally:
        db.close()
    return {"phrases": count, "seconds": time.perf_counter() - started,
            "bytes": path.stat().st_size}


def _songs(db):
    """Yield one list of lyric rows per song, in the order stored."""
    rows = []
    song_id = None
    for row in db.execute(
            "SELECT song_id,position,stanza,text,translation,start_ms,end_ms "
            "FROM lyric_lines ORDER BY song_id,position"):
        if row[0] != song_id:
            if rows:
                yield rows
            song_id, rows = row[0], []
        rows.append(row)
    if rows:
        yield rows


def _fill_phrases(db):
    phrase_id = 0
    buffer = []
    for rows in _songs(db):
        by_position = {row[1]: row for row in rows}
        groups = phrases((row[1], row[2], row[3]) for row in rows)
        for idx, (first, last, text, stanza) in enumerate(groups):
            head, tail = by_position[first], by_position[last]
            translation = "\n".join(
                by_position[position][4] for position in range(first, last + 1)
                if by_position[position][4])
            phrase_id += 1
            buffer.append((phrase_id, rows[0][0], idx, stanza, first, last, text,
                           translation or None, normalized(text),
                           head[5], tail[6]))
        if len(buffer) >= 2000:
            db.executemany(INSERT_PHRASE, buffer)
            buffer.clear()
    if buffer:
        db.executemany(INSERT_PHRASE, buffer)
    db.commit()
    return phrase_id


def _fill_fts(db):
    rows = db.execute("SELECT id,text,translation FROM phrases ORDER BY id")
    buffer = []
    for phrase_id, text, translation in rows:
        buffer.append((phrase_id,) + token_columns(text, translation))
        if len(buffer) >= 2000:
            db.executemany("INSERT INTO lyric_fts(rowid,u,b,t) VALUES(?,?,?,?)", buffer)
            buffer.clear()
    if buffer:
        db.executemany("INSERT INTO lyric_fts(rowid,u,b,t) VALUES(?,?,?,?)", buffer)
    db.commit()


def _fill_grams(db):
    db.execute("CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main, lyric_fts, 'row')")
    db.execute("INSERT INTO grams(gram,df) SELECT term, doc FROM temp.vocab")
    db.execute("DROP TABLE temp.vocab")
    db.commit()


def _fill_norms(db, count, progress=None):
    # One pass: every phrase's vector is its unigram/bigram/trigram counts
    # weighted by the global idf, and we only need its length.
    weights = {gram: log((1 + count) / (1 + df)) + 1
               for gram, df in db.execute("SELECT gram, df FROM grams")}
    done = 0
    buffer = []
    for phrase_id, text, translation in db.execute(
            "SELECT id,text,translation FROM phrases ORDER BY id"):
        doc = normalized(text) + normalized(translation or "")
        counts = Counter()
        for size in NGRAM_SIZES:
            counts.update(ngrams(doc, size))
        norm = sqrt(sum((tf * weights.get(gram, 1.0)) ** 2 for gram, tf in counts.items()))
        buffer.append((norm, phrase_id))
        if len(buffer) >= 5000:
            db.executemany("UPDATE phrases SET norm=? WHERE id=?", buffer)
            buffer.clear()
            db.commit()
            done += 5000
            if progress is not None:
                progress(min(done, count), count)
    if buffer:
        db.executemany("UPDATE phrases SET norm=? WHERE id=?", buffer)
    db.commit()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="为曲库构建检索索引（短语、FTS5、DF、向量范数）。")
    parser.add_argument("database", type=Path, help="曲库 SQLite 文件")
    args = parser.parse_args(argv)

    def report(done, total):
        print(f"norm {done}/{total}", flush=True)

    try:
        result = build_index(args.database, progress=report)
    except (OSError, sqlite3.Error) as error:
        raise SystemExit(f"构建失败：{error}") from error
    print(f"phrases={result['phrases']} seconds={result['seconds']:.1f} "
          f"bytes={result['bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
