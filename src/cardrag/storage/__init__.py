"""Safe external-file storage primitives."""

from .objects import (
    ContentAddressedObjectStore,
    ObjectIntegrityError,
    StoredObject,
    write_artifact_manifest,
)
from .paths import (
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_within_root,
    portable_relative_path,
    resolve_within_root,
)

__all__ = [
    "ContentAddressedObjectStore",
    "ObjectIntegrityError",
    "StoredObject",
    "UnsafePathError",
    "atomic_write_bytes",
    "atomic_write_within_root",
    "portable_relative_path",
    "resolve_within_root",
    "write_artifact_manifest",
]
