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
            self.assertFalse(root.exists())

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
                self.assertTrue(skill.is_file())
                doctor_report = doctor()
                self.assertTrue(doctor_report["installed"])
                self.assertEqual(Path(doctor_report["launcher_path"]), launcher)
                self.assertEqual(Path(doctor_report["skill_path"]), skill)
                self.assertFalse(doctor_report["usable"])  # isolated HOME is not on PATH
                uninstall()
                self.assertFalse(launcher.exists())
                self.assertFalse(skill.exists())


if __name__ == "__main__": unittest.main()
