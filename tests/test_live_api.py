"""Live contract tests of the DTF API (network!). Off by default:

    DTF_LIVE=1 python -m unittest tests.test_live_api            # all checks
    DTF_LIVE=1 DTF_USER=someone python -m unittest tests.test_live_api

Each test is one check from dtf_backup/checkapi.py; a failure message says what broke and which module to adapt
(see docs/DTF_API.md).
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dtf_backup.checkapi import CHECKS, run_checks  # noqa: E402

LIVE = os.environ.get("DTF_LIVE") == "1"


@unittest.skipUnless(LIVE, "сетевые тесты выключены: задайте DTF_LIVE=1")
class LiveApiTest(unittest.TestCase):
    results: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        # checks share discovered data (profile -> posts -> a post with comments ...), so run them once in order
        cls.results = {r["key"]: r for r in run_checks(os.environ.get("DTF_USER", "petra"))}


def _make(key: str, name: str):
    def test(self: LiveApiTest) -> None:
        r = self.results.get(key)
        self.assertIsNotNone(r, f"проверка {key} не запускалась")
        if r.get("warn"):
            self.skipTest(f"⚠ {r['detail']}")
        self.assertTrue(r["ok"], f"{name}: {r['detail']}\n→ смотреть: {r.get('hint')}")
    test.__doc__ = name
    return test


for _c in CHECKS:
    setattr(LiveApiTest, f"test_{_c.key}", _make(_c.key, _c.name))


if __name__ == "__main__":
    unittest.main()
