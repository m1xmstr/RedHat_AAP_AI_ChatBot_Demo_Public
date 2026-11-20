import json
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from edu_ai_demo_builder import write_app
from edu_retrieval import make_chunks
from public_crawl import FetchError, PageParser, PublicSite


class PublicCorpusTest(unittest.TestCase):
    def test_main_body_and_table_survive_navigation(self):
        parser = PageParser("https://school.example/")
        parser.feed("<main><nav>Account login</nav><h1>Registration dates</h1>"
                    "<p>Enrollment for visiting students in the fall term opens after the registrar reviews the application.</p>"
                    "<table><tr><td>Visiting applications</td><td>August 16</td></tr></table></main>")
        self.assertIn("Visiting applications August 16", parser.content())
        self.assertNotIn("Account login", parser.content())

    def test_private_resolution_rejected_before_connect(self):
        site = PublicSite("school.example")
        record = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        with patch("public_crawl.socket.getaddrinfo", return_value=[record]), patch("public_crawl.socket.socket") as connect:
            with self.assertRaisesRegex(FetchError, "public addresses"):
                site.request("https://school.example/")
            connect.assert_not_called()

    def test_offsite_redirect_is_never_fetched(self):
        site = PublicSite("school.example")
        with patch.object(site, "request", return_value=(302, "https://unrelated.example/", "")) as request:
            with self.assertRaisesRegex(FetchError, "leaves"):
                site.fetch("https://school.example/robots.txt", robots_request=True)
            self.assertEqual(1, request.call_count)

    def test_answer_uses_downloaded_passages_and_reports_model_failure(self):
        pages = [{"url": "https://school.example/tuition/", "title": "Tuition deadlines",
                  "text": "Visiting students must submit tuition deposits by August 16."}]
        rows = list(make_chunks(pages))
        profile = {"customer_name": "Example School", "customer_slug": "example-school"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_app(root, profile, {"pages": pages}, rows)
            app = runpy.run_path(str(root / "app" / "app.py"))
            self.assertEqual([json.loads(line) for line in (root / "app" / "rag.jsonl").read_text().splitlines()], rows)
            with patch.dict(os.environ, {"DEMO_LLM_ENDPOINT": "https://model.example/", "DEMO_AI_PROVIDER": "openshift_ai"}):
                with patch("urllib.request.urlopen") as send:
                    unsupported = app["answer"]("Interstellar blueberries?")
                    self.assertFalse(unsupported["sources"])
                    send.assert_not_called()
                    send.return_value.__enter__.return_value.read.return_value = b'{"choices":[{"message":{"content":"August 16."}}]}'
                    result = app["answer"]("When are tuition deposits due?")
                    self.assertEqual(result["provider"], "openshift_ai")
                    self.assertIn("August 16", json.loads(send.call_args.args[0].data)["messages"][1]["content"])
                    send.side_effect = TimeoutError()
                    fallback = app["answer"]("When are tuition deposits due?")
                    self.assertEqual(fallback["provider"], "evidence_fallback")
                    self.assertIn("August 16", fallback["answer"])


if __name__ == "__main__":
    unittest.main()
