import runpy
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from edu_ai_demo_builder import write_app


class ChatActivityTests(unittest.TestCase):
    def test_sessions_are_scoped_and_validation_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_app(root, {"customer_name": "School"}, {"pages": []}, [])
            event = runpy.run_path(str(root / "app/app.py"))["activity_event"]
            nonce = "abcd" * 8
            headers = {"X-Demo-Session": nonce, "X-Forwarded-For": "private", "User-Agent": "private"}
            row = event(headers, "School")
            self.assertEqual(set(row), {"v", "event", "source", "session"})
            self.assertEqual(row["source"], "browser")
            self.assertNotEqual(row["session"], nonce)
            self.assertEqual(event(headers, "School"), row)
            self.assertNotEqual(event(headers, "Other")["session"], row["session"])
            self.assertEqual(event({**headers, "X-Demo-Client": "validation"}, "School"),
                             {"event": "edu_demo_chat", "v": 1, "source": "validation"})
            self.assertEqual(event({"X-Demo-Session": "private"}, "School"),
                             {"event": "edu_demo_chat", "v": 1, "source": "api"})
