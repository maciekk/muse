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
└── .muse/
```

The default can be overridden for tests or alternate installations:

```bash
muse --root /path/to/music-vault status
```

Muse never scans the OS-managed `~/Music/` directory by default.

## Current commands

```bash
muse                       # show styled help
muse help dupes            # show dupes arguments and options
muse doctor                # inspect layout and supporting tools
muse status                # show top-level repository status
muse stats                 # count files and disk usage
muse stats backlog         # inspect one path beneath the root
muse stats --json
muse dupes                 # find exact duplicates across the library
muse dupes backlog         # inspect one path beneath the root
muse dupes --rehash        # bypass the persistent hash cache
muse dupes --trees backlog  # report maximal exact duplicate directory trees
muse prune                   # discard hashes for files that no longer exist
muse diff tree-a tree-b      # explain why two trees differ
muse compact [backlog]        # create the sole pending compaction plan
muse compact --apply          # reverify, confirm, then apply that plan
muse mv old-path new-path     # move or rename content; preserve cached hashes
muse slag                      # inspect preserved non-music artifacts
muse slag backlog/pc-2007 --apply
muse slag backlog/pc-2007 --thorough  # include artwork and release-adjacent files
muse dupes --progress always
muse dupes --json
```

Muse uses a flat command grammar: `muse COMMAND [PATH ...] [--OPTION ...]`. Positional arguments after the command are always filesystem scopes; operations use distinct command names or dashed options. `doctor`, `status`, and `stats` do not modify the repository or create Muse state. `dupes` never changes music files, but stores SHA-256 hashes and file size/modification-time metadata in `.muse/muse.db`; unchanged files reuse their cached hashes on later runs. Files with a size that occurs only once cannot be exact duplicates and are not hashed by the default report. `--trees` hashes every file because whole-tree equality requires complete coverage. Use `--rehash` to bypass cached hashes. `prune` immediately removes cache entries whose files no longer exist; it never changes library content, and an accidentally discarded hash can be recomputed.

## Terminal output

Muse treats the terminal as its primary user interface. Human-readable output uses restrained semantic styling: green for success, yellow for warnings, red for errors, and subdued text for secondary information. Color is emitted automatically only for a terminal and is removed from pipes and redirected output.

```bash
muse --color auto status    # default
muse --color always status
muse --color never status
NO_COLOR=1 muse status
```

Machine-readable JSON never contains terminal styling. Potentially slow operations delay their progress display to avoid flicker for quick work, but show it immediately when preflight identifies a large workload. Progress is written to stderr and can be controlled with `--progress auto|always|never`.

`slag/` isolates non-music artifacts moved from backlog for later triage. It retains the original backlog-relative path so provenance remains visible.

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
