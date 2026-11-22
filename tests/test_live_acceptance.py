"""Use synthetic campus responses to check the customer-facing acceptance gate."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

path = Path(__file__).resolve().parents[1] / "roles/edu_ai_demo_builder/files/check_live.py"
spec = importlib.util.spec_from_file_location("acceptance", path)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.registration = "https://registrar.unc.edu/fixture"
        self.bookstore = "https://book.unc.edu/fixture"
        self.dining = "https://dining.unc.edu/fixture"
        self.places = [{"name":"Example Bookstore","category":"bookstore","source_url":self.bookstore,"address":"Pine Hall"},
                       {"name":"Campus Bistro","category":"dining","source_url":self.dining,"address":"Elm Hall"}]
        self.rows = [{"id":"r", "url":self.registration, "title":"Spring 2030 registration",
                      "text":"2029-11-15: Undergraduate enrollment for Spring 2030 opens. Check your enrollment appointment in ConnectCarolina."},
                     {"id":"b", "url":self.bookstore, "title":"Campus shops",
                      "text":"Example Bookstore in Pine Hall sells textbooks."},
                     {"id":"d", "url":self.dining, "title":"Dining",
                      "text":"Campus Bistro serves lunch in Elm Hall."}]
        self.manifest = {"source_count":3, "passage_count":3, "customer":"Fixture","places":self.places,
                         "sources":[{"url":c["url"],"passages":1} for c in self.rows]}
        self.profile = {"page_count":3,"chunk_count":3,"customer_name":"Fixture","customer_website":"https://unc.edu",
                        "sample_question":"Tell me about campus","provider":"openshift_ai"}
        self.answers = {check.UNC_QUESTIONS[0]:("Undergraduate enrollment for Spring 2030 opens November 15, 2029.",self.registration),
                        check.UNC_QUESTIONS[1]:("The total is not specified. Example Bookstore is in Pine Hall.",self.bookstore),
                        check.UNC_QUESTIONS[2]:("Campus Bistro is in Elm Hall.",self.dining)}

    def request(self, url, data=None, build=None):
        if url.endswith("/rag.jsonl"):
            return "\n".join(map(json.dumps,self.rows)).encode()
        if url.endswith("/api/sources"):
            return json.dumps(self.manifest).encode()
        if url.endswith("/api/profile"):
            return json.dumps(self.profile).encode()
        answer, source = self.answers.get(data["question"], ("The campus offers lunch.",self.dining))
        return json.dumps({"confidence":"evidence_found", "provider":self.profile["provider"], "answer":answer,
                           "sources":[{"url":source}]}).encode()

    def run_check(self):
        with patch.object(check,"request",side_effect=self.request):
            return check.check("https://example.test","Fixture",self.profile["provider"])

    def test_both_ai_backends_rehearse_actual_questions(self):
        for backend in ("openshift_ai","rhel_vm"):
            self.profile["provider"] = backend
            self.assertEqual([r["question"] for r in self.run_check()["rehearsal"]],list(check.UNC_QUESTIONS))

    def test_stale_router_response_is_retried_before_using_its_corpus(self):
        stale, current = MagicMock(), MagicMock()
        stale.headers = {}
        current.headers = {"X-Demo-Build": "sample-new"}
        current.read.return_value = b'current source'
        replies = [MagicMock(), MagicMock()]
        for reply, value in zip(replies, [stale, current]):
            reply.__enter__.return_value = value
        with patch("urllib.request.urlopen", side_effect=replies) as fetch, patch.object(check.time, "sleep"):
            self.assertEqual(check.request("https://school.example/rag.jsonl", build="sample-new"), b'current source')
        self.assertEqual(fetch.call_count, 2)
        stale.read.assert_not_called()

    def test_new_build_rejects_an_html_response_at_the_icon_url(self):
        def missing_icon(url, data=None, build=None):
            if url.endswith("/favicon.ico"):
                return b"<html><body>Demo chat</body></html>"
            return self.request(url, data, build)

        with patch.object(check, "request", side_effect=missing_icon):
            with self.assertRaisesRegex(ValueError, "favicon"):
                check.check("https://example.test", "Fixture", "openshift_ai", build="new")

    def test_rejects_wrong_date_or_missing_semester(self):
        self.rows[0]["text"] += "\n2029-11-20: Registration ends for Spring 2030."
        for answer in ("Spring 2030 enrollment starts November 19.", "Enrollment starts November 15.",
                       "For Spring 2030 registration starts 11/20/2029."):
            self.answers[check.UNC_QUESTIONS[0]] = (answer, self.registration)
            with self.assertRaisesRegex(ValueError, "When is enrollment"):
                self.run_check()

    def test_rejects_unsubstantiated_total_or_unrelated_food_source(self):
        self.answers[check.UNC_QUESTIONS[1]] = ("There are two bookstores on campus, including Example Bookstore.",self.bookstore)
        with self.assertRaisesRegex(ValueError, "How many bookstores"):
            self.run_check()
        self.answers[check.UNC_QUESTIONS[1]] = ("A total is not specified, but Example Bookstore sells textbooks.",self.bookstore)
        self.answers[check.UNC_QUESTIONS[2]] = ("Campus Bistro is in Elm Hall.", self.registration)
        with self.assertRaisesRegex(ValueError, "Where can I eat"):
            self.run_check()

    def test_generic_customers_still_validate_every_source_count(self):
        self.profile["customer_website"] = "https://other-school.example"
        self.answers.clear()
        self.assertNotIn("rehearsal",self.run_check())
        self.manifest["sources"][2]["passages"] = 2
        with self.assertRaisesRegex(ValueError, "counts disagree"):
            self.run_check()


if __name__ == "__main__":
    unittest.main()
