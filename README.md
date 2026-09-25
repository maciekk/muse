# Muse

Muse is a CLI for curating a canonical music archive and publishing selected music to Apple Music. The initial implementation is intentionally read-only.

## Library root

Muse manages one isolated data root:

```text
~/music-vault/
├── master/
├── stopgap/
├── incoming/
├── backlog/
├── duplicates/
└── .muse/
```

The default can be overridden for tests or alternate installations:

```bash
muse --root /path/to/music-vault status
```

Muse never scans the OS-managed `~/Music/` directory by default.

## Current commands

```bash
muse                 # show styled help
muse doctor          # inspect layout and supporting tools
muse status          # show top-level repository status
muse stats           # count files and disk usage
muse stats backlog   # inspect one path beneath the root
muse stats --json
```

`doctor`, `status`, and `stats` do not modify the repository or create Muse state. Planned command groups are visible in `muse --help`, but return an explicit “not implemented” error.

## Terminal output

Muse treats the terminal as its primary user interface. Human-readable output uses restrained semantic styling: green for success, yellow for warnings, red for errors, and subdued text for secondary information. Color is emitted automatically only for a terminal and is removed from pipes and redirected output.

```bash
muse --color auto status    # default
muse --color always status
muse --color never status
NO_COLOR=1 muse status
```

Machine-readable JSON never contains terminal styling.

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
