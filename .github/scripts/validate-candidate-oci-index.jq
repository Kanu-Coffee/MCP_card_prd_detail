def positive_integer:
  type == "number" and . > 0 and floor == .;

.mediaType == "application/vnd.oci.image.index.v1+json"
and (.manifests | type == "array" and length == 2)
and ([.manifests[].digest] | unique | length == 2)
and ([.manifests[] | select(
  .digest == $platform_digest
  and .mediaType == "application/vnd.oci.image.manifest.v1+json"
  and (.size | positive_integer)
  and .platform == {"architecture": "amd64", "os": "linux"}
)] | length == 1)
and ([.manifests[] | select(
  .digest == $attestation_digest
  and .mediaType == "application/vnd.oci.image.manifest.v1+json"
  and (.size | positive_integer)
  and .platform == {"architecture": "unknown", "os": "unknown"}
  and .annotations["vnd.docker.reference.type"] == "attestation-manifest"
  and .annotations["vnd.docker.reference.digest"] == $platform_digest
)] | length == 1)
