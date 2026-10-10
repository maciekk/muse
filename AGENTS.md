# Contributor guidance

## Documentation audiences

- `README.md` is user-facing and documents the implemented interface.
- `ARCHITECTURE.md` maps the implementation as it exists for contributors.
- `DESIGN.md` records stable design principles and proposed behavior needed to evaluate changes.
- Personal workflow, machine-specific paths, implementation planning, experiments, and extended rationale do not belong in the public repository.
- If an untracked `AGENTS.local.md` exists, read it for maintainer-local context before product or architecture work. Do not copy private context into public files unless it is necessary to understand or use the project.

## Stable design principles

- Canonical music files are authoritative and as self-contained as their formats permit.
- Muse stores operational relationships and decisions, not a competing metadata catalogue.
- Whole-file SHA-256 identifies an exact file version, not an enduring track.
- Rebuildable indexes are non-authoritative; files win disagreements with a cache.
- Metadata preparation tools are optional and interchangeable.
- Apple is a replaceable publication surface and owns live playback behaviour.
- Prefer simple, inspectable state and recovery from ordinary files and portable manifests.
