# TODO

Open gaps in the Python-to-Rust port. Each entry names the legacy behavior,
where the current implementation diverges, and what would need to change.

## Manifest resolver

### `manifest.version` is parsed but not validated

**Legacy behavior.** Versions outside the supported window were rejected at
parse time — too-high → `ManifestVersionError`, too-low (< 0.6.99) →
`MalformedManifest`. Covered in `tests-legacy/test_manifest.py:96` and
`:149`.

**Current behavior.** `ManifestSection.version` is stored without validation
(`crates/west-core/src/manifest.rs:343,908,1425`); `ManifestVersionError` in
`src/west/manifest.py:131` is a stub kept only so legacy `from west.manifest
import ManifestVersionError` doesn't break.

**To do.** Add a check in `Resolver::process_root` (or `into_manifest`) that
parses the version string and rejects anything outside the supported window.
Introduce a dedicated `ManifestError::UnsupportedVersion` variant, map it to
`ManifestVersionError` in `manifest_error_to_py` (`crates/west-cli/src/python/manifest.rs:843`),
and leave the low-version case mapping to `MalformedManifest`.

## Tooling

### Publish a JSON Schema for the manifest format

**Legacy state.** west v1 shipped a `pykwalify`-style YAML schema as
part of the python package (`manifest-schema.yml`). Editors, CI checks,
and downstream tools could validate `west.yml` files against it
without depending on west itself.

**Current state.** The Rust port encodes the manifest format only in
the `ManifestFile` / `ManifestSection` / `ImportSchema` / `ImportMap`
structs, plus the hand-rolled `Deserialize` impls. There is no
external artifact downstream consumers can read. Editors, the Zephyr
docs site, and third-party tooling that wants to validate manifests
have nothing to point at.

**To do.** Wire `schemars` (or hand-author, the schema is small enough)
to emit a JSON Schema from the manifest structs and publish it as
`west-manifest.schema.json` alongside the wheel / sdist. Pin the
schema with a regression test that diffs the generated output against
a committed snapshot — drift between the structs and the schema would
otherwise be invisible until a downstream user complained. Suggested
follow-ups once the schema exists:

- Reference the schema URL from a `# yaml-language-server: $schema=...`
  hint in west.yml examples, so VS Code (and any editor using the
  YAML language server) gives autocompletion and inline validation out
  of the box.
- Mention the schema location in `doc/`.
- The runtime parsing path stays serde + derive + the small custom
  Deserialize impls — JSON Schema isn't a replacement for those, just
  a published *description* of what they accept.
