# Muse

Muse is a CLI for curating a canonical music archive and publishing selected music to Apple Music. The initial implementation is intentionally conservative: inspection is read-only, while reusable operational metadata is kept under `.muse/`.

The music files are the durable product: canonical audio, intrinsic metadata, and primary artwork should remain self-contained and usable without Muse. Muse records workflow, provenance, integrity, and publication relationships without becoming a competing metadata catalogue. Apple is a replaceable listening projection and owns live playback behaviour. See [`DESIGN.md`](DESIGN.md) for the planned metadata-helper, cache, and Apple-adapter boundaries; those parts are not yet implemented.

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
muse apple-status REQUEST RESULT # validate a returned Music.app observation
muse stats                  # count files and disk usage
muse stats backlog          # inspect one path beneath the root
muse stats --json           # emit structured output
muse info backlog/old-album # review songs, tags, artwork, and numbering gaps
muse info backlog/old-album --all # include full metadata for each track
muse info backlog/old-album --json
muse check backlog          # fully decode audio recursively to find playback errors
muse check /media/old-drive/track.flac --json
muse play master/artists/Artist/Album/song.flac # play one track locally with mpv
muse play backlog/album     # play recognized audio recursively as a playlist
```

`play` accepts one or more audio files or directories. Relative targets are resolved beneath the library root. Directories are expanded recursively in deterministic path order without following symlinks, duplicate selections are played once, and non-audio files are skipped. Muse prints the number of selected audio files with a one-line playback-control reminder and runs `mpv` interactively with cover-art display disabled. It disables mpv's automatic playlist creation so the playlist contains exactly Muse's selected files. Routine mpv messages are replaced by one updating status line showing the selection position and count, title, elapsed time, and duration; warnings and errors remain visible. Muse returns mpv's exit status. `mpv` is an optional external dependency; playback does not create Muse state or record listening behaviour.

`status` compares audio paths in `master/` with the durable item ledger without reading audio contents or creating state. It reports unregistered Master files and suggests `muse register`; it also warns about registered item paths that are no longer present.

`check` accepts one audio file or a directory. Relative targets use the current working
directory, including paths outside the vault. It checks recognized audio extensions with
FFmpeg, compares decoded length with the duration reported by FFprobe, and uses
`flac --test` for FLAC files when that tool is installed. It skips other
files and symlinks, lists failures, and exits with status 1 if a file fails or traversal
has an error. A duration difference greater than both 5 seconds and 5% is reported as
a failure. A successful decode cannot prove that all intended audio is present in
formats without an embedded checksum or trustworthy original to compare against.

`info` accepts an album directory (including disc subdirectories) or one audio file.
Relative targets are resolved beneath the library root. The default table lists every
readable track on one line, with its title, artist, duration, and missing metadata.
Use `--all` to include audio properties, all text tags, and embedded artwork details
for each track. JSON always includes the full report. The review highlights missing core
metadata, numbering gaps or duplicates, and inconsistent album-level tags. These are cues for a manual
review, not a claim that an album is complete or an audio integrity check; use `check`
to decode the files. `info` is read-only and returns an error status only for unreadable
files, traversal errors, or an empty target.

### Probe Music.app through a manual bundle

The initial macOS bridge is a separate read-only helper. It does not yet publish
music or update Muse's ledger. Create a fresh inspection request on either host:

```bash
muse-apple-bridge prepare-inspection /path/to/inspection-request
```

Copy that whole directory to a logged-in Mac on which Muse is installed, then run:

```bash
muse-apple-bridge apply /path/to/inspection-request /path/to/inspection-result
```

The helper uses `osascript` to read Music.app's version, library identity and name,
total track count, and the combined size of the `Muse — Master` and
`Muse — Stopgap` playlists. macOS may ask for Automation permission. The result is
written as `inspection-result/result.json`; copy the result directory back intact,
then validate and display it together with the originating request:

```bash
muse apple-status /path/to/inspection-request /path/to/inspection-result
muse apple-status /path/to/inspection-request /path/to/inspection-result --json
```

`apple-status` verifies the exact bundle inventories, staged-file hashes where
present, protocol version, batch and operation identities, and canonical request
digest before reporting the observation. It is read-only and does not require a
library root. Reapplying the same request on the Mac returns its journaled result
without querying Music.app again. Bridge identity and operation outcomes are retained by
default under the current macOS user's Application Support directory. Use
`--state PATH` after `apply` only for testing or an intentionally separate bridge
identity.

This workflow requires no SSH and performs no Music.app writes. The managed
playlists must not overlap: this initial probe reports the sum of their membership
counts and rejects it if that sum exceeds the total Music.app track count.

### Upgrade embedded album artwork

```bash
muse fix-art backlog/old-album              # preview MusicBrainz match and cover dimensions
muse fix-art master/artists/Artist/Album --apply
muse fix-art backlog/one.mp3 --min-size 1000 --json
muse fix-art backlog/old-album --cover /path/to/front.jpg --apply
muse fix-art backlog/old-album --allow-small --apply
```

`fix-art` accepts one or more audio files or directories beneath the library root and
recurses into directories without following symlinks. It uses a tagged MusicBrainz
release ID when available, otherwise a release-group ID, otherwise an album and
album-artist search. If an exact title is absent, a unique release-group title extending
a long tagged prefix is accepted. Ambiguous matches and missing covers are skipped.
Artwork comes from the Cover Art Archive's original front-cover endpoint (up to 20 MB
per image). Use `--cover IMAGE` to supply a local image for one tagged album when the
online search has no usable result. The default minimum is 600 pixels in both dimensions.
Muse also requires a new image to be at least as large in both dimensions and larger in
one before replacing an existing cover. If either condition fails, an interactive
terminal shows both image sizes and asks whether to replace the affected files anyway.
One approval overrides both checks for that album. In scripts or JSON mode, blocked
files are reported as needing approval; use `--allow-small` and/or `--force` to make the
choice explicitly. JPEG and PNG are supported. The
command shows a preview by default; `--apply` embeds the image in each eligible file
through an atomic file replacement. Originals are preserved under `.muse/art-backups/`
and each updated file reports its backup path. Network access is required. Failures are
reported per album or file, and a failed album does not prevent other selected albums
from being processed.

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
- Thorough mode stores vault file hashes in `.muse/muse.db` and reuses size and modification-time matching entries on later scans, duplicate reports, and tree comparisons. It always hashes external source files freshly and never stores their hashes in the vault cache.
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
Vault-wide duplicate searches skip `slag/` and `trash/`. To inspect either area deliberately, pass it as an explicit target (for example, `muse dupes slag`).

Duplicate reporting rules:

- Human-readable output shows the 10 largest groups by default. Use `--all` or `--json` for all groups.
- Zero-byte files are omitted from the ordinary duplicate-file report; `stats` counts them separately.
- `--trees` includes zero-byte files when comparing trees that contain data, but omits tree groups whose total size is zero.
- `--rehash` bypasses cached hashes.
- `prune` only removes cache entries for missing files. It never changes library content, and discarded hashes can be recomputed.

### Plan and apply library changes

```bash
muse import some-song.mp3                     # suggest an artists/ destination from tags (source is in backlog/)
muse import backlog/album                     # the explicit backlog/ prefix is also accepted
muse import backlog/album games/album         # plan a release-directory import at an explicit destination
muse import backlog/mixes/song.flac games/mixes/ # plan a single or selection into a shared directory
muse import --apply                           # reverify and move the sole current plan
muse import --abort                           # discard the current plan

muse compact [backlog]                # create the sole compaction plan
muse compact backlog --prefer backlog/archive-a --prefer backlog/archive-b
muse compact backlog --all            # show every move in the new plan
muse compact --apply                  # reverify, confirm, and apply the plan

muse register                         # validate existing Master files and plan item registration
muse register master/games/old-album  # limit the bootstrap to one Master path
muse register --apply                 # reverify and create durable item identities
muse register --abort                 # discard the current registration plan

muse mv old-path new-path             # move content and preserve cached hashes and item IDs
```

For `mv` and `import`, a destination ending in `/` names a directory. Muse creates it and any missing parents when applying the operation, then places the source file or directory inside it. For example, `muse mv backlog/albumA backlog/dest/dir/` results in `backlog/dest/dir/albumA`.

#### Register existing Master files

`register` is a one-time bootstrap for canonical audio that was already in `master/` before Muse began assigning operational item IDs. It scans all of `master/` by default or one explicitly selected path beneath it. Unregistered audio is probed, fully decoded, checked with the native FLAC verifier when available, and freshly SHA-256 hashed. Existing registered paths are skipped, ordinary non-audio release artifacts are counted but left alone, and traversal errors, symbolic links, unsupported media, missing required tags, or failed integrity checks block the plan.

The default invocation writes a durable preview plan but does not create item rows or modify content. `--apply` inventories the scope again, fully reverifies every planned file, and then creates stable item and initial exact-version rows in `.muse/muse.db`; it never changes audio, tags, artwork, or paths. Application is idempotent after an interruption. Use `--all` to show every planned file and `--progress auto|always|never` to control validation progress. This command is a migration path for existing canonical content, not a replacement for normal validated import.

#### Import

The `import` implementation accepts nonempty release directories and individual audio files beneath `backlog/`. It:

- uses ffprobe and ffmpeg to recognize and fully decode MP3, M4A (AAC or Apple Lossless), Ogg Vorbis/Opus, common PCM WAV, and FLAC;
- fills deterministic missing metadata: album artist from artist, track number from a numeric filename prefix, and disc number as `1/1`; imports explicitly destined for a `singles/` directory get missing track and disc numbers as `1/1`;
- checks essential tags and release numbering after those fixups;
- shows inconsistent album artist values for review and, in interactive mode, allows the user to accept intentional differences as a recorded warning;
- preserves the original album position of an individually imported selection;
- identifies genuine one-track releases as standalone singles;
- preserves nonempty image files, including back covers and booklets, and embeds one unambiguous nearby front cover when an audio file has no embedded artwork;
- reports mixed codecs, sample rates, or bit depths but preserves them as-is; and
- runs the native FLAC integrity check when available.

Planning may perform the listed lossless tag and artwork fixups in `backlog/`; the preview reports each change. Muse permits at most one current import plan, so `muse import`, `muse import --apply`, and `muse import --abort` do not require its often-long source path. Only `--apply` moves the planned content into `master/`. When no destination is supplied and no plan exists, Muse suggests and records an `artists/<Album Artist>/...` destination from the tags: complete releases go directly below the artist, standalone singles below `singles/`, and album selections below `selections/`. Supplying a destination overrides that organization. For a file source, a destination without the source's audio extension is treated as a directory and the filename is appended. Missing destination directories are created during apply; an exact new filename with the same extension remains supported. Individually imported album selections retain positions such as `4/10` without requiring the other tracks; directory imports still require coherent, contiguous release numbering. This supports directory-wrapped singles, album selections, and independently imported singles grouped in a human-friendly directory. Nonempty JPEG, PNG, GIF, WebP, BMP, TIFF, AVIF, HEIF, and JPEG XL images are accepted by extension and preserved with their original bytes. Other non-audio artifacts remain blockers.

If application stops after the rename, repeat `muse import --apply`. Muse verifies the content at its destination, updates cached paths, and archives the same plan. Planning and application hold a repository mutation lock; a concurrent mutation exits with an error. Successful application also creates one durable operational item row per imported audio file and records its admitted SHA-256 version. These internal IDs preserve identity for later publication without becoming canonical metadata or being embedded in the file.

#### Compaction

Compaction reverifies every planned duplicate tree before mutation, then moves redundant trees into a dated receipt beneath `trash/`, preserving their repository-relative paths. These same-filesystem renames are cheap and recoverable; permanent trash purging is a separate future operation.

Repeat `--prefer PATH` in priority order to favor authoritative subtrees within a duplicate group. Preferences rank copies only within the same managed-area tier, so the built-in `master`, `backlog`, `stopgap`, `incoming`, `slag`, and `trash` policy still takes precedence.

Plans record filenames, file sizes, and high-resolution modification/change timestamps. Applying a current plan therefore does not need to hash content or query the hash cache per file; older pending plans fall back to content-fingerprint verification.

If compaction stops after moving some trees, repeat `muse compact --apply`. The plan records its trash receipt before the first move, so the retry verifies retained content and completed moves and continues into that receipt. Legacy ready plans still apply; a legacy plan with missing source trees and no recorded receipt requires manual inspection.

### Preserve non-music artifacts

```bash
muse slag                              # inspect preserved non-music artifacts
muse slag backlog/pc-2007 --apply
muse slag backlog/pc-2007 --thorough  # include artwork and release-adjacent files
```

`slag/` isolates non-music artifacts moved from backlog for later triage. It retains the original backlog-relative path so provenance remains visible.
Copies are verified in a temporary file beside their final destination, then published before the backlog source is removed. An interrupted copy leaves the source available for a retry. An existing final destination is accepted only when its content matches the source.

An interrupted `muse mv` can be retried with the same source and destination. Its operation record lets Muse finish companion slag moves and reconcile cached hash paths and durable item paths after a rename. A plain move preserves a registered item's lifecycle area; moving it between `master/` and `stopgap/` requires a future explicit lifecycle operation.

## Terminal output

Muse treats the terminal as its primary user interface. Human-readable output uses restrained semantic styling: green for success, yellow for warnings, red for errors, and subdued text for secondary information. Color is emitted automatically only for a terminal and is removed from pipes and redirected output.

```bash
muse --color auto status    # default
muse --color always status
muse --color never status
NO_COLOR=1 muse status
```

Machine-readable JSON never contains terminal styling. Automatic progress waits two seconds to avoid flicker for quick scans, pulls, duplicate searches, compactions, and slag copies, but starts immediately when preflight identifies a large workload. `--progress always` starts immediately where that option is available. Progress is written to stderr and can be controlled with `--progress auto|always|never` on commands that offer it.

## Documentation

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the current code and recovery model, and [`DESIGN.md`](DESIGN.md) for proposed behavior and future work. This README and the CLI help document the implemented interface.

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
