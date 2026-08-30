def positive_integer:
  type == "number" and . > 0 and floor == .;

def sha256_digest:
  type == "string" and test("^sha256:[0-9a-f]{64}$");

.schemaVersion == 2
and .mediaType == "application/vnd.oci.image.manifest.v1+json"
and .artifactType == "application/vnd.docker.attestation.manifest.v1+json"
and .config == {
  "data": "e30=",
  "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
  "mediaType": "application/vnd.oci.empty.v1+json",
  "size": 2
}
and (.subject | keys == ["digest", "mediaType", "size"])
and .subject.mediaType == "application/vnd.oci.image.manifest.v1+json"
and (.subject.size | positive_integer)
and .subject.digest == $platform_digest
and (.layers | type == "array" and length == 2)
and ([.layers[].digest] | unique | length == 2)
and all(.layers[];
  .mediaType == "application/vnd.in-toto+json"
  and (.size | positive_integer)
  and (.digest | sha256_digest)
)
and ([.layers[] | .annotations["in-toto.io/predicate-type"]] | sort) == ([
  "https://slsa.dev/provenance/v0.2",
  "https://spdx.dev/Document"
] | sort)
