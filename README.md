# BlendGit

Version history, branches and GitHub push for .blend files, in a Blender sidebar tab.

## Install
1. Install [Git](https://git-scm.com/downloads), if you don't have it yet.
2. Blender 4.2 or newer: **Edit → Preferences → Get Extensions → ⌄ (top right) → Install from Disk…**, then pick `blendgit-1.0.0.zip`.
3. In the 3D View, press **N** and open the **BlendGit** tab.

## Use
- Click **Create Versioned Project**, type a name, and pick a location. BlendGit creates the folder, copies in every file the scene uses (textures, sounds, fonts, movies, caches, linked libraries), saves the model as `<name>.blend` and starts versioning. Your original files are left untouched.
- **Commit**: write a message and click Commit. This saves the file and records the version.
- **History**: select a version to see its thumbnail. **Preview** (eye icon) opens it in a second Blender window without touching your file. **Restore** rolls back to it as a new commit, so nothing is lost.
- **Branches**: preview, switch, or create branches. "Branch from here" starts a branch from the selected version.
- **GitHub**: create an empty repository on github.com, paste its URL, and click **Push**. You sign in through Git's usual login (the browser opens on Windows/macOS).
- Textures stored outside the folder? Click **Pack External Files** first.

GitHub rejects single files over 100 MB.
