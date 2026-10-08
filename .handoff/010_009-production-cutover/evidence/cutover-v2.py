#!/usr/bin/python3
"""Root-only controlled cutover. No OCR, embedding, or Worker run."""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

OLD = Path("/opt/cardrag/008-6b42a1a")
NEW = Path("/opt/cardrag/009-b544a80")
CURRENT = Path("/opt/cardrag/current")
JOURNAL = NEW / "operations/name-migration-journal.json"
PAIRS = [
    ("cardrag-mcp-v129-candidate-state", "cardrag-mcp-state"),
    ("cardrag-worker-v130-candidate-state", "cardrag-worker-state"),
    ("cardrag-worker-v120-recovery-auth-20260910", "cardrag-worker-auth"),
]


def run(*args, capture=False, check=True):
    return subprocess.run(args, check=check, text=True, stdout=subprocess.PIPE if capture else None)  # noqa: S603 -- fixed operational commands, no shell


def inspect(name):
    return json.loads(run("docker", "volume", "inspect", name, capture=True).stdout)[0]


def compose(snapshot, service, *args):
    return run(str(snapshot / "operations" / f"{service}-compose.sh"), *args)


def save(state):
    temporary = JOURNAL.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, JOURNAL)


def switch(snapshot):
    temporary = CURRENT.with_name("current.retry-" + str(os.getpid()))
    os.symlink(str(snapshot), temporary)
    os.replace(temporary, CURRENT)


def ready():
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:18015/health/ready", timeout=5) as response:
                if response.status == 200 and json.load(response).get("ready") is True:
                    return True
        except (OSError, ValueError):
            time.sleep(0)  # Connection/readiness errors are expected during boot.
        time.sleep(5)
    return False


def configure(snapshot):
    # Preserve all environment/secret values. Only append name overrides.
    for service, volumes in [
        ("mcp", {"mcp-state": "cardrag-mcp-state"}),
        (
            "worker",
            {
                "worker-state": "cardrag-worker-state",
                "codex-home": "cardrag-worker-auth",
                "paddle-models": "cardrag-worker-paddleocr-models",
            },
        ),
    ]:
        path = snapshot / "deploy" / service / "compose.secrets.yaml"
        text = path.read_text()
        if "\nvolumes:" in text:
            raise RuntimeError("Unexpected existing overlay volume definition")
        if service == "mcp":
            text = text.replace("  mcp:\n", "  mcp:\n    container_name: cardrag-mcp\n", 1)
        text += "\nvolumes:\n" + "".join(
            f"  {key}:\n    external: true\n    name: {value}\n" for key, value in volumes.items()
        )
        path.write_text(text)
    wrapper = snapshot / "operations/mcp-compose.sh"
    wrapper.write_text(wrapper.read_text().replace("-p cardrag-stable-v1026 ", "-p cardrag-mcp "))


def restore(state):
    print("Restoring original volume locations and deployment.", flush=True)
    # Never move a volume while any container references it, even a stopped one.
    run("docker", "rm", "-f", "cardrag-mcp", check=False)
    for _old, new in PAIRS:
        references = run("docker", "ps", "-aq", "--filter", "volume=" + new, capture=True).stdout.strip()
        if references:
            raise RuntimeError("Cannot restore: container still references " + new)
    for entry in reversed(state["moves"]):
        source, destination = Path(entry["source"]), Path(entry["destination"])
        if destination.exists() or destination.is_symlink():
            if source.exists() or source.is_symlink():
                raise RuntimeError("Both migration locations occupied; manual recovery required")
            os.rename(destination, source)
        elif not (source.exists() or source.is_symlink()):
            raise RuntimeError("Missing migration entry; manual recovery required")
    for name, text in state["backups"].items():
        Path(name).write_text(text)
    switch(OLD)
    compose(OLD, "mcp", "up", "-d", "--no-build", "--pull", "never", "mcp")
    if not ready():
        raise RuntimeError("Restored MCP did not become ready. Timer remains stopped.")
    run("systemctl", "start", "cardrag-worker.timer")
    state["status"] = "restored"
    save(state)
    print("Original deployment ready; timer active.", flush=True)


def main():
    os.environ["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    if os.geteuid() != 0:
        raise RuntimeError("Run with sudo")
    if "--restore" in sys.argv:
        state = json.loads(JOURNAL.read_text())
        if state["status"] != "in_progress":
            raise RuntimeError("Recovery is allowed only for an interrupted migration")
        run("systemctl", "stop", "cardrag-worker.timer")
        restore(state)
        return
    if JOURNAL.exists():
        raise RuntimeError(
            "Migration journal exists; inspect it before retry. Interrupted migration: use --restore."
        )
    if CURRENT.resolve() != OLD:
        raise RuntimeError("Current deployment changed")
    run("systemctl", "is-active", "--quiet", "cardrag-worker.timer")
    for image in [
        "cardrag-mcp:009-b544a80",
        "cardrag-worker:009-b544a80",
        "cardrag-mcp:007-31edb1d",
        "cardrag-worker:008-6b42a1a",
    ]:
        run("docker", "image", "inspect", image, capture=True)
    for snapshot in [OLD, NEW]:
        for service in ["mcp", "worker"]:
            compose(snapshot, service, "config", "--quiet")
    originals = {}
    for snapshot in [OLD, NEW]:
        for relative in [
            "deploy/mcp/compose.secrets.yaml",
            "deploy/worker/compose.secrets.yaml",
            "operations/mcp-compose.sh",
        ]:
            path = snapshot / relative
            originals[str(path)] = path.read_text()
    destinations = []
    for old, new in PAIRS:
        source = inspect(old)
        if source["Driver"] != "local" or source.get("Options"):
            raise RuntimeError("Only plain local volumes supported")
        if run("docker", "volume", "ls", "-q", "--filter", "name=^" + new + "$", capture=True).stdout.strip():
            raise RuntimeError("Destination volume already exists: " + new)
        destinations.append((old, new, source["Mountpoint"]))
    run("systemctl", "stop", "cardrag-worker.timer")
    state = {"status": "in_progress", "moves": [], "backups": originals}
    # Journal contains host-local overlay paths/settings. Keep it root-only.
    os.umask(0o077)
    save(state)
    try:
        if (
            run("systemctl", "is-active", "cardrag-worker.service", capture=True, check=False).stdout.strip()
            != "inactive"
        ):
            raise RuntimeError("Worker service is not inactive")
        if run(
            "docker", "ps", "-q", "--filter", "label=com.docker.compose.service=worker", capture=True
        ).stdout.strip():
            raise RuntimeError("Worker is running")
        # Remove only the old MCP container; do not remove volumes or networks.
        run("docker", "stop", "cardrag-stable-v1026-mcp-1")
        run("docker", "rm", "cardrag-stable-v1026-mcp-1")
        for old, new, mountpoint in destinations:
            if run("docker", "ps", "-aq", "--filter", "volume=" + old, capture=True).stdout.strip():
                raise RuntimeError("Volume still referenced: " + old)
            run("docker", "volume", "create", new, capture=True)
            destination = inspect(new)
            root = Path(mountpoint)
            target = Path(destination["Mountpoint"])
            if list(target.iterdir()) or root.stat().st_dev != target.stat().st_dev:
                raise RuntimeError("Destination must be empty and on same filesystem")
            stat = root.stat()
            os.chown(target, stat.st_uid, stat.st_gid)
            os.chmod(target, stat.st_mode & 0o7777)
            for item in root.iterdir():
                entry = {"source": str(item), "destination": str(target / item.name)}
                state["moves"].append(entry)
                save(state)  # Persist before each same-filesystem rename; no byte copies.
                os.rename(entry["source"], entry["destination"])
        configure(NEW)
        compose(NEW, "mcp", "config", "--quiet")
        compose(NEW, "worker", "config", "--quiet")
        switch(NEW)
        compose(NEW, "mcp", "up", "-d", "--no-build", "--pull", "never", "mcp")
        print(
            "MCP startup validation in progress; waiting up to 15 minutes. No Worker is launched.", flush=True
        )
        if not ready():
            raise RuntimeError("New MCP did not become ready within 15 minutes")
        # The single rollback deployment uses the canonical volumes too.
        configure(OLD)
        compose(OLD, "mcp", "config", "--quiet")
        compose(OLD, "worker", "config", "--quiet")
        run("systemctl", "start", "cardrag-worker.timer")
        run("systemctl", "is-active", "--quiet", "cardrag-worker.timer")
        state["status"] = "deployed"
        save(state)
        (NEW / "operations/cutover-status.json").write_text(
            json.dumps(
                {
                    "status": "deployed",
                    "snapshot": str(NEW),
                    "readiness": True,
                    "timer": "active",
                    "container": "cardrag-mcp",
                    "volumes": [new for _, new in PAIRS],
                },
                indent=2,
            )
            + "\n"
        )
        print(
            "Cutover complete: cardrag-mcp ready, Worker timer active. Old empty volume metadata retained pending final smoke.",
            flush=True,
        )
    except BaseException:
        restore(state)
        raise


if __name__ == "__main__":

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except BaseException as error:
        print("Cutover stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
