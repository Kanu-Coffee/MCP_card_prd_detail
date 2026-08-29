# Third-Party Notices

CardRAG's own code and container images are distributed under the project's
Proprietary license. Third-party components remain subject to their own license
terms. This summary is informational and is not a replacement for those terms.

The locked Python dependency names, versions, source artifacts, and hashes are
recorded in `uv.lock`. Installed distribution metadata contains the applicable
license files. Notable runtime components include:

- `pypdfium2` and its PDFium binary (BSD-3-Clause, Apache-2.0, and bundled
  dependency licenses)
- Pillow (MIT-CMU)
- certifi (MPL-2.0)
- NumPy (BSD-3-Clause and bundled dependency licenses)
- FastAPI, MCP, Pydantic, Typer, and other Python packages under the licenses
  declared in their installed package metadata
- the OpenAI Codex CLI in the Worker image, distributed under its upstream
  project terms
- Chainguard Python and Wolfi base-image packages, with package license and SBOM
  metadata retained in the image
- Wolfi `bubblewrap` and `libcap` in the Worker image, installed from the
  official signed Wolfi package index at the exact versions recorded in the
  Dockerfile

The release workflow attaches an SBOM attestation to each Worker and MCP image.
This notice is copied into both images at
`/usr/share/doc/cardrag/THIRD_PARTY_NOTICES.md`.
