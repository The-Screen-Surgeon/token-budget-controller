"""Deterministic, provider-neutral token budget controller.

The controller stores only budgets, usage facts, decisions, and audit metadata.
It never estimates missing usage.  Calls are hard-gated only when routed
through :func:`run_wrapped`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
import threading
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class UsageAdapter(Protocol):
    """Provider-neutral adapter contract for factual window snapshots."""
    name: str
    def snapshot(self) -> Mapping[str, Any]: ...


class StaticAdapter:
    """Test adapter returning a supplied immutable snapshot."""
    name = "static"
    def __init__(self, snapshot: Mapping[str, Any]): self._snapshot = dict(snapshot)
    def snapshot(self) -> Mapping[str, Any]: return dict(self._snapshot)


@dataclass(frozen=True)
class GateDecision:
    decision: str
    reason: str
    reason_code: str
    project_id: str
    call_id: str | None = None
    reserved_tokens: int = 0
    reserved_micropct: int = 0
    enforcement_mode: str = "managed-wrapper"
    def to_dict(self) -> dict[str, Any]: return asdict(self)


class ControllerError(ValueError): pass

SUPPORTED_ADAPTERS = {"static", "manual", "codex"}


def _json(value: Any) -> str: return json.dumps(value, sort_keys=True, separators=(",", ":"))
def _int(value: Any, name: str, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ControllerError(f"{name} must be a {'positive ' if positive else ''}integer")
    return value


def _serialized(method):
    def call(self, *args, **kwargs):
        with self._lock: return method(self, *args, **kwargs)
    call.__name__ = method.__name__
    call.__doc__ = method.__doc__
    return call


class Controller:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self.db = sqlite3.connect(self.database, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS projects (
          project_id TEXT PRIMARY KEY, task_cap INTEGER NOT NULL, session_cap INTEGER NOT NULL,
          coordinator_reserve INTEGER NOT NULL, consumed_tokens INTEGER NOT NULL DEFAULT 0,
          plan_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, replan_required INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1, quarantined INTEGER NOT NULL DEFAULT 0, violated INTEGER NOT NULL DEFAULT 0, token_violated INTEGER NOT NULL DEFAULT 0, window_violated INTEGER NOT NULL DEFAULT 0,
          freshness_seconds INTEGER NOT NULL, adapter TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS windows (
          project_id TEXT NOT NULL, name TEXT NOT NULL, baseline INTEGER NOT NULL, cap INTEGER NOT NULL,
          current INTEGER, reserved INTEGER NOT NULL DEFAULT 0, reset_id TEXT NOT NULL, resets_at INTEGER NOT NULL,
          observed_at INTEGER, covered_call_ids_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(project_id,name));
        CREATE TABLE IF NOT EXISTS barrier_state (
          project_id TEXT NOT NULL, budget_key TEXT NOT NULL, crossed INTEGER NOT NULL DEFAULT 0,
          acknowledged INTEGER NOT NULL DEFAULT 0, crossing_call_id TEXT, PRIMARY KEY(project_id,budget_key));
        CREATE TABLE IF NOT EXISTS acknowledgments (
          project_id TEXT NOT NULL, budget_key TEXT NOT NULL, acknowledged_at INTEGER NOT NULL,
          PRIMARY KEY(project_id,budget_key));
        CREATE TABLE IF NOT EXISTS calls (
          project_id TEXT NOT NULL, call_id TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
          token_reservation INTEGER NOT NULL, micropct_reservation INTEGER NOT NULL, status TEXT NOT NULL,
          actual_tokens INTEGER, actual_micropct INTEGER, launch_status TEXT, created_at INTEGER NOT NULL, window_reservations_json TEXT NOT NULL DEFAULT '{}', actual_windows_json TEXT NOT NULL DEFAULT '{}', barrier_crossing INTEGER NOT NULL DEFAULT 0, launch_claim_hash TEXT,
          PRIMARY KEY(project_id,call_id));
        CREATE TABLE IF NOT EXISTS revisions (
          project_id TEXT NOT NULL, revision INTEGER NOT NULL, plan_json TEXT NOT NULL, trigger TEXT NOT NULL,
          created_at INTEGER NOT NULL, PRIMARY KEY(project_id,revision));
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, kind TEXT NOT NULL,
          payload TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
        """)
        try:
            self.db.execute("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = self.db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            call_columns = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
            window_columns = {r[1] for r in self.db.execute("PRAGMA table_info(windows)")}
            project_columns = {r[1] for r in self.db.execute("PRAGMA table_info(projects)")}
            for col, ddl in (("window_reservations_json", "TEXT NOT NULL DEFAULT '{}'"), ("actual_windows_json", "TEXT NOT NULL DEFAULT '{}'"), ("barrier_crossing", "INTEGER NOT NULL DEFAULT 0"), ("launch_claim_hash", "TEXT")):
                if col not in call_columns: self.db.execute(f"ALTER TABLE calls ADD COLUMN {col} {ddl}")
            if "covered_call_ids_json" not in window_columns: self.db.execute("ALTER TABLE windows ADD COLUMN covered_call_ids_json TEXT NOT NULL DEFAULT '[]'")
            barrier_columns = {r[1] for r in self.db.execute("PRAGMA table_info(barrier_state)")}
            if "crossing_call_id" not in barrier_columns: self.db.execute("ALTER TABLE barrier_state ADD COLUMN crossing_call_id TEXT")
            for col, ddl in (("token_violated", "INTEGER NOT NULL DEFAULT 0"), ("window_violated", "INTEGER NOT NULL DEFAULT 0"), ("quarantined", "INTEGER NOT NULL DEFAULT 0")):
                if col not in project_columns: self.db.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            # Legacy data is usable only when there are no old calls whose per-window facts are absent.
            if version is not None and (not str(version[0]).isdigit() or int(version[0]) > 4):
                raise ControllerError("database schema version is unsupported")
            legacy = self.db.execute("SELECT DISTINCT project_id FROM calls WHERE window_reservations_json='{}' OR (status='reconciled' AND actual_micropct>0 AND actual_windows_json='{}')").fetchall()
            for row in legacy:
                self.db.execute("UPDATE projects SET quarantined=1,active=0 WHERE project_id=?", (row[0],))
            for row in self.db.execute("SELECT project_id,call_id FROM calls WHERE status='reconciled' AND actual_micropct=0 AND actual_windows_json='{}'").fetchall():
                names = [r[0] for r in self.db.execute("SELECT name FROM windows WHERE project_id=?", (row[0],))]
                self.db.execute("UPDATE calls SET actual_windows_json=? WHERE project_id=? AND call_id=?", (_json({n: 0 for n in names}), row[0], row[1]))
            if version is not None and int(version[0]) <= 3:
                uncertain = self.db.execute("SELECT DISTINCT project_id FROM calls WHERE status='reconciled'").fetchall()
                for row in uncertain: self.db.execute("UPDATE projects SET quarantined=1,active=0 WHERE project_id=?", (row[0],))
            self.db.execute("INSERT OR REPLACE INTO schema_meta VALUES('schema_version','4')")
            for p in self.db.execute("SELECT project_id FROM projects").fetchall():
                pid = p[0]
                for win in self.db.execute("SELECT * FROM windows WHERE project_id=?", (pid,)).fetchall():
                    reconstructed = 0
                    for call in self.db.execute("SELECT window_reservations_json FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (pid,)):
                        reconstructed += json.loads(call[0]).get(win["name"], 0)
                    if reconstructed != win["reserved"]: self.db.execute("UPDATE projects SET quarantined=1,active=0 WHERE project_id=?", (pid,))
                self.db.execute("INSERT OR IGNORE INTO barrier_state(project_id,budget_key,crossed,acknowledged,crossing_call_id) VALUES(?,?,0,0,NULL)", (pid, "task"))
                task = self.db.execute("SELECT * FROM projects WHERE project_id=?", (pid,)).fetchone()
                crossed = task["consumed_tokens"] * 2 >= task["task_cap"]
                if crossed:
                    self.db.execute("UPDATE barrier_state SET crossed=1 WHERE project_id=? AND budget_key='task'", (pid,))
                    state = self.db.execute("SELECT acknowledged FROM barrier_state WHERE project_id=? AND budget_key='task'", (pid,)).fetchone()
                    if state and not state[0]: self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (pid,))
                for w in self.db.execute("SELECT * FROM windows WHERE project_id=?", (pid,)).fetchall():
                    key = f"window:{w['name']}:{w['reset_id']}"
                    self.db.execute("INSERT OR IGNORE INTO barrier_state(project_id,budget_key,crossed,acknowledged,crossing_call_id) VALUES(?,?,0,0,NULL)", (pid, key))
                    if w["current"] is not None and (w["current"]-w["baseline"]+w["reserved"])*2 >= w["cap"]:
                        self.db.execute("UPDATE barrier_state SET crossed=1 WHERE project_id=? AND budget_key=?", (pid, key))
                        state = self.db.execute("SELECT acknowledged FROM barrier_state WHERE project_id=? AND budget_key=?", (pid, key)).fetchone()
                        if state and not state[0]: self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (pid,))
            self.db.commit()
        except BaseException:
            self.db.rollback(); raise

    def close(self) -> None: self.db.close()

    def _event(self, project_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        if not self.db.in_transaction: raise ControllerError("event append requires an active write transaction")
        row = self.db.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        body = _json({"project_id": project_id, "kind": kind, "payload": payload,
                      "previous_hash": previous})
        digest = hashlib.sha256(body.encode()).hexdigest()
        self.db.execute("INSERT INTO events(project_id,kind,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?)",
                        (project_id, kind, _json(payload), previous, digest, int(time.time())))

    @_serialized
    def create_project(self, project_id: str, *, task_cap: int, session_cap: int,
                       coordinator_reserve: int, windows: Mapping[str, Mapping[str, Any]],
                       plan: Mapping[str, Any] | None = None, freshness_seconds: int = 300,
                       adapter: str = "static", now: int | None = None) -> dict[str, Any]:
        if not isinstance(project_id, str) or not project_id: raise ControllerError("project_id is required")
        task_cap = _int(task_cap, "task_cap", True); session_cap = _int(session_cap, "session_cap", True)
        coordinator_reserve = _int(coordinator_reserve, "coordinator_reserve")
        if coordinator_reserve >= task_cap: raise ControllerError("coordinator reserve must leave call budget")
        freshness_seconds = _int(freshness_seconds, "freshness_seconds", True)
        if adapter not in SUPPORTED_ADAPTERS: raise ControllerError("unsupported adapter")
        if not isinstance(windows, Mapping) or not windows: raise ControllerError("windows are required")
        now = int(time.time()) if now is None else _int(now, "now")
        config = {}
        for name, w in windows.items():
            if not isinstance(name, str) or not name or not isinstance(w, Mapping): raise ControllerError("invalid provider window configuration")
            baseline = _int(w.get("baseline_used_micropct"), f"{name}.baseline_used_micropct")
            cap = _int(w.get("cap_micropct"), f"{name}.cap_micropct", True)
            current = _int(w.get("current_used_micropct"), f"{name}.current_used_micropct")
            if current < baseline: raise ControllerError(f"{name} current usage is below baseline")
            if current-baseline > cap: raise ControllerError(f"{name} initial usage exceeds its window cap")
            observed = _int(w.get("observed_at"), f"{name}.observed_at")
            reset = w.get("reset_id")
            if not isinstance(reset, str) or not reset: raise ControllerError(f"{name}.reset_id is required")
            resets_at = _int(w.get("resets_at"), f"{name}.resets_at", True)
            if observed > now or now-observed > freshness_seconds: raise ControllerError(f"{name} initial snapshot is stale")
            if resets_at <= now: raise ControllerError(f"{name} provider window is expired")
            config[name] = {"baseline": baseline, "cap": cap, "current": current, "observed": observed, "reset_id": reset, "resets_at": resets_at}
        if plan is not None and not isinstance(plan, Mapping): raise ControllerError("plan must be an object")
        plan_json = _json(plan or {})
        event_payload = {"task_cap": task_cap, "windows": config}
        _json(event_payload)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO projects(project_id,task_cap,session_cap,coordinator_reserve,consumed_tokens,plan_json,revision,replan_required,active,violated,freshness_seconds,adapter,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, task_cap, session_cap, coordinator_reserve, 0, plan_json, 0, 0, 1, 0, freshness_seconds, adapter, now))
            for name, w in config.items():
                self.db.execute("INSERT INTO windows(project_id,name,baseline,cap,current,reserved,reset_id,resets_at,observed_at,covered_call_ids_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (project_id, name, w["baseline"], w["cap"], w["current"], 0, w["reset_id"], w["resets_at"], w["observed"], "[]"))
                self.db.execute("INSERT INTO barrier_state(project_id,budget_key,crossed,acknowledged,crossing_call_id) VALUES(?,?,0,0,NULL)", (project_id, f"window:{name}:{w['reset_id']}"))
            self.db.execute("INSERT INTO barrier_state(project_id,budget_key,crossed,acknowledged,crossing_call_id) VALUES(?,?,0,0,NULL)", (project_id, "task"))
            self._event(project_id, "project_created", event_payload)
            self.db.commit()
        except BaseException as exc:
            self.db.rollback()
            if isinstance(exc, sqlite3.IntegrityError): raise ControllerError("project already exists") from exc
            raise
        return self.status(project_id, now=now)

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if row is None: raise ControllerError("unknown project")
        return row

    def _validate_snapshot(self, name: str, w: sqlite3.Row, snap: Mapping[str, Any] | None,
                           now: int, freshness: int, *, allow_call_ids: set[str] | None = None, extra_actual: Mapping[str, Mapping[str, int]] | None = None):
        if not isinstance(snap, Mapping): return None, None, None, "DENY_USAGE_UNAVAILABLE"
        observed_probe = snap.get("observed_at")
        if type(observed_probe) is int and (observed_probe > now or now-observed_probe > freshness): return None, None, None, "STALE_USAGE"
        try:
            current = _int(snap.get("current_used_micropct"), f"{name}.current_used_micropct")
            observed = _int(snap.get("observed_at"), f"{name}.observed_at")
            resets_at = _int(snap.get("resets_at"), f"{name}.resets_at", True)
        except ControllerError: return None, None, None, "MALFORMED_USAGE"
        reset = snap.get("reset_id"); covered = snap.get("covered_call_ids")
        if observed > now or now-observed > freshness: return None, None, None, "STALE_USAGE"
        if not isinstance(reset, str) or not reset or not isinstance(covered, list) or any(not isinstance(x, str) or not x for x in covered) or len(covered) != len(set(covered)):
            return None, None, None, "MALFORMED_USAGE"
        if resets_at <= now or resets_at != w["resets_at"] or reset != w["reset_id"]: return None, None, None, "RESET_MISMATCH"
        if current < w["baseline"] or (w["current"] is not None and current < w["current"]): return None, None, None, "USAGE_REGRESSION"
        known = allow_call_ids if allow_call_ids is not None else {r[0] for r in self.db.execute("SELECT call_id FROM calls WHERE project_id=? AND status='reconciled'", (w["project_id"],))}
        if not set(covered).issubset(known): return None, None, None, "CONTRADICTORY_USAGE"
        previous_coverage = set(json.loads(w["covered_call_ids_json"]))
        if not previous_coverage.issubset(set(covered)): return None, None, None, "COVERAGE_REGRESSION"
        covered_sum = 0
        for cid in covered:
            if extra_actual and cid in extra_actual: covered_sum += extra_actual[cid].get(name, 0)
            else:
                row = self.db.execute("SELECT actual_windows_json FROM calls WHERE project_id=? AND call_id=?", (w["project_id"], cid)).fetchone()
                if row is None: return None, None, None, "CONTRADICTORY_USAGE"
                covered_sum += json.loads(row[0]).get(name, 0)
        if current-w["baseline"] < covered_sum: return None, None, None, "INSUFFICIENT_USAGE_COVERAGE"
        return current, observed, covered, None

    def _cached_snapshot(self, w: sqlite3.Row) -> dict[str, Any]:
        return {"current_used_micropct": w["current"], "observed_at": w["observed_at"], "reset_id": w["reset_id"], "resets_at": w["resets_at"], "covered_call_ids": json.loads(w["covered_call_ids_json"])}

    def _window_liability(self, project_id: str, w: sqlite3.Row, covered: set[str]) -> int:
        total = 0
        for call in self.db.execute("SELECT call_id,window_reservations_json FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)):
            if call["call_id"] not in covered: total += json.loads(call["window_reservations_json"]).get(w["name"], 0)
        return total

    def _validate_snapshot_set(self, project_id: str, snapshots: Mapping[str, Mapping[str, Any]], now: int):
        rows = list(self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,)))
        if not isinstance(snapshots, Mapping) or set(snapshots) != {w["name"] for w in rows}:
            return None, ("MALFORMED_USAGE", "a complete snapshot is required for every configured window")
        updates = {}
        freshness = self._project(project_id)["freshness_seconds"]
        for w in rows:
            current, observed, covered, error = self._validate_snapshot(w["name"], w, snapshots[w["name"]], now, freshness)
            if error: return None, (error, f"{w['name']} snapshot failed validation")
            updates[w["name"]] = (current, observed, _json(covered))
        return updates, None

    def _store_snapshot_updates(self, project_id: str, updates: Mapping[str, tuple[int, int, str]]) -> None:
        for name, (current, observed, coverage_json) in updates.items():
            self.db.execute("UPDATE windows SET current=?,observed_at=?,covered_call_ids_json=? WHERE project_id=? AND name=?", (current, observed, coverage_json, project_id, name))

    @_serialized
    def refresh_snapshots(self, project_id: str, snapshots: Mapping[str, Mapping[str, Any]], *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else _int(now, "now")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            p = self._project(project_id)
            if p["quarantined"]: raise ControllerError("quarantined project requires accounting recovery")
            updates, error = self._validate_snapshot_set(project_id, snapshots, now)
            if error: raise ControllerError(f"{error[0]}: {error[1]}")
            self._store_snapshot_updates(project_id, updates)
            self._event(project_id, "usage_refreshed", {"windows": sorted(updates), "observed_at": now})
            self.db.commit()
            return self.status(project_id, now=now)
        except BaseException:
            self.db.rollback(); raise

    def _gate(self, project_id: str, call_id: str | None, token_reservation: int,
              window_reservations: Mapping[str, int] | None, model: str | None, purpose: str | None,
              now: int, snapshots: Mapping[str, Mapping[str, Any]] | None = None) -> GateDecision:
        p = self._project(project_id)
        amounts: dict[str, int] = {}
        def deny(code, reason): return GateDecision("DENY", reason, code, project_id, call_id, token_reservation, sum(amounts.values()))
        if p["quarantined"]: return deny("PROJECT_QUARANTINED", "project requires safe accounting recovery")
        if not p["active"]: return deny("INACTIVE_TASK", "project is inactive")
        if p["violated"]: return deny("BUDGET_VIOLATED", "project exceeded a reservation")
        if p["adapter"] not in SUPPORTED_ADAPTERS: return deny("UNSUPPORTED_ADAPTER", "provider adapter is not implemented")
        if not model or not purpose: return deny("CALL_METADATA_MISSING", "model and purpose are required")
        if p["replan_required"]: return deny("REPLAN_REQUIRED", "50% project budget barrier requires a complete plan revision")
        try:
            token_reservation = _int(token_reservation, "token_reservation", True)
            if not isinstance(window_reservations, Mapping): raise ControllerError("explicit per-window reservations are required")
            rows = list(self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,)))
            if set(window_reservations) != {r["name"] for r in rows}: raise ControllerError("reservations must name every configured provider window exactly once")
            for w in rows: amounts[w["name"]] = _int(window_reservations[w["name"]], f"{w['name']}.reservation")
        except (ControllerError, TypeError): return deny("INVALID_RESERVATION", "valid token and explicit per-window reservations are required")
        active_reserved = self.db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)).fetchone()[0]
        projected_tokens = p["consumed_tokens"] + active_reserved + token_reservation
        task_budget = p["task_cap"] - p["coordinator_reserve"]
        if projected_tokens > p["session_cap"]:
            return deny("SESSION_CAP_EXHAUSTED", "session cap is insufficient")
        if projected_tokens > task_budget:
            return deny("TASK_CAP_EXHAUSTED", "task cap or coordinator reserve is insufficient")
        if projected_tokens * 5 >= task_budget * 4:
            return deny("UNIVERSAL_80_PERCENT_STOP", "80% task-token stop is a universal hard stop")
        for w in rows:
            snap = (snapshots or {}).get(w["name"])
            if snap is None: snap = self._cached_snapshot(w)
            current, _, covered_ids, error = self._validate_snapshot(w["name"], w, snap, now, p["freshness_seconds"])
            if error: return deny(error, "factual usage snapshot failed validation")
            liability = self._window_liability(project_id, w, set(covered_ids))
            used = current - w["baseline"] + liability
            projected = used + amounts[w["name"]]
            key = f"window:{w['name']}:{w['reset_id']}"
            state = self.db.execute("SELECT crossed,acknowledged FROM barrier_state WHERE project_id=? AND budget_key=?", (project_id, key)).fetchone()
            if used * 2 >= w["cap"] and state and not state["crossed"] and not state["acknowledged"]:
                self.db.execute("UPDATE barrier_state SET crossed=1 WHERE project_id=? AND budget_key=?", (project_id, key))
                self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (project_id,))
                return deny("REPLAN_REQUIRED", f"50% {w['name']} provider-window budget barrier requires a remaining-plan revision")
            if projected * 5 >= w["cap"] * 4:
                return deny("UNIVERSAL_80_PERCENT_STOP", "80% provider-window stop is a universal hard stop")
            if state and state["crossed"] and not state["acknowledged"]:
                return deny("REPLAN_REQUIRED", f"50% {w['name']} provider-window budget barrier requires a remaining-plan revision")
            if projected > w["cap"]: return deny("WINDOW_CAP_EXHAUSTED", f"{w['name']} window cap is insufficient")
        return GateDecision("ALLOW", "all deterministic budget checks passed", "ALLOW", project_id, call_id, token_reservation, sum(amounts.values()))

    @_serialized
    def reserve_call(self, project_id: str, *, call_id: str, model: str, purpose: str,
                     token_reservation: int, window_reservations: Mapping[str, int] | None = None,
                     now: int | None = None,
                     snapshots: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else _int(now, "now")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if snapshots is not None:
                updates, snapshot_error = self._validate_snapshot_set(project_id, snapshots, now)
                if snapshot_error:
                    decision = GateDecision("DENY", snapshot_error[1], snapshot_error[0], project_id, call_id, token_reservation, 0)
                    self._event(project_id, "call_denied", decision.to_dict()); self.db.commit(); return decision.to_dict()
                self._store_snapshot_updates(project_id, updates)
            decision = self._gate(project_id, call_id, token_reservation, window_reservations, model, purpose, now, None)
            if decision.decision == "ALLOW":
                self.db.execute("INSERT INTO calls(project_id,call_id,model,purpose,token_reservation,micropct_reservation,status,actual_tokens,actual_micropct,launch_status,created_at,window_reservations_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (project_id, call_id, model, purpose, token_reservation, decision.reserved_micropct, "reserved", None, None, None, now, _json(window_reservations)))
                for name, amount in window_reservations.items(): self.db.execute("UPDATE windows SET reserved=reserved+? WHERE project_id=? AND name=?", (amount, project_id, name))
                token_used = self._project(project_id)["consumed_tokens"] + self.db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)).fetchone()[0]
                crossed = token_used * 2 >= self._project(project_id)["task_cap"]
                if crossed:
                    state = self.db.execute("SELECT crossed,acknowledged FROM barrier_state WHERE project_id=? AND budget_key='task'", (project_id,)).fetchone()
                    if state and not state["crossed"] and not state["acknowledged"]:
                        self.db.execute("UPDATE barrier_state SET crossed=1,crossing_call_id=? WHERE project_id=? AND budget_key='task'", (call_id, project_id))
                        self.db.execute("UPDATE calls SET barrier_crossing=1 WHERE project_id=? AND call_id=?", (project_id, call_id))
                        self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (project_id,))
                for w in self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,)):
                    used = (w["current"] - w["baseline"] if w["current"] is not None else 0) + w["reserved"]
                    key = f"window:{w['name']}:{w['reset_id']}"
                    state = self.db.execute("SELECT crossed,acknowledged FROM barrier_state WHERE project_id=? AND budget_key=?", (project_id, key)).fetchone()
                    if used * 2 >= w["cap"] and state and not state["crossed"] and not state["acknowledged"]:
                        self.db.execute("UPDATE barrier_state SET crossed=1,crossing_call_id=? WHERE project_id=? AND budget_key=?", (call_id, project_id, key))
                        self.db.execute("UPDATE calls SET barrier_crossing=1 WHERE project_id=? AND call_id=?", (project_id, call_id))
                        self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (project_id,))
                self._event(project_id, "call_reserved", decision.to_dict())
            else: self._event(project_id, "call_denied", decision.to_dict())
            self.db.commit()
            return decision.to_dict()
        except BaseException:
            self.db.rollback(); raise

    def _validate_remaining_plan(self, project_id: str, plan: Mapping[str, Any], now: int) -> None:
        required = {"deliverables", "calls", "budgets", "acceptance_checks"}
        if not isinstance(plan, Mapping) or set(plan) != required:
            raise ControllerError("remaining plan must contain exactly deliverables, calls, budgets, and acceptance_checks")
        for key in ("deliverables", "acceptance_checks"):
            if not isinstance(plan[key], list) or not plan[key] or any(not isinstance(v, str) or not v.strip() for v in plan[key]):
                raise ControllerError(f"remaining plan {key} must be a nonempty list of descriptions")
        calls = plan["calls"]
        if not isinstance(calls, list) or not calls: raise ControllerError("remaining plan calls must be a nonempty list")
        # Calls are concrete mappings, while string descriptions remain accepted for the other lists.
        if any(not isinstance(c, Mapping) or set(c) != {"id", "purpose", "deliverable", "tokens", "windows"} for c in calls):
            raise ControllerError("each remaining call needs id, purpose, tokens, and windows")
        ids = [c["id"] for c in calls]
        if any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
            raise ControllerError("remaining call ids must be unique nonempty strings")
        deliverables = set(plan["deliverables"])
        if any(not isinstance(c["deliverable"], str) or c["deliverable"] not in deliverables for c in calls):
            raise ControllerError("each remaining call must be assigned to a declared deliverable")
        if {c["deliverable"] for c in calls} != deliverables:
            raise ControllerError("remaining calls must cover every declared deliverable")
        p = self._project(project_id)
        windows = {r["name"]: r for r in self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,))}
        budgets = plan["budgets"]
        if not isinstance(budgets, Mapping) or set(budgets) != {"tokens", "windows"} or not isinstance(budgets["windows"], Mapping) or set(budgets["windows"]) != set(windows):
            raise ControllerError("remaining budgets must specify tokens and every provider window")
        token_budget = _int(budgets["tokens"], "remaining token budget")
        window_budgets = {name: _int(value, f"{name} remaining budget") for name, value in budgets["windows"].items()}
        token_demand = 0; window_demand = {name: 0 for name in windows}
        for call in calls:
            token_demand += _int(call["tokens"], "call token demand")
            if not isinstance(call["windows"], Mapping) or set(call["windows"]) != set(windows): raise ControllerError("each call must allocate demand to every provider window")
            for name in windows: window_demand[name] += _int(call["windows"][name], f"{name} call demand")
        active = self.db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)).fetchone()[0]
        projected_tokens = p["consumed_tokens"] + active + token_demand
        task_budget = p["task_cap"] - p["coordinator_reserve"]
        if token_budget != token_demand or projected_tokens * 5 >= task_budget * 4:
            raise ControllerError("remaining token demand exceeds the strict 80% task limit")
        if projected_tokens > p["session_cap"]: raise ControllerError("remaining token demand exceeds session capacity")
        for name, w in windows.items():
            cached = self._cached_snapshot(w)
            current, _, covered_ids, error = self._validate_snapshot(name, w, cached, now, p["freshness_seconds"])
            if error: raise ControllerError(f"remaining plan requires fresh valid {name} usage: {error}")
            liability = self._window_liability(project_id, w, set(covered_ids))
            projected = current - w["baseline"] + liability + window_demand[name]
            if window_budgets[name] != window_demand[name] or projected * 5 >= w["cap"] * 4:
                raise ControllerError(f"remaining {name} demand exceeds the strict 80% window limit")
            if projected > w["cap"]: raise ControllerError(f"remaining {name} demand exceeds window capacity")

    @_serialized
    def record_plan_revision(self, project_id: str, plan: Mapping[str, Any], *, trigger: str = "50_percent", now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else _int(now, "now")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            p = self._project(project_id)
            if p["quarantined"]: raise ControllerError("quarantined projects require accounting recovery before revision")
            if not p["replan_required"]: raise ControllerError("plan revision is accepted only at an active 50% barrier")
            self._validate_remaining_plan(project_id, plan, now)
            revision = p["revision"] + 1
            self.db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (project_id, revision, _json(plan), trigger, now))
            self.db.execute("UPDATE projects SET plan_json=?,revision=?,replan_required=0 WHERE project_id=?", (_json(plan), revision, project_id))
            self.db.execute("UPDATE barrier_state SET acknowledged=1 WHERE project_id=? AND crossed=1", (project_id,))
            self._event(project_id, "plan_revision", {"revision": revision, "trigger": trigger, "plan": plan})
            self.db.commit()
            return {"project_id": project_id, "revision": revision, "trigger": trigger}
        except BaseException:
            self.db.rollback(); raise

    def acknowledge_80_percent(self, project_id: str, window: str, *, remaining_plan: Mapping[str, Any], now: int | None = None) -> None:
        raise ControllerError("universal 80% stop has no supported override")

    def _release_window_reservations(self, project_id: str, reservations: Mapping[str, int]) -> None:
        for name, amount in reservations.items():
            cur = self.db.execute("UPDATE windows SET reserved=reserved-? WHERE project_id=? AND name=? AND reserved>=?", (amount, project_id, name, amount))
            if cur.rowcount != 1: raise ControllerError("window reservation liability is inconsistent")

    @_serialized
    def reconcile(self, project_id: str, call_id: str, *, actual_tokens: int,
                  actual_windows: Mapping[str, int], snapshots: Mapping[str, Mapping[str, Any]],
                  now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else _int(now, "now")
        actual_tokens = _int(actual_tokens, "actual_tokens")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            p = self._project(project_id)
            if p["quarantined"]: raise ControllerError("quarantined projects cannot be reconciled from incomplete evidence")
            c = self.db.execute("SELECT * FROM calls WHERE project_id=? AND call_id=?", (project_id, call_id)).fetchone()
            if c is None or c["status"] != "launched": raise ControllerError("call is not reconcilable")
            if not isinstance(actual_windows, Mapping) or not isinstance(snapshots, Mapping): raise ControllerError("actual window usage and complete snapshots are required")
            rows = list(self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,)))
            if set(actual_windows) != {w["name"] for w in rows} or set(snapshots) != {w["name"] for w in rows}: raise ControllerError("reconciliation must include every configured window")
            observed_values = {}
            actual_values = {name: _int(value, f"{name}.actual") for name, value in actual_windows.items()}
            allowed_covered = {r[0] for r in self.db.execute("SELECT call_id FROM calls WHERE project_id=? AND status='reconciled'", (project_id,))} | {call_id}
            window_overrun = False
            for w in rows:
                actual = actual_values[w["name"]]
                snap = snapshots[w["name"]]
                if call_id not in snap.get("covered_call_ids", []): raise ControllerError("reconciliation snapshot must cover the reconciled call")
                _, observed, _, error = self._validate_snapshot(w["name"], w, snap, now, p["freshness_seconds"], allow_call_ids=allowed_covered, extra_actual={call_id: actual_values})
                if error: raise ControllerError(f"invalid {w['name']} snapshot: {error}")
                observed_values[w["name"]] = (snap["current_used_micropct"], observed, _json(snap["covered_call_ids"]))
                if actual > json.loads(c["window_reservations_json"])[w["name"]]: window_overrun = True
            overrun = actual_tokens > c["token_reservation"] or window_overrun
            self.db.execute("UPDATE calls SET status='reconciled',actual_tokens=?,actual_micropct=?,actual_windows_json=? WHERE project_id=? AND call_id=? AND status='launched'", (actual_tokens, sum(actual_windows.values()), _json(actual_values), project_id, call_id))
            if self.db.execute("SELECT changes()").fetchone()[0] != 1: raise ControllerError("call was concurrently reconciled")
            call_window_reservations = json.loads(c["window_reservations_json"])
            self._release_window_reservations(project_id, call_window_reservations)
            for w in rows:
                current, observed, covered_json = observed_values[w["name"]]
                self.db.execute("UPDATE windows SET current=?,observed_at=?,covered_call_ids_json=? WHERE project_id=? AND name=?", (current, observed, covered_json, project_id, w["name"]))
            consumed = p["consumed_tokens"] + actual_tokens
            task_crossed = consumed * 2 >= p["task_cap"]
            task_state = self.db.execute("SELECT crossed,acknowledged FROM barrier_state WHERE project_id=? AND budget_key='task'", (project_id,)).fetchone()
            task_barrier = bool(task_crossed and task_state and not task_state["acknowledged"] and not task_state["crossed"])
            if task_crossed: self.db.execute("UPDATE barrier_state SET crossed=1 WHERE project_id=? AND budget_key='task'", (project_id,))
            window_barrier = False
            for w in rows:
                current, _, covered_json = observed_values[w["name"]]
                covered_ids = set(json.loads(covered_json))
                liability = self._window_liability(project_id, w, covered_ids)
                used = current - w["baseline"] + liability
                key = f"window:{w['name']}:{w['reset_id']}"
                state = self.db.execute("SELECT crossed,acknowledged FROM barrier_state WHERE project_id=? AND budget_key=?", (project_id, key)).fetchone()
                crossed = used * 2 >= w["cap"]
                if crossed and state and not state["acknowledged"] and not state["crossed"]: window_barrier = True
                if crossed: self.db.execute("UPDATE barrier_state SET crossed=1 WHERE project_id=? AND budget_key=?", (project_id, key))
            barrier = task_barrier or window_barrier
            self.db.execute("UPDATE projects SET consumed_tokens=?,replan_required=CASE WHEN ? THEN 1 ELSE replan_required END,token_violated=CASE WHEN ? THEN 1 ELSE token_violated END,window_violated=CASE WHEN ? THEN 1 ELSE window_violated END,violated=CASE WHEN ? THEN 1 ELSE violated END WHERE project_id=?", (consumed, int(barrier), int(actual_tokens > c["token_reservation"]), int(window_overrun), int(overrun), project_id))
            self._event(project_id, "call_reconciled", {"call_id": call_id, "actual_tokens": actual_tokens, "actual_windows": dict(actual_windows), "overrun": overrun})
            self.db.commit()
            return {"call_id": call_id, "actual_tokens": actual_tokens, "overrun": overrun, "replan_required": bool(barrier or p["replan_required"]), "decision": "BUDGET_VIOLATED" if overrun else "RECORDED"}
        except BaseException:
            self.db.rollback(); raise

    @_serialized
    def _claim_launch(self, project_id: str, call_id: str, *, now: int | None = None) -> str | None:
        now = int(time.time()) if now is None else _int(now, "now")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            p = self._project(project_id)
            c = self.db.execute("SELECT * FROM calls WHERE project_id=? AND call_id=? AND status='reserved'", (project_id, call_id)).fetchone()
            reason = None
            if c is None: reason = "reservation is no longer claimable"
            elif p["quarantined"]: reason = "project accounting is quarantined"
            elif not p["active"]: reason = "project is inactive"
            elif p["violated"]: reason = "project has a recorded budget violation"
            elif p["adapter"] not in SUPPORTED_ADAPTERS: reason = "provider adapter is unsupported"
            if reason is None and p["replan_required"]:
                pending = list(self.db.execute("SELECT crossing_call_id FROM barrier_state WHERE project_id=? AND crossed=1 AND acknowledged=0", (project_id,)))
                if not c["barrier_crossing"] or not pending or any(row[0] != call_id for row in pending):
                    reason = "a 50% budget barrier is pending"
            if reason is None:
                active = self.db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)).fetchone()[0]
                projected = p["consumed_tokens"] + active
                task_budget = p["task_cap"] - p["coordinator_reserve"]
                if projected > p["session_cap"]: reason = "session cap is exhausted"
                elif projected > task_budget: reason = "task cap is exhausted"
                elif projected * 5 >= task_budget * 4: reason = "80% task-token stop is active"
            if reason is None:
                for w in self.db.execute("SELECT * FROM windows WHERE project_id=?", (project_id,)):
                    snap = self._cached_snapshot(w)
                    current, _, covered_ids, error = self._validate_snapshot(w["name"], w, snap, now, p["freshness_seconds"])
                    if error: reason = f"{w['name']} usage is no longer valid: {error}"; break
                    liability = self._window_liability(project_id, w, set(covered_ids))
                    used = current - w["baseline"] + liability
                    key = f"window:{w['name']}:{w['reset_id']}"
                    state = self.db.execute("SELECT crossed,acknowledged,crossing_call_id FROM barrier_state WHERE project_id=? AND budget_key=?", (project_id, key)).fetchone()
                    if used * 2 >= w["cap"] and state and not state["crossed"] and not state["acknowledged"]:
                        self.db.execute("UPDATE barrier_state SET crossed=1,crossing_call_id=NULL WHERE project_id=? AND budget_key=?", (project_id, key))
                        self.db.execute("UPDATE projects SET replan_required=1 WHERE project_id=?", (project_id,))
                        reason = f"{w['name']} 50% barrier is active"; break
                    owns_crossing = c["barrier_crossing"] and state and state["crossing_call_id"] == call_id
                    if state and state["crossed"] and not state["acknowledged"] and not owns_crossing:
                        reason = f"{w['name']} 50% barrier is active"; break
                    if used * 5 >= w["cap"] * 4: reason = f"{w['name']} 80% stop is active"; break
                    if used > w["cap"]: reason = f"{w['name']} window cap is exhausted"; break
            if reason is not None:
                self._event(project_id, "launch_denied", {"call_id": call_id, "reason": reason})
                self.db.commit(); return None
            claim = secrets.token_urlsafe(32)
            claim_hash = hashlib.sha256(claim.encode()).hexdigest()
            cur = self.db.execute("UPDATE calls SET status='launched',launch_status='launched',launch_claim_hash=? WHERE project_id=? AND call_id=? AND status='reserved'", (claim_hash, project_id, call_id))
            if cur.rowcount != 1:
                self.db.rollback(); return None
            self._event(project_id, "call_launched", {"call_id": call_id})
            self.db.commit(); return claim
        except BaseException:
            self.db.rollback(); raise

    @_serialized
    def launch(self, project_id: str, call_id: str, *, now: int | None = None) -> bool:
        return self._claim_launch(project_id, call_id, now=now) is not None

    @_serialized
    def cancel_unlaunched(self, project_id: str, call_id: str, *, reason: str = "cancelled") -> bool:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            c = self.db.execute("SELECT * FROM calls WHERE project_id=? AND call_id=? AND status='reserved'", (project_id, call_id)).fetchone()
            if c is None: self.db.rollback(); return False
            self._release_window_reservations(project_id, json.loads(c["window_reservations_json"]))
            self.db.execute("UPDATE calls SET status='cancelled',launch_status=? WHERE project_id=? AND call_id=? AND status='reserved'", (reason, project_id, call_id))
            self._event(project_id, "call_cancelled", {"call_id": call_id, "reason": reason})
            self.db.commit(); return True
        except BaseException:
            self.db.rollback(); raise

    @_serialized
    def fail_launch(self, project_id: str, call_id: str, claim: str, error: str) -> bool:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            claim_hash = hashlib.sha256(claim.encode()).hexdigest()
            c = self.db.execute("SELECT * FROM calls WHERE project_id=? AND call_id=? AND status='launched' AND launch_claim_hash=?", (project_id, call_id, claim_hash)).fetchone()
            if c is None: self.db.rollback(); return False
            self._release_window_reservations(project_id, json.loads(c["window_reservations_json"]))
            self.db.execute("UPDATE calls SET status='launch_failed',launch_status='launch_failed',launch_claim_hash=NULL WHERE project_id=? AND call_id=? AND status='launched' AND launch_claim_hash=?", (project_id, call_id, claim_hash))
            self._event(project_id, "launch_failed", {"call_id": call_id, "error": error})
            self.db.commit(); return True
        except BaseException:
            self.db.rollback(); raise

    @_serialized
    def _event_transaction(self, project_id: str, kind: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try: self._event(project_id, kind, payload); self.db.commit()
            except BaseException: self.db.rollback(); raise

    @_serialized
    def status(self, project_id: str, *, now: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now is None else now; p = self._project(project_id)
        windows = []
        for w in self.db.execute("SELECT * FROM windows WHERE project_id=? ORDER BY name", (project_id,)):
            current = w["current"]
            windows.append({"name": w["name"], "baseline_used_micropct": w["baseline"], "cap_micropct": w["cap"], "current_used_micropct": current, "reserved_micropct": w["reserved"], "reset_id": w["reset_id"], "resets_at": w["resets_at"], "countdown_seconds": max(0, w["resets_at"] - now)})
        outstanding = self.db.execute("SELECT COALESCE(SUM(token_reservation),0) FROM calls WHERE project_id=? AND status IN ('reserved','launched')", (project_id,)).fetchone()[0]
        task_budget = p["task_cap"] - p["coordinator_reserve"]
        projected = p["consumed_tokens"] + outstanding
        ordinary_headroom = min(task_budget-projected, p["session_cap"]-projected)
        stop_headroom = (4*task_budget-1)//5 - projected
        available_tokens = max(0, min(ordinary_headroom, stop_headroom))
        if p["quarantined"] or not p["active"] or p["violated"] or p["replan_required"]: available_tokens = 0
        return {"project_id": project_id, "active": bool(p["active"]), "quarantined": bool(p["quarantined"]), "task_cap": p["task_cap"], "session_cap": p["session_cap"], "coordinator_reserve": p["coordinator_reserve"], "consumed_tokens": p["consumed_tokens"], "outstanding_token_reservations": outstanding, "remaining_tokens": available_tokens, "available_tokens": available_tokens, "revision": p["revision"], "replan_required": bool(p["replan_required"]), "violated": bool(p["violated"]), "token_violated": bool(p["token_violated"]), "window_violated": bool(p["window_violated"]), "windows": windows, "enforcement_mode": "managed-wrapper"}

    @_serialized
    def receipt(self, project_id: str, *, now: int | None = None) -> dict[str, Any]:
        result = self.status(project_id, now=now); p = self._project(project_id)
        calls = [dict(r) for r in self.db.execute("SELECT * FROM calls WHERE project_id=? ORDER BY created_at,call_id", (project_id,))]
        result.update({"plan": json.loads(p["plan_json"]), "calls": calls, "plan_revisions": [dict(r) for r in self.db.execute("SELECT * FROM revisions WHERE project_id=? ORDER BY revision", (project_id,))], "events": [dict(r) for r in self.db.execute("SELECT * FROM events WHERE project_id=? ORDER BY seq", (project_id,))], "warnings": ["50% project budget reached"] if p["replan_required"] or p["consumed_tokens"] * 2 >= p["task_cap"] else [], "overruns": [r["call_id"] for r in self.db.execute("SELECT call_id FROM calls WHERE project_id=? AND actual_tokens>token_reservation", (project_id,))], "window_actuals": {r["call_id"]: json.loads(r["actual_windows_json"]) for r in self.db.execute("SELECT call_id,actual_windows_json FROM calls WHERE project_id=? AND status='reconciled'", (project_id,))}, "window_overruns": {r["call_id"]: {name: value for name, value in json.loads(r["actual_windows_json"]).items() if value > json.loads(r["window_reservations_json"]).get(name, 0)} for r in self.db.execute("SELECT call_id,actual_windows_json,window_reservations_json FROM calls WHERE project_id=? AND status='reconciled'", (project_id,))}, "violations": {"tokens": bool(p["token_violated"]), "windows": bool(p["window_violated"])}, "enforcement_mode": "managed-wrapper"})
        return result

    @_serialized
    def verify_chain(self) -> bool:
        previous = "0" * 64
        for r in self.db.execute("SELECT * FROM events ORDER BY seq"):
            body = _json({"project_id": r["project_id"], "kind": r["kind"], "payload": json.loads(r["payload"]), "previous_hash": previous})
            if r["previous_hash"] != previous or hashlib.sha256(body.encode()).hexdigest() != r["event_hash"]: return False
            previous = r["event_hash"]
        return True

    def run_wrapped(self, project_id: str, *, call_id: str, model: str, purpose: str, command: Sequence[str], token_reservation: int, window_reservations: Mapping[str, int], now: int | None = None, snapshots: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        decision = self.reserve_call(project_id, call_id=call_id, model=model, purpose=purpose, token_reservation=token_reservation, window_reservations=window_reservations, now=now, snapshots=snapshots)
        if decision["decision"] != "ALLOW": return decision
        claim = self._claim_launch(project_id, call_id, now=now)
        if claim is None:
            self.cancel_unlaunched(project_id, call_id, reason="launch_ineligible")
            raise ControllerError("wrapper failed to claim its eligible reserved call")
        try: completed = subprocess.run(list(command), check=False, capture_output=True, text=True)
        except OSError as exc:
            self.fail_launch(project_id, call_id, claim, str(exc))
            raise
        self._event_transaction(project_id, "wrapper_completed", {"call_id": call_id, "returncode": completed.returncode})
        return {**decision, "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
