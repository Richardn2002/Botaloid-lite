import unittest

from botaloid_lite.text import ngrams, normalized, phrases, split_line


class TextTests(unittest.TestCase):
    def test_normalized_ignores_width_case_and_punctuation(self):
        self.assertEqual(normalized("ＡＢ　c。"), "abc")
        self.assertEqual(normalized("夜を、歩く！"), "夜を歩く")

    def test_ngrams(self):
        self.assertEqual(ngrams("あいう", 1), ["あ", "い", "う"])
        self.assertEqual(ngrams("あいう", 2), ["あい", "いう"])
        self.assertEqual(ngrams("あいう", 3), ["あいう"])
        self.assertEqual(ngrams("あ", 2), [])

    def test_split_line_on_sentences_and_clauses(self):
        self.assertEqual(split_line("一。二！三？"), ["一。", "二！", "三？"])
        self.assertEqual(split_line("期待なんてされてない　できない　いらない"),
                         ["期待なんてされてない", "できない", "いらない"])
        self.assertEqual(split_line(""), [])
        self.assertEqual(split_line("！！！"), [])

    def test_phrases_merge_a_line_break_inside_a_clause(self):
        rows = [(1, 0, "夜の"), (2, 0, "街を歩く"), (3, 1, "朝")]
        self.assertEqual(phrases(rows), [
            (1, 2, "夜の街を歩く", 0),
            (3, 3, "朝", 1),
        ])

    def test_phrases_do_not_merge_across_stanzas(self):
        rows = [(1, 0, "夜の"), (2, 1, "街")]
        self.assertEqual(phrases(rows), [(1, 1, "夜の", 0), (2, 2, "街", 1)])

    def test_phrases_keep_sentence_boundaries(self):
        rows = [(1, 0, "終わり。次へ")]
        self.assertEqual(phrases(rows), [(1, 1, "終わり。", 0), (1, 1, "次へ", 0)])


if __name__ == "__main__":
    unittest.main()
