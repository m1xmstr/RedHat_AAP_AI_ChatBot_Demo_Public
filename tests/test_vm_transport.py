"""Check safe archive cleanup without sending any cluster mutations."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vm"))
import transport


class TransportOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.labels = {transport.VM_LABEL: "university-a", transport.COMPONENT: "vm-image"}
        self.notes = {"edu-ai-demo/customer-name": "University A"}
        self.old = {"kind": "Pod", "metadata": {
            "name": "university-a-image", "uid": "original", "labels": self.labels, "annotations": self.notes,
        }}

    def test_foreign_archive_survives(self):
        with patch.object(transport, "oc") as call:
            with self.assertRaisesRegex(ValueError, "Unowned"):
                transport.delete_owned("pool", "Pod", self.old, self.labels, {"edu-ai-demo/customer-name": "University B"})
            call.assert_not_called()

    def test_replacement_is_never_deleted(self):
        with patch.object(transport, "oc") as call, patch.object(transport, "get", return_value={"metadata": {"uid": "new"}}):
            transport.delete_owned("pool", "Pod", self.old, self.labels, self.notes)
            call.assert_called_once()
            self.assertEqual(call.call_args.args[1]["preconditions"], {"uid": "original"})


if __name__ == "__main__":
    unittest.main()
