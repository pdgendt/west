# Rust review: `west-core` + `west-cli` (architectural pass)

**Scope**: ~21k LoC of production Rust + ~11k LoC of tests across two crates. Biased toward
trait/API design, correctness/security, and PyO3 bindings. Every finding cites file:line.

---

## Top-line: the codebase is in genuinely good shape

Before the punch-list, the surprising **positives** worth noting (easy to take for granted):

- **Zero `unwrap`/`expect`/`panic!` in non-test code in `manifest.rs` (3500+ lines)** -- that's
  exceptional discipline. `config.rs` has 2 `expect()`s, both on `parse_key`-validated invariants
  with inline justification (`config.rs:207, 441`).
- **Cycle/depth detection is explicit**, not "rely on RecursionError" -- `manifest.rs:887`
  (`visited_files`, `visited_projects`) plus scope-tracked insert/remove for proper unwinding.
- **Subprocess hygiene is bulletproof**: every `git` invocation goes through
  `Command::new(...).args(...)` (argv-based, no shell). Only intentional shell evaluation is
  `forall -c CMD` (user opt-in). `shlex` is used in two places: alias parsing and
  `grep.{tool}-args` config -- both feed argv-based downstream.
- **Path-traversal hygiene is solid**: `relative_path_escapes_root` (`manifest.rs:1574`) uses
  `path_clean` for purely lexical normalization (no filesystem touch, no TOCTOU window) plus an
  explicit `path_collides_with_west_dir` (`manifest.rs:1586`) for the `.west` reserved dir. Reused
  by `extension.rs:310` for the `west-commands.yml` `file:` rule.
- **Error types are well-modeled** (`thiserror` with `#[source]` chains, focused variant sets,
  no `Box<dyn Error>` bailouts).
- **1 TODO marker in 21k LoC** of production code. Drift indicator: green.
- **18 clippy warnings total** with `--all-targets` (categories: 2x `collapsible_if`, 1x
  `manual_map`, 1x `useless_conversion`, 1x `redundant_pattern_matching`, 1x upstream
  `garde_derive` `js-sys` cfg). For this size, exceptional.

---

## Architecture

- **2-crate split is clean**: `west-core` (10 files / 9k LoC) is the library; `west-cli` (41 files
  / 12k LoC) is the binary + PyO3 bindings. CLI depends on core only via core's public surface.
- **`west-core/src/lib.rs` is just 14 lines** -- pure module-index plus the `WestNotFound` error.
  The intent is right.
- **Subdirectories within `west-cli/src/commands/`** group cohesively (`config/{get,set,unset,
  list,migrate,mod}`, `update/{cache,error,import_source,indicatif_reporter,mod,output}`).
- **`pub:pub(crate):pub(super)` ratio = 198:30:17**. The 198 `pub`s lean a bit hot -- many
  `commands/*` items don't need to be globally public -- but most are justified by the PyO3
  binding consumption pattern. **Should fix**: a sweep with `cargo public-api` would likely
  tighten this by ~20-30 items.

---

## Trait & API design (high bias)

### `Vcs` trait (`west-core/src/vcs/mod.rs`)

**Strengths**:

- Genuinely declarative -- "what I want", not "how to do it". `set_manifest_rev`/`manifest_rev`
  are storage-agnostic (git uses `refs/heads/manifest-rev`, the trait doesn't pin it).
- `Spec` struct pattern (`CloneSpec`, `InitSpec`, `FetchSpec`, `DiffSpec`, `StatusSpec`) keeps
  trait signatures stable as new knobs are added -- exactly the right move for a trait with
  future second/third implementations.
- `RevSpec` enum (`mod.rs:177`) closes the magic-string class of bug. Deliberately no
  `From<&str>` -- well-reasoned comment at `mod.rs:171-175`.
- Object-safe; `Send + Sync` bound is explicit and justified (`mod.rs:156`).
- `fmt::Debug` requirement is useful for trace logs.

**Should fix**:

1. **`ColorMode::Auto` is documented as a footgun** (`mod.rs:565-575`) -- "clients usually
   default to `Never` when stdout is a pipe, which is the case when `Vcs::diff` captures into a
   `Vec<u8>`." The trait shape lets callers pass `Auto` while capturing, getting `Never` despite
   "auto"-meaning intent. The CLI layer already resolves `Auto` to `Always`/`Never` before
   calling -- so `Auto` on the trait is dead surface area. Consider removing the variant or
   splitting `ColorMode` (trait-side) from `ColorArg` (CLI-side).
2. **`Vcs::ls_tree_at_ref` returns `Vec<String>` of filenames** with no file-vs-dir distinction
   (`mod.rs:220-225`). Callers can't tell which entries are subdirectories without a follow-up
   call. If the import resolver only cares about files at the directory level, that's the
   documented contract -- but the trait surface should make that explicit (return
   `Option<Vec<TreeEntry>>` where `TreeEntry { name, kind }`).
3. **Inconsistency between `Output` and `Write`**: `clone`/`fetch`/`checkout`/`rebase`/
   `update_submodules` take `&mut Output<'_>`; `diff`/`status` take `&mut dyn Write`. The split
   is defensible (progress vs result) but it forces callers to know which path they're on. Not
   a bug; worth a sentence in the trait doc.
4. **`SubmoduleScope::Specific(&[])` contract is "must be a no-op"** (`mod.rs:520-522`) but
   enforcement is per-client. Could be tightened: a `pub fn is_empty(&self) -> bool` on
   `SubmoduleScope` plus a default `update_submodules` early-return for empty scope.

**Nit**:

- `Vcs::update_submodules` mixes data + behavior + hint positionally (`scope`, `strategy`,
  `reference`). A `SubmoduleSpec` struct would be consistent with the rest of the trait.
- `from_config` (`mod.rs:676`) is the only construction entry point. Tests wanting a hand-built
  `GitOptions` have to construct a `Configuration` first. A `GitClient::new(opts)` would unlock
  simpler unit tests.

### `ImportSource` trait + `ProgressSink` (small but worth a nod)

- `ProgressSink: Send` is correctly stated (`mod.rs:99`); `NullSink` is a useful default;
  `LineSink<W: Write + Send>` is a generic-over-writer wrapper. No issues.
- `PyImportSource` (in `python/manifest.rs:650`) does the right `Python::attach` + GIL-bound
  callback.

---

## PyO3 bindings (high bias)

`crates/west-cli/src/python/` -- 1731 LoC across 6 files.

**Strengths**:

- **Modern PyO3 0.28 throughout**: `Bound<'_, PyModule>`, `Bound<'py, PyAny>`, `into_pyobject`
  (no `IntoPy`-deprecation warnings).
- **GIL discipline is correct**: `Python::attach` in `PyImportSource::project_manifest`
  (`python/manifest.rs:660`). Callbacks correctly bound only inside the GIL scope.
- **Error mapping is thoughtful** (`python/manifest.rs:865-879`): `UnknownProject -> PyKeyError`,
  `Io -> PyIOError`, import failures -> `ManifestImportFailed`, rest -> `MalformedManifest`.
  Matches Python convention.
- **Exception hierarchy via `create_exception!`** (proper PyException subclasses).
- **`#[pyclass(frozen)]` on `Submodule`/`GroupFilterEntry`/`ManifestRepo`** -- immutability
  where appropriate.
- **`subclass`** on `Manifest`/`Project` so Python wrappers can extend.
- **`unsendable`** correctly applied to `Manifest`/`LoadedManifest` (their `inner` cores carry
  non-Sync state).
- **`bool` checked before `int`** in `py_to_value` (`python/data.rs:129-131`) -- crucial because
  Python's `bool` is an `int` subclass. Easy to miss.
- **NaN/inf rejection** for float (`python/data.rs:140-144`).
- **`#[allow(clippy::upper_case_acronyms)]`** with justification ("python ConfigFile.ALL etc.
  are stable") on `python/config.rs:44`.

**Should fix**:

1. **`Project::from_core` (and `ManifestRepo::from_core`) deep-clone everything on every
   `Manifest::projects` access** (`python/manifest.rs:302-322`). For workspaces with 100+
   projects, `manifest.projects` is a per-project full clone including `userdata:
   serde_json::Value` recursion. Comment at line 535 says "shallow clone" -- it's not shallow
   given `Value` depth. Consider holding `Arc<core::Project>` in the Python `Project` instead.
2. **`Manifest::is_active` and `LoadedManifest::is_active` reconstruct `core::Project` from the
   Python `Project`'s unpacked fields** (`python/manifest.rs:581-593` + `811-823`) -- duplicated
   across the two methods, ~13 lines each, and the comment says "only the `groups` field
   matters." Refactor: either store `core::Project` directly in the Python `Project` (alongside
   unpacked fields, or as the source of truth), OR expose a `is_active_by_groups(&[String]) ->
   bool` shortcut on `LoadedManifest`.
3. **`from_yaml_str_with_imports` default `import_flags=FLAG_FORCE_PROJECTS`**
   (`python/manifest.rs:511`) -- a non-zero default. A caller forgetting the flag implicitly
   opts into `FORCE_PROJECTS`. Documented inline but a footgun; consider requiring an explicit
   value or making the default `0`.
4. **`value_to_py(Value::String(s))` does `s.clone()`** (`python/data.rs:103`) -- unnecessary,
   `s.as_str().into_pyobject(py)` would borrow. Per-string nit; matters at scale.
5. **u64-doesn't-fit-i64 stringifies** (`python/data.rs:97-100`). Python has arbitrary-precision
   int -- could surface as a Python int via `PyLong::new_from_str`. Edge case but technically
   lossy today.

**Nit**:

- The `from_py_object` attribute is set on `Submodule`/`GroupFilterEntry`/`Project`/
  `ManifestRepo`. The 0.27 transition deprecation around this was already handled with
  `skip_from_py_object` on `ProjectFilter`. Worth re-auditing the four `from_py_object` cases:
  are any of them never converted *back* from Python? If so, drop the attribute for consistency.

---

## Correctness & security (high bias)

- **`topdir.rs`** (`west-core/src/topdir.rs`): walks `cur.pop()` purely on the in-memory path
  -- no symlink traversal, no `canonicalize`. No DoS path, no TOCTOU. Documented invariant
  absent; **should add** a one-line "we never canonicalize; manifest paths resolve as-written"
  doc.
- **Path-traversal**: `relative_path_escapes_root` + `path_collides_with_west_dir` cover both
  `..`-traversal and `.west` collision via `path_clean` (lexical, no filesystem). Used
  consistently across `manifest.rs:1140` and `extension.rs:310`. **Solid.**
- **Subprocess argv**: 100% argv-based for git; `shlex::split` only on user-explicit config
  strings (`alias.*`, `grep.{tool}-args`) which feed argv-based downstream. **No injection
  surface.**
- **Cycle detection** in the resolver uses an explicit canonicalized-path visited set with
  scoped insert/remove (`manifest.rs:1320,1339`).
- **The `forall` shell-evaluation path** is documented as user-intent and gated by `-c CMD`. As
  designed.

**Should add**: documented invariant comments at `topdir.rs:1` and at the head of `manifest.rs`
for the "no fs touch during resolution" property -- this is a security-relevant choice and
currently invisible.

---

## Other observations

### Build & lint hygiene

- **No workspace `[lints]` table.** Clippy isn't a CI gate. The 18 warnings (mostly nits) sit
  there. **Should fix**: add a workspace `[lints.clippy] = { deny = "warnings" }` plus a CI
  clippy job; current count is so low that this won't bite.
- **No `cargo fmt --check`** in CI.
- **MSRV (1.93) declared in Cargo.toml** but no `rust-toolchain.toml` enforcing locally --
  different environments could compile with newer features inadvertently. Nice-to-have.
- **CI is otherwise security-conscious**: scorecards, codeql, dependabot, dependency-review.
  Worth keeping.

### Documentation

- **~52% of `pub` items have rustdoc.** The half that's missing is mostly internal-feeling pubs
  (e.g., binding glue methods). Worth tightening before 2.0 -- the trait surface docs are
  already in great shape, so closing the gap is mostly mechanical.
- **Module-level docstrings are excellent** -- `west-core/src/vcs/mod.rs`, `commands/init.rs`,
  etc. all open with substantial intent-conveying comments.

### Test coverage

- 524 inline `#[test]`s + ~11k LoC integration tests. The integration test density is unusual
  (in a good way) for a Rust project this size.
- **Gap**: most error-path tests in the resolver verify *that* an error is raised, fewer
  verify *exact* error structure. Not critical at this stage.

### Investigated and intentionally not flagged

- **`--color` as a global flag.** Considered hoisting `--color {always,never,auto}` from
  per-command (today: `diff`, `status`, `compare`, `grep`, `forall`, `update`) to top-level
  `Cli` with `global = true`, splicing into config like `--raw` does. Conclusion: leave it
  per-command. The case for sugar is weak because `--config color.ui=...` already covers the
  rare "global-runtime override" tier, persistent preferences live in the `color.ui`
  config-file, and runtime overrides are dominated by the per-command form. `--raw` earns its
  globalization by frequency (CI scripts toggle it per-invocation); `--color` doesn't clear the
  same bar.

---

## Punch-list (severity-tiered)

### Must fix

*(None in scope of this pass -- no correctness/safety bugs surfaced.)*

### Should fix (functional or hygiene)

1. **`Project::from_core` deep-clones on every `Manifest::projects` access**
   (`python/manifest.rs:302`). Switch to `Arc<core::Project>`-backed storage.
2. **`is_active` duplication** between `Manifest`/`LoadedManifest` Python bindings
   (`python/manifest.rs:573 + 805`). Either store `core::Project` directly or add a
   groups-shortcut on core.
3. **`from_yaml_str_with_imports` non-zero default `import_flags`** (`python/manifest.rs:511`)
   -- easy footgun.
4. **No clippy/fmt CI gate.** 18-warning starting point is tiny; add it now while the cost is
   zero.
5. **Audit `pub` vs `pub(crate)`** across `crates/west-cli/src/commands/*` -- likely 20-30
   items can tighten.
6. **`ColorMode::Auto` on the `Vcs` trait** is dead surface area; CLI layer always resolves to
   Always/Never first. Remove or split trait/CLI color types.
7. **`Vcs::ls_tree_at_ref` lacks file-vs-dir distinction** -- `Option<Vec<TreeEntry>>` would
   prevent caller bugs.

### Nit (style/polish)

8. `value_to_py(Value::String(s))` does `s.clone()` (`python/data.rs:103`). Use `s.as_str()`.
9. `SubmoduleScope::Specific(&[])` no-op contract should be enforced via a default method.
10. `Vcs::update_submodules` positional args (`scope`, `strategy`, `reference`) -- wrap into
    `SubmoduleSpec`.
11. Document the "no `canonicalize`, no symlink follow" invariant at `topdir.rs:1`.
12. ~50% rustdoc coverage on `pub` items -- close the gap.

---

## What I'd do next if you wanted to go deeper

The architectural pass surfaced 7 should-fixes and 5 nits. Items 1 and 2 (Project clone /
is_active duplication) are the single biggest cleanup. None of the findings are blockers --
this is genuinely a well-disciplined codebase that's getting better.

If you wanted the **medium review** (the next ~3h tier sketched in the original plan), it
would focus on:

- Per-implementation read of `vcs/git.rs` (subprocess error paths, refspec construction)
- `commands/update/mod.rs` workflow (worker pool, reporter wiring, error wiring)
- Cross-cutting concurrency lock graph (the `MultiProgress` singleton + `suspend()` is
  non-trivial)
- Performance hot paths for `west update -j 8` on a 100-project workspace
