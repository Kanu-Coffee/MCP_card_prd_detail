def sha256_hex:
  type == "string" and test("^[0-9a-f]{64}$");

def sha1_hex:
  type == "string" and test("^[0-9a-f]{40}$");

def exact_keys($expected):
  (keys | sort) == ($expected | sort);

def expected_subject_name:
  "pkg:docker/ghcr.io/kanu-coffee/mcp-card-prd-detail-candidate"
  + "@candidate-v1.0.11-\($role)-\($source_commit)?platform=linux%2Famd64";

def exact_subject:
  . == [{
    "name": expected_subject_name,
    "digest": {"sha256": $platform_digest_hex}
  }];

def expected_build_args:
  {
    "build-arg:APP_VERSION": "1.0.11",
    "build-arg:CODEX_SHA256":
      "605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6",
    "build-arg:CODEX_VERSION": "0.151.0",
    "build-arg:PYTHON_DEV_IMAGE":
      "cgr.dev/chainguard/python:latest-dev@sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2",
    "build-arg:PYTHON_RUNTIME_IMAGE":
      "cgr.dev/chainguard/python:latest@sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332",
    "build-arg:UV_IMAGE":
      "ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1",
    "build-arg:VCS_REF": $source_commit
  };

def expected_frontend_args:
  expected_build_args + {
    "cmdline":
      "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
    "source":
      "docker/dockerfile:1.7@sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
    "target": $role
  };

def expected_root:
  {
    "configSource": {
      "uri": $source_uri,
      "digest": {"sha1": $source_commit},
      "path": "Dockerfile"
    },
    "request": {
      "args": (expected_build_args + {"target": $role})
    }
  };

def expected_git_secret_declarations:
  [
    {"id": "GIT_AUTH_HEADER", "optional": true},
    {"id": "GIT_AUTH_TOKEN", "optional": true}
  ] | sort_by(.id);

def exact_frontend_request:
  .predicate.invocation.parameters as $parameters
  | ($parameters | exact_keys([
      "args",
      "compatibilityVersion",
      "frontend",
      "root",
      "secrets"
    ]))
  and $parameters.frontend == "gateway.v0"
  and $parameters.compatibilityVersion == 30
  and $parameters.args == expected_frontend_args
  and $parameters.root == expected_root
  and (($parameters.secrets | sort_by(.id)) == expected_git_secret_declarations);

def expected_git_source:
  {
    "identifier":
      "git://github.com/Kanu-Coffee/MCP_card_prd_detail.git#\($source_commit)",
    "attrs": {
      "git.authheadersecret": "GIT_AUTH_HEADER",
      "git.authtokensecret": "GIT_AUTH_TOKEN",
      "git.fullurl": "https://github.com/Kanu-Coffee/MCP_card_prd_detail.git"
    }
  };

def exact_git_source_without_secret_mounts:
  ([
    .predicate.buildConfig.llbDefinition[].op.Op.source?
    | select(
        (.identifier? | type) == "string"
        and (.identifier | startswith("git://"))
      )
  ] == [expected_git_source])
  and ([
    .predicate.buildConfig
    | paths(objects) as $path
    | getpath($path) as $object
    | select(any($object | keys[]; ascii_downcase | contains("secret")))
    | $object
  ] == [expected_git_source.attrs]);

def expected_materials:
  ([
    {
      "uri": "pkg:docker/cgr.dev/chainguard/python@latest-dev?digest=sha256:4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2&platform=linux%2Famd64",
      "digest": {"sha256": "4e2adecf67a1d18773c55b5526b47436392b9816ae6b8d92575979a2ab9de8b2"}
    },
    {
      "uri": "pkg:docker/docker/buildkit-syft-scanner@stable-1?digest=sha256:ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9",
      "digest": {"sha256": "ae4f3b554449e7e25548e7d8ccc029d17357348e30c6e3df01b92bc93654d6a9"}
    },
    {
      "uri": "pkg:docker/docker/dockerfile@1.7?digest=sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720&platform=linux%2Famd64",
      "digest": {"sha256": "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"}
    },
    {
      "uri": "pkg:docker/docker/dockerfile@1.7?digest=sha256:b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720",
      "digest": {"sha256": "b5f3b260a9678e1d83d2fce86eeddf79420b79147eaba2a25986f47133d73720"}
    },
    {
      "uri": "pkg:docker/ghcr.io/astral-sh/uv@0.8.17?digest=sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1&platform=linux%2Famd64",
      "digest": {"sha256": "e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1"}
    },
    {
      "uri": $source_uri,
      "digest": {"sha1": $source_commit}
    }
  ] + if $role == "worker" then [
    {
      "uri": "https://github.com/openai/codex/releases/download/rust-v0.151.0/codex-x86_64-unknown-linux-musl.tar.gz",
      "digest": {"sha256": "605b4b183f22c645f5def63a5b7191767407fb66a6feaec4eaf10b5b7e0058f6"}
    }
  ] elif $role == "mcp" then [
    {
      "uri": "pkg:docker/cgr.dev/chainguard/python@latest?digest=sha256:f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332&platform=linux%2Famd64",
      "digest": {"sha256": "f47d995d001c1f949d560b1158d7f3ae556aad75a1044e72a125c900c1f05332"}
    }
  ] else [] end)
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

($source_commit | sha1_hex)
and ($platform_digest_hex | sha256_hex)
and ($role == "worker" or $role == "mcp")
and (exact_keys(["_type", "predicate", "predicateType", "subject"]))
and ._type == "https://in-toto.io/Statement/v1"
and .predicateType == "https://slsa.dev/provenance/v0.2"
and (.subject | exact_subject)
and (.predicate | exact_keys([
  "buildConfig",
  "builder",
  "buildType",
  "invocation",
  "materials",
  "metadata"
]))
and .predicate.buildType == "https://mobyproject.org/buildkit@v1"
and .predicate.builder == {"id": ""}
and (.predicate.invocation | exact_keys(["configSource", "environment", "parameters"]))
and .predicate.invocation.configSource == {
  "digest": {"sha1": $source_commit},
  "entryPoint": "Dockerfile",
  "uri": $source_uri
}
and .predicate.invocation.environment == {"platform": "linux/amd64"}
and .predicate.metadata.completeness == {
  "environment": true,
  "materials": true,
  "parameters": true
}
and (.predicate.buildConfig | type == "object")
and (.predicate.buildConfig.llbDefinition | type == "array" and length > 0)
and exact_frontend_request
and exact_git_source_without_secret_mounts
and exact_immutable_materials
