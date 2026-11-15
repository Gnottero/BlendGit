# Changelog

All notable changes to BlendGit are listed here. Versions follow the rules in the [README](README.md#versioning).

## [2.0.0] - 2026-11-15

### Changed
- **Breaking:** projects are stored as one file per datablock under `blocks/<name>/`, and the `.blend` is no longer tracked. Commits and pushes only carry what changed. Projects from 1.0 convert on their next commit, and must then be opened with 2.0 or newer.
- **Breaking:** Blender 5.0 or newer is required.
- The version cache is now private to each Blender session.

### Added
- "Changed in or since this version" list, with an arrow for each item that rolls it back to the selected version.
- Open Cloned Project, which rebuilds the `.blend` of a project cloned from GitHub.
- Install guide with screenshots, and a logo.

### Fixed
- Switch and Restore refuse to run when saved work has not been committed.
- Rolling back a deleted camera makes it the scene camera again.
- Rolling back a deleted object puts it back in the collections it was in, if they still exist.
- Preview opens a copy and no longer asks to save on close, so edits made in a preview cannot change what rollbacks restore.

### Security
- Manifests that point outside the project or repeat a piece are rejected.
- Symlinks in a repository can no longer redirect writes or deletions outside the project.

## [1.0.0] - 2026-10-27

### Added
- First release: versions, branches, restore, preview and GitHub push from the 3D View sidebar.
