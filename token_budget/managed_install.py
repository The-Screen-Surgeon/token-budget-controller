"""Safe per-user installation for the universal protocol and Codex launcher."""
from __future__ import annotations
import hashlib, json, os, platform, shutil, subprocess, sys
from pathlib import Path

MARKER = "token-budget-managed-v1"

def default_root() -> Path:
    home = Path.home()
    if platform.system() == "Darwin": return home / "Library" / "Application Support" / "token-budget"
    if platform.system() == "Linux": return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "token-budget"
    raise RuntimeError("managed install supports Linux and macOS only")

def _payloads() -> dict[str, str]:
    root = Path(__file__).resolve().parent.parent
    return {
        "UNIVERSAL-USAGE-BUDGET.md": (root / "UNIVERSAL-USAGE-BUDGET.md").read_text(),
        "skills/token-budget/SKILL.md": "# Token Budget managed workflow\n\nBefore model work, inspect factual usage and gate each call through the managed Token Budget controller. Calls outside the managed Codex launcher are unmanaged. Reconcile only with factual post-call sources; otherwise leave reconciliation pending.\n",
        "skills/token-budget/UNIVERSAL-USAGE-BUDGET.md": (root / "UNIVERSAL-USAGE-BUDGET.md").read_text(),
    }

def _destinations(root: Path | None) -> tuple[Path, dict[str, Path], bool]:
    if root is not None:
        return root, {rel: root / rel for rel in _payloads()} | {
            "bin/codex-managed": root / "bin" / "codex-managed"}, True
    central = default_root()
    home = Path.home()
    launcher = (home / ".local" / "bin" / "codex-managed")
    skill = home / ".codex" / "skills" / "token-budget"
    dest = {"UNIVERSAL-USAGE-BUDGET.md": central / "UNIVERSAL-USAGE-BUDGET.md",
            "skills/token-budget/SKILL.md": skill / "SKILL.md",
            "skills/token-budget/UNIVERSAL-USAGE-BUDGET.md": skill / "UNIVERSAL-USAGE-BUDGET.md",
            "bin/codex-managed": launcher}
    return central, dest, False

def _launcher_content() -> str:
    py = sys.executable
    return f"#!/bin/sh\nexec {json.dumps(py)} -m token_budget.cli --db \"${{TOKEN_BUDGET_DB:-$HOME/.local/state/token-budget/controller.sqlite}}\" codex-managed \"$@\"\n"

def install(root: Path | None = None, *, dry_run: bool = False) -> dict:
    target, destinations, staging = _destinations(root)
    manifest_path = target / ".token-budget-manifest.json"
    payloads = _payloads() | {"bin/codex-managed": _launcher_content()}
    manifest = {"marker": MARKER, "files": sorted(payloads),
                "paths": {name: str(destinations[name]) for name in payloads},
                "sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in payloads.items()},
                "staging": staging}
    if target.exists() and not target.is_dir(): raise RuntimeError("install destination is not a directory")
    if manifest_path.exists():
        try: old = json.loads(manifest_path.read_text())
        except Exception: raise RuntimeError("managed manifest is malformed") from None
        if old.get("marker") != MARKER: raise RuntimeError("install destination is not owned by this installer")
        if set(old.get("files", [])) != set(payloads):
            raise RuntimeError("managed manifest file set differs from this installer")
        if any(Path(old.get("paths", {}).get(rel, "")) != destinations[rel] for rel in payloads):
            raise RuntimeError("managed manifest destination mismatch")
    for rel, content in payloads.items():
        path = destinations[rel]
        if path.is_symlink(): raise RuntimeError(f"refusing to manage symbolic link: {path}")
        if path.exists() and not manifest_path.exists():
            raise RuntimeError(f"refusing to claim or overwrite unowned file: {path}")
        if path.exists() and manifest_path.exists():
            if rel not in old.get("files", []): raise RuntimeError(f"refusing to overwrite unowned file: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != old.get("sha256", {}).get(rel): raise RuntimeError(f"refusing to overwrite modified managed file: {path}")
    if dry_run: return {"action": "install", "root": str(target), "files": manifest["files"], "dry_run": True}
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for rel, content in _payloads().items():
        path = destinations[rel]; path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(content); path.chmod(0o600)
    path = destinations["bin/codex-managed"]; path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(payloads["bin/codex-managed"]); path.chmod(0o700)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return {"action": "install", "root": str(target), "files": manifest["files"], "dry_run": False}

def doctor(root: Path | None = None) -> dict:
    target, destinations, staging = _destinations(root); manifest = target / ".token-budget-manifest.json"
    try: data = json.loads(manifest.read_text())
    except Exception: data = {}
    owned = data.get("marker") == MARKER
    files = data.get("files", []) if owned else []
    valid = owned and all(destinations.get(f, Path("/nonexistent")) == Path(data.get("paths", {}).get(f, ""))
                          and destinations.get(f, Path("/nonexistent")).is_file()
                          and hashlib.sha256(destinations[f].read_bytes()).hexdigest() == data.get("sha256", {}).get(f)
                          for f in files)
    python_available = Path(sys.executable).is_file()
    try:
        module_check = subprocess.run([sys.executable, "-c", "import token_budget"],
                                      cwd=Path.home(), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=3, check=False)
        module_importable = module_check.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        module_importable = False
    codex_path = shutil.which("codex")
    launcher = destinations.get("bin/codex-managed")
    launcher_path = bool(launcher and str(launcher.parent) in os.environ.get("PATH", "").split(os.pathsep))
    skill_path = destinations.get("skills/token-budget/SKILL.md")
    skill_discoverable = bool(skill_path and skill_path.is_file() and ".codex/skills/" in skill_path.as_posix())
    boundary = "Explicit codex-managed calls only; Codex UI and ordinary CLI calls remain unmanaged."
    usable = bool(valid and not staging and python_available and module_importable and codex_path and launcher_path and skill_discoverable)
    guidance = []
    if not python_available: guidance.append("Install Python 3 and rerun doctor.")
    if python_available and not module_importable: guidance.append("Install Token Budget into the launcher's Python environment (for example: python3 -m pip install --user -e /path/to/token-budget).")
    if not codex_path: guidance.append("Install Codex CLI and ensure `codex` is on PATH.")
    if not launcher_path: guidance.append(f"Add {launcher.parent if launcher else '~/.local/bin'} to PATH and rerun doctor.")
    if not skill_discoverable: guidance.append("Ensure the managed skill is under ~/.codex/skills/token-budget and rerun doctor.")
    if staging: guidance.append("Staging installs are intentionally not discoverable; install without --root after reviewing the file list.")
    return {"root": str(target), "installed": bool(valid), "usable": usable,
            "staging_only": staging, "owned_files": [f for f in files if destinations.get(f) and destinations[f].is_file()],
            "python_available": python_available, "python_module_importable": module_importable, "python": sys.executable,
            "codex_available": bool(codex_path), "codex_path": codex_path,
            "launcher_discoverable": launcher_path, "launcher_path": str(launcher) if launcher else None,
            "skill_discoverable": skill_discoverable, "skill_path": str(skill_path) if skill_path else None,
            "unmanaged_boundary": boundary, "platform": platform.system(), "guidance": guidance}

def uninstall(root: Path | None = None, *, dry_run: bool = False) -> dict:
    target, destinations, _ = _destinations(root); manifest_path = target / ".token-budget-manifest.json"
    try: manifest = json.loads(manifest_path.read_text())
    except Exception: raise RuntimeError("valid managed manifest not found") from None
    if manifest.get("marker") != MARKER: raise RuntimeError("installation ownership marker mismatch")
    expected_files = set(_payloads()) | {"bin/codex-managed"}
    if set(manifest.get("files", [])) != expected_files:
        raise RuntimeError("managed manifest file set differs from this installer")
    if any(Path(manifest.get("paths", {}).get(rel, "")) != destinations[rel] for rel in expected_files):
        raise RuntimeError("managed manifest destination mismatch")
    removed = []
    for rel in manifest.get("files", []):
        path = destinations[rel]
        if path.exists():
            if path.is_symlink() or not path.is_file(): raise RuntimeError(f"refusing to remove unsafe path: {path}")
            expected = manifest.get("sha256", {}).get(rel)
            if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f"refusing to remove modified managed file: {path}")
            removed.append(rel)
    if not dry_run:
        for rel in removed: destinations[rel].unlink()
        manifest_path.unlink()
        if manifest.get("staging"):
            parents = set()
            for rel in removed:
                parent = destinations[rel].parent
                while parent != target and target in parent.parents:
                    parents.add(parent); parent = parent.parent
            for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
                try: parent.rmdir()
                except OSError: pass
        try: target.rmdir()
        except OSError: pass
    return {"action": "uninstall", "root": str(target), "removed": removed, "dry_run": dry_run}
