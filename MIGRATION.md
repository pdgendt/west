# west v1 -> v2: differences (migration notes)

Working aggregation of behavioral and interface **differences** between west v1 (pure Python) and
west v2 (Rust core + PyO3 bindings + thin Python wrappers). **This is a scratch/handoff document,
not user-facing docs** -- the goal is to capture what changed so a v1 user knows what to adjust.
Parity items (v2 matches v1) are intentionally not listed.

Each entry is tagged:

- **[changed]** -- behavior differs from v1.
- **[dropped]** -- v1 feature intentionally removed in v2.
- **[gap]** -- not yet implemented in v2; potential drop.

---

## Architecture

- **[changed]** v2's CLI is a single Rust binary (`west`). v1 was an in-process Python program.
  Consequences ripple through everything below -- most visibly, errors surface as a process exit
  code rather than a Python exception.
- **[changed]** Extension commands run by spawning `python -m west._dispatch <module> <class> ...`
  rather than importing and calling in-process.

## Errors & output

- **[changed]** Error prefix is `west: error: <msg>` (red bold on a TTY); warning prefix is `west:
  warning: <msg>` (yellow bold). v1 prefixed with `FATAL ERROR:` / `ERROR:` / `WARNING:` (colored
  via `colorama`, no bold). v2 standardizes the `west:` namespace and adds the bold weight.
- **[changed]** Because the CLI is a subprocess, a failing command exits non-zero instead of raising
  a Python exception in-process. Code that caught specific exceptions (e.g.
  `yaml.parser.ParserError` from `cmd('list')` on a malformed manifest) now sees a non-zero exit +
  the parse error on stderr.
- **[changed]** Parser diagnostics come from serde/saphyr, not pykwalify: e.g. a scalar where a list
  is expected reads `expected a sequence` (was `is not a list`); a malformed manifest reads `YAML
  parse error: ...`.
- **[changed]** Per-project `=== ...` banners and tail summaries go to **stderr**; only the actual
  result hits stdout. `west diff > out.patch`, `west grep PATTERN > matches`,
  `west forall -c '...' > out` now produce clean machine-readable stdout. v1 mixed banners and
  bodies on stdout.
- **[changed]** `--color {always,never,auto}` is now a per-command CLI flag on `update`, `forall`,
  `diff`, `status`, `compare`, and `grep`. `auto` (the default) resolves via `color.ui` config -- so
  the v1 single-knob behavior still works -- and the flag is an opt-in override. v1 had no
  per-command `--color`; `color.ui` was the only knob.

### Exit codes

v2 reserves a small, documented set of exit codes -- see `crates/west-cli/src/exit.rs` for the
canonical definitions:

| Code  | Meaning                                                                    |
|-------|----------------------------------------------------------------------------|
| `0`   | success                                                                    |
| `1`   | non-usage failure (also `--exit-code` divergence from diff/status/compare) |
| `2`   | user-error / pre-condition not met (see bullets below for examples)        |
| `141` | killed by SIGPIPE (`west cmd \| head`) -- see `bin/west.rs`                |

- **[changed]** `west diff PROJECT`, `west status PROJECT`, `west compare PROJECT`, `west forall -c
  CMD PROJECT`, `west grep PATTERN -p PROJECT` against an uncloned project now exit `2`. v1 returned
  `1` via `die()` -- every error was `1`. v2 reserves `2` for "the invocation itself was wrong" so
  scripts can distinguish a real runtime failure (`1`) from a pre-condition miss (`2`).
- **[changed]** `west config set` against a missing workspace returns `1` (FAILURE), not the
  previous v2 `3`. The `3` was an unintended outlier -- every other command surfaces "no workspace"
  as FAILURE.

### Verbosity

- **[changed]** Default log level is **Warn** (v1 was Info -- its `WestCommand.verbosity` defaulted
  to `Verbosity.INF`, so `inf()` / `wrn()` / `err()` all printed). v2 is one notch quieter by
  default; pass `-v` to recover the v1 default volume. Full mapping: `-v` -> Info, `-vv` -> Debug,
  `-vvv` -> Trace; `-q` -> Error, `-qq` -> Off.
- **[dropped]** The v1 `-vvv` debug behavior where `log.die()` raised a `RuntimeError` with a stack
  trace instead of exiting. v2's `-vvv` only raises the log level.
- **[changed]** v2 separates the "verbosity" and "chrome" knobs. `-q` drops the log level **and**
  splices `output.quiet=true` into inline config, which is what gates per-project banners + tail
  summaries. In v1 banner suppression was a side effect of `-q` reducing the single
  `WestCommand.verbosity` enum below `INF`; v2's split lets config alone hide banners without
  otherwise quieting warnings.

## Configuration

- **[changed]** Config files are **TOML** (`.west/config.toml`), not the INI format Python's
  `configparser` produced. Keys are dotted (`tool.git.fetch.tags`). Run `west config migrate` to
  convert an existing v1 INI file to v2 TOML at the same scope; the v1 file is left untouched, the
  new TOML is written next to it with type-aware coercion (booleans, integers, comma-separated
  lists).
- **[changed]** `west config` requires an explicit subcommand: `west config
  set|get|unset|list|migrate`. v1's positional form (`west config <key> [<value>]`) is gone.
- **[changed]** `west config get NAME --default VALUE` returns VALUE on stdout (exit 0) when NAME
  isn't set, mirroring `git config --get --default`. v1 exited non-zero with `"<name> was not set in
  requested location(s)"` on a missing key -- scripts had to wrap the call to provide a fallback.
- **[changed]** Per-VCS behavior knobs live under a `tool.<client>.*` namespace (modeled on
  `pyproject.toml`'s `[tool.<name>]`), read inside the client:
  - `tool.git.binary`
  - `tool.git.fetch.{strategy,tags,narrow,depth,force,extra-args}`
  - `tool.git.clone.extra-args`
  - `tool.git.submodules.{recurse,sync,init-config}`
- **[changed]** Two v1 keys move namespace; `west config migrate` applies the rewrites
  automatically:
  - `update.fetch` -> `tool.git.fetch.strategy` (same `always` / `smart` enum, just relocated).
  - `update.sync-submodules` (one boolean) -> `tool.git.submodules.sync` *and*
    `tool.git.submodules.recurse` (v2 separates the two effects v1 collapsed).
- **[dropped]** `zephyr.base` and `zephyr.base-prefer` -- v1 cached the active zephyr repo here; v2
  treats this as a zephyr-extension concern. The migrate command preserves them verbatim as strings;
  remove them from your config if you don't need them.

## Manifest schema & parsing

- **[dropped]** Numeric scalars in `groups:` lists are no longer coerced to strings (`groups: [1,
  3.14]`). Quote them: `["1", "3.14"]`. `is_group()` likewise no longer accepts numbers.
- **[dropped]** The legacy top-level `west:` section (pre-0.6 self-bootstrap config). v1 ignored it;
  v2 rejects it as an unknown field. Remove it from old manifests.
- **[gap]** `manifest.version` is parsed but not yet validated against the supported window;
  `ManifestVersionError` is a compat stub. (`TODO.md`)
- **[gap]** Legacy `name-whitelist` / `name-blacklist` / `path-whitelist` / `path-blacklist` aliases
  on an `import:` map are not yet accepted; use `name-allowlist` / `name-blocklist` / `path-*list`.
  (`TODO.md`)

## Imports & resolution

- **[changed]** Import loops are reported as `ManifestImportFailed: ... import cycle detected` via
  direct visited-file tracking. v1 relied on Python's `RecursionError` and reported
  `_ManifestImportDepth` ("too deep"); that class remains as a no-op compat subclass.
- **[dropped]** v0.9 group-filter semantics (imports don't contribute; the
  `_legacy_group_filter_warned` attribute). Only v0.10 semantics -- imported group-filter entries
  merge into the resolved filter, then simplify to the disabled set -- are supported.

## `west init`

- **[changed]** New `-t` / `--topdir <WORKSPACE_DIR>` flag for explicit workspace location. Works in
  both modes. Mirrors upstream PR #953's direction.
- **[changed]** `--manifest-path` is a new flag with one consistent meaning across both modes --
  "the manifest dir's subpath relative to workspace." In `-l` mode it's an alternative to the
  positional (both must agree if given). In remote mode it sets the clone target subpath.
- **[changed]** Remote-mode positional `directory` (= workspace target) is soft-deprecated in favour
  of `-t`. v1 used it as the canonical workspace arg.
- **[changed]** No default manifest URL. v1's bare `west init` defaulted to cloning the Zephyr URL;
  v2 requires `-u`/`--url` or `-l`/`--local`.

## `west update`

- **[changed]** Network (no-cache) materialization uses `git init` + `git fetch` instead of `git
  clone` -- a single surgical fetch, no default-branch over-fetch, and no stray local branch (only
  `manifest-rev` lives in `refs/heads/`). Cache-backed materialization still clones from the local
  cache (hardlinked objects).
- **[changed]** west fetches by **URL**, not by the configured remote name -- the `[remote
  "<name>"]` git config is a user convenience only.
- **[changed]** `west update <project>...` rejects a selector that's only reachable via a
  per-project `import:` (must clone the parent, or run bare `west update`, first) -- and bare `west
  update` no longer clones unrelated import-providers just to update a named project. The synthetic
  manifest project (`manifest` / configured `manifest.path`) gets a dedicated rejection: "the
  manifest project itself is not a west update target -- use `west init` to change it."
- **[changed]** Parallel by default. `update.jobs` defaults to `min(num_cpus, 8)`; v1 was always
  serial. On a TTY the progress UX is an indicatif `MultiProgress` (per-project bars + a "Updated
  N/M" summary).
- **[changed]** `--narrow` / `update.narrow` map to `tool.git.fetch.narrow`.
- **[changed]** `--submodule-init-config KEY=VALUE` CLI flag replaced by the
  `tool.git.submodules.init-config` config list (e.g. set `protocol.file.allow=always`).

## `west list`

- **[changed]** Uncloned per-project imports are skipped with a single trailing warning rather than
  v1's `die()` -- keeps `list` useful in pre-update CI flows.

## `west diff`

- **[changed]** Tail summary `Empty diff in N projects.` (on stderr) when at least one project was
  non-empty. v1 had no per-run summary -- users had to scan the (empty-project-suppressed) output by
  hand to know how many projects were clean.
- **[changed]** `-j/--jobs N` parallel (`diff.jobs` twin). v1 was serial.

## `west status`

- **[changed]** Default output is `git status --short` (porcelain v1 shape). v1 ran the verbose `git
  status` per project; v1 already skipped *clean* projects, so the noisy-clean problem doesn't
  factor -- the change is the compact format for the projects that *do* have something to show.
- **[changed]** `--long` opts back into v1's verbose `git status` form AND shows every project,
  including clean ones (the long-form's value is the per-project context).
- **[changed]** `--exit-code` returns 1 when any project is dirty. v1 had no equivalent.

## `west compare`

- **[changed]** `-f/--format <TPL>` switches to a machine-readable per-line layout (same template
  keys as `west list`'s `{abspath}` / `{posixpath}` / etc.). v1 only had the human-readable form.
- **[changed]** `-j/--jobs N` parallel via rayon. v1 was serial. (`--exit-code` and
  `--ignore-branches` / `--no-ignore-branches` predate v2.)

## `west grep`

v1 added `west grep` in 1.5.0 with the three-tool dispatch (`git-grep` / `ripgrep` / `grep`) and the
per-tool config keys (`grep.tool`, `grep.{git-grep,ripgrep,grep}-{path,args}`, `grep.color`); those
carry over unchanged. v2 differs in:

- **[changed]** Parallel-by-default, manifest-order drain (`grep.jobs` controls parallelism). v1 was
  serial.

## `west forall`

- **[changed]** `-j N` parallel via rayon (`forall.jobs` twin). v1 was serial (a single
  `subprocess.Popen(...).wait()` loop with natural stdio inheritance).
- **[changed]** Parallel mode demultiplexes per-project stdio: workers capture child stdout/stderr
  separately and the driver drains each child's stdout onto the parent's stdout and each child's
  stderr onto the parent's stderr, keeping the streams separated even under capture (v1's
  serial-inherit didn't need this because each child owned the parent's fds in turn).

## `west manifest`

- **[changed]** `--active-only` (resolve / freeze) filters out projects the workspace's
  group/project filter marks inactive. Fixes the v1 papercut where `west update` with a default
  group-filter followed by `west manifest --freeze` errored on the first uncloned inactive project.
- **[changed]** When the source manifest doesn't set `self.path:` explicitly (the resolver defaults
  it to the literal `"manifest"`), the emitted output substitutes the workspace's configured
  `manifest.path` so the freeze reflects the real layout. v1 emitted the literal default.
- **[changed]** `--format yaml|toml|json` + `--out PATH` apply to resolve/freeze/untracked. Format
  default follows the source manifest's extension. v1 was YAML-only.

## Python API

- **[changed]** `Manifest.as_yaml()` / `as_frozen_yaml()` no longer accept `**kwargs`. v1 passed
  them to `yaml.safe_dump` (`sort_keys`, `indent`, `default_flow_style`, ...); `serde_yaml_ng` emits
  canonical block-style YAML and doesn't expose those knobs.
- **[changed]** YAML key order changed. `Manifest.as_yaml()` and `west manifest --resolve` /
  `--freeze` now emit `group-filter`, `projects`, `self` at the manifest level and `name`,
  `description`, `url`, `revision`, `path`, `clone-depth`, `west-commands`, `groups`, `submodules`,
  `userdata` per project. v1 used pyyaml's alphabetic default.
- **[changed]** `Manifest.has_imports` is always `False`. The rust resolver inlines imports during
  parsing; by the time the python wrapper sees the manifest, there's no marker for whether the
  original YAML had `import:` directives. Extensions that branched on `has_imports` will always take
  the false branch.
- **[changed]** `Manifest.yaml_path` is deprecated in favour of `Manifest.path_raw` (same value,
  same `None`-when-absent semantics). Accessing `yaml_path` emits a `DeprecationWarning`.
- **[changed]** `west.commands.extension_commands()` schema validation downgraded -- `_ext_specs()`
  no longer pykwalify-checks `west-commands` files; malformed input surfaces as `KeyError` /
  `TypeError` rather than a pykwalify schema error. The rust binary's own extension-discovery path
  validates fully (garde derives), so dispatch is unaffected; only direct python callers of
  `extension_commands()` see the regression.

## Extension commands & help

- **[changed]** Extension discovery is done in Rust (reads the project's `west-commands` file
  directly, format chosen by extension -- `.yml` / `.yaml` / `.toml` / `.json`). The Python
  `extension_commands()` / `WestExtCommandSpec` discovery API was removed (it was unused).
- **[changed]** `west -h` / `--help` is clap's static help (built-ins only). The workspace-aware
  listing (extensions + aliases) is under `west help`. v1 showed extensions under `-h`.
- **[changed]** Help listing format: two-column, no `:` after the name, and help-less commands show
  a bare name (v1 used `<name>:` plus a "(no help provided; try ...)" placeholder).
- **[changed]** Extension-command collisions resolve identically to v1 (a built-in beats a
  same-named extension; the first-declared extension beats a later duplicate) but **silently** -- v2
  doesn't print v1's "ignoring project ... extension command ..." warnings on every invocation.

## Tooling

- **[gap]** No published JSON Schema for the manifest format yet (v1 shipped a pykwalify
  `manifest-schema.yml`). The format lives in the Rust structs + custom `Deserialize` impls.
  (`TODO.md`)

## Removed features

Hard "no" list -- features that existed in v1 solely for back-compat or that v2 explicitly walks
away from. None affect normal day-to-day use.

- **[dropped]** `west selfupdate`. Already a stub error in v1; upgrades happen via `cargo install` /
  `brew upgrade` / system package manager.
- **[dropped]** `west update --exclude-west`. Deprecated and ignored with a warning in v1; had no
  coherent meaning in v2's project model.
- **[dropped]** Schema v0.9 group-filter quirks. v0.10+ semantics only.
- **[dropped]** Legacy import-map aliases (`name-whitelist`, `path-whitelist`, `name-blacklist`,
  `path-blacklist`). Modern names (`*-allowlist`, `*-blocklist`) only.
- **[dropped]** Wildcard-refspec SHA fallback (`refs/heads/*:refs/west/*`). GitHub / GitLab / Gerrit
  all honour `uploadpack.allowAnySHA1InWant`.
- **[dropped]** `ZEPHYR_BASE` fallback in `west_topdir`. Zephyr-specific environment policy; v2's
  contract is workspace-or-bust.
- **[dropped]** Deprecated python API: `Manifest.path` property; the top-level
  `read_config`/`update_config`/`delete_config` free functions in `west.configuration`; the entire
  `west.log` module.
