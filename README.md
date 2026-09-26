# Muse

Muse is a CLI for curating a canonical music archive and publishing selected music to Apple Music. The initial implementation is intentionally conservative: inspection is read-only, while reusable operational metadata is kept under `.muse/`.

## Design principles

- Preserve source material. Destructive operations must be explicit, previewable, conservative, and recoverable.
- Keep expensive work observable. Slow operations identify their current phase and, when possible, show measurable progress and an ETA.
- Keep expensive work reusable and resumable. Checksums and other derived facts are persisted rather than discarded, with source metadata used to identify stale results.
- Make operations idempotent whenever practical. Repeating the same request should continue or verify the intended work rather than duplicate it or force the user to start over.
- Keep at most one current plan for a given object. Users identify the object, not a plan ID: Muse continues or replaces its latest plan, and completed or discarded work leaves no competing current plan.
- Report meaningful efficiency statistics. Elapsed time, phase timings, bytes read, throughput, and cache effectiveness are part of the product rather than debugging trivia.
- Optimize physical work, not merely command count. Avoid unnecessary disk reads and expose both file-based and byte-based measurements.
- Never silently move substantial work into the background. Future detached jobs will be explicit, inspectable, cancellable, and safe to resume.
- Preserve clean interfaces. Progress belongs on stderr; structured results on stdout remain suitable for scripts and pipes.

## Library root

Muse manages one isolated data root:

```text
~/music-vault/
├── master/
├── stopgap/
├── incoming/
├── backlog/
├── slag/
├── trash/
└── .muse/
```

The default can be overridden for tests or alternate installations:

```bash
muse --root /path/to/music-vault status
```

Muse never scans the OS-managed `~/Music/` directory by default.

## Current commands

Muse uses a flat command grammar:

```text
muse COMMAND [PATH ...] [--OPTION ...]
```

Arguments after the command are filesystem scopes, except for `search`, where they are query terms. Run `muse` for styled help or `muse help COMMAND` for details about a command.

### Inspect the vault

These commands are read-only and do not create Muse state.

```bash
muse doctor                 # inspect layout and supporting tools
muse status                 # show top-level repository status
muse stats                  # count files and disk usage
muse stats backlog          # inspect one path beneath the root
muse stats --json           # emit structured output
```

### Find music

```bash
muse search battle theme              # require both terms, case-insensitively
muse search remix -live               # exclude paths containing "live"
muse search "final fantasy" --json    # emit structured output

muse scan /media/old-drive            # quickly find audio absent from the vault
muse scan /media/old-drive --thorough # compare SHA-256 content checksums
muse scan /media/old-drive --all      # list every not-found audio file
muse scan /media/old-drive --pull     # copy relevant directories into backlog
muse scan /media/old-drive --progress always
```

#### Search behavior

- `search` checks file and directory names in every content area.
- Matching is case-insensitive, and every positive term must occur in the name.
- Prefix a term with `-` to exclude results containing it anywhere in the vault-relative path.
- Symlinks are not followed, and internal `.muse` state is not searched.

#### Scan behavior

- `scan` compares an external file or directory with `master`, `stopgap`, `incoming`, `backlog`, and `slag`.
- Relative scan targets are resolved from the current working directory, unlike vault-scoped paths.
- Quick mode uses a case-insensitive filename and size match as a likely copy. `--thorough` ignores names and locations and verifies SHA-256 checksums.
- Only definite audio files are compared and reported. `.mp4` is considered ambiguous because it commonly contains home video; photos and other files are ignored.
- Results include the target's definite-audio extension mix and coverage. Not-found audio is grouped by directory, with a few of each directory's largest files shown. `--all` expands the terminal table; JSON always contains every path.

With `--pull`, Muse prompts for a new relative destination beneath `backlog/`. It copies each whole directory containing not-found audio, including adjacent cue sheets, artwork, booklets, and metadata. Nested selected directories are copied only once. Pull refuses scan errors, file targets, existing destinations, and `--json`; its byte-based progress bar follows the scan command's `--progress` policy.

### Find and manage duplicates

```bash
muse dupes                         # find exact duplicate files
muse dupes backlog                 # limit the search to one vault path
muse dupes --trees backlog         # find maximal duplicate directory trees
muse dupes backlog --all           # show every duplicate group
muse dupes backlog --max-threads 4 # limit concurrent hashing workers
muse dupes --rehash                # bypass the persistent hash cache
muse dupes --progress always
muse dupes --json

muse diff tree-a tree-b            # explain why two trees differ
muse prune                         # discard hashes for files that no longer exist
```

`dupes` does not change music files. It stores SHA-256 hashes and file size/modification-time metadata in `.muse/muse.db`, then reuses hashes for unchanged files. The default report avoids hashing files whose size occurs only once, since they cannot be exact duplicates.

Duplicate reporting rules:

- Human-readable output shows the 10 largest groups by default. Use `--all` or `--json` for all groups.
- Zero-byte files are omitted from the ordinary duplicate-file report; `stats` counts them separately.
- `--trees` includes zero-byte files when comparing trees that contain data, but omits tree groups whose total size is zero.
- `--rehash` bypasses cached hashes.
- `prune` only removes cache entries for missing files. It never changes library content, and discarded hashes can be recomputed.

### Plan and apply library changes

```bash
muse import backlog/album games/album # create a durable import plan
muse import backlog/album --apply     # reverify and move it into master
muse import backlog/album --abort     # discard a ready import plan

muse compact [backlog]                # create the sole compaction plan
muse compact backlog --prefer backlog/archive-a --prefer backlog/archive-b
muse compact backlog --all            # show every move in the new plan
muse compact --apply                  # reverify, confirm, and apply the plan

muse mv old-path new-path             # move content and preserve cached hashes
```

#### Import

The initial `import` implementation accepts only nonempty, audio-only directories beneath `backlog/`. It:

- uses ffprobe and ffmpeg to recognize and fully decode MP3, M4A (AAC or Apple Lossless), Ogg Vorbis/Opus, common PCM WAV, and FLAC;
- checks essential tags and album numbering; and
- runs the native FLAC integrity check when available.

Import does not edit tags, embed artwork, or classify artifacts. A destination is required to create a plan, and only `--apply` moves the planned directory into `master/`.

#### Compaction

Compaction reverifies every planned duplicate tree before mutation, then moves redundant trees into a dated receipt beneath `trash/`, preserving their repository-relative paths. These same-filesystem renames are cheap and recoverable; permanent trash purging is a separate future operation.

Repeat `--prefer PATH` in priority order to favor authoritative subtrees within a duplicate group. Preferences rank copies only within the same managed-area tier, so the built-in `master`, `backlog`, `stopgap`, `incoming`, `slag`, and `trash` policy still takes precedence.

Plans record filenames, file sizes, and high-resolution modification/change timestamps. Applying a current plan therefore does not need to hash content or query the hash cache per file; older pending plans fall back to content-fingerprint verification.

### Preserve non-music artifacts

```bash
muse slag                              # inspect preserved non-music artifacts
muse slag backlog/pc-2007 --apply
muse slag backlog/pc-2007 --thorough  # include artwork and release-adjacent files
```

`slag/` isolates non-music artifacts moved from backlog for later triage. It retains the original backlog-relative path so provenance remains visible.

## Terminal output

Muse treats the terminal as its primary user interface. Human-readable output uses restrained semantic styling: green for success, yellow for warnings, red for errors, and subdued text for secondary information. Color is emitted automatically only for a terminal and is removed from pipes and redirected output.

```bash
muse --color auto status    # default
muse --color always status
muse --color never status
NO_COLOR=1 muse status
```

Machine-readable JSON never contains terminal styling. Potentially slow operations delay their progress display to avoid flicker for quick work, but show it immediately when preflight identifies a large workload. Progress is written to stderr and can be controlled with `--progress auto|always|never`.

## Design and roadmap

See [`DESIGN.md`](DESIGN.md) for proposed behavior, implementation constraints, and incremental plans. It describes future work and is not a list of currently available commands; this README and the CLI help document the implemented interface.

## Installation

During development, install Muse as an editable uv tool and put its launcher in `~/bin`:

```bash
UV_TOOL_BIN_DIR="$HOME/bin" uv tool install --editable "$HOME/src/muse"
muse status
```

Python source edits take effect immediately. Re-run the installation with `--force` after changing project metadata, dependencies, or console entry points:

```bash
UV_TOOL_BIN_DIR="$HOME/bin" uv tool install --editable --force "$HOME/src/muse"
```

For a stable snapshot that does not follow source edits, omit `--editable` and use `--force` to replace the development installation.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run muse --help
```
