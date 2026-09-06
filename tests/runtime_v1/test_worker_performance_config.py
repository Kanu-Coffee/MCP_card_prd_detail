from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_worker_compose_preserves_gc_defaults_and_explicit_performance_overrides() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise AssertionError("docker compose is required to verify worker configuration")
    defaults = {
        "CARDRAG_COLLECT_REMOTE_GARBAGE": "false",
        "CARDRAG_GARBAGE_GRACE_DAYS": "30",
        "CARDRAG_RETAIN_GENERATIONS": "2",
        "CARDRAG_PDF_CONCURRENCY": "8",
        "CARDRAG_PDF_CONCURRENCY_PER_ISSUER": "2",
        "CARDRAG_LOCAL_PROCESSING_WORKERS": "4",
        "CARDRAG_STATE_SQLITE_CACHE_MIB": "256",
        "CARDRAG_STATE_SQLITE_MMAP_MIB": "2048",
        "CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB": "8",
    }
    source = (ROOT / "deploy/worker/compose.yaml").read_text()
    for name in defaults:
        assert source.count(f"- {name}=") == 1, f"duplicate environment definition: {name}"
    environment = {"PATH": os.environ["PATH"], "CARDRAG_WEBDAV_BASE_URL": "https://example.invalid/dav"}
    overrides = {
        "CARDRAG_COLLECT_REMOTE_GARBAGE": "true",
        "CARDRAG_GARBAGE_GRACE_DAYS": "1",
        "CARDRAG_RETAIN_GENERATIONS": "3",
        "CARDRAG_PDF_CONCURRENCY": "5",
        "CARDRAG_PDF_CONCURRENCY_PER_ISSUER": "1",
        "CARDRAG_LOCAL_PROCESSING_WORKERS": "2",
        "CARDRAG_STATE_SQLITE_CACHE_MIB": "16",
        "CARDRAG_STATE_SQLITE_MMAP_MIB": "0",
        "CARDRAG_WEBDAV_UPLOAD_CHUNK_MIB": "1",
    }
    for supplied, expected in (({}, defaults), (overrides, overrides)):
        result = subprocess.run(  # noqa: S603 - configuration only; no daemon or real environment
            [
                docker,
                "compose",
                "--env-file",
                os.devnull,
                "-f",
                "deploy/worker/compose.yaml",
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment | supplied,
            capture_output=True,
            text=True,
            check=True,
        )
        actual = json.loads(result.stdout)["services"]["worker"]["environment"]
        assert {name: actual[name] for name in expected} == expected
