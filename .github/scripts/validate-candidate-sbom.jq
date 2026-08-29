._type == "https://in-toto.io/Statement/v0.1"
and .predicateType == "https://spdx.dev/Document"
and (.subject | type == "array" and length == 1)
and (.subject[0].name | type == "string" and length > 0)
and .subject[0].digest == {"sha256": $platform_digest_hex}
and .predicate.spdxVersion == "SPDX-2.3"
and (.predicate.packages | type == "array" and length > 0)
