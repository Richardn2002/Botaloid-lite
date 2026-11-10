import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from botaloid_lite import Library, MissingIndexError, ModelError, Settings
from botaloid_lite import prompts
from botaloid_lite.build import build_index

SCHEMA = """
CREATE TABLE songs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
                    duration_ms INTEGER);
CREATE TABLE lyric_lines (id INTEGER PRIMARY KEY, song_id INTEGER NOT NULL REFERENCES songs(id),
                          position INTEGER NOT NULL, stanza INTEGER NOT NULL, text TEXT NOT NULL,
                          translation TEXT, start_ms INTEGER, end_ms INTEGER,
                          UNIQUE(song_id, position));
"""

SONGS = [
    ("夜", "作者甲", 60000, [
        ("静かな夜を歩いた", "走过安静的夜", 1000, 3000),
        ("君の声を聞かせて", "让我听听你的声音", 3000, 5000),
        ("また明日", None, 5000, 7000),
    ]),
    ("朝", "作者乙", 30000, [
        ("同じ歌詞の行", None, 1000, 2000),
        ("違う続き", None, 2000, 4000),
    ]),
    ("別", "作者甲", 40000, [
        ("同じ歌詞の行", None, 5000, 6000),
        ("光が差す", "光照射下来", 6000, 9000),
    ]),
]


def build(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        for title, author, duration, lyrics in SONGS:
            song_id = db.execute("INSERT INTO songs(title,author,duration_ms) VALUES(?,?,?)",
                                 (title, author, duration)).lastrowid
            db.executemany(
                "INSERT INTO lyric_lines(song_id,position,stanza,text,translation,start_ms,end_ms) "
                "VALUES(?,?,?,?,?,?,?)",
                [(song_id, position, 0, text, translation, start, end)
                 for position, (text, translation, start, end) in enumerate(lyrics, 1)])
    build_index(path)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "lyrics.sqlite3"
        build(self.path)
        self.library = Library(self.path)
        self.addCleanup(self.library.close)

    def test_search_returns_lyrics_with_metadata(self):
        night = next(hit for hit in self.library.search("静かな夜を歩いた") if hit.title == "夜")
        data = night.to_dict()
        self.assertEqual((data["title"], data["author"]), ("夜", "作者甲"))
        self.assertEqual(data["text"], "静かな夜を歩いた\n君の声を聞かせて\nまた明日")
        self.assertEqual(data["translation"], "走过安静的夜\n让我听听你的声音")
        self.assertEqual((data["start_ms"], data["end_ms"]), (1000, 7000))
        self.assertEqual(data["position"], 1)
        self.assertEqual(data["source"], {"song_id": 1, "start": 0, "end": 2})
        self.assertEqual([(line["text"], line["start_ms"], line["end_ms"]) for line in data["lines"]],
                         [("静かな夜を歩いた", 1000, 3000),
                          ("君の声を聞かせて", 3000, 5000),
                          ("また明日", 5000, 7000)])

    def test_search_can_limit_the_span(self):
        data = self.library.search("静かな夜を歩いた", phrases=1)[0].to_dict()
        self.assertEqual(data["text"], "静かな夜を歩いた")
        self.assertEqual((data["start_ms"], data["end_ms"]), (1000, 3000))
        self.assertEqual(data["source"], {"song_id": 1, "start": 0, "end": 0})

    def test_search_filters_by_author_and_title(self):
        self.assertTrue(all(hit.author == "作者甲" for hit in self.library.search("夜を歩いた", author="作者甲")))
        titles = [hit.title for hit in self.library.search(author="作者甲", limit=5)]
        self.assertEqual(sorted(titles), ["別", "夜"])
        self.assertEqual([hit.title for hit in self.library.search(title="夜")], ["夜"])

    def test_search_navigates_from_a_source(self):
        source = {"song_id": 1, "start": 1, "end": 1}
        after = self.library.search(source=source, direction="after")
        self.assertEqual([hit.text for hit in after], ["また明日"])
        self.assertEqual(after[0].start_ms, 5000)
        before = self.library.search(source=source, direction="before")
        self.assertEqual([hit.text for hit in before], ["静かな夜を歩いた"])
        around = self.library.search(source=source, direction="around")
        self.assertEqual([hit.text for hit in around], ["静かな夜を歩いた\n君の声を聞かせて\nまた明日"])
        at = self.library.search(source=source, direction="at")
        self.assertEqual([hit.text for hit in at], ["君の声を聞かせて"])

    def test_search_at_song_end_is_empty(self):
        self.assertEqual(self.library.search(source={"song_id": 1, "start": 2, "end": 2},
                                             direction="after"), [])

    def test_search_can_exclude_texts(self):
        hits = self.library.search("静かな夜を歩いた", exclude=["静かな夜を歩いた"])
        self.assertFalse(any("静かな夜を歩いた" in hit.text for hit in hits))

    def test_refine_can_pick_a_candidate(self):
        seen = []

        def refine(options):
            seen.extend(options)
            return {"choices": [{"id": 2, "count": 1}]}

        hits = self.library.search(author="作者甲", refine=refine)
        self.assertEqual([item["title"] for item in seen], ["夜", "別"])
        self.assertEqual([hit.title for hit in hits], ["別"])

    def test_step_sends_a_lyric_through_both_stages(self):
        called = []

        def model(system, prompt, temperature, schema):
            called.append(system)
            if system == prompts.PLAN:
                return {"queries": [["静かな夜"]], "author": "", "title": ""}
            self.assertIn("候选", prompt)
            return {"choices": [{"id": 1, "count": 1}]}

        result = self.library.step([{"role": "user", "text": "同じ歌詞の行"}],
                                   settings=Settings(model=model))
        self.assertEqual(called, [prompts.PLAN, prompts.SELECT])
        self.assertEqual(result[-1]["status"], "selected")
        self.assertEqual(len(result[-1]["fragments"]), 1)

    def test_step_does_not_repeat_the_previous_reply(self):
        result = self.library.step([
            {"role": "assistant", "text": "静かな夜を歩いた", "source": None},
            {"role": "user", "text": "夜を歩いた"},
        ])
        self.assertNotIn("静かな夜を歩いた", result[-1]["text"])

    def test_step_requires_a_final_user_turn(self):
        with self.assertRaises(ValueError):
            self.library.step([{"role": "assistant", "text": "hello"}])

    def test_step_without_a_model_is_deterministic(self):
        result = self.library.step([{"role": "user", "text": "夜"}])
        self.assertEqual(result[-1]["status"], "selected")
        self.assertTrue(result[-1]["fragments"])
        self.assertTrue(all(fragment["start_ms"] is not None for fragment in result[-1]["fragments"]))

    def test_the_same_title_is_quoted_once(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "dup.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
            for author, text in [("a", "ひとつめ"), ("b", "ふたつめ")]:
                song_id = db.execute("INSERT INTO songs(title,author) VALUES('同名',?)",
                                     (author,)).lastrowid
                db.execute("INSERT INTO lyric_lines(song_id,position,stanza,text) "
                           "VALUES(?,1,0,?)", (song_id, text))
        build_index(path)
        with closing(Library(path)) as library:
            passages = library.search(author="a", refine=lambda options: {
                "choices": [{"id": 1, "count": 1}, {"id": 2, "count": 1}]})
        self.assertEqual([passage.author for passage in passages], ["a"])

    def test_missing_index_is_reported(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "raw.sqlite3"
        build(path)
        with sqlite3.connect(path) as db:
            db.executescript("DROP TABLE phrases; DROP TABLE grams; DROP TABLE lyric_fts;")
        with self.assertRaises(MissingIndexError):
            Library(path)

    def test_build_index_stores_phrases_grams_and_norms(self):
        with sqlite3.connect(self.path) as db:
            phrases = db.execute("select count(*) from phrases").fetchone()[0]
            grams = db.execute("select count(*) from grams").fetchone()[0]
            unset = db.execute("select count(*) from phrases where norm <= 0").fetchone()[0]
            fts = db.execute("select count(*) from lyric_fts").fetchone()[0]
        self.assertGreater(phrases, 0)
        self.assertGreater(grams, 0)
        self.assertEqual(unset, 0)
        self.assertEqual(fts, phrases)

    def test_model_failures_are_reported(self):
        def model(*_args):
            raise RuntimeError("down")

        with self.assertRaises(ModelError):
            self.library.step([{"role": "user", "text": "同じ歌詞の行"}],
                              settings=Settings(model=model))


if __name__ == "__main__":
    unittest.main()
