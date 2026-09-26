# Muse architecture

This document maps the implementation as it exists today. [README.md](README.md)
describes the user interface; [DESIGN.md](DESIGN.md) records proposed behavior and
future work. The code and tests are authoritative when they differ from this map.

## Boundaries

`muse.cli:main` is the installed entry point. `cli.py` defines arguments and
dispatches to handlers in `src/muse/commands/`. The handlers own command workflows,
prompts, and rendering. `commands/progress.py` and `reporting.py` own Rich progress
and terminal policy; progress goes to stderr and JSON results to stdout. Domain
modules return reports or emit progress events without depending on argparse or
Rich.

| Responsibility | Owner |
| --- | --- |
| Vault layout and path resolution | `config.py`, `repository.py` |
| Non-following filesystem inventory | `filesystem.py` |
| Fresh SHA-256 and file identity | `hashing.py` |
| SQLite hash cache schema and operations | `cache.py` |
| Recursive tree fingerprint encoding | `trees.py` |
| Duplicate and tree comparison workflows | `duplicates.py`, `tree_diff.py` |
| External scan and backlog pull | `scanning.py` |
| Media inspection and deterministic tag/artwork repair | `media.py`, `media_fixup.py` |
| Import, compaction, move, and artifact workflows | `importing.py`, `compaction.py`, `moves.py`, `slag.py` |
| Shared mutation mechanics | `mutation.py` |

The common modules provide mechanics; each workflow retains its own selection,
exclusion, destination, and recovery policy. `filesystem.walk` reports errors and
does not follow descendant directory symlinks. `trees.fingerprints` includes empty
directories in the digest and is shared by duplicate discovery and verification.

## Derived data

`.muse/muse.db` stores path, size, modification time, and SHA-256 for reusable
vault hashes. `cache.py` owns its schema, lookup, pruning, and path relocation.
Matching metadata permits cache reuse for duplicate reports, tree comparison,
and thorough vault scanning; it is not proof of unchanged bytes. Import
verification, verified copies, and external scan targets read content freshly.
`--rehash` forces duplicate analysis to read files again. Mutations reconcile
cached paths after content moves, and can repeat reconciliation during recovery.

## Content mutations and recovery

`mutation.py` provides a repository mutation lock, lexical path and endpoint
checks, atomic JSON writes, exact same-filesystem renames, verified staged copies,
and audit archiving. It rejects occupied destinations, including dangling
symlinks. Import planning also holds the lock because it may repair tags or embed
artwork in backlog files. The import and compaction modules own their durable
plans and decide how to resume from verified filesystem state.

- Import keeps one current plan under `.muse/imports/`. Planning may perform
  deterministic media repairs. Apply reverifies content, records the applying
  state, renames into `master/`, verifies the destination, reconciles cached paths,
  and archives the plan under `.muse/audit/`. Retrying after a rename completes
  these remaining steps.
- Compaction keeps a current plan at `.muse/compact-plan.json`. Current plans use
  metadata tree fingerprints for application checks; legacy plans can use content
  fingerprints. Before moving a duplicate tree, apply records a dated `trash/`
  receipt and its moving state. A retry verifies retained and moved trees and
  continues into the same receipt. Completed plans move to `.muse/audit/`.
- `mv` records its intended renames in `.muse/move.json` before moving content or
  companion slag paths. A retry completes those renames and cache reconciliation.
  Slag copies are verified before publication, then their sources are removed.
- Scan pull stages selected source directories beside the destination in
  `backlog/` and publishes the completed copy by renaming the staging directory.

The filesystem and SQLite cache do not share a transaction. Recovery therefore
checks actual source and destination state and repeats cache reconciliation;
unexpected content blocks a mutation rather than being overwritten. JSON plans,
receipts, and journals are persistent formats: change their schema deliberately
and preserve supported older plans when altering them.

## Verification

`tests/` exercises reports, CLI contracts, cache reuse, and interruption recovery
using temporary repositories. Run `uv run pytest`, `uv run ruff check .`, and
`uv run muse --help` for the project checks. Tests that generate media require
FFmpeg and ffprobe; native FLAC checks are conditional on the tool being present.
