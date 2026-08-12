from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.check_dependency_licenses as license_check
from scripts.check_dependency_licenses import check

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "legal/dependency-license-policy.json"
LOCK = ROOT / "uv.lock"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"


def run_check(
    *,
    policy_path: Path = POLICY,
    output: Path | None = None,
    release: bool = False,
    notices_only: bool = False,
    notice_output_root: Path | None = None,
    notices_path: Path = NOTICES,
) -> dict[str, object]:
    return check(
        pyproject=ROOT / "pyproject.toml",
        policy_path=policy_path,
        lock_path=LOCK,
        notices_path=notices_path,
        output=output,
        release=release,
        notices_only=notices_only,
        notice_output_root=notice_output_root,
    )


def test_inventory_records_pdfium_and_excludes_development_pdf_tool(tmp_path: Path) -> None:
    report = run_check(output=tmp_path / "licenses.json")
    assert report["status"] == "passed"
    assert report["mode"] == "inventory"
    assert report["manual_review_resolved"] == []
    reviews = {item["name"]: item for item in report["manual_review_required"]}
    assert {"certifi", "psycopg", "psycopg-binary", "psycopg-pool", "pypdfium2"} <= reviews.keys()
    assert reviews["pypdfium2"] == {
        "name": "pypdfium2",
        "version": "5.12.1",
        "license": "BSD-3-Clause, Apache-2.0, dependency licenses",
    }
    packages = {item["name"] for item in report["packages"]}
    assert {"pillow", "pypdfium2", "psycopg-binary", "psycopg-pool"} <= packages
    assert "pymupdf" not in packages
    assert "pypdf" not in packages
    payload = {item["name"]: item for item in report["license_payloads"]}["pypdfium2"]
    assert len(payload["files"]) == 19


def test_release_gate_resolves_every_review_with_a_bounded_compliance_record(
    tmp_path: Path,
) -> None:
    report = run_check(output=tmp_path / "release-licenses.json", release=True)
    assert report["mode"] == "release"
    assert report["status"] == "passed"
    assert report["manual_review_required"] == []
    resolved = {item["name"]: item for item in report["manual_review_resolved"]}
    assert set(resolved) == {
        "certifi",
        "psycopg",
        "psycopg-binary",
        "psycopg-pool",
        "pypdfium2",
    }
    assert resolved["pypdfium2"]["decision"] == (
        "include-with-recorded-notice-and-compliance-obligations"
    )
    assert len(resolved["pypdfium2"]["required_license_files"]) == 19


def test_release_fails_when_review_record_is_missing_but_inventory_can_pass(
    tmp_path: Path,
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    del policy["review_required"]["certifi"]["release_review"]["decision"]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assert run_check(policy_path=policy_path)["status"] == "passed"
    report = run_check(policy_path=policy_path, release=True)
    assert report["status"] == "failed"
    assert "certifi release review fields do not match policy schema" in report["errors"]
    assert "certifi release decision is missing or not allowed: None" in report["errors"]
    assert {item["name"] for item in report["manual_review_required"]} == {"certifi"}


def test_missing_nested_payload_map_fails_every_mode_closed(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    del policy["review_required"]["pypdfium2"]["release_review"][
        "required_license_payload"
    ]["files_sha256"]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    for options in ({}, {"release": True}, {"notices_only": True}):
        report = run_check(policy_path=policy_path, **options)
        assert report["status"] == "failed"
        assert (
            "pypdfium2 exact license payload file map is missing or invalid"
            in report["errors"]
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "decision",
            "legally-approved",
            "certifi release decision is missing or not allowed",
        ),
        (
            "reference",
            "THIRD_PARTY_NOTICES.md#does-not-exist",
            "certifi release reference anchor does not exist",
        ),
    ],
)
def test_release_fails_on_review_decision_or_reference_mismatch(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"]["certifi"]["release_review"][field] = value
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path, release=True)
    assert report["status"] == "failed"
    assert any(message in error for error in report["errors"])


def test_release_fails_on_required_payload_source_mismatch(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"]["psycopg"]["release_review"][
        "required_license_payload"
    ]["source"] = "unreviewed-location"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path, release=True)
    assert report["status"] == "failed"
    assert "psycopg release license payload source does not match policy" in report["errors"]


def test_release_fails_when_required_license_payload_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = license_check._distribution_license_files

    def without_certifi(name: str) -> dict[str, Path]:
        return {} if name == "certifi" else original(name)

    monkeypatch.setattr(license_check, "_distribution_license_files", without_certifi)
    report = run_check(release=True)
    assert report["status"] == "failed"
    assert "certifi license payload file is missing: LICENSE" in report["errors"]
    assert {item["name"] for item in report["manual_review_required"]} == {"certifi"}


def test_release_fails_if_policy_overstates_the_record_as_legal_approval(
    tmp_path: Path,
) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["release_review_policy"]["semantics"] = "Legally approved."
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path, release=True)
    assert report["status"] == "failed"
    assert "release review policy semantics are missing or overstated" in report["errors"]


def test_notice_payload_is_staged_with_exact_manifest(tmp_path: Path) -> None:
    target = tmp_path / "image-licenses"
    report = run_check(
        notices_only=True,
        notice_output_root=target,
        output=tmp_path / "notices-report.json",
    )
    assert report["status"] == "passed"
    assert report["mode"] == "notices-only"
    assert (target / "THIRD_PARTY_NOTICES.md").read_bytes() == NOTICES.read_bytes()
    manifest = json.loads((target / "dependency-license-manifest.json").read_text())
    payloads = {item["name"]: item for item in manifest["license_payloads"]}
    assert {"certifi", "pillow", "psycopg", "psycopg-binary", "psycopg-pool", "pypdfium2"} <= payloads.keys()
    assert all(payloads[name]["files"] for name in ("certifi", "pillow", "psycopg", "pypdfium2"))
    files = payloads["pypdfium2"]["files"]
    assert len(files) == 19
    for item in files:
        copied = target / "pypdfium2" / item["path"]
        assert copied.is_file()
        assert license_check._sha256(copied) == item["sha256"]


def test_release_notice_payload_records_both_modes(tmp_path: Path) -> None:
    report = run_check(
        release=True,
        notices_only=True,
        notice_output_root=tmp_path / "image-licenses",
    )
    assert report["status"] == "passed"
    assert report["mode"] == "release-notices-only"
    assert report["manual_review_required"] == []


def test_changed_pdfium_notice_hash_fails_closed(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"]["pypdfium2"]["release_review"][
        "required_license_payload"
    ]["files_sha256"]["data/linux_x64/BUILD_LICENSES/pdfium.txt"] = "0" * 64
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path, notices_only=True)
    assert report["status"] == "failed"
    assert any("license payload hash changed" in error for error in report["errors"])


def test_changed_pdfium_lock_hash_fails_closed(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"]["pypdfium2"]["expected_lock_hashes"] = ["sha256:" + "0" * 64]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path, notices_only=True)
    assert report["status"] == "failed"
    assert any("locked artifact hash is absent" in error for error in report["errors"])


def test_undeclared_pdfium_notice_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = license_check._distribution_license_files("pypdfium2")
    source = next(iter(actual.values()))
    monkeypatch.setattr(
        license_check,
        "_distribution_license_files",
        lambda _: {**actual, "unexpected.txt": source},
    )
    report = run_check(notices_only=True)
    assert report["status"] == "failed"
    assert "pypdfium2 license payload has an undeclared file: unexpected.txt" in report["errors"]


def test_project_notice_required_text_fails_closed(tmp_path: Path) -> None:
    notices = tmp_path / "THIRD_PARTY_NOTICES.md"
    notices.write_text("incomplete\n", encoding="utf-8")
    report = run_check(notices_path=notices, notices_only=True)
    assert report["status"] == "failed"
    assert any("third-party notice is missing required text" in error for error in report["errors"])


def test_unlisted_review_trigger_fails_closed(tmp_path: Path) -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["review_required"] = {}
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = run_check(policy_path=policy_path)
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: pypdfium2" not in report["errors"]
    assert any("review-triggering dependency is not declared" in error for error in report["errors"])


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
    report = run_check(output=tmp_path / "report.json")
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: new-package" in report["errors"]


@pytest.mark.parametrize("license_text", ["LGPL-3.0-only", "MPL-2.0"])
def test_unlisted_weak_copyleft_is_review_triggering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, license_text: str
) -> None:
    monkeypatch.setattr(
        license_check,
        "runtime_dependencies",
        lambda _: [{"name": "library", "version": "1.0", "license": license_text}],
    )
    report = run_check(output=tmp_path / "report.json")
    assert report["status"] == "failed"
    assert "review-triggering dependency is not declared in policy: library" in report["errors"]
