import io
import json
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from token_budget.codex_adapter import CodexAppServerAdapter, CodexUsageError
from token_budget.managed_install import install, doctor, uninstall
from token_budget.controller import Controller
from token_budget.managed_codex import launch_managed_codex
from token_budget.managed_codex import _controller_snapshots


class FakeProcess:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))
        self.terminated = False
        self.killed = False
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.killed = True


def success_lines():
    return [
        {"id": 1, "result": {"userAgent": "test"}},
        {"id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 15, "windowDurationMins": 300, "resetsAt": 2000},
            "secondary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 3000}}}},
        {"id": 3, "result": {"summary": {
            "lifetimeTokens": 10, "peakDailyTokens": 2, "longestRunningTurnSec": 3,
            "currentStreakDays": 4, "longestStreakDays": 5}, "dailyUsageBuckets": []}},
    ]


class CodexAdapterTests(unittest.TestCase):
    def test_read_only_handshake_snapshot_privacy_and_termination(self):
        proc = FakeProcess(success_lines())
        adapter = CodexAppServerAdapter(popen=lambda *a, **kw: proc, clock=lambda: 1000)
        result = adapter.snapshot()
        self.assertEqual(result["windows"]["primary"]["used_micropct"], 15_000_000)
        self.assertEqual(result["windows"]["secondary"]["reset_id"], "3000")
        requests = [json.loads(x) for x in proc.stdin.getvalue().splitlines()]
        self.assertEqual([x.get("method") for x in requests], ["initialize", "initialized", "account/rateLimits/read", "account/usage/read"])
        self.assertEqual(requests[0]["params"]["clientInfo"], {"name": "token_budget", "title": "Token Budget", "version": "1.0.0"})
        self.assertNotIn("jsonrpc", requests[0])
        self.assertTrue(proc.terminated)
        self.assertNotIn("content", json.dumps(result))

    def test_malformed_rpc_shapes_fail_closed_and_terminate(self):
        for lines in (
            [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"primary": None, "secondary": None}}}],
            [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": None, "resetsAt": None}, "secondary": {}}}}],
        ):
            proc = FakeProcess(lines)
            with self.assertRaises(CodexUsageError): CodexAppServerAdapter(popen=lambda *a, **kw: proc, clock=lambda: 1000).snapshot()
            self.assertTrue(proc.terminated)

    def test_rate_limits_by_id_selects_codex_bucket(self):
        result = {"rateLimitsByLimitId": {
            "codex": {"limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 2000},
                      "secondary": {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 3000}},
            "other": {"limitId": "other", "primary": None, "secondary": None}}}
        got = CodexAppServerAdapter.validate_rate_limits(result, now=1000)
        self.assertEqual((got["primary"]["used_percent"], got["secondary"]["used_percent"]), (12, 34))
        with self.assertRaises(CodexUsageError):
            CodexAppServerAdapter.validate_rate_limits({"rateLimitsByLimitId": {"other": result["rateLimitsByLimitId"]["other"]}}, now=1000)
        for key, bucket in (("codex", {**result["rateLimitsByLimitId"]["codex"], "limitId": "other"}),
                            ("other", result["rateLimitsByLimitId"]["codex"])):
            with self.assertRaises(CodexUsageError):
                CodexAppServerAdapter.validate_rate_limits({"rateLimitsByLimitId": {key: bucket}}, now=1000)
        with self.assertRaises(CodexUsageError):
            CodexAppServerAdapter.validate_rate_limits({"rateLimits": {**result["rateLimitsByLimitId"]["codex"], "limitId": "other"}}, now=1000)

    def test_timeout_and_missing_binary(self):
        class Blocking:
            def write(self, _): pass
            def flush(self): pass
            def readline(self): time.sleep(.3); return ""
        class Proc(FakeProcess):
            def __init__(self): super().__init__([]); self.stdin = Blocking(); self.stdout = Blocking()
        proc = Proc()
        with self.assertRaises(TimeoutError): CodexAppServerAdapter(popen=lambda *a, **kw: proc, timeout=.03).snapshot()
        self.assertTrue(proc.terminated)
        with self.assertRaises(CodexUsageError): CodexAppServerAdapter(popen=lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError())).snapshot()

    def test_mocked_managed_launch_keeps_reconciliation_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller(Path(tmp) / "controller.sqlite")
            now = 1000
            raw = {"observed_at": now, "windows": {
                "primary": {"used_micropct": 10_000_000, "reset_id": "2000", "resets_at": 2000},
                "secondary": {"used_micropct": 20_000_000, "reset_id": "3000", "resets_at": 3000}}}
            windows = {name: {"baseline_used_micropct": 0, "cap_micropct": 100_000_000,
                              "current_used_micropct": v["used_micropct"], "reset_id": v["reset_id"],
                              "resets_at": v["resets_at"], "observed_at": now, "covered_call_ids": []}
                       for name, v in raw["windows"].items()}
            controller.create_project("p", task_cap=1000, session_cap=1000,
                coordinator_reserve=10, windows=windows, adapter="codex", now=now)
            class Adapter:
                calls = 0
                def snapshot(self):
                    self.calls += 1
                    return raw
            seen = []
            result = launch_managed_codex(controller, Adapter(), project_id="p", call_id="c",
                model="gpt", purpose="task", token_reservation=10,
                window_reservations={"primary": 1_000_000, "secondary": 1_000_000},
                codex_args=["--help"], now=now,
                runner=lambda command, **kwargs: (seen.append((command, kwargs)) or SimpleNamespace(returncode=0)))
            self.assertEqual(result["reconciliation"], "pending_factual_per_call_token_usage")
            self.assertFalse(result["content_stored"])
            self.assertEqual(controller.db.execute("SELECT status FROM calls WHERE call_id='c'").fetchone()[0], "launched")
            self.assertEqual(seen[0][0], ["codex", "--help"])
            controller.close()

    def test_managed_conversion_preserves_only_reconciled_coverage_for_next_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller(Path(tmp) / "controller.sqlite")
            now = 1000
            initial = {"primary": {"baseline_used_micropct": 10_000_000, "current_used_micropct": 10_000_000,
                                   "reset_id": "p2", "resets_at": 2000},
                       "secondary": {"baseline_used_micropct": 20_000_000, "current_used_micropct": 20_000_000,
                                     "reset_id": "s2", "resets_at": 3000}}
            windows = {name: {**v, "cap_micropct": 100_000_000, "observed_at": now, "covered_call_ids": []}
                       for name, v in initial.items()}
            controller.create_project("p", task_cap=1000, session_cap=1000,
                coordinator_reserve=10, windows=windows, adapter="codex", now=now)
            start = {name: {"current_used_micropct": v["current_used_micropct"], "reset_id": v["reset_id"],
                            "resets_at": v["resets_at"], "observed_at": now, "covered_call_ids": []}
                     for name, v in initial.items()}
            controller.reserve_call("p", call_id="A", model="gpt", purpose="A", token_reservation=1,
                window_reservations={"primary": 2_000_000, "secondary": 2_000_000}, snapshots=start, now=now)
            controller.launch("p", "A", now=now)
            after_a = {"primary": {**start["primary"], "current_used_micropct": 11_000_000, "observed_at": 1001, "covered_call_ids": ["A"]},
                       "secondary": {**start["secondary"], "current_used_micropct": 21_000_000, "observed_at": 1001, "covered_call_ids": ["A"]}}
            controller.reconcile("p", "A", actual_tokens=1,
                actual_windows={"primary": 1_000_000, "secondary": 1_000_000}, snapshots=after_a, now=1001)
            raw = {"observed_at": 1002, "windows": {
                "primary": {"used_micropct": 11_000_000, "reset_id": "p2", "resets_at": 2000},
                "secondary": {"used_micropct": 21_000_000, "reset_id": "s2", "resets_at": 3000}}}
            converted = _controller_snapshots(raw, controller, "p")
            self.assertEqual(converted["primary"]["covered_call_ids"], ["A"])
            self.assertNotIn("B", converted["primary"]["covered_call_ids"])
            class Adapter:
                def snapshot(self): return raw
            result = launch_managed_codex(controller, Adapter(), project_id="p", call_id="B",
                model="gpt", purpose="B", token_reservation=1,
                window_reservations={"primary": 2_000_000, "secondary": 2_000_000},
                codex_args=["--help"], now=1002,
                runner=lambda command, **kw: SimpleNamespace(returncode=0))
            self.assertEqual(result["decision"], "LAUNCHED")
            self.assertEqual(controller.db.execute("SELECT status FROM calls WHERE call_id='B'").fetchone()[0], "launched")
            controller.close()

    def test_absolute_provider_80_percent_stop_catches_high_baseline_and_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = Controller(Path(tmp) / "controller.sqlite")
            now = 1000
            def snap(value, observed=now):
                return {"short": {"current_used_micropct": value, "reset_id": "r", "resets_at": 2000,
                                  "observed_at": observed, "covered_call_ids": []}}
            high = {"short": {"baseline_used_micropct": 85_000_000, "cap_micropct": 10_000_000,
                               "current_used_micropct": 85_000_000, "reset_id": "r",
                               "resets_at": 2000, "observed_at": now, "covered_call_ids": []}}
            controller.create_project("high", task_cap=1000, session_cap=1000,
                coordinator_reserve=10, windows=high, now=now)
            denied = controller.reserve_call("high", call_id="denied", model="gpt", purpose="test",
                token_reservation=1, window_reservations={"short": 1}, snapshots=snap(85_000_000), now=now)
            self.assertEqual(denied["reason_code"], "ABSOLUTE_PROVIDER_80_PERCENT_STOP")
            ordinary = {"short": {"baseline_used_micropct": 70_000_000, "cap_micropct": 20_000_000,
                                  "current_used_micropct": 70_000_000, "reset_id": "r",
                                  "resets_at": 2000, "observed_at": now, "covered_call_ids": []}}
            controller.create_project("jump", task_cap=1000, session_cap=1000,
                coordinator_reserve=10, windows=ordinary, now=now)
            reserved = controller.reserve_call("jump", call_id="jump", model="gpt", purpose="test",
                token_reservation=1, window_reservations={"short": 1}, snapshots=snap(70_000_000), now=now)
            self.assertEqual(reserved["decision"], "ALLOW")
            controller.refresh_snapshots("jump", snap(85_000_000), now=now + 1)
            self.assertFalse(controller._claim_launch("jump", "jump", now=now + 1))
            controller.close()


class ManagedInstallTests(unittest.TestCase):
    def test_idempotent_dry_run_collision_and_owned_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            preview = install(root, dry_run=True)
            self.assertTrue(preview["dry_run"])
            self.assertFalse(root.exists())
            install(root)
            first = (root / ".token-budget-manifest.json").read_text()
            install(root)
            self.assertEqual(first, (root / ".token-budget-manifest.json").read_text())
            self.assertTrue(doctor(root)["installed"])
            self.assertFalse(doctor(root)["usable"])
            self.assertTrue(doctor(root)["staging_only"])
            report = doctor(root)
            self.assertIn("python_available", report)
            self.assertIn("codex_available", report)
            self.assertFalse(report["launcher_discoverable"])
            self.assertFalse(report["skill_discoverable"])
            with self.assertRaises(RuntimeError):
                (root / "UNIVERSAL-USAGE-BUDGET.md").write_text("user edit")
                install(root)
            self.assertFalse(doctor(root)["installed"])
            with self.assertRaises(RuntimeError): uninstall(root)

    def test_collision_refused_and_dry_run_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"; root.mkdir()
            (root / "UNIVERSAL-USAGE-BUDGET.md").write_text("user data")
            with self.assertRaises(RuntimeError): install(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"; install(root)
            result = uninstall(root, dry_run=True)
            self.assertTrue(result["dry_run"])
            self.assertTrue(doctor(root)["installed"])
            uninstall(root)
            self.assertFalse(doctor(root)["installed"])
            self.assertFalse((root / "bin/codex-managed").exists())

    def test_launcher_collision_and_manifest_path_tampering_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            launcher = root / "bin/codex-managed"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("user launcher")
            with self.assertRaises(RuntimeError): install(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            install(root)
            manifest_path = root / ".token-budget-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            victim = Path(tmp) / "victim"
            victim.write_text("keep")
            manifest["paths"]["bin/codex-managed"] = str(victim)
            manifest["sha256"]["bin/codex-managed"] = __import__("hashlib").sha256(victim.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(RuntimeError): uninstall(root)
            self.assertEqual(victim.read_text(), "keep")

    def test_default_install_uses_user_bin_and_codex_skill_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"; home.mkdir()
            with patch("token_budget.managed_install.Path.home", return_value=home), \
                 patch.dict("os.environ", {"XDG_CONFIG_HOME": str(home / ".config")}):
                report = install()
                launcher = home / ".local/bin/codex-managed"
                skill = home / ".codex/skills/token-budget/SKILL.md"
                self.assertTrue(launcher.is_file())
                self.assertTrue(launcher.stat().st_mode & 0o111)
                self.assertTrue(skill.is_file())
                doctor_report = doctor()
                self.assertTrue(doctor_report["installed"])
                self.assertTrue(doctor_report["launcher_executable"])
                self.assertTrue(doctor_report["skill_discoverable"])
                self.assertEqual(Path(doctor_report["launcher_path"]), launcher)
                self.assertEqual(Path(doctor_report["skill_path"]), skill)
                self.assertFalse(doctor_report["usable"])  # isolated HOME is not on PATH
                uninstall()
                self.assertFalse(launcher.exists())
                self.assertFalse(skill.exists())

    def test_install_resumes_safe_partial_write_and_serializes_races(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            import token_budget.managed_install as installer
            original = installer._link_staged
            interrupted = {"done": False}
            def stop_after_stage(path, temp_name, data, mode=0o600):
                original(path, temp_name, data, mode)
                if not interrupted["done"]:
                    interrupted["done"] = True
                    raise OSError("simulated interruption")
            with patch.object(installer, "_link_staged", side_effect=stop_after_stage):
                with self.assertRaises(OSError): install(root)
            self.assertTrue((root / ".token-budget-recovery.json").is_file())
            install(root)
            self.assertTrue(doctor(root)["installed"])
            self.assertFalse((root / ".token-budget-recovery.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"; outcomes = []; errors = []
            def worker():
                try: outcomes.append(install(root))
                except Exception as exc: errors.append(exc)
            threads = [threading.Thread(target=worker) for _ in range(4)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(len(outcomes), 4)
            self.assertEqual(errors, [])
            self.assertTrue(doctor(root)["installed"])
            removed = []; failures = []
            def remove_worker():
                try: removed.append(uninstall(root))
                except RuntimeError as exc: failures.append(exc)
            threads = [threading.Thread(target=remove_worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(len(removed), 1)
            self.assertEqual(len(failures), 1)
            self.assertFalse(doctor(root)["installed"])

    def test_symlink_manifest_ancestor_and_manifest_hash_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            outside = Path(tmp) / "outside"; outside.mkdir()
            (root / "bin").parent.mkdir(parents=True)
            (root / "bin").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeError): install(root)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"; install(root)
            manifest_path = root / ".token-budget-manifest.json"
            manifest_path.unlink()
            external = Path(tmp) / "external.json"
            external.write_text("{}")
            manifest_path.symlink_to(external)
            with self.assertRaises(RuntimeError): uninstall(root)
            self.assertEqual(external.read_text(), "{}")
            self.assertTrue(doctor(root)["unsafe_paths"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"; install(root)
            manifest_path = root / ".token-budget-manifest.json"
            original = json.loads(manifest_path.read_text())
            for hashes in (dict(original["sha256"], **{"bin/codex-managed": ""}),
                           {k: v for k, v in original["sha256"].items() if k != "bin/codex-managed"}):
                manifest = dict(original); manifest["sha256"] = hashes
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(RuntimeError): uninstall(root)
                self.assertTrue((root / "bin/codex-managed").exists())
            launcher = root / "bin/codex-managed"
            launcher.write_text("user replacement")
            manifest = dict(original)
            manifest["sha256"] = dict(original["sha256"], **{
                "bin/codex-managed": __import__("hashlib").sha256(b"user replacement").hexdigest()})
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(RuntimeError): uninstall(root)


if __name__ == "__main__": unittest.main()
