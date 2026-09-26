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
- Keep the command vocabulary small. Related operations should share a memorable verb so users can discover precise variants without recalling a large set of similar or specialized verbs.
- Keep positional arguments unambiguous. For operational commands, every positional argument after the command names a filesystem path or path-like repository identifier; operation variants belong in dashed command names, not subcommands.

## Repository areas

The intended repository roles are:

- `master/`: canonical, curated content.
- `backlog/`: the mutable queue of content that still needs attention.
- `incoming/`: newly acquired, not yet classified content.
- `stopgap/`: temporary noncanonical content.
- `slag/`: preserved non-music or release-adjacent artifacts deliberately excluded from `master/`; backlog-relative provenance remains visible.
- `trash/`: recoverable content removed by Muse operations. Trash is not canonical content and must not satisfy imported-content pruning.
- `.muse/`: operational state, current plans, hash cache, locks, and audit records.

If immutable copies of source archives are required, keep them as a separate concern, preferably outside the managed vault. `backlog/` should answer a simple question when inspected with ordinary filesystem tools: “what remains to be processed?”

## Canonical releases and individual tracks

Canonical content is music deliberately retained by the user; it is not limited to complete albums. Muse must support both release-shaped content and independently curated tracks without inventing release metadata or making an incomplete album look complete.

The recommended organization distinguishes the reason a track stands alone:

```text
master/
├── artists/
│   └── Artist/
│       ├── Album/                         # complete release
│       ├── singles/Single Title/          # actual standalone release
│       └── selections/Album/
│           └── 07 - Selected Track.flac   # retained from an incomplete album
└── games/
    └── Game or Series/
        ├── Soundtrack Album/
        └── remixes/
            └── Remixer - Remix Title/
                └── Remix Title.flac
```

An official one-track single remains a release and belongs under the credited artist. A track selected from a larger album should retain its true album identity and original track position, but its `selections/` path must make clear that the vault does not contain the complete release. A remix strongly associated with one game belongs under `games/<Game or Series>/remixes/`; a multi-game mashup may use `games/crossovers/`. When the remixer is the track's stronger primary identity, it belongs under `artists/<Remixer>/remixes/` instead. `misc/` is a fallback only when no useful primary identity exists.

Paths provide one stable browsing axis, not every possible classification. Artist, source work, game, remixer, and release relationships should remain in metadata where the format and catalogue support them rather than being duplicated into increasingly elaborate directory trees.

Even one audio file should normally occupy its own directory as the canonical import unit. This leaves a stable place for artwork, source notes, cue sheets, alternate versions, and later repairs. It does not turn that directory into an album.

Release import and individual-track import require distinct validation profiles:

- A release profile requires coherent album identity and contiguous disc and track numbering for the imported release.
- A selection profile validates tracks independently. It requires at least title and artist, preserves real album metadata when known, and accepts an original position such as track `7/12` without requiring tracks 1–6 to be present.
- A standalone-single profile may use release validation when it is genuinely tagged as a one-track release, normally track `1/1`.
- Reports and durable plans must identify the selected profile and whether content represents a complete release, a standalone single, or a selection.

The initial importer remains deliberately release-shaped: it requires album and album-artist tags and contiguous numbering beginning at one. Consequently, a genuine `1/1` single can be imported today, while an untouched `7/12` album selection is expected to block. Future selection support must add the separate profile rather than weakening release-coherence checks or asking users to falsify tags.

## Command grammar and discoverability

Muse uses a flat command grammar:

```text
muse VERB[-VARIANT] [PATH ...] [--OPTION ...]
```

For operational commands, positional arguments are exclusively filesystem paths or path-like identifiers within the repository. A word after a command must never be ambiguous between a subcommand and an object on the filesystem. Operation variants therefore use dashed names such as `prune-cache` and `prune-imported`, rather than a nested form such as `prune cache`. Meta-commands such as `help`, whose arguments name commands or command families, are the exception.

The set of leading verbs should remain deliberately small. Commands that share a broad user intent and a meaningful semantic core should form a dashed family. This reduces the vocabulary users must recall even when the complete command set becomes large. It should not, however, force unrelated operations or operations with materially different meanings into one command merely to reduce the verb count; variants retain their own behavior, safety rules, and help.

Family-level help supports discovery without making command execution accept arbitrary prefixes. For example, `muse help prune` should explain the shared meaning of pruning and list commands such as `prune-cache` and `prune-imported`. Only complete command names execute, so adding a new variant cannot make an existing abbreviated invocation ambiguous or change its meaning. Shell completion can provide typing convenience independently.

A bare family name need not be executable. If it is, it must have one clear meaning rather than implicitly combining every variant in the family. In particular, bare `prune` must not silently combine cache maintenance with repository-content mutation.

## Command responsibilities

Commands should have distinct lifecycle meanings:

- `muse mv`: rename or relocate content without changing its lifecycle.
- `muse prune-cache`: remove stale, reproducible cache records.
- `muse prune-imported`: remove from a working area content already secured in `master/`.
- `muse slag`: inspect or preserve artifacts excluded from canonical content.
- `muse import`: plan and perform a validated transition into `master/`.
- `muse fix`: inspect and transactionally reconcile existing canonical content with current policy.
- `muse trash`: inspect recoverable removals; dashed variants restore or permanently purge them.

A plain `mv` should not be the normal way to cross from `backlog/` into `master/`. Import validation and journaling would otherwise be easy to bypass accidentally. Moves within an area remain appropriate for correcting organization. Whether exceptional cross-area moves require a force option can be decided when import is implemented.

## Pruning imported content

### Purpose

Imported-content pruning makes the filesystem reflect the remaining import workload. It is not merely a duplicate report.

Conceptual interface:

```bash
muse prune-imported backlog
muse prune-imported backlog/archive-a
muse prune-imported backlog/archive-a --apply
```

The selected path identifies removal candidates, while `master/` is the reference set. Scope must not accidentally limit the reference scan to the selected backlog subtree.

### Rules

- A candidate is removable only if a byte-identical copy exists in `master/` and is reverified immediately before mutation.
- Another backlog copy is not sufficient justification. Until content is in `master/`, every archive containing it should continue to show it as pending.
- “Removal” means moving the candidate to `trash/`, not unlinking it.
- Newly empty directories may be removed.
- Exact matching is safe for automatic action. Similar recordings, alternate encodings, and files differing in tags or artwork require a decision and are not imported-content pruning matches.
- Imported-content pruning must be idempotent and resumable using the same operation-state rules as import.

After importing an album, pruning imported content from all of `backlog/` removes matching copies from every archive and restores the “backlog means work remaining” invariant.

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
inspect -> prune imported content -> classify -> validate -> resolve -> plan -> apply -> verify
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

Automatic imported-content pruning remains byte-exact.

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
- optional post-import pruning across the backlog;
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
8. Prune matching imported copies elsewhere in backlog, moving them into the same trash receipt if that action was in the previewed plan.
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
- Use a repository-level application lock initially. Concurrent planning is acceptable, but concurrent applies are unsafe when post-import pruning can touch shared backlog trees.

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
- groups everything displaced by one import or imported-content pruning operation;
- gives users a practical unit to test, restore, or purge;
- remains an internal plan detail until it becomes a visible trash artifact.

If an expected trash destination already exists in an unrecognized state, block rather than overwrite, merge, or assume equivalence.

### Inspection, restoration, and purge

Intended interface:

```bash
muse trash
muse trash 2025-09-26
muse trash-restore 2025-09-26/143012-import-some-game-soundtrack-a13f
muse trash-purge 2025-09-26/143012-import-some-game-soundtrack-a13f
muse trash-purge 2025-09-26 --apply
muse trash-purge --older-than 7d
muse trash-purge --older-than 30d --apply
```

The dashed restore and purge variants preserve the flat grammar: every positional argument is a path-like selection beneath `trash/`.

Behavior:

- List receipts grouped by local date, including age, originating operation, file count, and size.
- A date selects all receipts from that date.
- A receipt selects exactly one operation's displaced content.
- Purge previews by default and requires `--apply` plus confirmation to unlink content permanently.
- Explicit purge requires at least one date/receipt selection; there is no accidental empty-argument “purge everything.”
- Age-based selection is supported for routine retention policies.
- Recent selections should display a conspicuous warning. A mandatory minimum age is not necessary because users may have already tested an import.
- Restore must refuse collisions and use receipt metadata to reconstruct original locations.

Trash should appear in storage statistics, but imported-content pruning must ignore it as a retained reference. Duplicate reporting may include it only when explicitly requested or when doing so is clearly labeled.

## Slag and import

Slag remains semantically different from trash:

- slag records a deliberate decision that an artifact does not belong in canonical music;
- trash records displacement by an operation and is intended for eventual restoration or permanent purge.

Both preserve provenance. Import should reuse slag's verified movement primitives while improving crash behavior where needed. A source plan's artifact decisions should be durable and content-bound, and import should move only the files explicitly shown in its plan.

## Canonical maintenance and `muse fix`

Import validation establishes that content is acceptable at one point in time, but it cannot keep `master/` permanently correct. Older imports may predate a requirement, policy may evolve, and users may deliberately improve tags, artwork, names, or organization later. Canonical maintenance is therefore a distinct lifecycle operation rather than an extension of import or `mv`.

The conceptual comprehensive interface is:

```bash
muse fix master/games/Some-Album
muse fix master/games/Some-Album --apply
muse fix master/games/Some-Album --abort
```

`fix` inspects the selected canonical content against the current policy, reports compliant facts and deviations, and creates or refreshes a repair plan. It is preview-only unless `--apply` is given. Initial implementation should require an explicit path beneath `master/`; repository-wide audits and batch application can be added only after single-album recovery is trustworthy.

The bare command has one clear meaning: comprehensive reconciliation with current policy. More focused family members may be added as the policies mature, for example:

```bash
muse fix-tags master/artists/Artist/Album
muse fix-artwork master/artists/Artist/Album
muse fix-names master/artists/Artist/Album
muse fix-layout master/artists/Artist/Album
```

These are scopes or profiles of the same repair system, not unrelated mutation commands. They should use the same analysis, policy definitions, operation primitives, and current plan for a target. Switching the requested profile while a plan is still a draft refreshes that plan rather than creating competing plans. Once application starts, its scope and decisions are immutable. Family-level `muse help fix` should explain the comprehensive command and list implemented focused variants.

Likely repair concerns include:

- missing, malformed, inconsistent, or obsolete tags;
- absent, unsuitable, or inconsistently embedded artwork;
- filenames and directory layout that no longer follow convention;
- stale release-adjacent files that should be preserved in slag;
- format or album-coherence findings introduced by newer validation policy;
- cache, catalogue, and published-library references affected by changed bytes or paths.

Policy-driven changes must be distinguishable from subjective editorial choices. Muse may safely normalize a value only when the desired result is deterministic from configured policy and verified source facts. Ambiguous artist credits, editions, dates, artwork selection, and similar decisions remain explicit questions or blockers. A newer convention must not silently rewrite the entire library merely because it can.

A fix plan should record the policy and schema versions used, observed content identities, proposed replacements and moves, user decisions, warnings, and expected before/after states. Re-running inspection after policy or content changes must mark stale conclusions rather than treating an old audit as proof of current compliance. A successful plan becomes audit history, as with import.

Repairs that change file bytes require stronger handling than ordinary moves. Tag or embedded-artwork rewriting should create a temporary sibling, fully validate and decode the replacement, verify the intended metadata, and only then perform a recoverable replacement. The original file must move to the operation's trash receipt rather than be overwritten or unlinked. Renames and layout changes must reject collisions, account for case-insensitive filesystems, preserve hash-cache knowledge where valid, and update dependent catalogue state transactionally or leave the plan blocked. External artwork and other potentially useful source material must remain represented in `master/`, `slag/`, or `trash/`.

`fix` is not a promise to reconstruct damaged audio without a trustworthy source, nor should routine metadata cleanup implicitly transcode recordings. Integrity failures, uncertain mappings, and lossy transformations block by default and explain what source or decision is required. The operation must preserve the same preview, explicit apply, locking, recovery, idempotence, and no-unrepresented-content guarantees as import.

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
  Prune matching imported copies elsewhere in backlog

Warnings:
  Mixed FLAC and MP3 encoding accepted

Status:
  Ready to import
```

JSON output should represent the same source, destination, decisions, blockers, warnings, and operations. Interactive questions require a terminal; noninteractive use should report unresolved decisions and exit without mutating content.

Progress remains on stderr. Plan/report data remains on stdout.

## Strict usable import milestone

The first production-useful import should be a strict readiness gate, not a metadata editor. Work that users can safely perform with existing tools—correcting tags, choosing artwork, renaming files, or resolving unusual artifacts—should remain user work initially. Muse inspects the prepared source, explains every blocker, and permits import only when the album is ready. This keeps the first trustworthy workflow small without weakening its guarantees.

A successful import must not require returning to the backlog source later. Muse must preserve every accepted audio file and every potentially useful adjacent artifact, together with enough provenance to support later tag or artwork improvements against the canonical copy. Automatic tag rewriting, artwork selection and embedding, destination inference, and broad format support are enhancements rather than prerequisites. If critical tags or policy decisions are unresolved, planning blocks and the user prepares the source externally before rerunning the same command.

The initial supported profile should be deliberately narrow in album shape—one album directory with an explicit destination—but must cover the formats likely to occur in older collections: MP3, Ogg Vorbis/Opus, WAV, and FLAC. Format support means inspecting the actual container and codec, decoding the complete stream, reading the format's applicable metadata, and applying explicit format-specific readiness rules; recognizing a filename extension is not support. Initial WAV support may be limited to common PCM variants, and unsupported or unusual codecs must block with a clear explanation. Expanding accepted inputs must not reduce validation.

An album is ready only when Muse can:

- inventory and account for every filesystem entry;
- identify actual containers and codecs rather than trust extensions;
- decode every audio file successfully and perform available codec integrity checks;
- verify essential tags and coherent album, disc, and track numbering;
- classify or block every non-audio artifact and verify supported manifests;
- show a complete durable plan without changing user content;
- reverify the plan immediately before mutation;
- preserve all content in `master/`, `slag/`, or `trash/` as planned;
- verify the final state and retain an audit record.

## Incremental implementation

This design should be delivered in small vertical slices rather than one large change. The sequence toward the strict usable milestone is:

### 1. Minimal import transaction

- Parse and normalize source and master-relative destination forms.
- Enforce one current plan per normalized source.
- Import a clean audio-only directory using an atomic final rename.
- Resume safely before and after the final rename.
- Keep extension-based acceptance explicitly provisional; it is not yet a production readiness check.

### 2. Media inspection and readiness reporting

- Initially support MP3, Ogg Vorbis/Opus, common PCM WAV, and FLAC as actual container/codec combinations rather than extension labels.
- Probe actual containers, codecs, duration, sample rate, channels, and bit depth.
- Decode every track and run format-specific integrity checks where available.
- Read ID3, Vorbis Comment, WAV-applicable, and FLAC metadata as appropriate; report album-level coherence, track/disc numbering, and blockers.
- Make metadata requirements capability-aware without silently waiving them: if a format cannot represent or expose a required fact reliably, report the limitation and require an explicit preparation or policy decision.
- Do not rewrite tags automatically; direct the user to fix blocked sources and re-plan.
- Add progress and reusable inspection results without mixing them into structured stdout.

### 3. Artifact inventory and preservation

- Inventory every non-audio file rather than rejecting the album generically.
- Recognize artwork, checksum manifests, cue sheets, rip logs, playlists, and text metadata.
- Verify supported manifests and references where possible.
- Persist content-bound classifications; unknown or unresolved files block application.
- Preserve useful artifacts with backlog-relative provenance, reusing or strengthening slag movement.
- Preserve external artwork even when embedded artwork exists so later improvements do not require the backlog source.

### 4. Transaction hardening

- Complete draft/ready/applying/blocked/completed state handling.
- Add a repository-level application lock and durable state transitions.
- Preserve or update cached hash paths when content moves.
- Reverify all source facts, destinations, and planned artifact movements before applying.
- Verify every final file and audit record after applying.
- Add deterministic interruption tests around durable writes and filesystem mutations.

### 5. Real-album acceptance

- Select one representative prepared album as an end-to-end acceptance case.
- Require Muse to explain every file, validate every track, report metadata blockers, preserve every artifact, and resume safely.
- Treat successful import of that album without later source reimport as the production-useful milestone.
- Expand formats and album layouts only through additional acceptance cases.

### 6. Imported-content pruning and trash lifecycle

- Add `trash/` as a managed area with safe receipt allocation and path normalization.
- Implement verified, idempotent move-to-trash, restore, and explicit purge primitives.
- Implement `prune-imported` with separate candidate and `master/` reference scopes.
- Plan exact file matches, move them into dated trash receipts, and remove empty directories.
- Include optional previewed global imported-content pruning in import.
- Add age-based purge selection, warnings, and interrupted-work reporting in `status` and `doctor`.

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

- `muse scan DIR` to compare an external prospective source against the vault before adding it to backlog.
