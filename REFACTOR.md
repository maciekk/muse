# Refactoring plan

This is an implementation handoff from the architectural review on 2026-09-26.
The review inspected source and relevant tests; it did not run tests or change
implementation code. Verify the current baseline before starting. Function names
below are navigation aids; source line numbers will change.

## Priorities and scope

The user's priorities, in order:

1. Avoid duplication. Compose reusable, general-purpose functions and modules.
2. Make code easy to understand for someone who reads this repository infrequently.
3. Prefer concise, professional code without unnecessary explanation or machinery.

Preserve the existing command-oriented organization. Extract concrete shared
operations, give each one an obvious owner, and keep command workflows explicit.
Use the standard library, FFmpeg, Mutagen, argparse, and Rich where already
appropriate. A new framework, plugin registry, generic workflow engine, or class
hierarchy is not a goal. Do not turn every similar loop into an abstraction.

Implement the phases below in order, in independently reviewable changes. Separate
mechanical extraction from changes to safety or recovery behavior. Proposed module
and API names are suggestions; choose names that fit the final responsibilities.

## Before implementation

- Read repository instructions, `README.md`, and relevant parts of `DESIGN.md`.
  README and command help describe current behavior; DESIGN includes future work
  and older proposals. Do not implement the entire roadmap as part of this plan.
- Inspect `git status` and preserve existing user changes.
- Run the existing tests and lint checks and record baseline failures separately.
- Read the tests for each command before moving its code.
- Keep CLI arguments, exit codes, JSON fields, prompts, report ordering, and
  persisted data compatible unless a deliberate behavior change is called out.
- Do not run mutating commands against the user's music vault. Use temporary test
  repositories. Tests that generate media need FFmpeg and ffprobe; native FLAC
  verification is conditional on the tool being present.

## 1. Give hashing and the cache one owner

### Evidence

File hashing is independently implemented in:

- `duplicates.py`: `_sha256`, with change detection and parallel scheduling in
  `_collect_hashes`.
- `tree_diff.py`: `_sha256` and cache handling in `_snapshot`.
- `scanning.py`: `_digest` and `_hash_files`.
- `importing.py`: `_hash`, used by `_inventory`.
- `slag.py`: `_sha256`.

`duplicates.py` and `tree_diff.py` both initialize the same SQLite table and
implement cache queries and writes. `cache.py` and `moves.py` import the private
initializer from `duplicates.py`. Thorough scanning does not use the cache.
The implementations use different file-change checks.

### Implementation

1. Add a small `hashing.py` with a streaming SHA-256 function, optional bytes-read
   callback, and explicit file identity/change verification. A small immutable
   identity record is appropriate if it removes repeated stat tuples.
2. Make `cache.py` own schema initialization, valid-entry lookup, insertion,
   pruning, and path relocation. Keep SQLite dependencies out of command modules
   where practical. Use a small connection-owning object or ordinary functions;
   choose whichever makes transaction boundaries clearest.
3. Migrate duplicates and tree comparison first. Preserve the existing database
   schema during extraction; stronger identity fields require a separate migration
   decision. Keep SQL writes serialized, periodic commits during long jobs,
   longest-file-first scheduling, and accurate physical bytes-read accounting.
4. Migrate scanning, import, and slag to the shared hash primitive. Share parallel
   scheduling only where it reduces real duplication without coupling the helper
   to command-specific report objects.
5. Allow thorough scans to reuse cached vault hashes. Decide explicitly whether
   external-source hashes should also be persisted. This is a behavior change:
   document operational-state writes and preserve the existing report fields.

Fresh verification and cached lookup must remain distinguishable. Import and
verified-copy checks must not silently switch to trusting old cached hashes.
Keep `--rehash` effective. Do not treat a metadata match as an unconditional proof
of unchanged content.

### Acceptance

- All five callers use the shared hashing primitive.
- Schema and cache SQL have one owner; no imports of another command's private
  database initializer remain.
- Existing databases still work, and hashes can be reused across commands.
- Tests cover cache hits, stale metadata, changes during hashing, forced rehash,
  pruning, and path relocation.
- Progress retains cache counts, bytes actually read, and worker information.

## 2. Share inventory and tree fingerprinting

### Evidence

Filesystem traversal is repeated in `repository.py`, `search.py`, `duplicates.py`,
`scanning.py`, `tree_diff.py`, `importing.py`, and `slag.py`.
`duplicates._analyze_trees` and `tree_diff._fingerprint` separately implement the
same recursive digest encoding. Compaction depends on their agreement.

Traversal semantics already differ: `tree_diff._snapshot` has no `os.walk`
error callback and includes directory symlinks in its directory set, while
`_metadata_fingerprint` reports walk errors and filters directory symlinks.

### Implementation

1. Introduce a small streaming inventory API, in `filesystem.py` or a similarly
   focused module. Expose paths, entry kinds, stat information when needed, and
   errors. Avoid materializing an entire tree when a caller only needs a stream.
2. Let callers choose policy: stats counts symlinks and special entries; search
   can report them; import rejects them; other commands may skip them. Traversal
   must not follow symlinked directories implicitly. Handle the starting path as
   deliberately as descendant entries.
3. Preserve command-specific exclusions, sorting, root-directory counting, and
   single-file behavior. In particular, stats and content searches do not have
   identical area/exclusion policies. Reuse mechanics without erasing policy.
4. Extract a pure tree fingerprint function into `trees.py`. It should consume
   relative directories and file identities/sizes and return per-directory
   fingerprints and totals. Duplicate detection needs all directory results;
   tree comparison usually needs the root result.
5. Feed either content digests or metadata-derived identities into that function.
   Preserve the existing digest encoding, including empty-directory participation,
   so persisted compaction plans and existing expectations remain meaningful.
6. Migrate callers incrementally. Ensure traversal failures cannot turn an
   incomplete inventory into a successful destructive-operation verification.

Do not over-generalize traversal with many boolean options. A small entry stream
plus explicit caller-side filtering is easier to understand.

### Acceptance

- Tree digest construction has one implementation.
- Discovery and later verification agree on fingerprints for the same tree.
- Tests cover empty directories/files, unreadable subtrees, symlinks, special
  entries, excluded state, and single-file inputs.
- Ordinary duplicate reports still omit zero-byte files; tree reports preserve
  their existing mixed-tree and all-zero-tree behavior.
- Preserve the test that a shared retained tree is verified only once. Existing
  tests patch `tree_diff.os.walk`; update patch locations after extraction while
  retaining the behavioral assertion.

## 3. Consolidate movement and durable-state primitives

This phase includes deliberate reliability improvements. Keep it distinct from
the earlier extractions and implement it in small slices.

### Evidence

- `moves.move` renames content, handles associated slag paths, and updates cached
  paths. Its exception rollback is not a durable recovery protocol across process
  termination, and the filesystem and database do not share an atomic transaction.
- `importing.apply_plan` renames directly, persists an applying state, and can
  recover after the source has moved. It does not relocate cached hash paths.
- `compaction.apply_plan` renames directly and allocates a receipt during apply.
  After interruption midway, another apply attempts to verify missing source
  trees. Plan and receipt JSON are written directly.
- `slag.apply` copies directly to the final destination before verification and
  source deletion. An interrupted copy can leave an incomplete final destination
  that blocks retry.
- Path containment, symlink checks, destination interpretation, and collision
  checks are distributed across these modules and CLI helpers.

### Implementation

1. Extract path-validation primitives. Normalize/reject traversal appropriately,
   inspect symlink ancestors, and treat dangling symlinks as occupied destinations.
   Keep user-facing destination interpretation in command code. An exact move
   primitive must not secretly append a basename when a destination is a directory.
2. Preserve supported symlinked vault roots: there is an existing compaction test
   for this. Distinguish a selected root alias from symlinks within the managed
   content. Avoid changing this policy accidentally through `resolve()`.
3. Add one atomic JSON writer using a unique temporary sibling, flush/fsync,
   replacement, and appropriate directory durability. Use it for plans and
   receipts. Clean up owned temporary files on ordinary failures.
4. Extract exact-destination rename and verified staged-copy primitives. Stage
   copies in the destination filesystem, verify before publication, and remove
   sources only after the destination is verified. Never overwrite unexpected
   user content. Account explicitly for same-filesystem and cross-filesystem cases.
5. Add a repository mutation lock shared by mutating entry points. Decide lock
   scope explicitly: import planning currently performs tag/artwork writes, so
   locking only functions named `apply` is insufficient. Avoid nested lock owners.
6. Keep operation workflows in import and compaction. Share only filesystem-state
   classification and persistence mechanics. Classify expected source/destination
   states as pending, completed, partially copied, or conflicting using verified
   identities, rather than trusting a step counter.
7. Persist compaction's receipt location and application state before the first
   content move. Resume into the same receipt after interruption, even across
   midnight. Reverify retained content before remaining removals and recognize
   verified completed moves at their receipt destinations.
8. Relocate cached paths through the cache API for all relevant moves, including
   companion slag moves. Make cache reconciliation repeatable after interruption.
   Cache maintenance must never justify discarding or overwriting user content.
9. Version changed persisted formats. Continue loading legacy ready plans where
   safe; if a legacy interrupted state cannot be recovered unambiguously, refuse
   with an actionable explanation. Never guess or silently replace active work.

Do not use the current `moves.move` wholesale as the shared primitive: it combines
CLI-like destination semantics, backlog/slag policy, filesystem work, and cache
updates. Extract its general mechanics, then compose them in command workflows.

Preserve compaction's efficient verification for current plans: new plans use
names, sizes, and timestamps; legacy plans fall back to content fingerprints.
Do not introduce per-file hashing or cache queries on every new-plan apply.
Design the expected identity for completed moves carefully because moving files
can change metadata; original metadata cannot always be reused blindly.

### Acceptance

- Add deterministic failure injection around durable writes, renames, copy
  publication, source removal, cache reconciliation, and audit finalization.
- Resume import and a multi-operation compaction after partial completion.
- Retry interrupted slag copying without accepting a partial final file.
- Reject changed source/retained content, conflicting destinations, unsafe paths,
  and concurrent mutation.
- Verify cache reuse after successful moves and recovery after a move occurred
  but cache reconciliation did not.
- No user content disappears outside the intended master/slag/trash disposition.
- Repeated application cannot duplicate moves or allocate competing receipts.

## 4. Split the CLI by command family

### Evidence

`cli.py` is roughly 1,900 lines, combining parsing, dispatch, workflow glue,
prompts, rendering, and progress classes. `_slag` repeats inventory/extraction
rendering. Scan, pull, duplicates, and compaction repeat progress lifecycle work.

### Implementation

1. Keep `muse.cli:main` stable as the installed entry point. Keep argument parsing
   and dispatch easy to locate.
2. Move handlers and their local rendering helpers into a few modules under
   `commands/`, grouped by command family. Prefer a small number of cohesive files
   over separate files for every tiny function. Shared command modules must not
   import `cli.py`, which would create circular dependencies.
3. Share Rich progress lifecycle and terminal policy using `reporting.py` or a
   focused `progress.py`. Keep domain event types and descriptions explicit;
   domain modules must remain independent of Rich and argparse.
4. Extract slag's repeated grouping/table rendering. Use concrete report types
   where the current CLI uses `Any` and the actual type is known.
5. Preserve help formatting, `muse help COMMAND`, negated search terms, color
   handling, JSON output, prompts, and cleanup of progress displays on exceptions.
   Keep progress on stderr. Any stdout/stderr bug correction should be explicit
   and tested, rather than an accidental consequence of moving code.

The current progress displays differ in their delay behavior. Establish the
intended common policy from README, then make any change to delays explicit.
Do not copy backend worker-count calculations into CLI presentation; use backend
events or a shared calculation when the number must be displayed.

### Acceptance

- CLI tests still cover parser behavior, help, exit codes, prompts, JSON, styling,
  and progress streams.
- Update monkeypatch targets when implementation moves; do not retain redundant
  production wrappers solely to preserve tests of private implementation details.
- An occasional reader can locate a command's workflow and output together.

## 5. Use structured import findings and share ID3 repairs

### Evidence

`cli._make_import_plan_with_confirmation` recognizes an overridable finding using
`blocker.startswith("inconsistent album artist tags:")`. Behavior therefore
depends on English wording. `media_fixup.repair_tags` duplicates MP3 and WAV ID3
defaults for album artist, track, and disc.

### Implementation

1. Introduce a small immutable finding record with a stable code, display message,
   and optional relative path. Add more fields only for actual consumers.
2. Make import validation expose findings. Keep familiar error strings and
   compatibility views such as `blockers` where needed by current callers.
3. Select the album-artist confirmation by finding code. Accepting it must not
   suppress unrelated blockers. Preserve noninteractive and JSON behavior.
4. Extract shared ID3 default-setting logic for MP3/WAV. Keep loading and saving
   explicit for each container. Share the repeated format loader or permission
   restoration only if it improves clarity; avoid a codec plugin framework.
5. Use `dataclasses.replace` for import state transitions instead of rebuilding
   objects from `__dict__`, as a small adjacent cleanup.

Preserve current import behavior: planning intentionally performs deterministic
tag/artwork fixups, individual selections preserve original album positions, and
release directories receive release-level validation. Moving fixups behind a new
apply command would be a separate product change, not this refactor.

### Acceptance

- Wording changes do not break album-artist acceptance.
- Unrelated validation failures still block import.
- Existing supported-format, numbering, artwork, and permission behavior remains
  covered, including both MP3 and WAV ID3 handling.

## Validation and completion

Run focused tests after each meaningful change, then the complete checks before
declaring the work complete:

```sh
uv run pytest
uv run ruff check .
uv run muse --help
```

Use the existing environment if available; dependency installation may require
network access. Record missing-tool skips and baseline failures honestly. Do not
add tests that merely mirror implementation structure; test shared guarantees and
observable command behavior. For the mutation phase, interruption tests are
essential.

Check persistent-format compatibility and existing JSON contracts explicitly.
Update README for any intentional user-visible behavior changes. Update DESIGN
only where necessary to describe the implemented recovery model accurately.

At the end of each phase, record completed work, checks run, and remaining work
here or in the implementation handoff. If interrupted, leave a precise status so
the next session can continue without repeating completed extraction.

## Suggested next-session instruction

> Implement REFACTOR.md incrementally, following its priority order. Start by
> checking repository instructions and the test baseline. Complete each phase
> with appropriate tests before proceeding. Preserve documented behavior and
> persisted data compatibility, and keep recovery improvements separate from
> mechanical extraction. Use small, explicit helpers and report any necessary
> deviations from the plan. Do not operate on my real music vault.

## Phase status (2026-09-26)

Phase 1 is complete. `hashing.py` owns fresh streaming SHA-256 and file-change
checks. `cache.py` owns the unchanged SQLite schema, lookup, insertion, pruning,
and path relocation. Duplicate reports, tree comparisons, scans, import, and slag
use the shared hash function. Thorough scans cache vault hashes only; external
sources and import/verified-copy checks are always read freshly. Existing report
fields remain, with scan cache and physical-read counters added. README documents
the scan's operational-state writes.

Baseline: 91 tests passed and Ruff lint passed. Phase 1 checks: full test suite,
Ruff lint, and `muse --help` passed. The changed Python files pass Ruff formatting;
repository-wide formatting has preexisting failures outside this phase. Phase 2
has not started.
