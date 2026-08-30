from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / ".github" / "scripts"
SOURCE_COMMIT = "a" * 40
SOURCE_URI = f"https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#{SOURCE_COMMIT}"
PLATFORM_HEX = "b" * 64
PLATFORM_DIGEST = f"sha256:{PLATFORM_HEX}"
ATTESTATION_DIGEST = f"sha256:{'c' * 64}"
CONFIG_DIGEST = f"sha256:{'1' * 64}"
EMPTY_CONFIG_DIGEST = f"sha256:{hashlib.sha256(b'{}').hexdigest()}"


def _jq(policy: str, document: dict[str, Any], *arguments: str) -> bool:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is required for the release policy contract test")
    result = subprocess.run(  # noqa: S603 - executable and policies are repository-controlled
        [jq, "-e", *arguments, "-f", str(POLICY_DIR / policy)],
        cwd=ROOT,
        input=json.dumps(document),
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _index() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": PLATFORM_DIGEST,
                "size": 1024,
                "platform": {"architecture": "amd64", "os": "linux"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": ATTESTATION_DIGEST,
                "size": 2048,
                "platform": {"architecture": "unknown", "os": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": PLATFORM_DIGEST,
                },
            },
        ],
    }


def _material(uri: str, algorithm: str, digest: str) -> dict[str, Any]:
    return {"uri": uri, "digest": {algorithm: digest}}


def _provenance(role: str = "worker") -> dict[str, Any]:
    materials = [
        _material(SOURCE_URI, "sha1", SOURCE_COMMIT),
        _material(
            "pkg:docker/docker/dockerfile@1.7?digest=sha256:"
            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
            "sha256",
            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
        ),
        _material(
            "pkg:docker/ghcr.io/astral-sh/uv@0.8.17?digest=sha256:"
            "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"
            "&platform=linux%2Famd64",
            "sha256",
            "db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3",
        ),
        _material(
            "pkg:docker/cgr.dev/chainguard/python@latest-dev?digest=sha256:"
            "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"
            "&platform=linux%2Famd64",
            "sha256",
            "48060899de1ce8c95d987a2fc0da2a3ca1ef28d4aac5073bff2068a63f3ccce0",
        ),
        _material(
            "pkg:docker/docker/buildkit-syft-scanner@stable-1?digest=sha256:"
            "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"
            "&platform=linux%2Famd64",
            "sha256",
            "187e1892a7752c9384c59aba9517dd8e40610b748c72773e87b63720514463c2",
        ),
    ]
    if role == "worker":
        materials.append(
            _material(
                "https://github.com/openai/codex/releases/download/"
                "rust-v0.147.0/codex-x86_64-unknown-linux-musl.tar.gz",
                "sha256",
                "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36",
            )
        )
    else:
        materials.append(
            _material(
                "pkg:docker/cgr.dev/chainguard/python@latest?digest=sha256:"
                "f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332"
                "&platform=linux%2Famd64",
                "sha256",
                "e410b1cf97a99710ca1393cd4640e97e2784b0b2f3f2455ac38a3eda9b7e74ce",
            )
        )
    return {
        "_type": "https://in-toto.io/Statement/v0.1",
        "predicateType": "https://slsa.dev/provenance/v0.2",
        "subject": [{"name": f"candidate:{role}", "digest": {"sha256": PLATFORM_HEX}}],
        "predicate": {
            "builder": {"id": ""},
            "buildType": "https://mobyproject.org/buildkit@v1",
            "materials": materials,
            "invocation": {
                "configSource": {
                    "uri": SOURCE_URI,
                    "digest": {"sha1": SOURCE_COMMIT},
                    "entryPoint": "Dockerfile",
                },
                "parameters": {
                    "frontend": "gateway.v0",
                    "args": {
                        "build-arg:APP_VERSION": "1.0.10",
                        "build-arg:CODEX_SHA256": (
                            "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"
                        ),
                        "build-arg:CODEX_VERSION": "0.147.0",
                        "build-arg:PYTHON_DEV_IMAGE": (
                            "cgr.dev/chainguard/python:latest-dev@sha256:"
                            "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"
                        ),
                        "build-arg:PYTHON_RUNTIME_IMAGE": (
                            "cgr.dev/chainguard/python:latest@sha256:"
                            "f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332"
                        ),
                        "build-arg:UV_IMAGE": (
                            "ghcr.io/astral-sh/uv:0.8.17@sha256:"
                            "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"
                        ),
                        "build-arg:VCS_REF": SOURCE_COMMIT,
                        "cmdline": (
                            "docker/dockerfile:1.7@sha256:"
                            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
                        ),
                        "source": (
                            "docker/dockerfile:1.7@sha256:"
                            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
                        ),
                        "target": role,
                    },
                    "locals": [],
                    "secrets": [],
                    "ssh": [],
                },
                "environment": {"platform": "linux/amd64"},
            },
            "metadata": {
                "completeness": {
                    "environment": True,
                    "materials": True,
                    "parameters": True,
                }
            },
        },
    }


def _attestation_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": "application/vnd.docker.attestation.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.empty.v1+json",
            "size": 2,
            "digest": EMPTY_CONFIG_DIGEST,
            "data": "e30=",
        },
        "subject": {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "size": 1024,
            "digest": PLATFORM_DIGEST,
        },
        "layers": [
            {
                "mediaType": "application/vnd.in-toto+json",
                "size": 100,
                "digest": f"sha256:{'d' * 64}",
                "annotations": {"in-toto.io/predicate-type": "https://slsa.dev/provenance/v0.2"},
            },
            {
                "mediaType": "application/vnd.in-toto+json",
                "size": 100,
                "digest": f"sha256:{'e' * 64}",
                "annotations": {"in-toto.io/predicate-type": "https://spdx.dev/Document"},
            },
        ],
    }


def _platform_manifest() -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": 4096,
            "digest": CONFIG_DIGEST,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 8192,
                "digest": f"sha256:{'2' * 64}",
            }
        ],
    }


INDEX_ARGS = (
    "--arg",
    "platform_digest",
    PLATFORM_DIGEST,
    "--arg",
    "attestation_digest",
    ATTESTATION_DIGEST,
)


def _provenance_passes(document: dict[str, Any], role: str = "worker") -> bool:
    return _jq(
        "validate-candidate-provenance.jq",
        document,
        "--arg",
        "source_commit",
        SOURCE_COMMIT,
        "--arg",
        "source_uri",
        SOURCE_URI,
        "--arg",
        "platform_digest_hex",
        PLATFORM_HEX,
        "--arg",
        "role",
        role,
    )


def test_oci_index_policy_accepts_only_the_sealed_two_descriptor_set() -> None:
    assert _jq("validate-candidate-oci-index.jq", _index(), *INDEX_ARGS)

    mutations: list[dict[str, Any]] = []
    extra_arm64 = copy.deepcopy(_index())
    extra_arm64["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{'d' * 64}",
            "size": 100,
            "platform": {"architecture": "arm64", "os": "linux"},
        }
    )
    mutations.append(extra_arm64)
    extra_unattested = copy.deepcopy(_index())
    extra_unattested["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{'e' * 64}",
            "size": 100,
            "platform": {"architecture": "amd64", "os": "linux"},
        }
    )
    mutations.append(extra_unattested)
    extra_attestation = copy.deepcopy(_index())
    extra_attestation["manifests"].append(copy.deepcopy(extra_attestation["manifests"][1]))
    extra_attestation["manifests"][-1]["digest"] = f"sha256:{'f' * 64}"
    mutations.append(extra_attestation)
    duplicate = copy.deepcopy(_index())
    duplicate["manifests"].append(copy.deepcopy(duplicate["manifests"][0]))
    mutations.append(duplicate)

    assert all(not _jq("validate-candidate-oci-index.jq", row, *INDEX_ARGS) for row in mutations)


def test_attestation_manifest_policy_binds_one_subject_and_two_exact_layers() -> None:
    arguments = ("--arg", "platform_digest", PLATFORM_DIGEST)
    assert _jq(
        "validate-candidate-attestation-manifest.jq",
        _attestation_manifest(),
        *arguments,
    )

    wrong_subject = _attestation_manifest()
    wrong_subject["subject"]["digest"] = f"sha256:{'f' * 64}"
    extra_attestation = _attestation_manifest()
    extra_attestation["layers"].append(copy.deepcopy(extra_attestation["layers"][0]))
    duplicate_provenance = _attestation_manifest()
    duplicate_provenance["layers"][1]["annotations"]["in-toto.io/predicate-type"] = (
        "https://slsa.dev/provenance/v0.2"
    )

    for document in (wrong_subject, extra_attestation, duplicate_provenance):
        assert not _jq(
            "validate-candidate-attestation-manifest.jq",
            document,
            *arguments,
        )


def test_platform_manifest_policy_binds_the_exact_runtime_config_digest() -> None:
    arguments = ("--arg", "config_digest", CONFIG_DIGEST)
    assert _jq("validate-candidate-platform-manifest.jq", _platform_manifest(), *arguments)

    wrong_config = copy.deepcopy(_platform_manifest())
    wrong_config["config"]["digest"] = f"sha256:{'9' * 64}"
    duplicate_layer = copy.deepcopy(_platform_manifest())
    duplicate_layer["layers"].append(copy.deepcopy(duplicate_layer["layers"][0]))
    foreign_layer = copy.deepcopy(_platform_manifest())
    foreign_layer["layers"][0]["mediaType"] = "application/vnd.docker.image.rootfs.diff.tar.gzip"

    for document in (wrong_config, duplicate_layer, foreign_layer):
        assert not _jq(
            "validate-candidate-platform-manifest.jq",
            document,
            *arguments,
        )


def test_provenance_policy_rejects_unsealed_source_frontend_args_and_materials() -> None:
    assert _provenance_passes(_provenance())
    assert _provenance_passes(_provenance("mcp"), "mcp")

    mutations: list[dict[str, Any]] = []
    bad_subject = _provenance()
    bad_subject["subject"][0]["digest"]["sha256"] = "0" * 64
    mutations.append(bad_subject)
    bad_source = _provenance()
    bad_source["predicate"]["invocation"]["configSource"]["uri"] = "https://attacker.invalid"
    mutations.append(bad_source)
    bad_target = _provenance()
    bad_target["predicate"]["invocation"]["parameters"]["args"]["target"] = "mcp"
    mutations.append(bad_target)
    security_override = _provenance()
    security_override["predicate"]["invocation"]["parameters"]["args"]["build-arg:PYTHON_DEV_IMAGE"] = (
        "attacker.invalid/python@sha256:" + "1" * 64
    )
    mutations.append(security_override)
    named_context = _provenance()
    named_context["predicate"]["invocation"]["parameters"]["args"]["context:source"] = (
        "docker-image://attacker.invalid/source@sha256:" + "2" * 64
    )
    mutations.append(named_context)
    alternate_dockerfile = _provenance()
    alternate_dockerfile["predicate"]["invocation"]["parameters"]["args"]["filename"] = "Dockerfile.attacker"
    mutations.append(alternate_dockerfile)
    extra_material = _provenance()
    extra_material["predicate"]["materials"].append(
        _material("pkg:docker/attacker/base@latest", "sha256", "3" * 64)
    )
    mutations.append(extra_material)
    injected_git_secret = _provenance()
    injected_git_secret["predicate"]["invocation"]["parameters"]["secrets"] = [
        {"id": "GIT_AUTH_TOKEN", "optional": True}
    ]
    mutations.append(injected_git_secret)
    missing_secrets = _provenance()
    del missing_secrets["predicate"]["invocation"]["parameters"]["secrets"]
    mutations.append(missing_secrets)
    incomplete_materials = _provenance()
    incomplete_materials["predicate"]["metadata"]["completeness"]["materials"] = False
    mutations.append(incomplete_materials)

    assert all(not _provenance_passes(row) for row in mutations)


def test_documented_public_source_candidate_producer_matches_the_provenance_policy() -> None:
    document = (ROOT / "docs/V1_0_10_CANDIDATE_ACCEPTANCE.md").read_text(encoding="utf-8")

    assert (
        'source_context="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"'
    ) in document
    assert "set -euo pipefail" in document
    assert "((${#buildkit_versions[@]} == 1))" in document
    assert 'test "${buildkit_versions[0]}" = "v0.32.2"' in document
    assert "GIT_AUTH_TOKEN" not in document
    assert "    --secret " not in document
    assert "공개된 source repository" in document
    assert "provenance의 `secrets`는 빈 배열" in document
    for build_arg in (
        "APP_VERSION=1.0.10",
        '"VCS_REF=$CANDIDATE_SOURCE_COMMIT"',
        "PYTHON_DEV_IMAGE=cgr.dev/chainguard/python:latest-dev@sha256:",
        "PYTHON_RUNTIME_IMAGE=cgr.dev/chainguard/python:latest@sha256:",
        "UV_IMAGE=ghcr.io/astral-sh/uv:0.8.17@sha256:",
        "CODEX_VERSION=0.147.0",
        "CODEX_SHA256=0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36",
    ):
        assert f"--build-arg {build_arg}" in document
    assert "--build-context" in document and "금지" in document
    assert "oci-mediatypes=true,oci-artifact=true" in document
    assert (
        "generator=docker.io/docker/buildkit-syft-scanner:stable-1@sha256:"
        "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"
    ) in document
    assert '"$source_context"' in document
    assert "docker buildx build \\" in document
    assert "docker buildx build ." not in document
    assert "외부 trust boundary" in document
    assert "raw SLSA statement" in document
    assert "기술적 release blocker" in document
    assert "hard external release blocker" not in document


@pytest.mark.parametrize(
    "raw",
    (
        '{"mediaType":"one","mediaType":"two"}',
        '{"outer":{"digest":"one","digest":"two"}}',
        '{"number":NaN}',
        '{"number":Infinity}',
    ),
)
def test_strict_json_boundary_rejects_ambiguous_oci_evidence(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "candidate.json"
    path.write_text(raw, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - interpreter and repository script are trusted
        [sys.executable, str(POLICY_DIR / "validate-strict-json.py"), str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "strict JSON validation failed\n"


def test_strict_json_boundary_accepts_unique_finite_json(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text('{"array":[1,2.5],"object":{"key":true}}\n', encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - interpreter and repository script are trusted
        [sys.executable, str(POLICY_DIR / "validate-strict-json.py"), str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
