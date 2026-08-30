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


def _subject_name(role: str) -> str:
    return (
        "pkg:docker/ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate"
        f"@candidate-v1.0.10-{role}-{SOURCE_COMMIT}?platform=linux%2Famd64"
    )


def _build_args() -> dict[str, str]:
    return {
        "build-arg:APP_VERSION": "1.0.10",
        "build-arg:CODEX_SHA256": ("0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"),
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
    }


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
            "pkg:docker/docker/dockerfile@1.7?digest=sha256:"
            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
            "&platform=linux%2Famd64",
            "sha256",
            "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
        ),
        _material(
            "pkg:docker/ghcr.io/astral-sh/uv@0.8.17?digest=sha256:"
            "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"
            "&platform=linux%2Famd64",
            "sha256",
            "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1",
        ),
        _material(
            "pkg:docker/cgr.dev/chainguard/python@latest-dev?digest=sha256:"
            "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"
            "&platform=linux%2Famd64",
            "sha256",
            "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2",
        ),
        _material(
            "pkg:docker/docker/buildkit-syft-scanner@stable-1?digest=sha256:"
            "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9",
            "sha256",
            "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9",
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
                "f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332",
            )
        )
    build_args = _build_args()
    frontend_args = {
        **build_args,
        "cmdline": (
            "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
        ),
        "source": (
            "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
        ),
        "target": role,
    }
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v0.2",
        "subject": [{"name": _subject_name(role), "digest": {"sha256": PLATFORM_HEX}}],
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
                    "args": frontend_args,
                    "compatibilityVersion": 30,
                    "root": {
                        "configSource": {
                            "uri": SOURCE_URI,
                            "digest": {"sha1": SOURCE_COMMIT},
                            "path": "Dockerfile",
                        },
                        "request": {"args": {**build_args, "target": role}},
                    },
                    "secrets": [
                        {"id": "GIT_AUTH_HEADER", "optional": True},
                        {"id": "GIT_AUTH_TOKEN", "optional": True},
                    ],
                },
                "environment": {"platform": "linux/amd64"},
            },
            "buildConfig": {
                "llbDefinition": [
                    {
                        "id": "step0",
                        "op": {
                            "Op": {
                                "source": {
                                    "identifier": (
                                        "git://github.com/Kanu-Coffee/"
                                        f"MCP_card_prd_detail.git#{SOURCE_COMMIT}"
                                    ),
                                    "attrs": {
                                        "git.authheadersecret": "GIT_AUTH_HEADER",
                                        "git.authtokensecret": "GIT_AUTH_TOKEN",
                                        "git.fullurl": (
                                            "https://github.com/Kanu-Coffee/MCP_card_prd_detail.git"
                                        ),
                                    },
                                }
                            }
                        },
                    }
                ]
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


def _cardrag_spdx_package(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "SPDXID": f"SPDXRef-Package-python-{name}",
        "versionInfo": "1.0.10",
        "licenseDeclared": "Apache-2.0",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:pypi/{name}@1.0.10",
            }
        ],
    }


def _sbom(role: str = "worker") -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://spdx.dev/Document",
        "subject": [{"name": _subject_name(role), "digest": {"sha256": PLATFORM_HEX}}],
        "predicate": {
            "SPDXID": "SPDXRef-DOCUMENT",
            "creationInfo": {
                "licenseListVersion": "3.28",
                "creators": [
                    "Organization: Anchore, Inc",
                    "Tool: syft-v1.51.0",
                    "Tool: buildkit-v0.32.2",
                ],
                "created": "2026-08-30T03:28:18Z",
            },
            "dataLicense": "CC0-1.0",
            "documentNamespace": ("https://anchore.com/syft/dir/sbom-89d0e9c7-6fa7-4345-90f7-76a49b26febb"),
            "files": [
                {
                    "SPDXID": "SPDXRef-File-app",
                    "fileName": f"/opt/cardrag/{role}",
                }
            ],
            "hasExtractedLicensingInfos": [],
            "name": "sbom",
            "packages": [
                _cardrag_spdx_package("cardrag-core"),
                _cardrag_spdx_package(f"cardrag-{role}"),
            ],
            "relationships": [
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relatedSpdxElement": "SPDXRef-DocumentRoot-Directory-sbom",
                    "relationshipType": "DESCRIBES",
                }
            ],
            "spdxVersion": "SPDX-2.3",
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


def _sbom_passes(document: dict[str, Any], role: str = "worker") -> bool:
    return _jq(
        "validate-candidate-sbom.jq",
        document,
        "--arg",
        "source_commit",
        SOURCE_COMMIT,
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
    legacy_statement = _provenance()
    legacy_statement["_type"] = "https://in-toto.io/Statement/v0.1"
    mutations.append(legacy_statement)
    bad_subject = _provenance()
    bad_subject["subject"][0]["digest"]["sha256"] = "0" * 64
    mutations.append(bad_subject)
    bad_subject_name = _provenance()
    bad_subject_name["subject"][0]["name"] = "candidate:worker"
    mutations.append(bad_subject_name)
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
    alternate_root_dockerfile = _provenance()
    alternate_root_dockerfile["predicate"]["invocation"]["parameters"]["root"]["configSource"]["path"] = (
        "Dockerfile.attacker"
    )
    mutations.append(alternate_root_dockerfile)
    root_arg_drift = _provenance()
    root_arg_drift["predicate"]["invocation"]["parameters"]["root"]["request"]["args"]["target"] = "mcp"
    mutations.append(root_arg_drift)
    compatibility_drift = _provenance()
    compatibility_drift["predicate"]["invocation"]["parameters"]["compatibilityVersion"] = 29
    mutations.append(compatibility_drift)
    extra_material = _provenance()
    extra_material["predicate"]["materials"].append(
        _material("pkg:docker/attacker/base@latest", "sha256", "3" * 64)
    )
    mutations.append(extra_material)
    missing_platform_frontend_material = _provenance()
    missing_platform_frontend_material["predicate"]["materials"] = [
        material
        for material in missing_platform_frontend_material["predicate"]["materials"]
        if not (
            material["uri"].startswith("pkg:docker/docker/dockerfile@1.7?")
            and "platform=linux%2Famd64" in material["uri"]
        )
    ]
    mutations.append(missing_platform_frontend_material)
    extra_git_secret = _provenance()
    extra_git_secret["predicate"]["invocation"]["parameters"]["secrets"].append(
        {"id": "DEPLOY_TOKEN", "optional": True}
    )
    mutations.append(extra_git_secret)
    required_git_secret = _provenance()
    required_git_secret["predicate"]["invocation"]["parameters"]["secrets"][1]["optional"] = False
    mutations.append(required_git_secret)
    missing_secrets = _provenance()
    del missing_secrets["predicate"]["invocation"]["parameters"]["secrets"]
    mutations.append(missing_secrets)
    secret_mount = _provenance()
    secret_mount["predicate"]["buildConfig"]["llbDefinition"].append(
        {
            "id": "step-secret",
            "op": {
                "Op": {
                    "exec": {"mounts": [{"dest": "/run/secrets/deploy", "secretOpt": {"ID": "DEPLOY_TOKEN"}}]}
                }
            },
        }
    )
    mutations.append(secret_mount)
    incomplete_materials = _provenance()
    incomplete_materials["predicate"]["metadata"]["completeness"]["materials"] = False
    mutations.append(incomplete_materials)

    assert all(not _provenance_passes(row) for row in mutations)


def test_sbom_policy_matches_buildkit_032_shape_and_rejects_unbound_inventory() -> None:
    assert _sbom_passes(_sbom())
    assert _sbom_passes(_sbom("mcp"), "mcp")

    mutations: list[dict[str, Any]] = []
    legacy_statement = _sbom()
    legacy_statement["_type"] = "https://in-toto.io/Statement/v0.1"
    mutations.append(legacy_statement)
    bad_subject_name = _sbom()
    bad_subject_name["subject"][0]["name"] = _subject_name("mcp")
    mutations.append(bad_subject_name)
    bad_subject_digest = _sbom()
    bad_subject_digest["subject"][0]["digest"]["sha256"] = "0" * 64
    mutations.append(bad_subject_digest)
    malformed_namespace = _sbom()
    malformed_namespace["predicate"]["documentNamespace"] = "https://anchore.com/syft/dir/sbom-not-a-uuid"
    mutations.append(malformed_namespace)
    wrong_buildkit_creator = _sbom()
    wrong_buildkit_creator["predicate"]["creationInfo"]["creators"][2] = "Tool: buildkit-v0.31.0"
    mutations.append(wrong_buildkit_creator)
    empty_packages = _sbom()
    empty_packages["predicate"]["packages"] = []
    mutations.append(empty_packages)
    duplicate_package_id = _sbom()
    duplicate_package_id["predicate"]["packages"][1]["SPDXID"] = duplicate_package_id["predicate"][
        "packages"
    ][0]["SPDXID"]
    mutations.append(duplicate_package_id)
    missing_role_package = _sbom()
    missing_role_package["predicate"]["packages"].pop()
    mutations.append(missing_role_package)
    wrong_license = _sbom()
    wrong_license["predicate"]["packages"][1]["licenseDeclared"] = "NOASSERTION"
    mutations.append(wrong_license)
    missing_describes = _sbom()
    missing_describes["predicate"]["relationships"] = []
    mutations.append(missing_describes)

    assert all(not _sbom_passes(row) for row in mutations)


def test_documented_public_source_candidate_producer_matches_the_provenance_policy() -> None:
    document = (ROOT / "docs/V1_0_10_CANDIDATE_ACCEPTANCE.md").read_text(encoding="utf-8")
    producer = document.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]

    assert (
        'source_context="https://github.com/Kanu-Coffee/MCP_card_prd_detail.git#$CANDIDATE_SOURCE_COMMIT"'
    ) in document
    assert "set -euo pipefail" in document
    assert "((${#buildkit_versions[@]} == 1))" in document
    assert 'test "${buildkit_versions[0]}" = "v0.32.2"' in document
    assert "GIT_AUTH_HEADER" not in producer
    assert "GIT_AUTH_TOKEN" not in producer
    assert "    --secret " not in producer
    assert "공개된 source repository" in document
    assert "두 Git auth ID의 optional 내장 선언" in document
    assert "이 선언 자체는 token 값의 미전달을 증명하지 않으므로" in document
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
