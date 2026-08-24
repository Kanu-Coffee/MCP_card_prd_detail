# Third-Party Notices

CardRAG's own code and container image are distributed under the project's
Proprietary license. The image also contains separately licensed third-party
components. Those licenses continue to apply to their respective components;
this notice does not replace or modify them.

## pypdfium2 5.12.1 and PDFium

CardRAG uses pypdfium2 5.12.1 to validate PDF documents and render individual
pages for LLM-based OCR. pypdfium2 is offered under Apache-2.0 or
BSD-3-Clause terms; this distribution relies on the BSD-3-Clause option for
pypdfium2 itself. The bundled PDFium library is distributed under its BSD-style
license and includes other components under their respective permissive
licenses.

The complete, unmodified license payload shipped in the selected pypdfium2
Linux x86-64 wheel is installed in the container at:

`/usr/share/licenses/cardrag/pypdfium2`

That directory contains the pypdfium2 license texts and PDFium's complete
build-specific `BUILD_LICENSES` set. It is the authoritative notice payload for
the binary included in this image.

Required acknowledgements from that payload include:

- This software is based in part on the work of the Independent JPEG Group.
- This product includes software developed by the FreeType Project
  (https://www.freetype.org/).

Neither Google, the PDFium authors, the pypdfium2 authors, nor their
contributors endorse CardRAG.

## Pillow

Pillow is used to encode rendered page bitmaps as PNG. Pillow is distributed
under the MIT-CMU license; its license metadata remains installed alongside the
Python distribution in the image.

## certifi 2026.7.22

certifi 2026.7.22 declares MPL-2.0. Its installed license text is copied to
`/usr/share/licenses/cardrag/certifi/LICENSE`; its locked source and binary
artifact references and hashes are recorded in `uv.lock`.

## psycopg packages

The image contains psycopg 3.3.4, psycopg-binary 3.3.4 and psycopg-pool 3.3.1,
which declare LGPL-3.0-only. Their installed license texts are copied to the
respective `/usr/share/licenses/cardrag/psycopg*` directories. The locked
artifact references and hashes are recorded in `uv.lock`. Release review must
continue to assess any applicable source, modification and relinking
obligations; including these notices is not by itself a legal conclusion.

## PostgreSQL 17.11 client tools

The owner-only admin image includes the PostgreSQL 17.11 `pg_dump`, `pg_restore`
and `psql` clients, plus libpq 18.6, for portable logical backup and restore. They
are distributed under the PostgreSQL License. The exact client binaries and
license are copied from the same digest-pinned pgvector 0.8.6/PostgreSQL 17.11
image used by the database server. The license notice is installed at
`/usr/share/licenses/cardrag/postgresql-client-17/COPYRIGHT`.  These tools are
not present in the public MCP or worker role images.

## Runtime license inventory

The release workflow creates an SBOM and a locked runtime dependency-license
inventory. The image records every installed runtime distribution, its version
and any installed `dist-info/licenses` files in
`/usr/share/licenses/cardrag/dependency-license-manifest.json`. Those license
files are copied to `/usr/share/licenses/cardrag/{normalized-package-name}/`.
The recorded release decisions are packaging and compliance-review records,
not legal advice or legal-license approvals.
