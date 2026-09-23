"""Explicit Codex launch boundary; never infers per-call usage."""
from __future__ import annotations
import json
import subprocess
from typing import Any, Mapping, Sequence

from .controller import Controller, ControllerError


def _controller_snapshots(raw: Mapping[str, Any], controller: Controller,
                           project_id: str) -> dict[str, dict[str, Any]]:
    rows = {row["name"]: row for row in controller.db.execute(
        "SELECT name,reset_id,resets_at,covered_call_ids_json FROM windows WHERE project_id=?", (project_id,))}
    snapshots = {}
    for name, win in raw["windows"].items():
        prior = rows.get(name)
        compatible = bool(prior and prior["reset_id"] == win["reset_id"] and prior["resets_at"] == win["resets_at"])
        covered = json.loads(prior["covered_call_ids_json"]) if compatible else []
        snapshots[name] = {"current_used_micropct": win["used_micropct"],
                           "reset_id": win["reset_id"], "resets_at": win["resets_at"],
                           "observed_at": raw["observed_at"], "covered_call_ids": covered}
    return snapshots


def launch_managed_codex(controller: Controller, adapter: Any, *, project_id: str,
                         call_id: str, model: str, purpose: str,
                         token_reservation: int, window_reservations: Mapping[str, int],
                         codex_args: Sequence[str], runner=subprocess.run,
                         now: int | None = None) -> dict[str, Any]:
    before = adapter.snapshot()
    decision = controller.reserve_call(project_id, call_id=call_id, model=model,
        purpose=purpose, token_reservation=token_reservation,
        window_reservations=window_reservations,
        snapshots=_controller_snapshots(before, controller, project_id), now=now)
    if decision["decision"] != "ALLOW": return decision
    claim = controller._claim_launch(project_id, call_id, now=now)
    if claim is None:
        controller.cancel_unlaunched(project_id, call_id, reason="launch_ineligible")
        raise ControllerError("managed Codex launch claim failed")
    try:
        completed = runner(["codex", *codex_args], check=False)
    except OSError as exc:
        controller.fail_launch(project_id, call_id, claim, type(exc).__name__)
        raise
    try:
        after = adapter.snapshot()
        factual = {k: w["used_micropct"] for k, w in after["windows"].items()}
        post_error = None
    except Exception as exc:
        factual, post_error = None, type(exc).__name__
    return {"decision": "LAUNCHED", "call_id": call_id,
            "returncode": completed.returncode,
            "reconciliation": "pending_factual_per_call_token_usage",
            "post_call_window_snapshot": factual, "post_call_snapshot_error": post_error,
            "content_stored": False, "enforcement_mode": "managed-codex-launcher"}
