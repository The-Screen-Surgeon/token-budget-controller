"""User-scoped Codex skill and launcher installation with ownership checks."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

MARKER = "token-budget-managed-v1"
MANIFEST_NAME = ".token-budget-manifest.json"
RECOVERY_NAME = ".token-budget-recovery.json"
LOCK_NAME = ".token-budget.lock"


def default_root() -> Path:
    home = Path.home()
    if platform.system() == "Darwin": return Path(os.path.abspath(home / "Library" / "Application Support" / "token-budget"))
    if platform.system() == "Linux": return Path(os.path.abspath(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "token-budget"))
    raise RuntimeError("managed install supports Linux and macOS only")


def _codex_home() -> Path:
    return Path(os.path.abspath(os.environ.get("CODEX_HOME", Path.home() / ".codex")))


def _payloads() -> dict[str, str]:
    repo = Path(__file__).resolve().parent.parent
    protocol = (repo / "UNIVERSAL-USAGE-BUDGET.md").read_text()
    skill = "---\nname: token-budget\ndescription: Check factual Codex usage and route supported CLI calls through the managed Token Budget gate.\n---\n\n# Token Budget managed workflow\n\nBefore model work, inspect factual usage and gate each supported Codex CLI call through `codex-managed`. Calls outside the launcher, including Codex UI calls, remain unmanaged. Reconcile only with factual post-call per-call usage; otherwise leave reconciliation pending.\n"
    return {
        "UNIVERSAL-USAGE-BUDGET.md": protocol,
        "skills/token-budget/SKILL.md": skill,
        "skills/token-budget/UNIVERSAL-USAGE-BUDGET.md": protocol,
    }


def _destinations(root: Path | None) -> tuple[Path, dict[str, Path], bool]:
    if root is not None:
        stage = Path(os.path.abspath(root))
        return stage, {rel: stage / rel for rel in _payloads()} | {"bin/codex-managed": stage / "bin/codex-managed"}, True
    central = default_root()
    home = Path.home()
    skill = _codex_home() / "skills" / "token-budget"
    dest = {"UNIVERSAL-USAGE-BUDGET.md": central / "UNIVERSAL-USAGE-BUDGET.md",
            "skills/token-budget/SKILL.md": skill / "SKILL.md",
            "skills/token-budget/UNIVERSAL-USAGE-BUDGET.md": skill / "UNIVERSAL-USAGE-BUDGET.md",
            "bin/codex-managed": home / ".local" / "bin" / "codex-managed"}
    return central, dest, False


def _launcher_content() -> str:
    return (f"#!/bin/sh\nexec {json.dumps(sys.executable)} -m token_budget.cli --db "
            "\"${TOKEN_BUDGET_DB:-$HOME/.local/state/token-budget/controller.sqlite}\" "
            "codex-managed \"$@\"\n")


def _expected(target: Path, destinations: dict[str, Path], staging: bool) -> dict:
    payloads = _payloads() | {"bin/codex-managed": _launcher_content()}
    return {"marker": MARKER, "files": sorted(payloads),
            "paths": {rel: str(destinations[rel]) for rel in payloads},
            "sha256": {rel: hashlib.sha256(content.encode()).hexdigest() for rel, content in payloads.items()},
            "staging": staging}


def _dirfd(path: Path, *, create: bool) -> int | None:
    """Open a directory path component by component without following symlinks."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, flags | nofollow, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                try: os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError: pass
                os.fsync(fd)
                child = os.open(component, flags | nofollow, dir_fd=fd)
            except OSError as exc:
                os.close(fd)
                raise RuntimeError(f"unsafe directory component: {absolute}") from exc
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        try: os.close(fd)
        except OSError: pass
        raise


def _check_path_no_symlinks(path: Path) -> None:
    fd = _dirfd(path, create=False)
    if fd is not None: os.close(fd)


def _read_at(parent: Path, name: str) -> bytes | None:
    fd = _dirfd(parent, create=False)
    if fd is None: return None
    try:
        try: filefd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        except FileNotFoundError: return None
        except OSError as exc: raise RuntimeError(f"unsafe managed file: {parent / name}") from exc
        try:
            if not stat.S_ISREG(os.fstat(filefd).st_mode): raise RuntimeError(f"managed path is not a regular file: {parent / name}")
            chunks = []
            while True:
                chunk = os.read(filefd, 65536)
                if not chunk: break
                chunks.append(chunk)
            return b"".join(chunks)
        finally: os.close(filefd)
    finally: os.close(fd)


def _exists_at(path: Path) -> bool:
    fd = _dirfd(path.parent, create=False)
    if fd is None: return False
    try:
        try: os.stat(path.name, dir_fd=fd, follow_symlinks=False); return True
        except FileNotFoundError: return False
    finally: os.close(fd)


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = _dirfd(path.parent, create=True)
    assert fd is not None
    temp = f".tb-{uuid.uuid4().hex}.tmp"
    try:
        tmpfd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=fd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(tmpfd, view):]
            os.fsync(tmpfd)
        finally: os.close(tmpfd)
        # link is atomic and fails if a destination appeared after preflight.
        os.link(temp, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.unlink(temp, dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        try: os.unlink(temp, dir_fd=fd)
        except OSError: pass
        raise
    finally: os.close(fd)


def _replace_owned(path: Path, old_data: bytes, new_data: bytes) -> None:
    """Atomically replace a verified private recovery record under its dirfd."""
    fd = _dirfd(path.parent, create=False)
    if fd is None: raise RuntimeError("recovery directory disappeared")
    temp = f".tb-{uuid.uuid4().hex}.tmp"
    try:
        oldfd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        try:
            old_st = os.fstat(oldfd)
            if not stat.S_ISREG(old_st.st_mode) or os.read(oldfd, max(old_st.st_size, 1) + 1) != old_data:
                raise RuntimeError("recovery metadata changed during operation")
        finally: os.close(oldfd)
        tmpfd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
        try:
            view = memoryview(new_data)
            while view: view = view[os.write(tmpfd, view):]
            os.fsync(tmpfd)
        finally: os.close(tmpfd)
        current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (old_st.st_dev, old_st.st_ino):
            raise RuntimeError("recovery metadata was replaced concurrently")
        os.replace(temp, path.name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        try: os.unlink(temp, dir_fd=fd)
        except OSError: pass
        raise
    finally: os.close(fd)


def _link_staged(path: Path, temp_name: str, data: bytes, mode: int = 0o600) -> None:
    fd = _dirfd(path.parent, create=True)
    assert fd is not None
    try:
        tempfd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode, dir_fd=fd)
        try:
            view = memoryview(data)
            while view: view = view[os.write(tempfd, view):]
            os.fsync(tempfd)
        finally: os.close(tempfd)
        os.fsync(fd)
    finally: os.close(fd)


def _stat_identity(path: Path) -> tuple[int, int] | None:
    fd = _dirfd(path.parent, create=False)
    if fd is None: return None
    try:
        try:
            st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode): raise RuntimeError(f"unsafe managed file: {path}")
            return st.st_dev, st.st_ino
        except FileNotFoundError: return None
    finally: os.close(fd)


def _link_temp_to_destination(path: Path, temp_name: str) -> None:
    fd = _dirfd(path.parent, create=False)
    if fd is None: raise RuntimeError("staged payload directory disappeared")
    try:
        os.link(temp_name, path.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally: os.close(fd)


def _unlink_temp(path: Path, temp_name: str) -> None:
    fd = _dirfd(path.parent, create=False)
    if fd is None: return
    try:
        try: os.unlink(temp_name, dir_fd=fd); os.fsync(fd)
        except FileNotFoundError: pass
    finally: os.close(fd)


def _capture_for_removal(path: Path, quarantine_name: str) -> None:
    """Atomically move a pathname into this transaction's private name."""
    fd = _dirfd(path.parent, create=False)
    if fd is None: raise RuntimeError(f"managed directory disappeared: {path.parent}")
    try:
        try:
            os.stat(quarantine_name, dir_fd=fd, follow_symlinks=False)
            raise RuntimeError(f"uninstall quarantine already exists: {path.parent / quarantine_name}")
        except FileNotFoundError:
            pass
        try: os.rename(path.name, quarantine_name, src_dir_fd=fd, dst_dir_fd=fd)
        except FileNotFoundError as exc: raise RuntimeError(f"managed file disappeared during uninstall: {path}") from exc
        os.fsync(fd)
    finally: os.close(fd)


def _unlink(path: Path) -> None:
    fd = _dirfd(path.parent, create=False)
    if fd is None: return
    try:
        try:
            st = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode): raise RuntimeError(f"refusing to remove unsafe path: {path}")
            os.unlink(path.name, dir_fd=fd); os.fsync(fd)
        except FileNotFoundError: pass
    finally: os.close(fd)


@contextmanager
def _ownership_lock(target: Path):
    rootfd = _dirfd(target, create=True)
    assert rootfd is not None
    try:
        lockfd = os.open(LOCK_NAME, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=rootfd)
        try:
            if not stat.S_ISREG(os.fstat(lockfd).st_mode): raise RuntimeError("unsafe ownership lock")
            fcntl.flock(lockfd, fcntl.LOCK_EX)
            yield rootfd
        finally:
            fcntl.flock(lockfd, fcntl.LOCK_UN); os.close(lockfd)
    finally: os.close(rootfd)


def _manifest(raw: bytes | None, expected: dict, *, recovery: bool = False) -> dict | None:
    if raw is None: return None
    try: data = json.loads(raw)
    except Exception: raise RuntimeError("managed manifest is malformed") from None
    if recovery:
        fields = {"marker", "action", "files", "paths", "sha256", "staging", "completed", "staged"}
        if (not isinstance(data, dict) or set(data) != fields or data["marker"] != MARKER
                or not isinstance(data["action"], str) or data["action"] not in {"install", "uninstall"}):
            raise RuntimeError("managed recovery metadata is malformed")
        for key in ("files", "paths", "sha256", "staging"):
            if data[key] != expected[key]: raise RuntimeError("recovery metadata does not match expected installation")
        _validate_hashes(data["sha256"], expected["files"])
        if data["sha256"] != expected["sha256"]:
            raise RuntimeError("recovery hashes do not match expected payloads")
        if (not isinstance(data["completed"], list) or any(not isinstance(v, str) for v in data["completed"])
                or len(set(data["completed"])) != len(data["completed"])
                or not set(data["completed"]).issubset(expected["files"])):
            raise RuntimeError("managed recovery completion list is malformed")
        if not isinstance(data["staged"], dict) or not set(data["staged"]).issubset(expected["files"]) or any(not isinstance(n, str) or re.fullmatch(r"\.tb-[0-9a-f]{32}\.tmp", n) is None for n in data["staged"].values()):
            raise RuntimeError("managed recovery staged file list is malformed")
        return data
    if not isinstance(data, dict) or set(data) != set(expected):
        raise RuntimeError("managed manifest schema is invalid")
    if (data["marker"] != MARKER or data["files"] != expected["files"] or data["paths"] != expected["paths"]
            or type(data["staging"]) is not bool or data["staging"] != expected["staging"]
            or not isinstance(data["files"], list) or any(not isinstance(v, str) for v in data["files"])
            or not isinstance(data["paths"], dict) or any(not isinstance(v, str) for v in data["paths"].values())):
        raise RuntimeError("managed manifest ownership or destination mismatch")
    _validate_hashes(data["sha256"], expected["files"])
    if data["sha256"] != expected["sha256"]:
        raise RuntimeError("managed manifest hashes do not match expected payloads")
    return data


def _validate_hashes(hashes: object, files: list[str]) -> None:
    if not isinstance(hashes, dict) or set(hashes) != set(files) or any(not isinstance(hashes[f], str) or re.fullmatch(r"[0-9a-f]{64}", hashes[f]) is None for f in files):
        raise RuntimeError("managed manifest must contain a valid SHA-256 for every owned file")


def _recovery_record(expected: dict, action: str) -> bytes:
    return (json.dumps({"marker": MARKER, "action": action, **{k: expected[k] for k in ("files", "paths", "sha256", "staging")}, "completed": [], "staged": {}}, sort_keys=True, indent=2) + "\n").encode()


def _recovery_update(path: Path, current: dict, mutate) -> dict:
    old = _read_at(path.parent, path.name)
    if old is None: raise RuntimeError("durable recovery metadata disappeared")
    updated = dict(current)
    mutate(updated)
    new = (json.dumps(updated, sort_keys=True, indent=2) + "\n").encode()
    _replace_owned(path, old, new)
    return updated


def install(root: Path | None = None, *, dry_run: bool = False) -> dict:
    target, destinations, staging = _destinations(root)
    expected = _expected(target, destinations, staging)
    payloads = _payloads() | {"bin/codex-managed": _launcher_content()}
    if dry_run:
        _check_path_no_symlinks(target)
        for path in destinations.values(): _check_path_no_symlinks(path.parent)
        return {"action": "install", "root": str(target), "files": expected["files"], "dry_run": True}
    with _ownership_lock(target):
        manifest_raw = _read_at(target, MANIFEST_NAME)
        recovery_raw = _read_at(target, RECOVERY_NAME)
        existing = _manifest(manifest_raw, expected)
        recovery = _manifest(recovery_raw, expected, recovery=True) if recovery_raw else None
        if recovery and recovery["action"] != "install": raise RuntimeError("an uninstall recovery is pending")
        if existing:
            if recovery: _unlink(target / RECOVERY_NAME)
            for rel in expected["files"]:
                if hashlib.sha256(_read_at(destinations[rel].parent, destinations[rel].name) or b"").hexdigest() != existing["sha256"][rel]:
                    raise RuntimeError(f"refusing to overwrite modified managed file: {destinations[rel]}")
            return {"action": "install", "root": str(target), "files": expected["files"], "dry_run": False}
        if recovery is None:
            for rel in expected["files"]:
                if _exists_at(destinations[rel]): raise RuntimeError(f"refusing to claim or overwrite unowned file: {destinations[rel]}")
            recovery_raw = _recovery_record(expected, "install")
            _write_new(target / RECOVERY_NAME, recovery_raw, 0o600)
            recovery = json.loads(recovery_raw)
        for rel in expected["files"]:
            path = destinations[rel]
            if rel in recovery["completed"]:
                existing_bytes = _read_at(path.parent, path.name)
                if existing_bytes is not None and hashlib.sha256(existing_bytes).hexdigest() != expected["sha256"][rel]:
                    raise RuntimeError(f"refusing to replace modified partially installed file: {path}")
                if existing_bytes is not None: continue
                recovery = _recovery_update(target / RECOVERY_NAME, recovery,
                    lambda d, r=rel: d["completed"].remove(r))
            temp_name = recovery["staged"].get(rel)
            if temp_name is None:
                temp_name = f".tb-{uuid.uuid4().hex}.tmp"
                recovery = _recovery_update(target / RECOVERY_NAME, recovery,
                    lambda d, r=rel, n=temp_name: d["staged"].__setitem__(r, n))
            temp_path = path.parent / temp_name
            temp_bytes = _read_at(path.parent, temp_name)
            if temp_bytes is None:
                if _exists_at(path):
                    raise RuntimeError(f"destination appeared before managed staging: {path}")
                _link_staged(path, temp_name, payloads[rel].encode(), 0o700 if rel == "bin/codex-managed" else 0o600)
                temp_bytes = _read_at(path.parent, temp_name)
            if hashlib.sha256(temp_bytes or b"").hexdigest() != expected["sha256"][rel]:
                raise RuntimeError(f"staged managed payload was modified: {temp_path}")
            temp_identity = _stat_identity(temp_path)
            destination_identity = _stat_identity(path)
            if destination_identity is None:
                _link_temp_to_destination(path, temp_name)
                destination_identity = _stat_identity(path)
            if destination_identity != temp_identity:
                raise RuntimeError(f"destination conflicts with this install transaction: {path}")
            recovery = _recovery_update(target / RECOVERY_NAME, recovery,
                lambda d, r=rel: d["completed"].append(r))
            _unlink_temp(path, temp_name)
        for rel, temp_name in recovery["staged"].items():
            _unlink_temp(destinations[rel], temp_name)
        _write_new(target / MANIFEST_NAME, (json.dumps(expected, sort_keys=True, indent=2) + "\n").encode(), 0o600)
        _unlink(target / RECOVERY_NAME)
        return {"action": "install", "root": str(target), "files": expected["files"], "dry_run": False}


def _parse_skill(text: str) -> bool:
    if not text.startswith("---\n"): return False
    parts = text.split("---\n", 2)
    if len(parts) != 3: return False
    front = {}
    for line in parts[1].splitlines():
        if ":" not in line: return False
        key, value = line.split(":", 1); front[key.strip()] = value.strip()
    return set(front) == {"name", "description"} and front.get("name") == "token-budget" and bool(front.get("description"))


def doctor(root: Path | None = None) -> dict:
    target, destinations, staging = _destinations(root)
    expected = _expected(target, destinations, staging)
    unsafe_paths = False
    try: raw = _read_at(target, MANIFEST_NAME)
    except RuntimeError: raw = None; unsafe_paths = True
    try: data = _manifest(raw, expected)
    except RuntimeError: data = None
    valid_files = []
    if data:
        for rel in expected["files"]:
            try: content = _read_at(destinations[rel].parent, destinations[rel].name)
            except RuntimeError: content = None; unsafe_paths = True
            if content is not None and hashlib.sha256(content).hexdigest() == data["sha256"][rel]: valid_files.append(rel)
    valid = data is not None and set(valid_files) == set(expected["files"])
    launcher = destinations["bin/codex-managed"]
    try: launcher_content = _read_at(launcher.parent, launcher.name)
    except RuntimeError: launcher_content = None; unsafe_paths = True
    launcher_executable = False
    fd = _dirfd(launcher.parent, create=False)
    if fd is not None:
        try:
            try: launcher_executable = bool(os.stat(launcher.name, dir_fd=fd, follow_symlinks=False).st_mode & 0o111)
            except FileNotFoundError: pass
        finally: os.close(fd)
    which_launcher = shutil.which("codex-managed")
    launcher_discoverable = bool(which_launcher and Path(which_launcher).absolute() == launcher.absolute())
    python_available = Path(sys.executable).is_file()
    try:
        module_importable = subprocess.run([sys.executable, "-c", "import token_budget"], cwd=Path.home(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired): module_importable = False
    codex_path = shutil.which("codex")
    skill = destinations["skills/token-budget/SKILL.md"]
    try: skill_raw = _read_at(skill.parent, skill.name)
    except RuntimeError: skill_raw = None; unsafe_paths = True
    skill_layout = skill.parent == _codex_home() / "skills" / "token-budget"
    skill_discoverable = bool(skill_layout and skill_raw is not None and _parse_skill(skill_raw.decode("utf-8", errors="replace")))
    usable = bool(valid and not unsafe_paths and not staging and launcher_executable and launcher_discoverable and python_available and module_importable and codex_path and skill_discoverable)
    guidance = []
    if staging: guidance.append("Staging installs are intentionally unusable; install without --root after review.")
    if not python_available or not module_importable: guidance.append("Install Python 3 and Token Budget into the launcher's Python environment.")
    if not codex_path: guidance.append("Install Codex CLI and ensure `codex` is on PATH.")
    if not launcher_discoverable: guidance.append(f"Add {launcher.parent} to PATH and verify `shutil.which('codex-managed')` resolves to the installed launcher.")
    if not skill_discoverable: guidance.append(f"Install a valid token-budget SKILL.md at {skill}.")
    if not valid: guidance.append("Run managed-install to repair a missing or invalid owned manifest/file set.")
    if unsafe_paths: guidance.append("A managed path contains a symbolic link or unsafe file type; remove the link and rerun doctor.")
    return {"root": str(target), "installed": bool(valid), "usable": usable, "staging_only": staging,
        "owned_files": valid_files, "python_available": python_available, "python_module_importable": module_importable,
        "python": sys.executable, "codex_available": bool(codex_path), "codex_path": codex_path,
        "launcher_executable": launcher_executable, "launcher_discoverable": launcher_discoverable,
        "launcher_path": str(launcher), "skill_discoverable": skill_discoverable, "skill_path": str(skill),
        "unmanaged_boundary": "Explicit codex-managed calls only; Codex UI and ordinary CLI calls remain unmanaged.",
        "platform": platform.system(), "unsafe_paths": unsafe_paths, "guidance": guidance}


def uninstall(root: Path | None = None, *, dry_run: bool = False) -> dict:
    target, destinations, staging = _destinations(root)
    expected = _expected(target, destinations, staging)
    _check_path_no_symlinks(target)
    for path in destinations.values(): _check_path_no_symlinks(path.parent)
    target_exists = _dirfd(target, create=False)
    if target_exists is None and not dry_run:
        raise RuntimeError("valid managed manifest not found")
    if target_exists is not None: os.close(target_exists)
    lock = nullcontext(None) if dry_run else _ownership_lock(target)
    with lock:
        manifest_raw = _read_at(target, MANIFEST_NAME)
        recovery_raw = _read_at(target, RECOVERY_NAME)
        if manifest_raw is not None:
            manifest = _manifest(manifest_raw, expected)
        else: manifest = None
        recovery = _manifest(recovery_raw, expected, recovery=True) if recovery_raw else None
        if recovery and recovery["action"] != "uninstall": raise RuntimeError("an install recovery is pending")
        if manifest is None and recovery is None: raise RuntimeError("valid managed manifest not found")
        hashes = manifest["sha256"] if manifest else recovery["sha256"]
        for rel in expected["files"]:
            raw = _read_at(destinations[rel].parent, destinations[rel].name)
            if raw is not None and hashlib.sha256(raw).hexdigest() != hashes[rel]:
                raise RuntimeError(f"refusing to remove modified managed file: {destinations[rel]}")
        if dry_run: return {"action": "uninstall", "root": str(target), "removed": expected["files"], "dry_run": True}
        if recovery is None:
            recovery_raw = _recovery_record(expected, "uninstall")
            _write_new(target / RECOVERY_NAME, recovery_raw, 0o600)
            recovery = _manifest(recovery_raw, expected, recovery=True)
            assert recovery is not None
        for rel in expected["files"]:
            path = destinations[rel]
            quarantine_name = recovery["staged"].get(rel)
            if quarantine_name is None:
                quarantine_name = f".tb-{uuid.uuid4().hex}.tmp"
                recovery = _recovery_update(target / RECOVERY_NAME, recovery,
                    lambda d, r=rel, n=quarantine_name: d["staged"].__setitem__(r, n))
            quarantine = path.parent / quarantine_name
            if rel not in recovery["completed"]:
                if _read_at(path.parent, quarantine_name) is None:
                    _capture_for_removal(path, quarantine_name)
                captured = _read_at(path.parent, quarantine_name)
                if captured is None or hashlib.sha256(captured).hexdigest() != hashes[rel]:
                    raise RuntimeError(f"refusing to remove unowned captured file: {quarantine}")
                recovery = _recovery_update(target / RECOVERY_NAME, recovery,
                    lambda d, r=rel: d["completed"].append(r))
            captured = _read_at(path.parent, quarantine_name)
            if captured is not None:
                if hashlib.sha256(captured).hexdigest() != hashes[rel]:
                    raise RuntimeError(f"refusing to remove modified quarantine file: {quarantine}")
                _unlink_temp(path, quarantine_name)
        _unlink(target / MANIFEST_NAME)
        _unlink(target / RECOVERY_NAME)
        return {"action": "uninstall", "root": str(target), "removed": expected["files"], "dry_run": False}
