<p align="center"><img src="docs/images/logo.png" alt="BlendGit logo" width="160"></p>

# BlendGit

Version history, branches and GitHub push for .blend files, in a Blender sidebar tab. Each commit stores only the objects that changed, so uploads stay small.

## Install
You need Blender 5.0 or newer and [Git](https://git-scm.com/downloads). In Blender, go to **Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…** and pick `blendgit-<version>.zip`. Then press **N** in the 3D View and open the **BlendGit** tab.

The [install guide](docs/INSTALL.md) has every step with pictures.

## Use
- Click **Create Versioned Project**, type a name, and pick a location. BlendGit creates the folder, copies in every file the scene uses (textures, sounds, fonts, movies, caches, linked libraries), saves the model as `<name>.blend` and starts versioning. Your original files are left untouched.
- **Commit**: write a message and click Commit. This saves the file and records the version.
- **History**: select a version to see its thumbnail and what changed in it or since it. **Preview** (eye icon) opens it in a second Blender window without touching your file. **Restore** rolls the whole model back as a new commit, so nothing is lost.
- **Changed in or since this version**: the arrow next to an item brings only that item back to how it was in the selected version, and everything else stays as it is. An object brings its mesh with it, and deleted objects come back too. Commit afterwards to keep the result, or undo.
- **Branches**: preview, switch, or create branches. "Branch from here" starts a branch from the selected version.
- **GitHub**: create an empty repository on github.com, paste its URL, and click **Push**. You sign in through Git's usual login (the browser opens on Windows/macOS).
- **Open Cloned Project**: rebuilds the `.blend` of a project you cloned from GitHub, and opens it.
- Textures stored outside the folder? Click **Pack External Files** first.

## How versions are stored
Git never sees the `.blend`. Each commit cuts it into one file per datablock (every object, mesh, material and so on) under `blocks/<name>/`, and `manifest.json` records their order. A datablock that didn't change is byte-for-byte the same file as before, so git doesn't store it or upload it again.

| One commit, 32 MB scene with 30 meshes | Whole `.blend` (1.0) | Pieces (2.0) |
| --- | --- | --- |
| First push | 12.8 MB | 4.6 MB |
| Move an object | up to 21.9 MB | 2 to 13 KB |
| Change one vertex | up to 21.9 MB | 750 KB |
| Add a material | 21.9 MB | 4 KB |

Rebuilding a version only swaps the pieces that differ, then joins them back into a `.blend`. Rolling back one item appends it from the rebuilt old version, so Blender reads it properly even if the file was saved by an older Blender.

GitHub rejects single files over 100 MB, which now means a single datablock and not the whole model. A `.blend` you copied into `libraries/` is still stored as one file.

## Development
```sh
blender -b --factory-startup --python-exit-code 1 --python tests/test_blendgit.py
blender --command extension build --source-dir blendgit --output-dir dist
```

## Versioning
BlendGit follows [Semantic Versioning](https://semver.org). A version is `MAJOR.MINOR.PATCH`:

| Part | Goes up when | Example |
| --- | --- | --- |
| `MAJOR` | Existing projects or setups have to change: a new storage format, a higher minimum Blender, a removed feature | `1.4.2` → `2.0.0` |
| `MINOR` | A feature is added and existing projects keep working as they are | `2.0.3` → `2.1.0` |
| `PATCH` | A bug is fixed and nothing else changes | `2.1.0` → `2.1.1` |

The version is written the same way everywhere:

| Where | Format | Example |
| --- | --- | --- |
| `version` in `blendgit/blender_manifest.toml` | `X.Y.Z` | `2.1.0` |
| Git tag | `vX.Y.Z` | `v2.1.0` |
| Zip file | `blendgit-X.Y.Z.zip` | `blendgit-2.1.0.zip` |
| Heading in [CHANGELOG.md](CHANGELOG.md) | `## [X.Y.Z] - YYYY-MM-DD` | `## [2.1.0] - 2026-11-20` |

A version counts as released once it has a tag, and a released version never changes. To release one:

1. Set `version` in `blendgit/blender_manifest.toml`.
2. Add its section to `CHANGELOG.md`, with today's date.
3. Run the tests. They fail if the version is malformed or has no changelog section.
4. Build the zip, which takes its name from the manifest.
5. Commit, then tag and push: `git tag -a vX.Y.Z -m "BlendGit X.Y.Z"` and `git push origin main vX.Y.Z`.
6. Publish the zip with the tag.
