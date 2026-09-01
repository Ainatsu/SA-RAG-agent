"""SessionManager 的离线测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))

from session import SessionManager  # noqa: E402


class SessionManagerTests(unittest.TestCase):
    def test_payloads_are_copied_and_snapshots_are_isolated(self) -> None:
        manager = SessionManager()
        state = manager.update_state_payload('{"x": 1, "health": 100}')
        pickups = manager.update_pickups_payload(
            '[{"kind":"armor","x":1,"y":2,"z":3}]')
        state["x"] = 99
        pickups[0]["x"] = 99

        self.assertEqual(manager.state_snapshot()["x"], 1)
        self.assertEqual(manager.pickups_snapshot()[0]["x"], 1)

    def test_invalid_payload_does_not_replace_existing_snapshot(self) -> None:
        manager = SessionManager()
        manager.update_state_payload('{"health": 100}')
        manager.update_pickups_payload('[]')
        self.assertIsNone(manager.update_state_payload("bad"))
        self.assertIsNone(manager.update_pickups_payload("{}"))
        self.assertEqual(manager.state_snapshot(), {"health": 100})
        self.assertEqual(manager.pickups_snapshot(), [])

    def test_clear_removes_session_data(self) -> None:
        manager = SessionManager()
        manager.update_state_payload('{"health": 100}')
        manager.update_pickups_payload('[{"kind":"health"}]')
        manager.clear()
        self.assertIsNone(manager.state_snapshot())
        self.assertEqual(manager.pickups_snapshot(), [])


if __name__ == "__main__":
    unittest.main()
