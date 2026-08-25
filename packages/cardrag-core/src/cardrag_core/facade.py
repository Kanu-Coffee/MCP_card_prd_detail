"""Read-only, hash-verifying artifact facade intended for the MCP service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from pydantic import ValidationError

from .canonical import sha256_bytes
from .domain import ArtifactRef, VerifiedArtifact
from .manifests import GenerationManifest, GenerationPointer, GenerationReady
from .paths import (
    STABLE_POINTER_PATH,
    generation_database_path,
    generation_manifest_path,
    generation_ready_path,
    object_path,
)
from .webdav import ReadOnlyWebDAVClient, WebDAVIntegrityError

_CONTROL_FILE_LIMIT = 8 * 1024 * 1024
ModelT = TypeVar("ModelT", GenerationManifest, GenerationPointer, GenerationReady)


@dataclass(frozen=True, slots=True)
class CurrentGeneration:
    pointer: GenerationPointer
    ready: GenerationReady
    manifest: GenerationManifest


class ArtifactContractError(WebDAVIntegrityError):
    """A control file is malformed, non-canonical, or incorrectly bound."""


class MCPArtifactReader:
    """MCP-safe facade exposing only verified read and download operations."""

    __slots__ = ("__reader",)

    def __init__(self, reader: ReadOnlyWebDAVClient) -> None:
        self.__reader = reader

    @staticmethod
    def _parse_canonical(payload: bytes, model_type: type[ModelT], *, label: str) -> ModelT:
        try:
            model = model_type.model_validate_json(payload)
        except (ValidationError, ValueError) as exc:
            raise ArtifactContractError(f"{label} is not a valid strict manifest") from exc
        if model.canonical_bytes() != payload:
            raise ArtifactContractError(f"{label} is not canonical JSON")
        return model

    def read_stable_pointer(self) -> GenerationPointer:
        payload = self.__reader.get(STABLE_POINTER_PATH, max_bytes=_CONTROL_FILE_LIMIT).content
        return self._parse_canonical(payload, GenerationPointer, label="stable generation pointer")

    def read_generation_ready(self, generation_id: str) -> tuple[GenerationReady, bytes]:
        payload = self.__reader.get(
            generation_ready_path(generation_id),
            max_bytes=_CONTROL_FILE_LIMIT,
        ).content
        ready = self._parse_canonical(payload, GenerationReady, label="generation READY")
        if ready.generation_id != generation_id:
            raise ArtifactContractError("generation READY ID does not match its path")
        return ready, payload

    def read_generation_manifest(
        self,
        generation_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> GenerationManifest:
        payload = self.__reader.get(
            generation_manifest_path(generation_id),
            max_bytes=_CONTROL_FILE_LIMIT,
        ).content
        if expected_sha256 is not None and sha256_bytes(payload) != expected_sha256:
            raise ArtifactContractError("generation manifest SHA-256 does not match READY")
        manifest = self._parse_canonical(payload, GenerationManifest, label="generation manifest")
        if manifest.generation_id != generation_id:
            raise ArtifactContractError("generation manifest ID does not match its path")
        return manifest

    def read_current_generation(self) -> CurrentGeneration:
        pointer = self.read_stable_pointer()
        ready, ready_bytes = self.read_generation_ready(pointer.generation_id)
        if sha256_bytes(ready_bytes) != pointer.ready_sha256:
            raise ArtifactContractError("stable pointer does not bind generation READY")
        if ready.manifest_sha256 != pointer.manifest_sha256:
            raise ArtifactContractError("stable pointer and generation READY disagree on manifest")
        manifest = self.read_generation_manifest(
            pointer.generation_id,
            expected_sha256=ready.manifest_sha256,
        )
        database = manifest.serving_database
        if (
            ready.serving_database_sha256 != database.sha256
            or ready.serving_database_size_bytes != database.size_bytes
        ):
            raise ArtifactContractError("generation READY does not bind the serving database")
        return CurrentGeneration(pointer=pointer, ready=ready, manifest=manifest)

    def read_object(self, reference: ArtifactRef, *, max_bytes: int | None = None) -> bytes:
        if PurePosixPath(reference.path) != object_path(reference.sha256):
            raise ArtifactContractError("object reference is not bound to its CAS path")
        limit = reference.size_bytes if max_bytes is None else min(max_bytes, reference.size_bytes)
        payload = self.__reader.get(reference.path, max_bytes=limit).content
        if len(payload) != reference.size_bytes or sha256_bytes(payload) != reference.sha256:
            raise ArtifactContractError("CAS object bytes do not match their artifact reference")
        return payload

    def download_object(self, reference: ArtifactRef, destination: str | Path) -> VerifiedArtifact:
        if PurePosixPath(reference.path) != object_path(reference.sha256):
            raise ArtifactContractError("object reference is not bound to its CAS path")
        return self.__reader.download(
            reference.path,
            destination,
            expected_sha256=reference.sha256,
            expected_size_bytes=reference.size_bytes,
        )

    def download_serving_database(
        self,
        destination: str | Path,
        *,
        current: CurrentGeneration | None = None,
    ) -> VerifiedArtifact:
        selected = current or self.read_current_generation()
        database = selected.manifest.serving_database
        expected_path = generation_database_path(selected.manifest.generation_id)
        if PurePosixPath(database.path) != expected_path:
            raise ArtifactContractError("serving database path does not match its generation")
        return self.__reader.download(
            database.path,
            destination,
            expected_sha256=database.sha256,
            expected_size_bytes=database.size_bytes,
        )
