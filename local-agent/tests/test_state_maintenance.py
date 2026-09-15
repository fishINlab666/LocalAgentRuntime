import importlib
import importlib.util
import unittest


class StateMaintenanceGateTests(unittest.TestCase):
    def setUp(self):
        specification = importlib.util.find_spec("local_agent.state_maintenance")
        self.assertIsNotNone(specification, "state maintenance gate is not implemented")
        module = importlib.import_module("local_agent.state_maintenance")
        self.StateBusy = module.StateBusy
        self.gate = module.StateMaintenanceGate()

    def assert_busy(self, action):
        with self.assertRaisesRegex(self.StateBusy, "STATE_BUSY") as caught:
            action()
        self.assertEqual(caught.exception.code, "STATE_BUSY")

    def enter_maintenance(self):
        with self.gate.maintenance():
            pass

    def test_same_activity_cannot_start_twice_but_different_activities_coexist(self):
        first = self.gate.start("session", "same")
        self.assert_busy(lambda: self.gate.start("session", "same"))
        second = self.gate.start("import", "same")
        third = self.gate.start("session", "other")
        first.close()
        second.close()
        third.close()

    def test_active_activity_blocks_maintenance_until_lease_closes(self):
        lease = self.gate.start("session", "one")
        self.assert_busy(self.enter_maintenance)
        lease.close()
        with self.gate.maintenance():
            self.assert_busy(lambda: self.gate.start("session", "two"))

    def test_maintenance_is_exclusive_and_releases_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with self.gate.maintenance():
                self.assert_busy(self.enter_maintenance)
                raise RuntimeError("stop")
        lease = self.gate.start("session", "after")
        lease.close()

    def test_lease_close_is_idempotent(self):
        lease = self.gate.start("session", "one")
        lease.close()
        lease.close()
        replacement = self.gate.start("session", "one")
        replacement.close()

    def test_invalid_activity_keys_are_rejected_without_polluting_gate(self):
        for kind, identity in (("", "id"), ("kind", ""), (None, "id"), ("kind", 1)):
            with self.subTest(kind=kind, identity=identity):
                with self.assertRaises(ValueError):
                    self.gate.start(kind, identity)
        with self.gate.maintenance():
            pass


if __name__ == "__main__":
    unittest.main()
