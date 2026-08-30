def sha256_hex:
  type == "string" and test("^[0-9a-f]{64}$");

def sha1_hex:
  type == "string" and test("^[0-9a-f]{40}$");

def exact_keys($expected):
  (keys | sort) == ($expected | sort);

def expected_subject_name:
  "pkg:docker/ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate"
  + "@candidate-v1.0.10-\($role)-\($source_commit)?platform=linux%2Famd64";

def exact_subject:
  . == [{
    "name": expected_subject_name,
    "digest": {"sha256": $platform_digest_hex}
  }];

def unique_spdx_ids:
  ([.predicate.packages[].SPDXID, .predicate.files[].SPDXID] | length)
  == ([.predicate.packages[].SPDXID, .predicate.files[].SPDXID] | unique | length);

def exact_cardrag_package($name):
  [.predicate.packages[] | select(.name == $name)] as $matches
  | ($matches | length == 1)
  and $matches[0].versionInfo == "1.0.10"
  and $matches[0].licenseDeclared == "Apache-2.0"
  and ([
    $matches[0].externalRefs[]?
    | select(
        .referenceCategory == "PACKAGE-MANAGER"
        and .referenceType == "purl"
        and .referenceLocator == "pkg:pypi/\($name)@1.0.10"
      )
  ] | length == 1);

($source_commit | sha1_hex)
and ($platform_digest_hex | sha256_hex)
and ($role == "worker" or $role == "mcp")
and (exact_keys(["_type", "predicate", "predicateType", "subject"]))
and ._type == "https://in-toto.io/Statement/v1"
and .predicateType == "https://spdx.dev/Document"
and (.subject | exact_subject)
and (.predicate | exact_keys([
  "SPDXID",
  "creationInfo",
  "dataLicense",
  "documentNamespace",
  "files",
  "hasExtractedLicensingInfos",
  "name",
  "packages",
  "relationships",
  "spdxVersion"
]))
and .predicate.spdxVersion == "SPDX-2.3"
and .predicate.dataLicense == "CC0-1.0"
and .predicate.SPDXID == "SPDXRef-DOCUMENT"
and .predicate.name == "sbom"
and (.predicate.documentNamespace | type == "string" and test(
  "^https://anchore\\.com/syft/dir/sbom-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
))
and (.predicate.creationInfo | exact_keys(["created", "creators", "licenseListVersion"]))
and .predicate.creationInfo.licenseListVersion == "3.28"
and (.predicate.creationInfo.creators | sort) == ([
  "Organization: Anchore, Inc",
  "Tool: buildkit-v0.32.2",
  "Tool: syft-v1.51.0"
] | sort)
and (.predicate.creationInfo.created | type == "string" and test(
  "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
))
and (.predicate.packages | type == "array" and length > 0)
and all(.predicate.packages[];
  type == "object"
  and (.SPDXID | type == "string" and startswith("SPDXRef-"))
  and (.name | type == "string" and length > 0)
)
and (.predicate.files | type == "array" and length > 0)
and all(.predicate.files[];
  type == "object"
  and (.SPDXID | type == "string" and startswith("SPDXRef-"))
  and (.fileName | type == "string" and length > 0)
)
and (.predicate.hasExtractedLicensingInfos | type == "array")
and (.predicate.relationships | type == "array" and length > 0)
and all(.predicate.relationships[];
  type == "object"
  and (.spdxElementId | type == "string" and length > 0)
  and (.relatedSpdxElement | type == "string" and length > 0)
  and (.relationshipType | type == "string" and length > 0)
)
and ([.predicate.relationships[] | select(
  .spdxElementId == "SPDXRef-DOCUMENT"
  and .relatedSpdxElement == "SPDXRef-DocumentRoot-Directory-sbom"
  and .relationshipType == "DESCRIBES"
)] | length == 1)
and unique_spdx_ids
and ([.predicate.packages[].name | select(startswith("cardrag-"))] | sort)
  == (["cardrag-core", "cardrag-\($role)"] | sort)
and exact_cardrag_package("cardrag-core")
and exact_cardrag_package("cardrag-\($role)")
