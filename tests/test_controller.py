import os
import tempfile
import unittest

from token_budget.controller import Controller


class ControllerFixture(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(delete=False)
        self.file.close()
        self.controller = Controller(self.file.name)
        self.window = {"short": {"baseline_used_micropct": 0, "cap_micropct": 100,
                                  "current_used_micropct": 0, "reset_id": "r1",
                                  "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}}
        self.snapshot = {"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}}
        self.controller.create_project("p", task_cap=100, session_cap=100,
                                       coordinator_reserve=10, windows=self.window, now=900)

    def tearDown(self):
        self.controller.close()
        os.unlink(self.file.name)

class ControllerTests(ControllerFixture):
    def test_allow_and_reservation_exhaustion(self):
        result = self.controller.reserve_call("p", call_id="a", model="m", purpose="test",
                                              token_reservation=20, window_reservations={"short": 10}, snapshots=self.snapshot, now=900)
        self.assertEqual(result["decision"], "ALLOW")
        denied = self.controller.reserve_call("p", call_id="b", model="m", purpose="test",
                                              token_reservation=81, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.assertEqual(denied["reason_code"], "SESSION_CAP_EXHAUSTED")

    def test_fifty_percent_barrier_and_revision(self):
        self.controller.reserve_call("p", call_id="a", model="m", purpose="test", token_reservation=50, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "a", now=900)
        self.controller.reconcile("p", "a", actual_tokens=50, actual_windows={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["a"]}}, now=901)
        after_a = {"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["a"]}}
        denied = self.controller.reserve_call("p", call_id="b", model="m", purpose="test", token_reservation=1, window_reservations={"short": 0}, snapshots=after_a, now=901)
        self.assertEqual(denied["reason_code"], "REPLAN_REQUIRED")
        self.controller.record_plan_revision("p", {
            "deliverables": ["finish test"],
            "calls": [{"id": "b", "purpose": "test", "deliverable": "finish test", "tokens": 1, "windows": {"short": 0}}],
            "budgets": {"tokens": 1, "windows": {"short": 0}},
            "acceptance_checks": ["test completes"],
        }, now=901)
        self.assertEqual(self.controller.reserve_call("p", call_id="b", model="m", purpose="test", token_reservation=1, window_reservations={"short": 0}, snapshots=after_a, now=901)["decision"], "ALLOW")

    def test_stale_reset_and_overrun_fail_closed(self):
        stale = {"short": {"current_used_micropct": 0, "reset_id": "r1", "observed_at": 1, "covered_call_ids": []}}
        self.assertEqual(self.controller.reserve_call("p", call_id="s", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots=stale, now=900)["reason_code"], "STALE_USAGE")
        mismatch = {"short": {"current_used_micropct": 0, "reset_id": "r2", "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}}
        self.assertEqual(self.controller.reserve_call("p", call_id="r", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots=mismatch, now=900)["reason_code"], "RESET_MISMATCH")
        self.controller.reserve_call("p", call_id="o", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.assertTrue(self.controller.launch("p", "o", now=900))
        self.assertTrue(self.controller.reconcile("p", "o", actual_tokens=2, actual_windows={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["o"]}}, now=901)["overrun"])

    def test_receipt_and_hash_chain(self):
        self.assertTrue(self.controller.verify_chain())
        receipt = self.controller.receipt("p", now=900)
        self.assertEqual(receipt["enforcement_mode"], "managed-wrapper")
        self.assertIn("windows", receipt)

    def test_wrapper_denial_does_not_launch(self):
        result = self.controller.run_wrapped("p", call_id="x", model="m", purpose="x", command=["/bin/echo", "ok"], token_reservation=1, window_reservations={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "wrong", "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}}, now=900)
        self.assertEqual(result["decision"], "DENY")


if __name__ == "__main__":
    unittest.main()

class ControllerAdversarialTests(ControllerFixture):
    def test_per_window_reservation_is_required_and_exact(self):
        missing = self.controller.reserve_call("p", call_id="missing", model="m", purpose="x", token_reservation=1, now=900)
        self.assertEqual(missing["reason_code"], "INVALID_RESERVATION")
        extra = self.controller.reserve_call("p", call_id="extra", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0, "long": 0}, snapshots=self.snapshot, now=900)
        self.assertEqual(extra["reason_code"], "INVALID_RESERVATION")

    def test_malformed_regressing_expired_and_missing_snapshots_fail_closed(self):
        cases = [
            ({"current_used_micropct": -1, "reset_id": "r1", "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}, "MALFORMED_USAGE"),
            ({"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 900, "covered_call_ids": []}, "USAGE_REGRESSION"),
            ({"current_used_micropct": 0, "reset_id": "r1", "resets_at": 899, "observed_at": 900, "covered_call_ids": []}, "RESET_MISMATCH"),
        ]
        for idx, (snap, expected) in enumerate(cases):
            if idx == 1:
                self.controller.db.execute("UPDATE windows SET current=10 WHERE project_id='p' AND name='short'")
            got = self.controller.reserve_call("p", call_id=f"bad{idx}", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots={"short": snap}, now=900)
            self.assertEqual(got["reason_code"], expected)

    def test_concurrent_reservations_serialize_against_all_caps(self):
        import threading
        from token_budget.controller import Controller
        other = Controller(self.file.name)
        barrier = threading.Barrier(3)
        results = []
        def reserve(controller, call_id):
            barrier.wait()
            results.append(controller.reserve_call("p", call_id=call_id, model="m", purpose="x", token_reservation=55, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)["decision"])
        a = threading.Thread(target=reserve, args=(self.controller, "c1"))
        b = threading.Thread(target=reserve, args=(other, "c2"))
        a.start(); b.start(); barrier.wait(); a.join(); b.join(); other.close()
        self.assertEqual(results.count("ALLOW"), 1)

    def test_reconciliation_is_one_time_atomic_and_violation_monotonic(self):
        self.controller.reserve_call("p", call_id="once", model="m", purpose="x", token_reservation=1, window_reservations={"short": 2}, snapshots=self.snapshot, now=900)
        factual = {"short": {"current_used_micropct": 5, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["once"]}}
        self.controller.launch("p", "once", now=900)
        self.controller.reconcile("p", "once", actual_tokens=2, actual_windows={"short": 3}, snapshots=factual, now=901)
        with self.assertRaises(ValueError): self.controller.reconcile("p", "once", actual_tokens=0, actual_windows={"short": 0}, snapshots=factual, now=901)
        self.assertTrue(self.controller.status("p", now=901)["violated"])
        self.assertTrue(self.controller.receipt("p", now=901)["violations"]["tokens"])
        self.assertTrue(self.controller.receipt("p", now=901)["violations"]["windows"])


class ControllerBarrierTests(ControllerFixture):
    def _complete_plan(self, tokens, windows):
        return {"deliverables": ["finish remaining work"],
                "calls": [{"id": "next", "purpose": "remaining work", "deliverable": "finish remaining work", "tokens": tokens, "windows": windows}],
                "budgets": {"tokens": tokens, "windows": windows},
                "acceptance_checks": ["remaining work passes"]}

    def test_task_barrier_acknowledgment_is_monotonic(self):
        self.controller.reserve_call("p", call_id="cross", model="m", purpose="x", token_reservation=50, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "cross", now=900)
        self.controller.reconcile("p", "cross", actual_tokens=50, actual_windows={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["cross"]}}, now=901)
        self.controller.record_plan_revision("p", self._complete_plan(20, {"short": 0}), now=901)
        result = self.controller.reserve_call("p", call_id="later", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["cross"]}}, now=901)
        self.assertEqual(result["decision"], "ALLOW")
        self.controller.launch("p", "later", now=901)
        self.controller.reconcile("p", "later", actual_tokens=1, actual_windows={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 902, "covered_call_ids": ["cross", "later"]}}, now=902)
        self.assertFalse(self.controller.status("p", now=902)["replan_required"])

    def test_provider_window_crossing_50_percent_triggers_plan_barrier(self):
        self.controller.reserve_call("p", call_id="half-window", model="m", purpose="x", token_reservation=1, window_reservations={"short": 50}, snapshots=self.snapshot, now=900)
        self.assertEqual(self.controller.reserve_call("p", call_id="blocked", model="m", purpose="x", token_reservation=1, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)["reason_code"], "REPLAN_REQUIRED")

    def test_task_80_percent_projected_reservation_fails_closed(self):
        result = self.controller.reserve_call("p", call_id="near-stop", model="m", purpose="x", token_reservation=72, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.assertEqual(result["reason_code"], "UNIVERSAL_80_PERCENT_STOP")

    def test_window_80_percent_projected_reservation_fails_closed(self):
        result = self.controller.reserve_call("p", call_id="near-stop", model="m", purpose="x", token_reservation=1, window_reservations={"short": 80}, snapshots=self.snapshot, now=900)
        self.assertEqual(result["reason_code"], "UNIVERSAL_80_PERCENT_STOP")

    def test_remaining_plan_must_cover_demand_and_fit_capacity(self):
        self.controller.reserve_call("p", call_id="cross", model="m", purpose="x", token_reservation=50, window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "cross", now=900)
        self.controller.reconcile("p", "cross", actual_tokens=50, actual_windows={"short": 0}, snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["cross"]}}, now=901)
        invalid = {"deliverables": ["work"], "calls": [{"id": "x", "purpose": "p", "deliverable": "work", "tokens": 22, "windows": {"short": 0}}], "budgets": {"tokens": 22, "windows": {"short": 0}}, "acceptance_checks": ["done"]}
        with self.assertRaises(ValueError): self.controller.record_plan_revision("p", invalid, now=902)
        invalid_window = self._complete_plan(20, {"short": 80})
        with self.assertRaises(ValueError): self.controller.record_plan_revision("p", invalid_window, now=902)
        valid = self._complete_plan(20, {"short": 0})
        self.assertEqual(self.controller.record_plan_revision("p", valid, now=902)["revision"], 1)

class ControllerSecondAuditTests(ControllerFixture):
    def test_cumulative_coverage_keeps_nine_calls_from_being_reused(self):
        windows = {"short": {"baseline_used_micropct": 0, "cap_micropct": 1000,
                             "current_used_micropct": 0, "reset_id": "r1",
                             "resets_at": 5000, "observed_at": 900}}
        self.controller.create_project("nine", task_cap=1000, session_cap=1000,
                                       coordinator_reserve=10, windows=windows, now=900)
        covered = []
        for index in range(1, 10):
            call_id = f"c{index}"
            fact = {"short": {"current_used_micropct": index - 1, "reset_id": "r1",
                               "resets_at": 5000, "observed_at": 900 + index,
                               "covered_call_ids": list(covered)}}
            result = self.controller.reserve_call("nine", call_id=call_id, model="m", purpose="x",
                token_reservation=1, window_reservations={"short": 1}, snapshots=fact, now=900 + index)
            self.assertEqual(result["decision"], "ALLOW")
            self.assertTrue(self.controller.launch("nine", call_id, now=900 + index))
            covered.append(call_id)
            fact["short"].update(current_used_micropct=index, observed_at=901 + index,
                                 covered_call_ids=list(covered))
            self.controller.reconcile("nine", call_id, actual_tokens=1, actual_windows={"short": 1},
                                      snapshots=fact, now=901 + index)

        repeated = {"short": {"current_used_micropct": 9, "reset_id": "r1", "resets_at": 5000,
                               "observed_at": 920, "covered_call_ids": covered}}
        result = self.controller.reserve_call("nine", call_id="c10", model="m", purpose="x",
            token_reservation=1, window_reservations={"short": 1}, snapshots=repeated, now=920)
        self.assertEqual(result["decision"], "ALLOW")
        self.assertTrue(self.controller.launch("nine", "c10", now=920))
        with self.assertRaisesRegex(ValueError, "COVERAGE_REGRESSION"):
            self.controller.reconcile("nine", "c10", actual_tokens=1, actual_windows={"short": 1},
                snapshots={"short": {**repeated["short"], "observed_at": 921,
                                      "covered_call_ids": covered[1:] + ["c10"]}}, now=921)
        with self.assertRaisesRegex(ValueError, "INSUFFICIENT_USAGE_COVERAGE"):
            self.controller.reconcile("nine", "c10", actual_tokens=1, actual_windows={"short": 1},
                snapshots={"short": {**repeated["short"], "observed_at": 921,
                                      "covered_call_ids": covered + ["c10"]}}, now=921)

    def test_denied_barrier_snapshot_is_saved_for_revision_and_refresh(self):
        project_windows = {"short": {"baseline_used_micropct": 0, "cap_micropct": 100,
                                     "current_used_micropct": 75, "reset_id": "s",
                                     "resets_at": 5000, "observed_at": 1000}}
        self.controller.create_project("p75", task_cap=1000, session_cap=1000,
            coordinator_reserve=10, windows=project_windows, now=1000)
        snapshot = {"short": {"current_used_micropct": 75, "reset_id": "s", "resets_at": 5000,
                              "observed_at": 1000, "covered_call_ids": []}}
        denied = self.controller.reserve_call("p75", call_id="next", model="m", purpose="x",
            token_reservation=1, window_reservations={"short": 0}, snapshots=snapshot, now=1000)
        self.assertEqual(denied["reason_code"], "REPLAN_REQUIRED")
        self.assertEqual(self.controller.status("p75", now=1000)["windows"][0]["current_used_micropct"], 75)
        refreshed = {"short": {**snapshot["short"], "observed_at": 1001}}
        self.controller.refresh_snapshots("p75", refreshed, now=1001)
        plan = {"deliverables": ["work"],
                "calls": [{"id": "next", "purpose": "work", "deliverable": "work",
                           "tokens": 1, "windows": {"short": 70}}],
                "budgets": {"tokens": 1, "windows": {"short": 70}},
                "acceptance_checks": ["work complete"]}
        with self.assertRaisesRegex(ValueError, "80%"):
            self.controller.record_plan_revision("p75", plan, now=1001)
        self.assertTrue(self.controller.status("p75", now=1001)["replan_required"])

    def test_launch_revalidates_violation_and_expiry_and_keeps_claim_liability(self):
        # The first launched call records a violation while a second reservation waits.
        for cid, reserve in (("overrun", 1), ("waiting", 2)):
            self.controller.reserve_call("p", call_id=cid, model="m", purpose="x",
                token_reservation=1, window_reservations={"short": reserve}, snapshots=self.snapshot, now=900)
        self.assertTrue(self.controller.launch("p", "overrun", now=900))
        self.controller.reconcile("p", "overrun", actual_tokens=2, actual_windows={"short": 2},
            snapshots={"short": {"current_used_micropct": 2, "reset_id": "r1", "resets_at": 1000,
                                  "observed_at": 901, "covered_call_ids": ["overrun"]}}, now=901)
        self.assertFalse(self.controller.launch("p", "waiting", now=901))
        self.assertEqual(self.controller.db.execute("SELECT status FROM calls WHERE call_id='waiting'").fetchone()[0], "reserved")

        # On a clean project the reservation itself is counted exactly once at claim.
        self.controller.create_project("clean", task_cap=100, session_cap=100, coordinator_reserve=10,
                                       windows=self.window, now=900)
        self.controller.reserve_call("clean", call_id="own", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 10}, snapshots=self.snapshot, now=900)
        self.assertTrue(self.controller.launch("clean", "own", now=900))
        self.assertEqual(self.controller.status("clean", now=900)["windows"][0]["reserved_micropct"], 10)
        self.controller.reserve_call("clean", call_id="expired", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 1}, snapshots=self.snapshot, now=900)
        self.assertFalse(self.controller.launch("clean", "expired", now=1000))
        self.assertEqual(self.controller.db.execute("SELECT status FROM calls WHERE project_id='clean' AND call_id='expired'").fetchone()[0], "reserved")

    def test_launch_failure_release_requires_private_claim_and_running_call_keeps_liability(self):
        self.controller.reserve_call("p", call_id="held", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 4}, snapshots=self.snapshot, now=900)
        claim = self.controller._claim_launch("p", "held", now=900)
        self.assertIsNotNone(claim)
        self.assertFalse(self.controller.fail_launch("p", "held", "not-the-claim", "fake"))
        self.assertEqual(self.controller.status("p", now=900)["windows"][0]["reserved_micropct"], 4)
        self.assertFalse(self.controller.cancel_unlaunched("p", "held", reason="launch_failed"))
        self.assertEqual(self.controller.db.execute("SELECT status FROM calls WHERE call_id='held'").fetchone()[0], "launched")
        self.assertTrue(self.controller.fail_launch("p", "held", claim, "spawn failed"))
        self.assertEqual(self.controller.db.execute("SELECT status FROM calls WHERE call_id='held'").fetchone()[0], "launch_failed")

    def test_create_config_validation_is_atomic(self):
        bad_windows = {
            "short": self.window["short"],
            "long": {"baseline_used_micropct": 5, "cap_micropct": 100,
                     "current_used_micropct": 4, "reset_id": "r2",
                     "resets_at": 1000, "observed_at": 900},
        }
        with self.assertRaises(ValueError):
            self.controller.create_project("partial", task_cap=100, session_cap=100,
                                           coordinator_reserve=10, windows=bad_windows, now=900)
        with self.assertRaises(ValueError): self.controller.status("partial", now=900)
        self.assertFalse(self.controller.db.execute("SELECT 1 FROM events WHERE project_id='partial'").fetchone())

    def test_cached_usage_expires_and_creation_requires_unexpired_facts(self):
        expired = {"short": {**self.window["short"], "resets_at": 900}}
        with self.assertRaises(ValueError):
            self.controller.create_project("expired", task_cap=100, session_cap=100,
                                           coordinator_reserve=10, windows=expired, now=900)
        result = self.controller.reserve_call("p", call_id="after-reset", model="m", purpose="x",
                                              token_reservation=1, window_reservations={"short": 0}, now=1000)
        self.assertEqual(result["reason_code"], "RESET_MISMATCH")

    def test_coverage_prevents_double_count_and_uncovered_liability_is_kept(self):
        self.controller.reserve_call("p", call_id="a", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 10}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "a", now=900)
        self.controller.reconcile("p", "a", actual_tokens=1, actual_windows={"short": 4},
            snapshots={"short": {"current_used_micropct": 7, "reset_id": "r1", "resets_at": 1000,
                                  "observed_at": 901, "covered_call_ids": ["a"]}}, now=901)
        self.controller.reserve_call("p", call_id="b", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 10},
                                     snapshots={"short": {"current_used_micropct": 7, "reset_id": "r1", "resets_at": 1000,
                                                           "observed_at": 902, "covered_call_ids": ["a"]}}, now=902)
        blocked = self.controller.reserve_call("p", call_id="c", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 63},
                                     snapshots={"short": {"current_used_micropct": 7, "reset_id": "r1", "resets_at": 1000,
                                                           "observed_at": 902, "covered_call_ids": ["a"]}}, now=902)
        self.assertEqual(blocked["reason_code"], "UNIVERSAL_80_PERCENT_STOP")
        contradictory = self.controller.reserve_call("p", call_id="d", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 0},
                                     snapshots={"short": {"current_used_micropct": 7, "reset_id": "r1", "resets_at": 1000,
                                                           "observed_at": 902, "covered_call_ids": ["b"]}}, now=902)
        self.assertEqual(contradictory["reason_code"], "CONTRADICTORY_USAGE")

    def test_unlaunched_calls_cannot_reconcile_and_launch_is_single_claim(self):
        self.controller.reserve_call("p", call_id="claim", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 3}, snapshots=self.snapshot, now=900)
        factual = {"short": {"current_used_micropct": 1, "reset_id": "r1", "resets_at": 1000,
                              "observed_at": 901, "covered_call_ids": ["claim"]}}
        with self.assertRaises(ValueError):
            self.controller.reconcile("p", "claim", actual_tokens=1, actual_windows={"short": 1}, snapshots=factual, now=901)
        self.assertTrue(self.controller.cancel_unlaunched("p", "claim"))
        self.assertEqual(self.controller.status("p", now=901)["windows"][0]["reserved_micropct"], 0)
        self.controller.reserve_call("p", call_id="owned", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 3}, snapshots=self.snapshot, now=900)
        self.assertTrue(self.controller.launch("p", "owned", now=900))
        self.assertFalse(self.controller.cancel_unlaunched("p", "owned"))
        self.assertFalse(self.controller.launch("p", "owned", now=900))
        self.assertEqual(self.controller.status("p", now=900)["windows"][0]["reserved_micropct"], 3)

    def test_per_window_actuals_do_not_cancel_in_receipt_and_available_tokens_are_enforceable(self):
        windows = {
            "short": {"baseline_used_micropct": 0, "cap_micropct": 100, "current_used_micropct": 0,
                      "reset_id": "s", "resets_at": 1000, "observed_at": 900},
            "long": {"baseline_used_micropct": 0, "cap_micropct": 100, "current_used_micropct": 0,
                     "reset_id": "l", "resets_at": 1000, "observed_at": 900},
        }
        self.controller.create_project("multi", task_cap=100, session_cap=100, coordinator_reserve=10, windows=windows, now=900)
        snapshots = {n: {"current_used_micropct": 0, "reset_id": w["reset_id"], "resets_at": 1000,
                         "observed_at": 900, "covered_call_ids": []} for n, w in windows.items()}
        self.controller.reserve_call("multi", call_id="multi-call", model="m", purpose="x", token_reservation=2,
                                      window_reservations={"short": 2, "long": 5}, snapshots=snapshots, now=900)
        before = self.controller.receipt("multi", now=900)
        self.assertEqual(before["available_tokens"], 69)
        self.controller.launch("multi", "multi-call", now=900)
        factual = {"short": {"current_used_micropct": 5, "reset_id": "s", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["multi-call"]},
                   "long": {"current_used_micropct": 0, "reset_id": "l", "resets_at": 1000, "observed_at": 901, "covered_call_ids": ["multi-call"]}}
        self.controller.reconcile("multi", "multi-call", actual_tokens=1, actual_windows={"short": 5, "long": 0}, snapshots=factual, now=901)
        receipt = self.controller.receipt("multi", now=901)
        self.assertEqual(receipt["window_overruns"]["multi-call"], {"short": 5})
        self.assertEqual(receipt["window_actuals"]["multi-call"], {"long": 0, "short": 5})
        self.assertEqual(receipt["available_tokens"], 0)

    def test_concurrent_create_events_preserve_global_hash_predecessor(self):
        import threading
        from token_budget.controller import Controller
        other = Controller(self.file.name)
        barrier = threading.Barrier(3)
        errors = []
        def create(controller, project):
            try:
                barrier.wait()
                controller.create_project(project, task_cap=100, session_cap=100, coordinator_reserve=10,
                                          windows=self.window, now=900)
            except Exception as exc: errors.append(exc)
        a = threading.Thread(target=create, args=(self.controller, "parallel-a"))
        b = threading.Thread(target=create, args=(other, "parallel-b"))
        a.start(); b.start(); barrier.wait(); a.join(); b.join()
        self.assertEqual(errors, [])
        self.assertTrue(self.controller.verify_chain())
        other.close()

    def test_legacy_unknown_accounting_is_quarantined_and_migration_is_idempotent(self):
        from token_budget.controller import Controller
        self.controller.db.execute("UPDATE projects SET consumed_tokens=55 WHERE project_id='p'")
        self.controller.db.execute("DELETE FROM schema_meta WHERE key='schema_version'")
        self.controller.db.execute("INSERT INTO calls(project_id,call_id,model,purpose,token_reservation,micropct_reservation,status,created_at,window_reservations_json) VALUES('p','legacy','m','x',1,1,'reserved',900,'{}')")
        self.controller.db.commit()
        self.controller.close()
        migrated = Controller(self.file.name)
        migrated_status = migrated.status("p", now=900)
        self.assertTrue(migrated_status["quarantined"])
        self.assertTrue(migrated_status["replan_required"])
        self.assertEqual(migrated.db.execute("SELECT COUNT(*) FROM barrier_state WHERE project_id='p'").fetchone()[0], 2)
        self.assertEqual(migrated.reserve_call("p", call_id="blocked", model="m", purpose="x", token_reservation=1,
                                               window_reservations={"short": 0}, snapshots=self.snapshot, now=900)["reason_code"], "PROJECT_QUARANTINED")
        self.assertFalse(migrated.launch("p", "legacy"))
        migrated.close()
        migrated = Controller(self.file.name)
        self.assertTrue(migrated.status("p", now=900)["quarantined"])
        migrated.close()


class ControllerRecoveryTests(ControllerFixture):
    def test_reconciliation_requires_current_fact_to_cover_reported_actual(self):
        self.controller.reserve_call("p", call_id="undercovered", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 5}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "undercovered", now=900)
        with self.assertRaises(ValueError):
            self.controller.reconcile("p", "undercovered", actual_tokens=1, actual_windows={"short": 5},
                snapshots={"short": {"current_used_micropct": 4, "reset_id": "r1", "resets_at": 1000,
                                      "observed_at": 901, "covered_call_ids": ["undercovered"]}}, now=901)
        self.assertEqual(self.controller.status("p", now=901)["windows"][0]["reserved_micropct"], 5)

    def test_wrapper_launch_failure_releases_claim_atomically(self):
        with self.assertRaises(FileNotFoundError):
            self.controller.run_wrapped("p", call_id="missing-executable", model="m", purpose="x",
                command=["/not/a/real/executable"], token_reservation=1, window_reservations={"short": 3},
                snapshots=self.snapshot, now=900)
        row = self.controller.db.execute("SELECT status FROM calls WHERE project_id='p' AND call_id='missing-executable'").fetchone()
        self.assertEqual(row[0], "launch_failed")
        self.assertEqual(self.controller.status("p", now=900)["windows"][0]["reserved_micropct"], 0)
        self.assertTrue(self.controller.verify_chain())

    def test_concurrent_launch_has_one_owner_and_valid_event_chain(self):
        import threading
        from token_budget.controller import Controller
        self.controller.reserve_call("p", call_id="one-owner", model="m", purpose="x", token_reservation=1,
                                     window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        other = Controller(self.file.name)
        gate = threading.Barrier(3); claims = []
        def claim(controller):
            gate.wait(); claims.append(controller.launch("p", "one-owner", now=900))
        a = threading.Thread(target=claim, args=(self.controller,)); b = threading.Thread(target=claim, args=(other,))
        a.start(); b.start(); gate.wait(); a.join(); b.join()
        self.assertEqual(claims.count(True), 1)
        self.assertTrue(self.controller.verify_chain())
        other.close()

class ControllerConcurrencyTests(ControllerFixture):
    def test_concurrent_reconciliation_releases_once_and_commits_one_event(self):
        import threading
        from token_budget.controller import Controller
        self.controller.reserve_call("p", call_id="race-reconcile", model="m", purpose="x", token_reservation=2,
                                     window_reservations={"short": 2}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "race-reconcile", now=900)
        other = Controller(self.file.name)
        gate = threading.Barrier(3); outcomes = []
        factual = {"short": {"current_used_micropct": 1, "reset_id": "r1", "resets_at": 1000,
                             "observed_at": 901, "covered_call_ids": ["race-reconcile"]}}
        def finish(controller):
            gate.wait()
            try:
                controller.reconcile("p", "race-reconcile", actual_tokens=1, actual_windows={"short": 1}, snapshots=factual, now=901)
                outcomes.append("ok")
            except ValueError: outcomes.append("already-final")
        a = threading.Thread(target=finish, args=(self.controller,)); b = threading.Thread(target=finish, args=(other,))
        a.start(); b.start(); gate.wait(); a.join(); b.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(self.controller.status("p", now=901)["consumed_tokens"], 1)
        self.assertEqual(self.controller.status("p", now=901)["windows"][0]["reserved_micropct"], 0)
        self.assertEqual(self.controller.db.execute("SELECT COUNT(*) FROM events WHERE project_id='p' AND kind='call_reconciled'").fetchone()[0], 1)
        self.assertTrue(self.controller.verify_chain())
        other.close()


class ControllerPlanFreshnessTests(ControllerFixture):
    def test_plan_revision_cannot_acknowledge_barrier_with_expired_window_facts(self):
        self.controller.reserve_call("p", call_id="threshold", model="m", purpose="x", token_reservation=50,
                                     window_reservations={"short": 0}, snapshots=self.snapshot, now=900)
        self.controller.launch("p", "threshold", now=900)
        self.controller.reconcile("p", "threshold", actual_tokens=50, actual_windows={"short": 0},
            snapshots={"short": {"current_used_micropct": 0, "reset_id": "r1", "resets_at": 1000,
                                  "observed_at": 901, "covered_call_ids": ["threshold"]}}, now=901)
        plan = {"deliverables": ["finish"], "calls": [{"id": "next", "purpose": "finish", "deliverable": "finish", "tokens": 20, "windows": {"short": 0}}],
                "budgets": {"tokens": 20, "windows": {"short": 0}}, "acceptance_checks": ["done"]}
        with self.assertRaises(ValueError): self.controller.record_plan_revision("p", plan, now=1000)
        self.assertTrue(self.controller.status("p", now=1000)["replan_required"])
