"""Explicit, lock-protected recovery of old Paddle input aliases."""

from __future__ import annotations

import fcntl
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .capacity_v5 import _directory_open_flags, _open_capacity_directory
from .state import AlreadyRunning

_RUN = re.compile(r"[0-9a-f]{32}\Z")
_DOCUMENT = re.compile(r"doc_[0-9a-f]{64}\Z")
_ALIAS = re.compile(r"\.paddle-input-[0-9]{1,20}\.pdf\Z")
_CAS_TARGET = re.compile(r"/var/lib/cardrag-worker/pdf-cache/objects/sha256/[0-9a-f]{2}/[0-9a-f]{64}\Z")
_MAX_DOCUMENTS = 100000
_MAX_ALIASES = 1000


class PaddleCleanupError(RuntimeError):
    """A bounded recovery refusal; no raw filesystem details are exposed."""


def _same_inode(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_mode) == (b.st_dev, b.st_ino, b.st_mode)


def _open_child(parent: int, name: str, stack: ExitStack, device: int) -> int | None:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(before.st_mode) or before.st_dev != device:
        raise PaddleCleanupError("paddle_cleanup_unsafe_directory")
    fd = os.open(name, _directory_open_flags(), dir_fd=parent)
    stack.callback(os.close, fd)
    if not _same_inode(before, os.fstat(fd)):
        raise PaddleCleanupError("paddle_cleanup_path_changed")
    return fd


def recover_paddle_aliases(state_dir: Path, run_id: str, *, apply: bool = False) -> dict[str, Any]:
    """Inspect or unlink exact legacy aliases for one run, under the Worker lock.

    A pre-existing regular worker.lock is required. Directory descriptors never
    follow symlinks and identities are checked again immediately before unlink.
    """
    if not _RUN.fullmatch(run_id):
        raise PaddleCleanupError("paddle_cleanup_invalid_run_id")
    entries: list[dict[str, str]] = []
    try:
        with (
            _open_capacity_directory(state_dir, allow_missing_suffix=False) as (_, _, root),
            ExitStack() as stack,
        ):
            device = os.fstat(root).st_dev
            try:
                lock_fd = os.open(
                    "worker.lock",
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=root,
                )
            except OSError:
                raise PaddleCleanupError("paddle_cleanup_unsafe_lock") from None
            stack.callback(os.close, lock_fd)
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise PaddleCleanupError("paddle_cleanup_unsafe_lock")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning("worker lock is busy") from None
            stack.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)

            runs = _open_child(root, "runs", stack, device)
            run = _open_child(runs, run_id, stack, device) if runs is not None else None
            documents = _open_child(run, "documents", stack, device) if run is not None else None
            if documents is None:
                raise PaddleCleanupError("paddle_cleanup_run_unavailable")
            assert runs is not None and run is not None
            names = sorted(os.listdir(documents))
            if len(names) > _MAX_DOCUMENTS:
                raise PaddleCleanupError("paddle_cleanup_document_limit")
            candidates: list[tuple[str, os.stat_result, str, tuple[os.stat_result, ...]]] = []
            prefix_stats = tuple(os.fstat(fd) for fd in (root, runs, run, documents))
            for document in names:
                if not _DOCUMENT.fullmatch(document):
                    raise PaddleCleanupError("paddle_cleanup_invalid_document")
                with ExitStack() as path_stack:
                    doc = _open_child(documents, document, path_stack, device)
                    if doc is None:
                        raise PaddleCleanupError("paddle_cleanup_path_changed")
                    ocr = _open_child(doc, "ocr", path_stack, device)
                    if ocr is None:
                        continue
                    for branch in ("", "primary", "fallback"):
                        with ExitStack() as branch_stack:
                            base = _open_child(ocr, branch, branch_stack, device) if branch else ocr
                            if base is None:
                                continue
                            checkpoints = _open_child(base, "checkpoints", branch_stack, device)
                            if checkpoints is None:
                                continue
                            for name in sorted(os.listdir(checkpoints)):
                                if not _ALIAS.fullmatch(name):
                                    continue
                                info = os.stat(name, dir_fd=checkpoints, follow_symlinks=False)
                                if not stat.S_ISLNK(info.st_mode):
                                    continue
                                target = os.readlink(name, dir_fd=checkpoints)
                                relative = "/".join(
                                    filter(
                                        None,
                                        (
                                            "runs",
                                            run_id,
                                            "documents",
                                            document,
                                            "ocr",
                                            branch,
                                            "checkpoints",
                                            name,
                                        ),
                                    )
                                )
                                ancestry = (*prefix_stats, os.fstat(doc), os.fstat(ocr))
                                if branch:
                                    ancestry = (*ancestry, os.fstat(base))
                                ancestry = (*ancestry, os.fstat(checkpoints))
                                candidates.append((relative, info, target, ancestry))
                                if len(candidates) > _MAX_ALIASES:
                                    raise PaddleCleanupError("paddle_cleanup_alias_limit")

            # Reopen the entire path and compare identities before every unlink.
            # The open root is also checked against its original absolute path.
            for relative, original, target, ancestry in candidates:
                parts = relative.split("/")
                with _open_capacity_directory(state_dir, allow_missing_suffix=False) as (_, _, current_root):
                    if not _same_inode(ancestry[0], os.fstat(current_root)):
                        raise PaddleCleanupError("paddle_cleanup_path_changed")
                    if not _same_inode(
                        lock_stat, os.stat("worker.lock", dir_fd=current_root, follow_symlinks=False)
                    ):
                        raise PaddleCleanupError("paddle_cleanup_path_changed")
                    with ExitStack() as path_stack:
                        parent = current_root
                        for index, part in enumerate(parts[:-1], 1):
                            child = _open_child(parent, part, path_stack, device)
                            if child is None or not _same_inode(ancestry[index], os.fstat(child)):
                                raise PaddleCleanupError("paddle_cleanup_path_changed")
                            parent = child
                        now = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                        if not stat.S_ISLNK(now.st_mode) or not _same_inode(original, now):
                            raise PaddleCleanupError("paddle_cleanup_path_changed")
                        if os.readlink(parts[-1], dir_fd=parent) != target:
                            raise PaddleCleanupError("paddle_cleanup_path_changed")
                        if apply:
                            os.unlink(parts[-1], dir_fd=parent)
                entries.append(
                    {
                        "path": relative,
                        "type": "symlink",
                        "target": target if _CAS_TARGET.fullmatch(target) else "<redacted>",
                        "action": "removed" if apply else "would_remove",
                    }
                )
    except AlreadyRunning:
        raise
    except PaddleCleanupError:
        raise
    except (OSError, RuntimeError):
        raise PaddleCleanupError("paddle_cleanup_path_unavailable_or_changed") from None
    return {"status": "ok", "run_id": run_id, "mode": "apply" if apply else "dry_run", "entries": entries}
