def sha256_hex:
  type == "string" and test("^[0-9a-f]{64}$");

def sha1_hex:
  type == "string" and test("^[0-9a-f]{40}$");

def exact_subject:
  type == "array"
  and length == 1
  and (.[0].name | type == "string" and length > 0)
  and .[0].digest == {"sha256": $platform_digest_hex};

def exact_frontend_request:
  .predicate.invocation.parameters as $parameters
  | $parameters.args as $args
  | ($parameters.frontend == "gateway.v0")
  and (($parameters.locals // []) == [])
  and ($parameters.secrets == [])
  and (($parameters.ssh // []) == [])
  and (($args | keys | sort) == ([
    "build-arg:APP_VERSION",
    "build-arg:CODEX_SHA256",
    "build-arg:CODEX_VERSION",
    "build-arg:PYTHON_DEV_IMAGE",
    "build-arg:PYTHON_RUNTIME_IMAGE",
    "build-arg:UV_IMAGE",
    "build-arg:VCS_REF",
    "cmdline",
    "source",
    "target"
  ] | sort))
  and $args["build-arg:APP_VERSION"] == "1.0.10"
  and $args["build-arg:VCS_REF"] == $source_commit
  and $args["build-arg:PYTHON_DEV_IMAGE"]
    == "cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"
  and $args["build-arg:PYTHON_RUNTIME_IMAGE"]
    == "cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332"
  and $args["build-arg:UV_IMAGE"]
    == "ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"
  and $args["build-arg:CODEX_VERSION"] == "0.147.0"
  and $args["build-arg:CODEX_SHA256"]
    == "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"
  and $args.cmdline
    == "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
  and $args.source
    == "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"
  and $args.target == $role;

def expected_materials:
  ([
    {
      "uri": $source_uri,
      "digest": {"sha1": $source_commit}
    },
    {
      "uri": "pkg:docker/docker/dockerfile@1.7?digest=sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
      "digest": {"sha256": "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"}
    },
    {
      "uri": "pkg:docker/ghcr.io/astral-sh/uv@0.8.17?digest=sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1&platform=linux%2Famd64",
      "digest": {"sha256": "db99140470350437166de1fc646323ecb59e4d99d7857d0baf429a7b4a9523f3"}
    },
    {
      "uri": "pkg:docker/cgr.dev/chainguard/python@latest-dev?digest=sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2&platform=linux%2Famd64",
      "digest": {"sha256": "48060899de1ce8c95d987a2fc0da2a3ca1ef28d4aac5073bff2068a63f3ccce0"}
    },
    {
      "uri": "pkg:docker/docker/buildkit-syft-scanner@stable-1?digest=sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9&platform=linux%2Famd64",
      "digest": {"sha256": "187e1892a7752c9384c59aba9517dd8e40610b748c72773e87b63720514463c2"}
    }
  ] + if $role == "worker" then [
    {
      "uri": "https://github.com/openai/codex/releases/download/rust-v0.147.0/codex-x86_64-unknown-linux-musl.tar.gz",
      "digest": {"sha256": "0246e2e773834e07f0fb5249ed6ebad12e4591e608f8c7bb97dd6a9690544c36"}
    }
  ] else [
    {
      "uri": "pkg:docker/cgr.dev/chainguard/python@latest?digest=sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332&platform=linux%2Famd64",
      "digest": {"sha256": "e410b1cf97a99710ca1393cd4640e97e2784b0b2f3f2455ac38a3eda9b7e74ce"}
    }
  ] end)
  | sort_by(.uri);

def exact_immutable_materials:
  (.predicate.materials | sort_by(.uri)) == expected_materials
  and all(.predicate.materials[];
    (.digest | to_entries | length == 1)
    and all(.digest | to_entries[];
      (.key == "sha1" and (.value | sha1_hex))
      or (.key == "sha256" and (.value | sha256_hex))
    )
  );

._type == "https://in-toto.io/Statement/v0.1"
and .predicateType == "https://slsa.dev/provenance/v0.2"
and (.subject | exact_subject)
and .predicate.buildType == "https://mobyproject.org/buildkit@v1"
and .predicate.builder == {"id": ""}
and .predicate.invocation.configSource == {
  "digest": {"sha1": $source_commit},
  "entryPoint": "Dockerfile",
  "uri": $source_uri
}
and .predicate.metadata.completeness == {
  "environment": true,
  "materials": true,
  "parameters": true
}
and .predicate.invocation.environment.platform == "linux/amd64"
and exact_frontend_request
and exact_immutable_materials
