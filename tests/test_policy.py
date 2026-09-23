import json
import subprocess
import sys
import unittest

from token_budget.policy import evaluate_policy


class PolicyTests(unittest.TestCase):
    def test_provider_band_boundaries(self):
        self.assertEqual(evaluate_policy(49.99, 0, 100).mode, "normal")
        self.assertEqual(evaluate_policy(50, 0, 100).mode, "efficient")
        self.assertEqual(evaluate_policy(69.99, 0, 100).mode, "efficient")
        self.assertEqual(evaluate_policy(70, 0, 100).mode, "conserve")
        self.assertEqual(evaluate_policy(79.99, 0, 100).mode, "conserve")
        self.assertEqual(evaluate_policy(80, 0, 100).mode, "stop")

    def test_task_cap_can_select_stricter_mode(self):
        decision = evaluate_policy(20, 75, 100)
        self.assertEqual(decision.mode, "conserve")
        self.assertIn("task usage", decision.reason)

    def test_stop_override_is_explicit_and_scoped(self):
        decision = evaluate_policy(80, 80, 100, explicit_override=True)
        self.assertEqual(decision.mode, "stop")
        self.assertTrue(decision.explicit_override)
        self.assertEqual(decision.allowed_behaviors[0], "make one explicitly authorized model call")

    def test_non_boolean_overrides_are_rejected(self):
        for value in ("true", 1, 0, [], {"override": True}, None):
            with self.assertRaises(ValueError):
                evaluate_policy(80, 80, 100, explicit_override=value)

    def test_cli_policy_outputs_json(self):
        result = subprocess.run(
            [sys.executable, "-m", "token_budget.cli", "policy",
             "--provider-used-percent", "50", "--task-observed-tokens", "1", "--task-cap", "100"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout)["mode"], "efficient")


if __name__ == "__main__":
    unittest.main()
