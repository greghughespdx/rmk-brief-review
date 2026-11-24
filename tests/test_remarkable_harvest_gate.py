"""mc-9jd4t: a registered Decisions document is never abandoned unharvested.

2026-09-13 the reader penned seven answers on the day's document and then deleted
it, because the writing was unreadable. The delivery script could not fetch
the registered document to archive it and aborted with a generic "could not
archive". His answers were in the tablet's trash, recorded nowhere, and the
rebuilt document was about to re-ask four of them.

These tests pin the stop: no harvest receipt, or one that does not belong to
the document being replaced, means the push refuses.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "remarkable" / "check_harvest_receipt.py"
DEVICE = "/Briefs/decisions-2026-09-13"
OLD_PAGE_ID = "decisions-2026-09-13-daf4148376c1"
NEW_PAGE_ID = "decisions-2026-09-13-66a96fa52fe5"


def write(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def registry(page_id: str = OLD_PAGE_ID) -> dict:
    return {"pages": {DEVICE: {"page_id": page_id}}}


def receipt(**over) -> dict:
    base = {
        "tool": "rmw-decisions-harvest-v1",
        "page_id": OLD_PAGE_ID,
        "ink_hash": "9da2d8def2507ff13cfa9bf5064710b8b43a51d8046565cb1e1f5b928b9c03da",
    }
    base.update(over)
    return base


class HarvestReceiptGate(unittest.TestCase):
    def check(self, receipt_payload, registry_payload=None, exists=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = root / "harvest-receipt.json"
            if exists:
                write(r, receipt_payload)
            g = write(root / "registry.json", registry_payload or registry())
            return subprocess.run(
                [sys.executable, str(CHECKER), str(r), str(g), DEVICE]
            ).returncode

    def test_valid_receipt_for_the_replaced_document_passes(self):
        self.assertEqual(0, self.check(receipt()))

    def test_missing_receipt_refuses(self):
        self.assertEqual(1, self.check(None, exists=False))

    def test_receipt_for_a_different_document_refuses(self):
        # The exact 2026-09-13 trap: a receipt naming the NEW page id proves
        # nothing about the old document whose ink is at risk.
        self.assertEqual(1, self.check(receipt(page_id=NEW_PAGE_ID)))

    def test_receipt_with_no_ink_hash_refuses(self):
        self.assertEqual(1, self.check(receipt(ink_hash="")))

    def test_receipt_from_another_tool_refuses(self):
        self.assertEqual(1, self.check(receipt(tool="something-else")))

    def test_unreadable_receipt_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = root / "harvest-receipt.json"
            r.write_text("{not json", encoding="utf-8")
            g = write(root / "registry.json", registry())
            code = subprocess.run(
                [sys.executable, str(CHECKER), str(r), str(g), DEVICE]
            ).returncode
        self.assertEqual(1, code)

    def test_device_path_absent_from_the_registry_refuses(self):
        self.assertEqual(1, self.check(receipt(), registry_payload={"pages": {}}))


class DeliveryScriptNamesTheHarvestStop(unittest.TestCase):
    def test_script_tells_the_operator_how_to_harvest(self):
        body = (
            ROOT / "remarkable" / "replace-decisions-document.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("HARVEST FIRST", body)
        # The generic message that hid the problem must be gone.
        self.assertNotIn(
            'echo "ABORT: could not archive the registered Decisions document"',
            body,
        )
        # It must point at the trash, which is where the ink actually was.
        self.assertIn("rmapi ls /trash", body)


if __name__ == "__main__":
    unittest.main()
