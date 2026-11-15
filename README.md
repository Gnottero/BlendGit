<p align="center"><img src="docs/images/logo.png" alt="BlendGit logo" width="160"></p>

# BlendGit

BlendGit is a Blender add-on that keeps a version history of a `.blend` file with Git. You commit, browse old versions, work on branches and push to GitHub from a tab in the 3D View sidebar, without opening a terminal.

A `.blend` is one large binary file, so plain Git stores a full copy of it every time anything changes. BlendGit stores each object, mesh and material as a separate file. A commit that moves one object adds a few kilobytes to the repository, where a new copy of the whole scene would add megabytes.

<p align="center"><img src="docs/images/use-2-history.png" alt="The BlendGit panel showing the history of a project and the items changed in the selected version" width="420"></p>

## Installation

Requirements:

- Blender 5.0 or newer (tested on 5.2.3)
- [Git](https://git-scm.com/downloads), installed so that `git --version` works in a terminal

Steps:

1. Download `blendgit-<version>.zip` from the releases page. Do not unzip it.
2. In Blender, open **Edit → Preferences → Get Extensions**.
3. Click the **⌄** arrow in the top right corner and choose **Install from Disk…**
4. Select the zip, keep **Enable on Install** ticked, and confirm.
5. Close Preferences, hover the 3D View and press **N**. The sidebar now has a **BlendGit** tab.

The [install guide](docs/INSTALL.md) shows each step with a screenshot and lists fixes for the common problems.

## Usage

Open the model you want to version and click **Create Versioned Project** in the BlendGit tab. Give the project a name and a location. BlendGit creates a folder there, copies in the model and every external file it uses (textures, sounds, fonts, movies, caches, linked libraries), and records the first version. The original files are left where they were.

From then on, the panel offers these actions:

| Action | What it does |
| --- | --- |
| Commit | Saves the file and records a version with the message you typed. |
| History | Lists the versions of the current branch. Selecting one shows its thumbnail and the items that changed in it or after it. |
| Preview (eye icon) | Opens the selected version in a second Blender window. Your open file is not touched. |
| Restore | Brings the whole model back to the selected version. The restore is recorded as a new commit, so later versions stay in the history. |
| Roll back (arrow next to an item) | Brings one item back to how it was in the selected version and leaves everything else alone. An object brings its mesh with it, and a deleted object is put back in its collections. Commit afterwards to keep the result, or undo. |
| Branches | Switches to another branch, previews it, or creates a new one. **Branch from here** starts a branch at the selected version. |
| Push to GitHub | Uploads the current branch. Create an empty repository on github.com first and paste its URL in the field above the button. Git asks you to sign in the first time. |
| Open Cloned Project | Rebuilds the `.blend` of a project you cloned from GitHub and opens it. A fresh clone has no `.blend` until you do this. |
| Pack External Files Into .blend | Embeds textures stored outside the project folder into the `.blend`, so they are versioned with it. |

Switch and Restore refuse to run while you have saved work that is not committed.

## Reporting issues

Bugs and feature requests go in this repository's issue tracker. Search the open issues first, and open one issue per problem.

A bug can only be fixed once someone else can reproduce it, so a report needs the exact steps and the exact error text. New issues open with the bug report template from [`.github/ISSUE_TEMPLATE`](.github/ISSUE_TEMPLATE). Fill in every section:

```markdown
### Environment
- BlendGit version:        (Preferences → Add-ons → BlendGit)
- Blender version:         (Help → About Blender)
- Operating system:
- Git version:             (output of `git --version`)

### Steps to reproduce
1.
2.
3.

### Expected result

### Actual result

### Error message

### Project
```

How to fill it in:

- In the title, name the action and the failure, for example "Roll back of a deleted light fails with 'does not exist in that version'".
- Number the steps and name the buttons as they are written in the panel. If the bug also happens in a new project, start the steps from there and say so.
- Paste the error message as text, in full, inside a code block. BlendGit errors appear in the status bar and in the Info editor. For a Python traceback, open **Window → Toggle System Console** on Windows, or start Blender from a terminal on Linux and macOS.
- Under Project, attach the smallest project that shows the bug, as a zip of the project folder or a link to its repository. If you cannot share the file, describe what the scene contains (number of objects, linked libraries, external textures).

For a feature request, describe what you are trying to do and what you do today to work around it.

## Development

Run the tests and build the zip from the repository root:

```sh
blender -b --factory-startup --python-exit-code 1 --python tests/test_blendgit.py
blender --command extension build --source-dir blendgit --output-dir dist
```

### Versioning

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

## License

BlendGit is released under the GNU General Public License, version 3 or later. The full text is in [LICENSE](LICENSE).

## How it works

The project folder is an ordinary Git repository, and BlendGit drives it by calling the `git` program installed on your computer. You can inspect or clone the repository with any other Git tool.

The `.blend` itself is listed in `.gitignore`. On each commit BlendGit saves the file uncompressed and cuts it at datablock boundaries into one file per datablock (an object, a mesh, a material) under `blocks/<name>/`. A `manifest.json` in the same folder records the order of the pieces. A few fields that Blender rewrites on every save, such as session identifiers, are zeroed in the pieces. An unchanged datablock therefore produces the same bytes as in the previous commit, and Git has nothing new to store or upload for it.

The table compares one commit of a 32 MB scene with 30 meshes under both storage formats:

| Change | Whole `.blend` (BlendGit 1.0) | Pieces (BlendGit 2.0) |
| --- | --- | --- |
| First push | 12.8 MB | 4.6 MB |
| Move an object | up to 21.9 MB | 2 to 13 KB |
| Change one vertex | up to 21.9 MB | 750 KB |
| Add a material | 21.9 MB | 4 KB |

To rebuild a `.blend`, BlendGit joins the pieces in manifest order into a temporary file and then replaces the old file with it, so a failure halfway leaves the old file intact. Switching branches and restoring a version let Git update the pieces and then rebuild the file this way.

Previews, thumbnails and single-item rollbacks read an old version without touching the project. BlendGit exports that version into a temporary folder that lasts for the Blender session and rebuilds its `.blend` there. A rollback then appends the requested datablock from that file through Blender's own library loader, which is why it works even if the old version was saved by an earlier Blender.

Two limits follow from this design. GitHub rejects single files over 100 MB, and here that limit applies to one datablock and no longer to the whole model. A `.blend` copied into `libraries/` as a linked library is still stored as one file.
