"""Small, strict domain primitives shared by Worker and MCP packages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .paths import object_path, validate_relative_path

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
IssuerCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{1,31}$")]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


def issuer_code(value: str) -> str:
    """Validate a portable issuer code without a closed-world enum."""

    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", value):
        raise ValueError("issuer code must be a safe lowercase identifier")
    return value


class ArtifactRef(StrictFrozenModel):
    """Hash-bound reference to one immutable remote artifact."""

    sha256: Sha256Hex
    size_bytes: NonNegativeInt
    media_type: NonEmptyText
    path: str

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        return validate_relative_path(value).as_posix()

    @property
    def mime_type(self) -> str:
        """Compatibility spelling for HTTP-oriented consumers."""

        return self.media_type

    @classmethod
    def for_cas(
        cls,
        *,
        sha256: str,
        size_bytes: int,
        media_type: str,
    ) -> Self:
        return cls(
            sha256=sha256,
            size_bytes=size_bytes,
            media_type=media_type,
            path=object_path(sha256).as_posix(),
        )

    @model_validator(mode="after")
    def cas_path_matches_hash_when_applicable(self) -> Self:
        path = PurePosixPath(self.path)
        if path.parts[:3] == ("v1", "objects", "sha256") and path != object_path(self.sha256):
            raise ValueError("CAS artifact path does not match its SHA-256")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Result returned only after remote bytes have been read and hashed."""

    path: PurePosixPath
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        if len(self.sha256) != 64 or any(character not in "0123456789abcdef" for character in self.sha256):
            raise ValueError("verified artifact SHA-256 is invalid")
        if self.size_bytes < 0:
            raise ValueError("verified artifact size cannot be negative")
