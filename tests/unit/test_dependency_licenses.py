from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.check_dependency_licenses as license_check
from scripts.check_dependency_licenses import check

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "legal/dependency-license-policy.json"


def test_inventory_records_dual_licensed_pymupdf(tmp_path: Path) -> None:
    report = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=tmp_path / "licenses.json",
        release=False,
        attestation=None,
    )
    assert report["status"] == "passed"
    reviews = {item["name"]: item for item in report["manual_review_required"]}
    assert {"certifi", "psycopg", "psycopg-binary", "psycopg-pool", "pymupdf"} <= reviews.keys()
    assert reviews["pymupdf"] == {
            "name": "pymupdf",
            "version": "1.28.2",
            "license": "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License",
    }
    assert {item["name"] for item in report["packages"]} >= {
        "psycopg-binary",
        "psycopg-pool",
    }


def test_release_fails_closed_without_exact_attestation(tmp_path: Path) -> None:
    missing = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=None,
        release=True,
        attestation=None,
    )
    assert missing["status"] == "failed"

    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    approved = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=tmp_path / "release-licenses.json",
        release=True,
        attestation=policy["review_required"]["pymupdf"]["release_attestation"],
    )
    assert approved["status"] == "passed"


def test_unlisted_review_trigger_fails_closed(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"] = {}
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=policy_path,
        output=None,
        release=False,
        attestation=None,
    )
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: pymupdf" in report["errors"]


@pytest.mark.parametrize(
    "license_text",
    [
        "GPL-3.0-only",
        "GPLv2",
        "GNU General Public License",
        "GNU Affero General Public License v3",
        "GNU Lesser General Public License v3",
    ],
)
def test_unlisted_plain_gpl_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, license_text: str
) -> None:
    monkeypatch.setattr(
        license_check,
        "runtime_dependencies",
        lambda _: [{"name": "new-package", "version": "1.0", "license": license_text}],
    )
    report = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=tmp_path / "report.json",
        release=False,
        attestation=None,
    )
    assert report["status"] == "failed"
    assert report["errors"][0].endswith("new-package")


def test_unlisted_lgpl_is_review_triggering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        license_check,
        "runtime_dependencies",
        lambda _: [{"name": "library", "version": "1.0", "license": "LGPL-3.0-only"}],
    )
    report = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=tmp_path / "report.json",
        release=False,
        attestation=None,
    )
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: library" in report["errors"]


def test_unlisted_mpl_is_review_triggering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        license_check,
        "runtime_dependencies",
        lambda _: [{"name": "library", "version": "1.0", "license": "MPL-2.0"}],
    )
    report = check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=POLICY,
        output=tmp_path / "report.json",
        release=False,
        attestation=None,
    )
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: library" in report["errors"]
