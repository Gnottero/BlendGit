"""
BlendGit: git-backed versioning for .blend files, in the 3D View sidebar.

The folder holding the .blend is the git repository. Each commit cuts the .blend into one file
per datablock under blocks/<name>/, so commits and pushes only carry what changed.
"""

import atexit
import glob
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile

import bpy
import bpy.utils.previews
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       IntProperty, PointerProperty, StringProperty)

from . import blocks

CACHE = tempfile.mkdtemp(prefix="blendgit-")
atexit.register(shutil.rmtree, CACHE, ignore_errors=True)
GITIGNORE = "*.blend1\n*.blend[0-9]\n*.blend[0-9][0-9]\n*.blend@\n"
ASSET_DIRS = {"images": "textures", "movieclips": "movies", "sounds": "sounds", "fonts": "fonts",
              "volumes": "volumes", "cache_files": "caches", "libraries": "libraries"}
BLOCKS_DIR = "blocks"
ROLLBACK_TYPES = {
    "OB": ("objects", 'OBJECT_DATA'), "ME": ("meshes", 'MESH_DATA'), "CU": ("curves", 'CURVE_DATA'),
    "CV": ("hair_curves", 'CURVES_DATA'), "AR": ("armatures", 'ARMATURE_DATA'),
    "CA": ("cameras", 'CAMERA_DATA'), "LA": ("lights", 'LIGHT_DATA'), "LT": ("lattices", 'LATTICE_DATA'),
    "MB": ("metaballs", 'META_DATA'), "GP": ("grease_pencils", 'OUTLINER_DATA_GREASEPENCIL'),
    "PT": ("pointclouds", 'POINTCLOUD_DATA'), "VO": ("volumes", 'VOLUME_DATA'),
    "MA": ("materials", 'MATERIAL'), "NT": ("node_groups", 'NODETREE'), "WO": ("worlds", 'WORLD'),
    "AC": ("actions", 'ACTION'),
}
ROLLBACK_SKIP = {"libraries", "screens", "workspaces"}
PREVIEW_SETUP = ("import bpy; p = bpy.context.preferences; "
                 "p.use_preferences_save = False; p.view.use_save_prompt = False")
_previews = None
_branch_items = []


## Raised when an action fails. Its message is shown to the user.
class GitError(Exception):
    pass


def repo_dir():
    return os.path.dirname(bpy.data.filepath)


def in_repo():
    return bool(bpy.data.filepath) and os.path.isdir(os.path.join(repo_dir(), ".git"))


##
# Run git in the project folder.
#
# @param args the git arguments
# @param raw True to get bytes instead of stripped text
# @return the output of the command
# @throws GitError if git is missing or the command fails
def git(*args, raw=False):
    try:
        r = subprocess.run(
            ["git", *args], cwd=repo_dir(), capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        raise GitError("Git is not installed: get it from git-scm.com")
    if r.returncode:
        raise GitError((r.stderr or r.stdout).decode(errors="replace").strip())
    return r.stdout if raw else r.stdout.decode(errors="replace").strip()


##
# Build the author overrides: preferences first, then git config, then defaults.
#
# @return list of -c arguments for git
def identity():
    addon = bpy.context.preferences.addons.get(__package__)
    prefs = addon.preferences if addon else None
    args = []
    for key, attr, default in (("user.name", "author_name", "Blender User"),
                               ("user.email", "author_email", "blender@localhost")):
        value = getattr(prefs, attr, "")
        if not value:
            try:
                git("config", key)
            except GitError:
                value = default
        if value:
            args += ["-c", f"{key}={value}"]
    return args


def blend_stem():
    return os.path.splitext(os.path.basename(bpy.data.filepath))[0]


def pieces_dir():
    return os.path.join(repo_dir(), BLOCKS_DIR, blend_stem())


def stamp_path(blend):
    return os.path.join(os.path.dirname(blend), ".git", f"blendgit-{os.path.basename(blend)}.stamp")


def file_stamp(blend):
    info = os.stat(blend)
    return f"{info.st_size} {info.st_mtime_ns}"


##
# Record the size and time of a .blend, to detect later saves.
#
# @param blend path of the .blend
def write_stamp(blend):
    with open(stamp_path(blend), "w") as f:
        f.write(file_stamp(blend))


##
# Tell whether the .blend was saved after its last split, which git cannot see.
#
# @return True if there is saved work that is not in the pieces yet
def blend_changed():
    if not os.path.isfile(os.path.join(pieces_dir(), blocks.MANIFEST)):
        return False
    try:
        with open(stamp_path(bpy.data.filepath)) as f:
            return f.read() != file_stamp(bpy.data.filepath)
    except OSError:
        return True


## Stop tracking the .blend. This also moves a 1.0 project to pieces on its next commit.
def ignore_blend():
    name = os.path.basename(bpy.data.filepath)
    entry = "/" + re.sub(r"([*?\[\\])", r"\\\1", name)
    path = os.path.join(repo_dir(), ".gitignore")
    if os.path.islink(path):
        raise GitError(".gitignore is a link to another file, so BlendGit won't write to it")
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    if entry not in text.splitlines():
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + entry + "\n")
    git("rm", "--cached", "--quiet", "--ignore-unmatch", "--", name)


##
# Save the .blend, split it and commit the pieces.
#
# @param message the commit message
# @return False if nothing changed
def commit(message):
    bpy.ops.wm.save_mainfile(compress=False)
    ignore_blend()
    blocks.split(bpy.data.filepath, pieces_dir())
    write_stamp(bpy.data.filepath)
    git("add", "-A")
    if not git("status", "--porcelain"):
        return False
    git(*identity(), "commit", "-m", message)
    return True


## Create the repository and record the first version.
def start_repo():
    git("init", "-b", "main")
    path = os.path.join(repo_dir(), ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(GITIGNORE)
    commit("Initial version")


##
# Copy an external file into the project, keeping both if the name is taken.
#
# @param src path of the file
# @param dest_dir folder to copy it into
# @param multi True to copy every frame or tile
# @return the new path, or None if the file is missing
def copy_asset(src, dest_dir, multi):
    base = os.path.basename(src)
    if multi:
        pattern = re.sub(r"<UDIM>|<UVTILE>|\d+(?=\.\w+$)", "*", base)
        files = glob.glob(os.path.join(glob.escape(os.path.dirname(src)), pattern))
    else:
        files = [src]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None
    target, n = dest_dir, 0
    while any(os.path.exists(os.path.join(target, os.path.basename(f))) for f in files):
        n += 1
        target = os.path.join(dest_dir, str(n))
    os.makedirs(target, exist_ok=True)
    for f in files:
        shutil.copy2(f, target)
    return os.path.join(target, base)


##
# Copy every external file the scene uses into the project, and point the datablocks at the copies.
#
# @param folder the project folder
# @return the source paths that could not be found
def gather_assets(folder):
    copied = {}
    for attr, sub in ASSET_DIRS.items():
        for block in getattr(bpy.data, attr):
            if block.library or getattr(block, "packed_file", None) or block.filepath in ("", "<builtin>"):
                continue
            if attr == "images" and block.is_dirty:
                block.pack()
                continue
            src = os.path.normpath(bpy.path.abspath(block.filepath))
            if src not in copied:
                multi = attr == "images" and block.source in {'SEQUENCE', 'TILED'}
                copied[src] = copy_asset(src, os.path.join(folder, sub), multi)
            if copied[src]:
                block.filepath = copied[src]
    return [src for src, dst in copied.items() if dst is None]


##
# Refuse to go on if there is uncommitted work.
#
# @throws GitError if there are uncommitted changes
def require_clean():
    if git("status", "--porcelain") or blend_changed():
        raise GitError("You have uncommitted changes: commit them first")


## Rebuild the .blend from the current pieces and reload it.
def reload_file():
    if os.path.isfile(os.path.join(pieces_dir(), blocks.MANIFEST)):
        blocks.assemble(pieces_dir(), bpy.data.filepath)
        write_stamp(bpy.data.filepath)
    bpy.ops.wm.revert_mainfile()


##
# Unpack a version into the cache.
#
# @param rev the version
# @return path of its .blend
# @throws GitError if the .blend does not exist in that version
def extract(rev):
    sha = git("rev-parse", rev + "^{commit}")
    dest = os.path.join(CACHE, sha)
    if not os.path.isdir(dest):
        tmp = dest + ".tmp"
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", sha, raw=True))) as tar:
            tar.extractall(tmp, filter="data")
        os.replace(tmp, dest)
    path = os.path.join(dest, os.path.basename(bpy.data.filepath))
    pieces = os.path.join(dest, BLOCKS_DIR, blend_stem())
    if not os.path.isfile(path) and os.path.isfile(os.path.join(pieces, blocks.MANIFEST)):
        blocks.assemble(pieces, path)
    if not os.path.isfile(path):
        raise GitError(f"{os.path.basename(path)} does not exist in that version")
    return path


##
# Build the command that opens a version in a throwaway Blender.
#
# It opens a copy, as rollbacks and thumbnails read the cached file and a save in the preview must
# not change it. The preview never asks to save, and never writes that choice to the preferences.
#
# @param path path of the cached .blend, from extract()
# @return the command line
def preview_command(path):
    folder, name = os.path.split(path)
    copy = os.path.join(folder, f"{os.path.splitext(name)[0]} (preview {os.path.basename(folder)[:7]}).blend")
    shutil.copyfile(path, copy)
    return [bpy.app.binary_path, copy, "--python-expr", PREVIEW_SETUP]


##
# Load the thumbnail of a version, if it is not loaded yet.
#
# @param sha the commit hash
def ensure_thumb(sha):
    if _previews is None or sha in _previews:
        return
    try:
        _previews.load(sha, extract(sha), 'BLEND')
    except (GitError, blocks.BlockError):
        pass


##
# Read the piece list of a version.
#
# @param rev the version
# @return {piece path: (type code, name)}, empty for versions from before 2.0
def manifest_at(rev):
    try:
        return blocks.read_manifest(git("show", f"{rev}:{BLOCKS_DIR}/{blend_stem()}/{blocks.MANIFEST}"))
    except (GitError, json.JSONDecodeError):
        return {}


##
# List the datablocks that can be rolled back to a commit.
#
# These are the ones the commit touched, plus the ones that changed between it and the last commit:
# an item moved in a later commit is not something the older commit touched.
#
# @param sha the commit hash
# @return list of (type code, name, git status, version to roll back to), objects first
def version_changes(sha):
    folder = f"{BLOCKS_DIR}/{blend_stem()}"
    known = {**manifest_at("HEAD"), **manifest_at(f"{sha}^"), **manifest_at(sha)}

    def diff(*revs):
        output = git("diff-tree", "-r", "-z", "--root", "--no-commit-id", "--name-status", "--no-renames",
                     *revs, "--", folder).split("\0")
        for status, path in zip(output[::2], output[1::2]):
            code, name = known.get(path[len(folder) + 1:], ("", ""))
            if code in ROLLBACK_TYPES:
                yield code, name, status

    changes = {(code, name): (status, f"{sha}^" if status == "D" else sha) for code, name, status in diff(sha)}
    for code, name, status in diff(sha, "HEAD"):
        if status != "A":
            changes[code, name] = (status, sha)
    return sorted(((code, name, *change) for (code, name), change in changes.items()),
                  key=lambda change: (change[0] != "OB", change[1]))


def libraries_of(path):
    return [library for library in bpy.data.libraries
            if os.path.realpath(bpy.path.abspath(library.filepath)) == os.path.realpath(path)]


##
# Find the collections an object was in, in an old version.
#
# @param path path of the old .blend
# @param name the object name
# @return the collection names, empty if it was only in the scene collection
def old_collections(path, name):
    # links every collection of the old version just to read membership, which loads
    # the whole old scene. Parse the collection pieces instead if this gets slow on big files.
    with bpy.data.libraries.load(path, link=True) as (old, new):
        new.collections = old.collections
    names = [collection.name for collection in new.collections
             if any(member.name == name for member in collection.objects)]
    bpy.data.batch_remove(libraries_of(path))
    return names


##
# Bring one datablock back from an old version, leaving everything else alone.
#
# It is appended from the rebuilt old version, as old pieces only match the DNA they were saved
# with. Dependencies that still exist are reused, and an object brings its data with it.
#
# @param rev the version to take it from
# @param code the datablock type code
# @param name the datablock name
# @throws GitError if the datablock does not exist in that version
def roll_back(rev, code, name):
    attr = ROLLBACK_TYPES[code][0]
    path = extract(rev)
    with bpy.data.libraries.load(path) as (old, _):
        old_names = {kind: getattr(old, kind) for kind in dir(old)
                     if isinstance(getattr(old, kind), list) and kind not in ROLLBACK_SKIP}
    if name not in old_names[attr]:
        raise GitError(f"{name} does not exist in that version")

    aside = []
    read_only = []
    try:
        for kind, names in old_names.items():
            collection = getattr(bpy.data, kind)
            for old_name in names:
                block = collection.get((old_name, None))
                if block is not None:
                    try:
                        block.name = f"blendgit-aside-{len(aside)}"
                    except AttributeError:
                        read_only.append((collection, block, set(collection)))
                        continue
                    aside.append((collection, block, old_name))

        with bpy.data.libraries.load(path, link=False) as (_, new):
            setattr(new, attr, [name])
        restored = getattr(new, attr)[0]

        wanted = {restored}
        if code == "OB" and restored.data:
            wanted.add(restored.data)
        unused = []
        for collection, block, old_name in aside:
            copy = collection.get((old_name, None))
            if copy in wanted:
                block.user_remap(copy)
                unused.append(block)
            elif copy is not None:
                copy.user_remap(block)
                unused.append(copy)
        for collection, block, before in read_only:
            for copy in set(collection) - before:
                if copy.name.startswith(block.name):
                    copy.user_remap(block)
                    unused.append(copy)
        bpy.data.batch_remove(unused + libraries_of(path))
        aside = [entry for entry in aside if entry[1] not in unused]
    finally:
        for _, block, old_name in aside:
            block.name = old_name

    if code == "OB":
        scene = bpy.context.scene
        if not restored.users_collection:
            homes = [bpy.data.collections.get((home, None)) for home in old_collections(path, name)]
            for collection in [home for home in homes if home] or [scene.collection]:
                collection.objects.link(restored)
        if restored.type == 'CAMERA' and scene.camera is None:
            scene.camera = restored


def state():
    return bpy.context.window_manager.bgit


## Reload the panel state from git.
def refresh():
    s = state()
    s.commits.clear()
    _branch_items.clear()
    if not in_repo():
        return
    try:
        s.branch = git("branch", "--show-current") or "(detached)"
        s.dirty = bool(git("status", "--porcelain")) or blend_changed()
        _branch_items.extend((b, b, "") for b in git("branch", "--format=%(refname:short)").splitlines())
        try:
            s.remote = git("remote", "get-url", "origin")
        except GitError:
            pass
        log = git("log", "--format=%H%x1f%an%x1f%ar%x1f%s%x1e")
    except GitError as e:
        print("BlendGit:", e)
        return
    for entry in filter(None, (e.strip() for e in log.split("\x1e"))):
        c = s.commits.add()
        c.sha, c.author, c.date, c.message = entry.split("\x1f", 3)
    if s.index >= len(s.commits):
        s.index = 0
    else:
        on_select(s, None)


@persistent
def on_file_event(*_):
    refresh()


def branch_items(self, context):
    return _branch_items or [("NONE", "(no branches)", "")]


## Load the thumbnail and the change list of the selected version.
def on_select(self, context):
    self.changes.clear()
    if 0 <= self.index < len(self.commits):
        sha = self.commits[self.index].sha
        ensure_thumb(sha)
        try:
            for code, name, status, rev in version_changes(sha):
                change = self.changes.add()
                change.code, change.name, change.status, change.rev = code, name, status, rev
        except GitError as e:
            print("BlendGit:", e)


##
# Get the version selected in the history.
#
# @return the selected commit
# @throws GitError if nothing is selected
def selected():
    s = state()
    if not 0 <= s.index < len(s.commits):
        raise GitError("Select a version in the history")
    return s.commits[s.index]


class BGIT_Commit(bpy.types.PropertyGroup):
    sha: StringProperty()
    author: StringProperty()
    date: StringProperty()
    message: StringProperty()


## One datablock changed by the selected version.
class BGIT_Change(bpy.types.PropertyGroup):
    code: StringProperty()
    status: StringProperty()
    rev: StringProperty()


## The panel state, stored on the window manager.
class BGIT_State(bpy.types.PropertyGroup):
    commits: CollectionProperty(type=BGIT_Commit)
    index: IntProperty(update=on_select)
    changes: CollectionProperty(type=BGIT_Change)
    change_index: IntProperty()
    branch: StringProperty()
    dirty: BoolProperty()
    target: EnumProperty(name="Branch", items=branch_items)
    message: StringProperty(name="Message", description="Describe what changed")
    new_branch: StringProperty(name="New branch")
    remote: StringProperty(name="GitHub URL", description="e.g. https://github.com/you/model.git")


class BGIT_Prefs(bpy.types.AddonPreferences):
    bl_idname = __package__
    author_name: StringProperty(name="Author name", description="Leave empty to use your git config")
    author_email: StringProperty(name="Author email", description="Leave empty to use your git config")

    def draw(self, context):
        self.layout.prop(self, "author_name")
        self.layout.prop(self, "author_email")


## Base operator: asks for confirmation if needed, reports errors and refreshes the panel.
class GitOp:
    bl_options = {'REGISTER'}
    confirm = ""

    def invoke(self, context, event):
        if not self.confirm:
            return self.execute(context)
        msg = self.confirm + ("\nUnsaved changes in Blender will be lost." if bpy.data.is_dirty else "")
        return context.window_manager.invoke_confirm(self, event, title=self.bl_label, message=msg)

    def execute(self, context):
        try:
            done = self.run(context)
        except (GitError, blocks.BlockError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh()
        if done:
            self.report({'INFO'}, done)
        return {'FINISHED'}


class BGIT_OT_init(GitOp, bpy.types.Operator):
    bl_idname = "bgit.init"
    bl_label = "Start Versioning"
    bl_description = "Turn the folder of this .blend into a version history (everything in it is tracked)"

    @property
    def confirm(self):
        return f"Every file in {repo_dir()} will be versioned."

    def run(self, context):
        start_repo()
        return "Versioning started"


class BGIT_OT_new_project(GitOp, bpy.types.Operator):
    bl_idname = "bgit.new_project"
    bl_label = "Create Versioned Project"
    bl_description = "Create a project folder, copy the model and every file it uses into it, and start versioning"
    name: StringProperty(name="Name", description="Name of the project folder and of the .blend")
    directory: StringProperty(name="Location", description="Where the project folder is created", subtype='DIR_PATH')

    def invoke(self, context, event):
        current = bpy.data.filepath
        documents = os.path.join(os.path.expanduser("~"), "Documents")
        self.name = os.path.splitext(os.path.basename(current))[0] if current else "MyModel"
        self.directory = os.path.dirname(current) if current else (
            documents if os.path.isdir(documents) else os.path.expanduser("~"))
        return context.window_manager.invoke_props_dialog(self, width=400, confirm_text="Create")

    def run(self, context):
        name = self.name.strip()
        if not name or set(name) & set('<>:"/\\|?*'):
            raise GitError('Pick a name without any of these characters: < > : " / \\ | ? *')
        folder = os.path.join(bpy.path.abspath(self.directory), name)
        if os.path.isdir(folder) and os.listdir(folder):
            raise GitError(f"{folder} already exists and is not empty")
        os.makedirs(folder, exist_ok=True)
        missing = gather_assets(folder)
        bpy.ops.wm.save_as_mainfile(filepath=os.path.join(folder, name + ".blend"), compress=False)
        bpy.ops.file.make_paths_relative()
        start_repo()
        if missing:
            self.report({'WARNING'}, "Not found, not copied: " + ", ".join(missing))
        return f"Project created in {folder}"


class BGIT_OT_open_project(GitOp, bpy.types.Operator):
    bl_idname = "bgit.open_project"
    bl_label = "Open Cloned Project"
    bl_description = "Rebuild the .blend of a project cloned from GitHub, and open it"
    directory: StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def run(self, context):
        folder = bpy.path.abspath(self.directory)
        manifests = sorted(glob.glob(os.path.join(glob.escape(folder), BLOCKS_DIR, "*", blocks.MANIFEST)))
        if not manifests:
            raise GitError("That folder is not a BlendGit project")
        if bpy.data.is_dirty:
            raise GitError("Save the file you have open first")
        blends = []
        for manifest in manifests:
            pieces = os.path.dirname(manifest)
            blend = os.path.join(folder, os.path.basename(pieces) + ".blend")
            if not os.path.exists(blend):
                blocks.assemble(pieces, blend)
                write_stamp(blend)
            blends.append(blend)
        bpy.ops.wm.open_mainfile(filepath=blends[0])


class BGIT_OT_commit(GitOp, bpy.types.Operator):
    bl_idname = "bgit.commit"
    bl_label = "Commit"
    bl_description = "Save and record the current state with a message"

    def run(self, context):
        s = state()
        if not s.message.strip():
            raise GitError("Write a commit message first")
        if not commit(s.message.strip()):
            return "Nothing changed since the last commit"
        s.message = ""
        s.index = 0
        return "Committed"


class BGIT_OT_preview(GitOp, bpy.types.Operator):
    bl_idname = "bgit.preview"
    bl_label = "Preview"
    bl_description = "Open this version in a separate Blender window, without touching your file"
    rev: StringProperty()

    def run(self, context):
        subprocess.Popen(preview_command(extract(self.rev)))
        return "Opening preview…"


class BGIT_OT_switch(GitOp, bpy.types.Operator):
    bl_idname = "bgit.switch"
    bl_label = "Switch Branch"
    bl_description = "Load the selected branch"
    confirm = "Switch to the selected branch?"

    def run(self, context):
        require_clean()
        git("switch", state().target)
        reload_file()


class BGIT_OT_branch(GitOp, bpy.types.Operator):
    bl_idname = "bgit.branch"
    bl_label = "Create Branch"
    bl_description = "Create a new branch and switch to it"
    from_selected: BoolProperty(description="Start from the selected version instead of the current one")

    def run(self, context):
        name = state().new_branch.strip()
        if not name:
            raise GitError("Type a branch name first")
        if self.from_selected:
            require_clean()
            git("switch", "-c", name, selected().sha)
            state().new_branch = ""
            reload_file()
        else:
            git("switch", "-c", name)
            state().new_branch = ""
            return f"On branch {name}"


class BGIT_OT_restore(GitOp, bpy.types.Operator):
    bl_idname = "bgit.restore"
    bl_label = "Restore This Version"
    bl_description = "Roll back to the selected version as a new commit (history is kept)"
    confirm = "Roll the model back to the selected version?"

    def run(self, context):
        require_clean()
        c = selected()
        git("restore", "--source", c.sha, "--staged", "--worktree", "--", ".")
        if not git("status", "--porcelain"):
            raise GitError("That version is identical to the current one")
        git(*identity(), "commit", "-m", f"Restore to {c.sha[:7]}: {c.message}")
        state().index = 0
        reload_file()


class BGIT_OT_rollback(GitOp, bpy.types.Operator):
    bl_idname = "bgit.rollback"
    bl_label = "Roll Back"
    bl_description = "Bring back only this item as it was in the selected version, everything else stays as it is"
    bl_options = {'REGISTER', 'UNDO'}
    rev: StringProperty()
    code: StringProperty()
    block_name: StringProperty()

    def run(self, context):
        if context.mode != 'OBJECT':
            raise GitError("Go back to Object Mode first")
        roll_back(self.rev, self.code, self.block_name)
        return f"{self.block_name} rolled back: commit to keep it, or undo"


class BGIT_OT_push(GitOp, bpy.types.Operator):
    bl_idname = "bgit.push"
    bl_label = "Push to GitHub"
    bl_description = "Upload this branch's history to the GitHub repository"

    def run(self, context):
        s = state()
        url = s.remote.strip()
        if not url:
            raise GitError("Paste your GitHub repository URL first")
        try:
            git("remote", "set-url", "origin", url)
        except GitError:
            git("remote", "add", "origin", url)
        git("push", "-u", "origin", s.branch)
        return "Pushed"


class BGIT_OT_refresh(GitOp, bpy.types.Operator):
    bl_idname = "bgit.refresh"
    bl_label = "Refresh"

    def run(self, context):
        pass


class BGIT_UL_commits(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row()
        row.label(text=item.message, icon='DECORATE_KEYFRAME' if index == 0 else 'DECORATE')
        row.label(text=item.date)


class BGIT_UL_changes(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row()
        label = item.name + (" (deleted)" if item.status == "D" else "")
        row.label(text=label, icon=ROLLBACK_TYPES[item.code][1])
        op = row.operator("bgit.rollback", text="", icon='LOOP_BACK', emboss=False)
        op.rev, op.code, op.block_name = item.rev, item.code, item.name


class VIEW3D_PT_blendgit(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BlendGit"
    bl_label = "Versions"

    def draw(self, context):
        layout = self.layout
        if not in_repo():
            layout.operator("bgit.new_project", icon='NEWFOLDER')
            layout.operator("bgit.open_project", icon='IMPORT')
            if bpy.data.filepath:
                layout.operator("bgit.init", text="Version Current Folder Instead", icon='FILE_FOLDER')
            return
        s = state()

        row = layout.row()
        row.label(text=s.branch, icon='TRIA_RIGHT')
        row.operator("bgit.refresh", text="", icon='FILE_REFRESH')
        if bpy.data.is_dirty or s.dirty:
            layout.label(text="Uncommitted changes", icon='ERROR')

        box = layout.box()
        box.prop(s, "message", text="", placeholder="Commit message")
        box.operator("bgit.commit", icon='CHECKMARK')

        box = layout.box()
        box.label(text="Branches", icon='OUTLINER')
        row = box.row(align=True)
        row.prop(s, "target", text="")
        if s.target != "NONE":
            row.operator("bgit.preview", text="", icon='HIDE_OFF').rev = s.target
        row.operator("bgit.switch", text="Switch")
        row = box.row(align=True)
        row.prop(s, "new_branch", text="", placeholder="New branch name")
        row.operator("bgit.branch", text="", icon='ADD').from_selected = False

        box = layout.box()
        box.label(text="History", icon='TIME')
        box.template_list("BGIT_UL_commits", "", s, "commits", s, "index", rows=6)
        if 0 <= s.index < len(s.commits):
            c = s.commits[s.index]
            if _previews is not None and c.sha in _previews:
                box.template_icon(icon_value=_previews[c.sha].icon_id, scale=8)
            col = box.column(align=True)
            col.label(text=c.message)
            col.label(text=f"{c.sha[:7]} · {c.author} · {c.date}")
            row = box.row(align=True)
            row.operator("bgit.preview", icon='HIDE_OFF').rev = c.sha
            row.operator("bgit.restore", text="Restore", icon='LOOP_BACK')
            row = box.row(align=True)
            row.prop(s, "new_branch", text="", placeholder="New branch name")
            row.operator("bgit.branch", text="Branch from here").from_selected = True
            if s.changes:
                box.label(text="Changed in or since this version")
                box.template_list("BGIT_UL_changes", "", s, "changes", s, "change_index", rows=4)

        box = layout.box()
        box.label(text="GitHub", icon='URL')
        box.prop(s, "remote", text="", placeholder="https://github.com/you/repo.git")
        box.operator("bgit.push", icon='EXPORT')
        layout.operator("file.pack_all", text="Pack External Files Into .blend", icon='PACKAGE')


classes = (BGIT_Commit, BGIT_Change, BGIT_State, BGIT_Prefs, BGIT_OT_init, BGIT_OT_new_project, BGIT_OT_open_project,
           BGIT_OT_commit, BGIT_OT_preview, BGIT_OT_switch, BGIT_OT_branch, BGIT_OT_restore, BGIT_OT_rollback,
           BGIT_OT_push, BGIT_OT_refresh, BGIT_UL_commits, BGIT_UL_changes, VIEW3D_PT_blendgit)
handlers = (bpy.app.handlers.load_post, bpy.app.handlers.save_post)


def register():
    global _previews
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.bgit = PointerProperty(type=BGIT_State)
    _previews = bpy.utils.previews.new()
    for h in handlers:
        h.append(on_file_event)
    bpy.app.timers.register(lambda: refresh(), first_interval=0.1)


def unregister():
    global _previews
    for h in handlers:
        if on_file_event in h:
            h.remove(on_file_event)
    bpy.utils.previews.remove(_previews)
    _previews = None
    del bpy.types.WindowManager.bgit
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
