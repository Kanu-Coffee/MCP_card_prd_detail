def positive_integer:
  type == "number" and . > 0 and floor == .;

def sha256_digest:
  type == "string" and test("^sha256:[0-9a-f]{64}$");

.schemaVersion == 2
and .mediaType == "application/vnd.oci.image.manifest.v1+json"
and ((.artifactType // null) == null)
and (.config | keys == ["digest", "mediaType", "size"])
and .config.mediaType == "application/vnd.oci.image.config.v1+json"
and .config.digest == $config_digest
and (.config.size | positive_integer)
and (.layers | type == "array" and length > 0)
and (([.layers[].digest] | unique | length) == (.layers | length))
and all(.layers[];
  .mediaType == "application/vnd.oci.image.layer.v1.tar+gzip"
  and (.size | positive_integer)
  and (.digest | sha256_digest)
)
