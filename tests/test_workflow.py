import json
import subprocess
import sys
import unittest

from token_budget.workflow import WorkflowError, build_receipt, plan_project

NOW = 1000


def request(**overrides):
    value = {"prompt": "Build the requested tool", "now": NOW, "max_snapshot_age_seconds": 60,
             "max_demand_age_seconds": 60, "max_calibration_age_seconds": 60,
             "scope": {"deliverables": ["tool"], "exclusions": [], "acceptance_checks": ["tests pass"],
                       "phases": [{"name": "Build", "objective": "Implement tool", "model": "codex", "token_budget": 10000}]},
             "usage": {"primary": {"used_percent": 20, "resets_at": 1300, "source": "provider", "observed_at": 1000},
                       "secondary": {"used_percent": 30, "resets_at": 2000, "source": "provider", "observed_at": 1000}},
             "caps": {"primary_points": 10, "secondary_points": 10},
             "required_primary_points": {"points": 2, "source": "budget", "observed_at": 1000, "calibrated_at": 1000},
             "required_secondary_points": {"points": 1, "source": "budget", "observed_at": 1000, "calibrated_at": 1000},
             "max_task_tokens": 20000, "max_session_tokens": 20000,
             "session_start_epochs": [1000]}
    value.update(overrides)
    return value


class WorkflowTests(unittest.TestCase):
    def test_missing_semantic_scope_needs_clarification(self):
        result = plan_project({"prompt": "Build", "now": NOW})
        self.assertEqual(result["status"], "needs_clarification")

    def test_one_session_validates_scope_and_budget(self):
        result = plan_project(request())
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["session_count"], 1)
        self.assertEqual(result["checkpoints"], [])
        self.assertEqual(result["phases"][0]["session"], 1)

    def test_multi_session_covers_all_sessions_and_uses_reset_timing(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": "A", "objective": "a", "model": "codex", "token_budget": 10000},
                            {"name": "B", "objective": "b", "model": "codex", "token_budget": 10000}]
        result = plan_project(request(scope=scope, max_task_tokens=20000, max_session_tokens=10000,
                                      session_count=2, session_start_epochs=[1000, 1100]))
        self.assertEqual(result["status"], "feasible")
        self.assertEqual({p["session"] for p in result["phases"]}, {1, 2})
        self.assertEqual(result["checkpoints"][0]["next_session_start_epoch"], 1100)

    def test_auto_sessions_use_ordered_greedy_phase_packing(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": name, "objective": name, "model": "m", "token_budget": 6}
                            for name in ("A", "B", "C")]
        result = plan_project(request(scope=scope, max_task_tokens=18, max_session_tokens=10,
                                      session_start_epochs=[1000, 1100, 1200]))
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["session_count"], 3)
        self.assertEqual([phase["session"] for phase in result["phases"]], [1, 2, 3])

    def test_rejects_after_reset_and_missing_demands(self):
        with self.assertRaises(WorkflowError): plan_project(request(now=1300))
        with self.assertRaises(WorkflowError): plan_project(request(required_primary_points=None))

    def test_rejects_stale_and_future_snapshots(self):
        usage = request()["usage"]
        usage["primary"]["observed_at"] = 998
        with self.assertRaises(WorkflowError): plan_project(request(max_snapshot_age_seconds=1, usage=usage))
        usage = request()["usage"]
        usage["primary"]["observed_at"] = 1001
        with self.assertRaises(WorkflowError): plan_project(request(usage=usage))

    def test_fractional_positive_demand_and_weight(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": "A", "objective": "a", "model": "m", "weight": .25}]
        demand = {"points": .5, "source": "budget", "observed_at": 1000, "calibrated_at": 1000}
        result = plan_project(request(scope=scope, required_primary_points=demand,
                                      required_secondary_points={**demand}))
        self.assertEqual(result["status"], "feasible")

    def test_demand_freshness_and_calibration_are_independent(self):
        demand = {"points": .5, "source": "budget", "observed_at": 900, "calibrated_at": 1000}
        with self.assertRaises(WorkflowError): plan_project(request(required_primary_points=demand))

    def test_extreme_finite_weights_do_not_overflow(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": "A", "objective": "a", "model": "m", "weight": 1e308},
                            {"name": "B", "objective": "b", "model": "m", "weight": 1e308}]
        result = plan_project(request(scope=scope, max_task_tokens=10))
        self.assertEqual(sum(p["token_budget"] for p in result["phases"]), 10)

    def test_weighted_allocation_reserves_each_phase(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": "A", "objective": "a", "model": "m", "weight": 1},
                            {"name": "B", "objective": "b", "model": "m", "weight": 3}]
        result = plan_project(request(scope=scope, max_task_tokens=5))
        self.assertEqual([p["token_budget"] for p in result["phases"]], [2, 3])

    def test_cross_reset_returns_refresh_checkpoint_with_all_phases(self):
        scope = request()["scope"]
        scope["phases"] = [{"name": "A", "objective": "a", "model": "m", "token_budget": 5000},
                            {"name": "B", "objective": "b", "model": "m", "token_budget": 5000}]
        result = plan_project(request(scope=scope, max_task_tokens=10000, max_session_tokens=5000,
                                      session_count=2, session_start_epochs=[1000, 1400]))
        self.assertEqual(result["status"], "needs_refresh_checkpoint")
        self.assertEqual(len(result["phases"]), 2)

    def test_empty_receipt_is_rejected(self):
        with self.assertRaises(WorkflowError): build_receipt(plan_project(request()), model_usage=[], final_usage={}, now=NOW)

    def test_infeasible_phase_total_and_session_limit(self):
        scope = request()["scope"]
        scope["phases"][0]["token_budget"] = 30000
        result = plan_project(request(scope=scope, max_task_tokens=20000))
        self.assertEqual(result["status"], "infeasible")

    def test_receipt_preserves_cache_and_partial_unknowns(self):
        plan = plan_project(request())
        usage = [{"model": "codex", "work_summary": "Implemented and tested the tool.", "quality": "partial",
                  "unknown_fields": ["reasoning_tokens", "total_tokens"], "input_tokens": 10, "cached_input_tokens": 2,
                  "cache_creation_input_tokens": 1, "output_tokens": 4, "reasoning_tokens": None, "total_tokens": None}]
        final = {"primary": {"used_percent": 25, "resets_at": 1300, "source": "provider", "observed_at": 1000},
                 "secondary": {"used_percent": 31, "resets_at": 2000, "source": "provider", "observed_at": 1000}}
        receipt = build_receipt(plan, model_usage=usage, final_usage=final, now=NOW)
        self.assertEqual(receipt["aggregate_status"], "partial")
        self.assertIsNone(receipt["aggregate_factual_tokens"]["total_tokens"])
        self.assertEqual(receipt["model_usage"][0]["cache_creation_input_tokens"], 1)
        with self.assertRaises(WorkflowError):
            build_receipt(plan, model_usage=[{**usage[0], "unknown_fields": []}], final_usage=final, now=NOW)

    def test_final_snapshot_freshness_and_partial_numeric_status(self):
        plan = plan_project(request())
        final = {"primary": {"used_percent": 25, "resets_at": 1300, "source": "provider", "observed_at": 900},
                 "secondary": {"used_percent": 31, "resets_at": 2000, "source": "provider", "observed_at": 1000}}
        entry = {"model": "codex", "work_summary": "Completed the implementation and checks.", "quality": "partial",
                 "unknown_fields": [], "input_tokens": 1, "cached_input_tokens": 0, "cache_creation_input_tokens": 0,
                 "output_tokens": 1, "reasoning_tokens": 0, "total_tokens": 2}
        with self.assertRaises(WorkflowError): build_receipt(plan, model_usage=[entry], final_usage=final, now=NOW)
        final["primary"]["observed_at"] = 1000
        receipt = build_receipt(plan, model_usage=[entry], final_usage=final, now=NOW)
        self.assertEqual(receipt["aggregate_status"], "partial")

    def test_cli_plan_json(self):
        payload = json.dumps(request())
        result = subprocess.run([sys.executable, "-m", "token_budget.cli", "plan"], input=payload,
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], "feasible")


if __name__ == "__main__": unittest.main()
