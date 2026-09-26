# Muse design

This document records product and implementation design that is not necessarily implemented yet. `README.md` describes the commands that exist today. When the two differ, the README and command help describe current behavior; this document describes the intended direction.

## Principles

- Preserve source material. Destructive operations must be explicit, previewable, conservative, and recoverable.
- Make operations idempotent whenever practical. Repeating the same request should continue or verify the intended work rather than duplicate it or force the user to start over.
- Keep at most one current plan for a given object. Users identify the object, not a plan ID: Muse continues or updates its latest plan, and completed or discarded work leaves no competing current plan.
- Make interrupted work resumable. Durable state must be written before mutating library content, and recovery must derive progress from verified filesystem state rather than trusting only a step counter.
- Keep expensive work observable, reusable, and resumable. Persist derived facts such as hashes, while using source metadata to recognize stale results.
- Report meaningful efficiency statistics: elapsed time, phase timings, bytes read, throughput, and cache effectiveness.
- Optimize physical work, not merely command count. Avoid unnecessary disk reads and copies.
- Never silently move substantial work into the background. Detached work must be explicit, inspectable, cancellable, and resumable.
- Keep clean interfaces. Progress belongs on stderr; structured results on stdout must remain usable by scripts.

## Repository areas

The intended repository roles are:

- `master/`: canonical, curated content.
- `backlog/`: the mutable queue of content that still needs attention.
- `incoming/`: newly acquired, not yet classified content.
- `stopgap/`: temporary noncanonical content.
- `slag/`: preserved non-music or release-adjacent artifacts deliberately excluded from `master/`; backlog-relative provenance remains visible.
- `trash/`: recoverable content removed by Muse operations. Trash is not canonical content and must not satisfy reconciliation.
- `.muse/`: operational state, current plans, hash cache, locks, and audit records.

If immutable copies of source archives are required, keep them as a separate concern, preferably outside the managed vault. `backlog/` should answer a simple question when inspected with ordinary filesystem tools: “what remains to be processed?”

## Command responsibilities

Commands should have distinct lifecycle meanings:

- `muse mv`: rename or relocate content without changing its lifecycle.
- `muse reconcile`: remove from a working area content already secured in `master/`.
- `muse slag`: inspect or preserve artifacts excluded from canonical content.
- `muse import`: plan and perform a validated transition into `master/`.
- `muse trash`: inspect, restore, or permanently purge recoverable removals.

A plain `mv` should not be the normal way to cross from `backlog/` into `master/`. Import validation and journaling would otherwise be easy to bypass accidentally. Moves within an area remain appropriate for correcting organization. Whether exceptional cross-area moves require a force option can be decided when import is implemented.

## Reconciliation

### Purpose

Reconciliation makes the filesystem reflect the remaining import workload. It is not merely a duplicate report.

Conceptual interface:

```bash
muse reconcile backlog
muse reconcile backlog/archive-a
muse reconcile backlog/archive-a --apply
```

The selected path identifies removal candidates, while `master/` is the reference set. Scope must not accidentally limit the reference scan to the selected backlog subtree.

### Rules

- A candidate is removable only if a byte-identical copy exists in `master/` and is reverified immediately before mutation.
- Another backlog copy is not sufficient justification. Until content is in `master/`, every archive containing it should continue to show it as pending.
- “Removal” means moving the candidate to `trash/`, not unlinking it.
- Newly empty directories may be removed.
- Exact matching is safe for automatic action. Similar recordings, alternate encodings, and files differing in tags or artwork require a decision and are not reconciliation matches.
- Reconciliation must be idempotent and resumable using the same operation-state rules as import.

After importing an album, reconciling all of `backlog/` removes matching copies from every archive and restores the “backlog means work remaining” invariant.

## Import

### Interface and path semantics

```bash
muse import SOURCE [DESTINATION]
muse import SOURCE --apply
muse import SOURCE --abort
```

The initial implementation should accept sources beneath `backlog/`. Supporting `incoming/` or `stopgap/` later must be an explicit policy decision.

`DESTINATION` is always interpreted beneath `master/`. Both concise and explicit spellings are accepted:

```bash
muse import backlog/some-game-soundtrack games/
muse import backlog/some-game-soundtrack master/games/
```

Both resolve beneath `<root>/master/games/`. Absolute destinations and `..` traversal are rejected. Prefixes for other managed areas are rejected. The normalized explicit `master/...` path is stored in the plan and shown in output.

If the destination names an existing directory, the source directory name is appended, matching normal move behavior. A nonexistent destination denotes the exact final path. This rule must be made conspicuous in previews and covered by tests because destination ambiguity is dangerous.

When no destination is supplied:

- an existing source plan is implied;
- a new plan may propose a destination from tags and organization policy, but an unresolved destination blocks application.

### Idempotent planning

`muse import SOURCE DESTINATION` is a planning operation. It may ask many questions over multiple invocations, but it must not create multiple competing plans.

For a normalized source there is either no current plan or one current plan. Repeating the command:

- creates the plan if absent;
- continues unanswered classification and policy questions;
- refreshes analysis and identifies stale source facts;
- updates the same draft plan when the destination or decisions change;
- displays the existing plan when there is nothing new to answer.

Each answer should be persisted as it is made so an interrupted planning session loses as little work as possible. Decisions tied to a file should include its content identity; a changed file must not silently inherit a stale decision.

Users never type or manage plan IDs. Muse may use internal identifiers for storage, locking, trash receipts, and auditing.

### Plan states

A useful state model is:

```text
draft -> ready -> applying -> completed
                    |
                    +-> blocked
```

- `draft`: unresolved questions, destination, or blocking checks remain.
- `ready`: all required decisions have been captured.
- `applying`: at least one content mutation may have occurred; material decisions and destination are immutable.
- `blocked`: observed filesystem state matches neither the pending nor completed form of an operation.
- `completed`: the desired final state has been verified and the current plan can become an audit record.

`--abort` may discard a `draft` or `ready` plan. Once applying has begun, abort would imply rollback and should not be offered until rollback has a deliberate design. Interrupted application remains available for resumption.

Completed plans move to audit history and cease to be current plans. Audit records may have internal transaction identifiers without exposing plan management to the user.

### Planning and validation phases

Import contains preparation internally rather than exposing a separate `prep` command. A separate preparation command would have to be revalidated by import anyway and could misleadingly imply that old results are still safe.

Conceptual phases are:

```text
inspect -> reconcile -> classify -> validate -> resolve -> plan -> apply -> verify
```

Planning should cover the following concerns.

#### Artifact classification

Every non-audio file should be visible and classified as:

- obvious slag;
- release-adjacent material requiring policy or review;
- unknown and requiring an explicit decision.

Release-adjacent examples include artwork, cue sheets, rip logs, checksums, playlists, liner notes, and text metadata. Files skipped by the conservative default behavior of `muse slag` must not become invisible during import.

Before moving release-adjacent files to slag, use them where possible:

- verify checksum manifests;
- check cue and playlist references;
- determine whether external artwork is represented adequately elsewhere;
- retain provenance such as rip logs even when it is excluded from `master/`.

Unknown files block application until explicitly classified.

#### Audio integrity

Blocking checks should include:

- successful decoding;
- zero-length or truncated files;
- agreement between extension and actual container;
- supported format policy;
- codec-level integrity checks such as FLAC checksums when available.

#### Album coherence

Report at least:

- inconsistent album or album-artist tags;
- absent, duplicate, or implausible track and disc numbers;
- gaps in track numbering;
- suspiciously mixed codecs, sample rates, or bit depths;
- inconsistent years or release identifiers.

Initially Muse can report these rather than rewrite tags. Some conditions block application; quality oddities can be explicitly accepted warnings.

#### Non-exact and semantic duplicates

Warn, but never automatically remove, likely alternate representations:

- lossless and lossy versions of the same recording;
- files differing only in tags or embedded artwork;
- alternate bitrates or encodings;
- similar titles with duplicate track numbers.

Automatic reconciliation remains byte-exact.

#### Filesystem hygiene

Check for:

- symlinks and special files;
- unreadable files;
- hidden and temporary files;
- case-insensitive filename collisions;
- unsafe or problematic names;
- empty directories;
- destination collisions.

#### Destination review

Show the fully normalized destination and whether the source basename will be appended. Destination conflicts block application rather than overwrite content.

### Planned actions

A ready plan should summarize, at minimum:

- source and normalized destination;
- exact master duplicates to move to trash;
- artifacts to move to slag;
- accepted audio files to move into master;
- accepted warnings and explicit classifications;
- empty directories to clean up;
- optional post-import backlog reconciliation;
- expected hashes and filesystem preconditions for every content operation.

Planning may write only operational state beneath `.muse/`. `--apply` is required before changing user content.

### Application order

A conservative order is:

1. Acquire the repository/import lock.
2. Persist and durably flush the immutable applying plan and its trash receipt.
3. Revalidate all inputs, retained master copies, and destination preconditions.
4. Move exact source duplicates into trash.
5. Move classified artifacts into slag using verified operations.
6. Remove empty source directories.
7. Atomically rename the accepted remainder into `master/` when on the same filesystem.
8. Reconcile matching copies elsewhere in backlog, moving them into the same trash receipt if that action was in the previewed plan.
9. Verify the complete desired state.
10. Write the audit record and retire the current plan.

The exact order may evolve, but no content should be discarded and each committed step must be recognizable on resume.

## Durable plans and recovery

### Plan identity and lookup

The current import plan can be stored beneath an internal key derived from the normalized repository-relative source path, for example under `.muse/imports/`. The storage name is not a user interface.

Plan lookup must happen before requiring that `SOURCE` currently exist. During an interrupted application, the source may already be partly emptied or atomically moved into `master/`; `muse import SOURCE --apply` must still locate and resume its plan.

A plan should record:

- normalized original source;
- normalized final destination;
- source facts and content hashes;
- user decisions and accepted warnings;
- declarative operations and their expected before/after states;
- trash receipt path;
- state and timestamps;
- schema version.

Plan writes must use a temporary file, flush, and atomic rename. If SQLite is used for operation state, filesystem outcomes must still be verified because the database and filesystem cannot share one atomic transaction.

### Operation recovery model

Do not rely solely on a persisted “current step.” On each application or resume, classify each operation from verified filesystem state:

- **Pending:** source exists with the expected identity; destination is absent.
- **Completed:** source is absent; destination exists with the expected identity.
- **Partially completed:** both exist with the expected identity, as may occur after verified copying but before source removal.
- **Conflict:** both, neither, or either path exists in an unexpected form.

Completed operations are skipped. Pending operations run. Partial operations finish only after verification. Conflicts block the plan for review. Running `--apply` after successful completion must not duplicate work.

### Mutation rules

- Never overwrite an unexpected destination.
- Prefer atomic rename within the vault filesystem.
- When copying is unavoidable, copy to a temporary sibling, flush and verify it, atomically rename it into place, and only then remove the source.
- Before moving a duplicate out of backlog, reverify the canonical master copy.
- Empty-directory removal is repeatable and does not require trash.
- Temporary implementation files may be unlinked; user content must remain represented in master, slag, or trash.
- Use a repository-level application lock initially. Concurrent planning is acceptable, but concurrent applies are unsafe when post-import reconciliation can touch shared backlog trees.

`muse status` should expose interrupted or blocked work and print the source-based command needed to resume it.

## Trash

### Purpose and layout

Trash turns removal into a recoverable relocation. It is a safeguard, not a canonical content source and not a substitute for backups.

The local calendar date is the first path component so recent mistakes and age-based cleanup are easy to find with ordinary tools:

```text
trash/
└── 2025-09-26/
    └── 143012-import-some-game-soundtrack-a13f/
        ├── backlog/
        │   └── some-game-soundtrack/
        │       └── duplicate-track.flac
        └── receipt.json
```

Below the receipt, preserve the complete original repository-relative hierarchy, including the original managed-area name. The date and receipt are allocated when application begins and stay fixed across resumes, including runs that cross midnight. Use local date for the human “yesterday” workflow and store an unambiguous timestamp with timezone in receipt metadata.

The receipt level:

- prevents collisions when the same original path is removed more than once;
- groups everything displaced by one import or reconciliation;
- gives users a practical unit to test, restore, or purge;
- remains an internal plan detail until it becomes a visible trash artifact.

If an expected trash destination already exists in an unrecognized state, block rather than overwrite, merge, or assume equivalence.

### Inspection, restoration, and purge

Intended interface:

```bash
muse trash
muse trash 2025-09-26
muse trash restore 2025-09-26/143012-import-some-game-soundtrack-a13f
muse trash purge 2025-09-26/143012-import-some-game-soundtrack-a13f
muse trash purge 2025-09-26 --apply
muse trash purge --older-than 7d
muse trash purge --older-than 30d --apply
```

Detailed grammar should be reconciled with Muse's current flat command grammar during implementation, but these user tasks must remain straightforward.

Behavior:

- List receipts grouped by local date, including age, originating operation, file count, and size.
- A date selects all receipts from that date.
- A receipt selects exactly one operation's displaced content.
- Purge previews by default and requires `--apply` plus confirmation to unlink content permanently.
- Explicit purge requires at least one date/receipt selection; there is no accidental empty-argument “purge everything.”
- Age-based selection is supported for routine retention policies.
- Recent selections should display a conspicuous warning. A mandatory minimum age is not necessary because users may have already tested an import.
- Restore must refuse collisions and use receipt metadata to reconstruct original locations.

Trash should appear in storage statistics, but normal reconciliation must ignore it as a retained reference. Duplicate reporting may include it only when explicitly requested or when doing so is clearly labeled.

## Slag and import

Slag remains semantically different from trash:

- slag records a deliberate decision that an artifact does not belong in canonical music;
- trash records displacement by an operation and is intended for eventual restoration or permanent purge.

Both preserve provenance. Import should reuse slag's verified movement primitives while improving crash behavior where needed. A source plan's artifact decisions should be durable and content-bound, and import should move only the files explicitly shown in its plan.

## User-visible reporting

A planning summary should be understandable without reading plan files:

```text
Import source:
  backlog/pc-2007/Artist/Album

Destination:
  master/artists/Artist/Album

Actions:
  Move 3 exact master duplicates to trash
  Move 4 artifacts to slag
  Import 12 audio files
  Remove 2 empty directories
  Reconcile matching copies elsewhere in backlog

Warnings:
  Mixed FLAC and MP3 encoding accepted

Status:
  Ready to import
```

JSON output should represent the same source, destination, decisions, blockers, warnings, and operations. Interactive questions require a terminal; noninteractive use should report unresolved decisions and exit without mutating content.

Progress remains on stderr. Plan/report data remains on stdout.

## Incremental implementation

This design should be delivered in small vertical slices rather than one large change.

### 1. Trash foundation

- Add `trash/` as a managed area.
- Define safe receipt allocation and path normalization.
- Add inventory/status reporting.
- Implement verified, idempotent move-to-trash primitives.
- Test collisions and interrupted move recovery.

### 2. Reconciliation

- Separate candidate scope from the `master/` reference scope.
- Plan exact file matches, not only maximal duplicate trees.
- Preview and apply moves into dated trash receipts.
- Remove empty directories.
- Make repeated and interrupted application safe.

### 3. Minimal import transaction

- Parse and normalize source and master-relative destination forms.
- Enforce one current plan per normalized source.
- Implement draft/ready/applying/blocked/completed states.
- Import a clean audio-only directory using an atomic final rename.
- Resume safely before and after the final rename.

### 4. Artifact decisions and slag integration

- Inventory every non-audio file.
- Persist obvious, adjacent, and unknown classifications.
- Reuse or strengthen verified slag movement.
- Block on unresolved files.

### 5. Integrity and coherence checks

- Add decoder/container validation.
- Add album-level tag and track checks.
- Verify checksums, cue sheets, and playlists where possible.
- Add warnings for likely non-exact duplicates.

### 6. Post-import reconciliation and trash lifecycle

- Include previewed global backlog reconciliation in import.
- Add restore and explicit purge.
- Add age-based purge selection and recent-item warnings.
- Surface interrupted work in `muse status` and `muse doctor` where appropriate.

## Testing requirements

Each slice should include tests for:

- running the same planning command repeatedly;
- running `--apply` repeatedly after success;
- interruption before and after every filesystem mutation;
- recovery based on actual filesystem state rather than only journal state;
- changed source files and stale decisions;
- destination and trash collisions;
- path traversal, symlinks, and managed-area boundary checks;
- plans resumed after the original source directory has moved;
- trash receipts resumed after local midnight;
- exact duplicates retained in master before backlog copies move to trash;
- no user content becoming unrepresented by master, slag, or trash;
- human and JSON output remaining consistent.

Tests should use explicit failpoints around durable writes, renames, verified copies, and source removal so power-loss recovery is exercised deterministically.

## Other future work

Previously identified work remains useful but is separate from import:

- `muse prune` to preview and remove stale hash-cache entries after content moves or source retirement.
- `muse scan DIR` to compare an external prospective source against the vault before adding it to backlog.
