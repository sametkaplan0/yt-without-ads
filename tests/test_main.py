import unittest
from unittest.mock import patch

from main import app, media_url_is_safe, normalize_query, parse_quality, valid_video_id


class ValidationTests(unittest.TestCase):
    def test_video_ids_are_strictly_validated(self):
        self.assertTrue(valid_video_id("dQw4w9WgXcQ"))
        self.assertFalse(valid_video_id("short"))
        self.assertFalse(valid_video_id("dQw4w9WgXcQ/extra"))

    def test_quality_and_query_validation(self):
        self.assertEqual(parse_quality("720"), 720)
        self.assertIsNone(parse_quality(""))
        with self.assertRaises(ValueError):
            parse_quality("999")
        self.assertEqual(normalize_query("  hello   world "), "hello world")
        with self.assertRaises(ValueError):
            normalize_query("x" * 201)

    def test_media_host_allowlist(self):
        self.assertTrue(media_url_is_safe("https://r1---sn.googlevideo.com/video"))
        self.assertFalse(media_url_is_safe("http://r1---sn.googlevideo.com/video"))
        self.assertFalse(media_url_is_safe("https://example.com/video"))


class RouteSmokeTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_home_and_invalid_routes(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"YouTube", home.data)
        self.assertEqual(self.client.get("/v?id=not-an-id").status_code, 400)
        self.assertEqual(self.client.get("/stream/not-an-id").status_code, 400)

    @patch("main.search_videos", return_value=[])
    def test_search_route_uses_normalized_query(self, search_videos):
        response = self.client.get("/search?q=  hello%20world  ")
        self.assertEqual(response.status_code, 200)
        search_videos.assert_called_once_with("hello world")


if __name__ == "__main__":
    unittest.main()
