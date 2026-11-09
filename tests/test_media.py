import unittest
import urllib.parse

from botaloid_lite.media import (LyricMeta, MediaLink, QuerierError, Resolver,
                                 VocadbQuerier, available)


def pv(video_id, service="Youtube", pv_type="Original",
       published="2018-02-27", disabled=False):
    return {"pvId": video_id, "service": service, "pvType": pv_type,
            "publishDate": published, "disabled": disabled}


def song(song_id, name, artists, pvs, song_type="Original"):
    return {"id": song_id, "name": name, "defaultName": name,
            "artistString": artists, "songType": song_type, "pvs": pvs}


class FakeFetch:
    def __init__(self, payloads=None):
        self.payloads = payloads or {}
        self.urls = []

    def __call__(self, url, timeout=15.0):
        self.urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
        return self.payloads.get(query, {"items": []})

    @property
    def queries(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
                for url in self.urls]


class LyricMetaTests(unittest.TestCase):
    def test_coerce_accepts_mapping_object_and_string(self):
        self.assertEqual(LyricMeta.coerce({"title": "a", "author": "b", "start_ms": "1500"}),
                         LyricMeta("a", "b", 1500))
        self.assertEqual(LyricMeta.coerce("a"), LyricMeta("a", "", None))

        class Fragment:
            title = "a"
            author = "b"
            start_ms = 2000

        self.assertEqual(LyricMeta.coerce(Fragment()), LyricMeta("a", "b", 2000))

    def test_start_seconds_floors_and_ignores_missing(self):
        self.assertEqual(LyricMeta("a", start_ms=65250).start_seconds, 65)
        self.assertIsNone(LyricMeta("a").start_seconds)


class VocadbTests(unittest.TestCase):
    def find(self, payloads, meta, **kwargs):
        fetch = FakeFetch(payloads)
        return VocadbQuerier(fetch=fetch, **kwargs).find(meta), fetch

    def test_returns_youtube_link_at_the_lyric(self):
        link, fetch = self.find({"ロキ": {"items": [
            song(1, "ロキ", "みきとP feat. 鏡音リン",
                 [pv("Xg-qfsKN2_E", published="2018-02-27")]),
        ]}}, {"title": "ロキ", "author": "みきとP / 鏡音リン", "start_ms": 65250})
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=Xg-qfsKN2_E&t=65")
        self.assertEqual((link.provider, link.service), ("vocadb", "youtube"))
        self.assertEqual((link.start_seconds, link.video_id), (65, "Xg-qfsKN2_E"))
        self.assertEqual(link.to_dict()["start_seconds"], 65)
        request = fetch.urls[0]
        self.assertIn("fields=PVs%2CArtists", request)
        self.assertIn("sort=FavoritedTimes", request)

    def test_prefers_the_original_entry_over_a_cover(self):
        link, _ = self.find({"曲": {"items": [
            song(1, "曲", "p feat. 初音ミク", [pv("cover")], song_type="Cover"),
            song(2, "曲", "p feat. 初音ミク", [pv("original")], song_type="Original"),
        ]}}, {"title": "曲", "author": "p / 初音ミク"})
        self.assertEqual(link.video_id, "original")

    def test_omits_the_timestamp_when_the_position_is_unknown(self):
        link, _ = self.find({"ロキ": {"items": [
            song(1, "ロキ", "みきとP", [pv("Xg-qfsKN2_E")]),
        ]}}, {"title": "ロキ"})
        self.assertEqual(link.url, "https://www.youtube.com/watch?v=Xg-qfsKN2_E")
        self.assertIsNone(link.start_seconds)

    def test_prefers_the_entry_with_the_matching_artist(self):
        link, _ = self.find({"曲": {"items": [
            song(1, "曲", "Someone Else feat. 初音ミク", [pv("wrong")]),
            song(2, "曲", "wotaku feat. 初音ミク", [pv("right")]),
        ]}}, {"title": "曲", "author": "wotaku / 初音ミク"})
        self.assertEqual(link.video_id, "right")

    def test_prefers_the_official_earliest_youtube_pv(self):
        link, _ = self.find({"曲": {"items": [
            song(1, "曲", "p feat. 初音ミク", [
                pv("reprint", pv_type="Other", published="2020-01-01"),
                pv("official", pv_type="Original", published="2018-01-01"),
                pv("sm1", service="NicoNicoDouga"),
                pv("disabled", disabled=True),
            ]),
        ]}}, {"title": "曲"})
        self.assertEqual(link.video_id, "official")

    def test_ignores_songs_without_a_playable_youtube_pv(self):
        link, _ = self.find({"曲": {"items": [
            song(1, "曲", "a", [pv("sm1", service="NicoNicoDouga")]),
            song(2, "曲", "b", [pv("gone", disabled=True)]),
        ]}}, {"title": "曲"})
        self.assertIsNone(link)

    def test_rejects_unrelated_search_results(self):
        link, _ = self.find({"ライカ": {"items": [
            song(1, "Aces High ～蒼穹のストライカー～", "ばにら feat. アサ音さな", [pv("x")]),
        ]}}, {"title": "ライカ", "author": "雨漏りP / 初音ミク"})
        self.assertIsNone(link)

    def test_strips_trailing_brackets_before_searching(self):
        link, fetch = self.find({"ピアノフィッシュ": {"items": [
            song(1, "ピアノフィッシュ", "いよわ feat. 初音ミク", [pv("yt")]),
        ]}}, {"title": "ピアノフィッシュ (feat. 初音ミク & v_flower)", "author": "いよわ"})
        self.assertTrue(link)
        self.assertEqual(fetch.queries, ["ピアノフィッシュ"])

    def test_falls_back_to_the_full_title(self):
        link, fetch = self.find({"曲名 (Remix)": {"items": [
            song(1, "曲名 (Remix)", "p feat. 初音ミク", [pv("yt")]),
        ]}}, {"title": "曲名 (Remix)"})
        self.assertTrue(link)
        self.assertEqual(fetch.queries, ["曲名", "曲名 (Remix)"])

    def test_caches_identical_requests(self):
        fetch = FakeFetch({"曲": {"items": [song(1, "曲", "p", [pv("yt")])]}})
        querier = VocadbQuerier(fetch=fetch)
        querier.find({"title": "曲", "start_ms": 1000})
        querier.find({"title": "曲", "start_ms": 2000})
        self.assertEqual(len(fetch.urls), 1)

    def test_wraps_transport_failures(self):
        def broken(url, timeout=15.0):
            raise OSError("offline")

        with self.assertRaises(QuerierError):
            VocadbQuerier(fetch=broken).find({"title": "曲"})


class ResolverTests(unittest.TestCase):
    class Good:
        name = "good"

        def find(self, meta):
            return MediaLink("https://example.com/" + meta.title, "good", "test",
                             meta.start_seconds)

    class Boom:
        name = "boom"

        def find(self, meta):
            raise QuerierError("down")

    def test_vocadb_is_registered(self):
        self.assertIn("vocadb", available())
        self.assertEqual(Resolver().providers, ["vocadb"])

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(ValueError):
            Resolver(["nope"])

    def test_skips_a_failing_querier(self):
        link = Resolver([self.Boom(), self.Good()]).find({"title": "t", "start_ms": 3000})
        self.assertEqual(link.url, "https://example.com/t")
        self.assertEqual(link.start_seconds, 3)

    def test_find_all_collects_every_link(self):
        links = Resolver([self.Good(), self.Good()]).find_all({"title": "t"})
        self.assertEqual([link.url for link in links], ["https://example.com/t"] * 2)


if __name__ == "__main__":
    unittest.main()
