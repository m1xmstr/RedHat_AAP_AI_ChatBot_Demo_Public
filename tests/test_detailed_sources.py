import datetime
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from edu_retrieval import PassageIndex, make_chunks
from public_crawl import PublicSite
from public_documents import calendar_text


class DetailedSourcesTests(unittest.TestCase):
    def test_calendar_preserves_date_and_cohort_and_ignores_alarm_text(self):
        data = ("BEGIN:VCALENDAR\r\nX-WR-CALNAME:Spring 2027 Registrar Calendar\r\nBEGIN:VEVENT\r\n"
                "DTSTART;VALUE=DATE:20261113\r\nSUMMARY:Open Enrollment Be\r\n gins at 12:00 am\r\n"
                "BEGIN:VALARM\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        title, text = calendar_text(data)
        self.assertEqual("2026-11-13 — Open Enrollment Begins at 12:00 am", text)
        row = {"title": title, "text": text, "url": "https://school.example/spring.ics", "format": "ical"}
        index = PassageIndex(make_chunks([row]))
        answer, _ = index.evidence("When can I register for classes?", "School")
        self.assertIn("2026-11-13", answer["answer"])
        self.assertEqual(index.search("When is enrollment for spring 2028?"), [])

    def test_bookstore_count_needs_explicit_total(self):
        rows = [{"title": "Campus shopping", "url": "https://school.example/stores/",
                 "text": "Our College Bookshop is located inside Student Stores. Textbooks are available here."},
                {"title": "Campus construction", "url": "https://school.example/updates/",
                 "text": "Three hallways on the campus will receive new lights."}]
        answer, matches = PassageIndex(make_chunks(rows)).evidence("How many bookstores are on campus?", "School")
        self.assertIn("do not establish the total", answer["answer"])
        self.assertIn("College Bookshop", answer["answer"])
        self.assertEqual([rows[0]["url"]], [r["url"] for r in matches])

    def test_https_uses_original_canonical_host_header(self):
        site = PublicSite("school.example")
        response = MagicMock(status=200)
        response.getheader.return_value = "text/plain"
        response.read.return_value = b"test"
        response.headers.get_content_charset.return_value = "utf-8"
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch("public_crawl.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
                patch("public_crawl.socket.socket"), patch.object(site.tls, "wrap_socket"), \
                patch("public_crawl.http.client.HTTPConnection", return_value=conn):
            site.request("https://dining.school.example/locations/")
        self.assertEqual(conn.request.call_args.kwargs["headers"]["Host"], "dining.school.example")


if __name__ == "__main__":
    unittest.main()
