from __future__ import annotations
import argparse, json
from pathlib import Path
from .adapters import codex_events, claude_events
from .codex_adapter import CodexAppServerAdapter, CodexUsageError
from .managed_install import install as managed_install, doctor as managed_doctor, uninstall as managed_uninstall
from .managed_codex import launch_managed_codex
from .core import Ledger
from .policy import evaluate_policy
from .workflow import plan_project, build_receipt
from .controller import Controller, ControllerError

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="token-budget")
    parser.add_argument("--db", default="token-budget.sqlite", help="SQLite ledger path")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest"); ingest.add_argument("--codex", type=Path); ingest.add_argument("--claude", type=Path)
    sub.add_parser("summary")
    policy = sub.add_parser("policy", help="evaluate adaptive execution policy")
    policy.add_argument("--provider-used-percent", type=float, required=True)
    policy.add_argument("--task-observed-tokens", type=int, required=True)
    policy.add_argument("--task-cap", type=int, required=True)
    policy.add_argument("--override", action="store_true", help="explicitly permit a stopped call")
    plan = sub.add_parser("plan", help="plan a complete project from JSON")
    plan.add_argument("--input", type=Path, help="JSON input file; stdin when omitted")
    receipt = sub.add_parser("receipt", help="create a build receipt from JSON")
    receipt.add_argument("--input", type=Path, help="JSON input file; stdin when omitted")
    create = sub.add_parser("create-project", help="create a managed controller project")
    create.add_argument("--input", type=Path)
    status = sub.add_parser("status", help="inspect managed project status")
    status.add_argument("project_id")
    reserve = sub.add_parser("reserve", help="gate and reserve a model call")
    reserve.add_argument("--input", type=Path)
    revise = sub.add_parser("revise", help="record a complete remaining plan revision")
    revise.add_argument("--input", type=Path)
    reconcile = sub.add_parser("reconcile", help="record factual completed call usage")
    reconcile.add_argument("--input", type=Path)
    export = sub.add_parser("export-receipt", help="export a factual controller receipt")
    export.add_argument("project_id")
    wrapper = sub.add_parser("exec", help="run a subprocess after the managed gate")
    wrapper.add_argument("--input", type=Path, required=True, help="JSON gate input; command is its command field")
    codex = sub.add_parser("codex-managed", help="launch Codex CLI through the controller gate; outside calls remain unmanaged")
    codex.add_argument("--input", type=Path, required=True, help="JSON containing project_id, call metadata, reservations, and codex_args")
    for name in ("install", "doctor", "uninstall"):
        cmd = sub.add_parser(f"managed-{name}", help=f"{name} user-scoped Token Budget files")
        cmd.add_argument("--root", type=Path)
        cmd.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv); ledger = Ledger(args.db)
    try:
        if args.command == "ingest":
            events = []
            if args.codex: events.extend(codex_events(args.codex))
            if args.claude: events.extend(claude_events(args.claude))
            added, skipped = ledger.add_many(events)
            print(json.dumps({"added": added, "skipped": skipped, "events": len(events)}))
        elif args.command == "summary":
            print(json.dumps(ledger.summary(), sort_keys=True))
        elif args.command == "policy":
            decision = evaluate_policy(
                args.provider_used_percent,
                args.task_observed_tokens,
                args.task_cap,
                args.override,
            )
            print(json.dumps(decision.to_dict(), sort_keys=True))
        elif args.command == "plan":
            raw = args.input.read_text() if args.input else __import__("sys").stdin.read()
            print(json.dumps(plan_project(json.loads(raw)), sort_keys=True))
        elif args.command == "receipt":
            raw = args.input.read_text() if args.input else __import__("sys").stdin.read()
            data = json.loads(raw)
            result = build_receipt(data["plan"], model_usage=data["model_usage"],
                                   final_usage=data["final_usage"], now=data["now"])
            print(json.dumps(result, sort_keys=True))
        elif args.command in {"create-project", "reserve", "revise", "reconcile", "exec"}:
            raw = args.input.read_text() if args.input else __import__("sys").stdin.read()
            data = json.loads(raw)
            controller = Controller(args.db)
            try:
                if args.command == "create-project":
                    result = controller.create_project(**data)
                elif args.command == "reserve":
                    result = controller.reserve_call(**data)
                elif args.command == "revise":
                    result = controller.record_plan_revision(**data)
                elif args.command == "reconcile":
                    result = controller.reconcile(**data)
                else:
                    command = data.pop("command")
                    result = controller.run_wrapped(command=command, **data)
            finally:
                controller.close()
            print(json.dumps(result, sort_keys=True))
        elif args.command == "status":
            controller = Controller(args.db)
            try: print(json.dumps(controller.status(args.project_id), sort_keys=True))
            finally: controller.close()
        elif args.command == "export-receipt":
            controller = Controller(args.db)
            try: print(json.dumps(controller.receipt(args.project_id), sort_keys=True))
            finally: controller.close()
        elif args.command in {"managed-install", "managed-doctor", "managed-uninstall"}:
            fn = {"managed-install": managed_install, "managed-doctor": managed_doctor,
                  "managed-uninstall": managed_uninstall}[args.command]
            options = {"root": args.root}
            if args.command != "managed-doctor": options["dry_run"] = args.dry_run
            print(json.dumps(fn(**options), sort_keys=True))
        elif args.command == "codex-managed":
            data = json.loads(args.input.read_text())
            argsv = data.get("codex_args")
            if not isinstance(argsv, list) or any(not isinstance(x, str) for x in argsv):
                raise ValueError("codex_args must be an array of strings")
            adapter = CodexAppServerAdapter()
            controller = Controller(args.db)
            try:
                call_data = {k: v for k, v in data.items() if k != "codex_args"}
                result = launch_managed_codex(controller, adapter, **call_data, codex_args=argsv)
                print(json.dumps(result, sort_keys=True))
            finally: controller.close()
    finally: ledger.close()
    return 0

if __name__ == "__main__": raise SystemExit(main())
