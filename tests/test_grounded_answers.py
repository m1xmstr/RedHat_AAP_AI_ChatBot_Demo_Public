"""Verify constrained answer rendering and honest provider attribution offline."""
import io
import json
import urllib.error
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from edu_ai_demo_builder import write_app
from edu_retrieval import explicit_book_count, make_chunks


class GroundedAnswerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.page = {"title": "Campus bookstore", "url": "https://school.example/store/",
                     "text": "Raven Bookshop is located inside Student Stores in the North Hall."}
        calendar = {"title": "Autumn 2027 Registration Calendar", "url": "https://school.example/fall.ics",
                    "text": "2027-06-12 — Open enrollment begins for existing students.", "format": "ical"}
        pages = [self.page, calendar]
        self.place = {"name": "Raven Bookshop", "category": "bookstore", "source_url": self.page["url"],
                      "source_title": self.page["title"], "address": "North Hall", "maps_url": "https://maps.google.com/"}
        write_app(self.root, {"customer_name": "Example School"}, {"pages": pages}, list(make_chunks(pages)))
        (self.root / "app" / "sources.json").write_text(json.dumps({"places": [self.place]}))
        self.answer = runpy.run_path(str(self.root / "app" / "app.py"))["answer"]

    def model_answer(self, question, text, *, retry=None, unsupported=False):
        with patch.dict(os.environ, {"DEMO_LLM_ENDPOINT": "https://model.example/", "DEMO_AI_PROVIDER": "rhel_vm"}):
            with patch("urllib.request.urlopen") as req:
                def completion(text):
                    return io.BytesIO(json.dumps({"choices": [{"message": {"content": text}}]}).encode())
                replies = [completion(text)]
                if unsupported:
                    replies.append(urllib.error.HTTPError("https://model.example/", 400, "unsupported response format", {}, None))
                if retry is not None:
                    replies.append(completion(retry))
                req.side_effect = replies
                result = self.answer(question)
                self.assertEqual(req.call_count, len(replies))
                self.requests = [json.loads(call.args[0].data) for call in req.call_args_list]
                return result

    def test_count_refers_to_a_selected_record_and_not_an_invented_total(self):
        result = self.model_answer("How many bookstores are on campus?", '{"choices":[1],"answer":"five bookstores"}')
        self.assertEqual((result["provider"], result["method"]), ("rhel_vm", "source_selection"))
        self.assertIn("do not establish", result["answer"])
        self.assertIn("Raven Bookshop", result["answer"])
        self.assertNotIn("five bookstores", result["answer"])
        self.assertEqual(result["places"], [self.place])
        self.assertEqual(result["sources"][0]["url"], self.place["source_url"])

    def test_invalid_selection_returns_cited_retrieval(self):
        result = self.model_answer("How many bookstores are on campus?", '{"choices":[999]}', retry='{"choices":[999]}')
        self.assertEqual((result["provider"], result["method"]), ("evidence", "source_retrieval"))
        self.assertTrue(result["sources"])

    def test_invalid_repeated_choices_retry_with_a_bounded_schema(self):
        result = self.model_answer("How many bookstores are on campus?", '{"choices":[1,1]}', retry='{"choices":[1]}')
        self.assertEqual(result["provider"], "rhel_vm")
        self.assertEqual(result["places"], [self.place])
        self.assertNotIn("response_format", self.requests[0])
        choices = self.requests[1]["response_format"]["json_schema"]["schema"]["properties"]["choices"]
        self.assertEqual((choices["minItems"], choices["maxItems"], choices["items"]["enum"]), (1, 1, [1]))

    def test_unsupported_schema_uses_a_genuine_plain_rerank(self):
        result = self.model_answer("How many bookstores are on campus?", "not valid", unsupported=True, retry="1")
        self.assertEqual(result["method"], "source_selection")
        self.assertNotIn("response_format", self.requests[2])
        self.assertNotEqual(self.requests[0]["messages"][0], self.requests[2]["messages"][0])

    def test_selected_calendar_date_keeps_the_term_and_group(self):
        result = self.model_answer("When is enrollment?", '{"choices":[1]}')
        self.assertIn("Autumn 2027 Registration Calendar", result["answer"])
        self.assertIn("2027-06-12", result["answer"])
        self.assertIn("existing students", result["answer"])
        self.assertEqual(result["method"], "source_selection")

    def test_exact_total_requires_an_inventory_statement_about_the_institution(self):
        self.assertTrue(explicit_book_count("The university operates two bookstores."))
        self.assertFalse(explicit_book_count("I visited two bookstores near campus."))
        self.assertFalse(explicit_book_count("There are at least three bookstores on campus."))


if __name__ == "__main__":
    unittest.main()
