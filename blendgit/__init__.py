"""BlendGit: git-backed versioning for .blend files, in the 3D View sidebar.

The folder holding the .blend is the git repository. Git stores every version
content-addressed, zlib-compressed and delta-packed, and gives back exact bytes.
"""

import glob
import io
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

CACHE = os.path.join(tempfile.gettempdir(), "blendgit")
GITIGNORE = "*.blend1\n*.blend[0-9]\n*.blend[0-9][0-9]\n*.blend@\n"
# bpy.data collections holding external files -> project subfolder they are gathered into
ASSET_DIRS = {"images": "textures", "movieclips": "movies", "sounds": "sounds", "fonts": "fonts",
              "volumes": "volumes", "cache_files": "caches", "libraries": "libraries"}
_previews = None
_branch_items = []  # Blender needs enum item strings kept alive


class GitError(Exception):
    pass


def repo_dir():
    return os.path.dirname(bpy.data.filepath)


def in_repo():
    return bool(bpy.data.filepath) and os.path.isdir(os.path.join(repo_dir(), ".git"))


def git(*args, raw=False):
    try:
        r = subprocess.run(
            ["git", *args], cwd=repo_dir(), capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},  # fail instead of hanging on a password prompt
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        raise GitError("Git is not installed: get it from git-scm.com")
    if r.returncode:
        raise GitError((r.stderr or r.stdout).decode(errors="replace").strip())
    return r.stdout if raw else r.stdout.decode(errors="replace").strip()


def identity():
    """-c overrides for author name/email: preferences first, defaults if git has none."""
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


def commit(message):
    """Save uncompressed (so git can delta it), stage everything, commit. False if nothing changed."""
    bpy.ops.wm.save_mainfile(compress=False)
    git("add", "-A")
    if not git("status", "--porcelain"):
        return False
    git(*identity(), "commit", "-m", message)
    return True


def start_repo():
    git("init", "-b", "main")
    path = os.path.join(repo_dir(), ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(GITIGNORE)
    commit("Initial version")


def copy_asset(src, dest_dir, multi):
    """Copy src (every frame/tile when `multi`) into dest_dir; return the new path, None if missing."""
    base = os.path.basename(src)
    if multi:  # image sequences and UDIM tiles are many files on disk
        pattern = re.sub(r"<UDIM>|<UVTILE>|\d+(?=\.\w+$)", "*", base)
        files = glob.glob(os.path.join(glob.escape(os.path.dirname(src)), pattern))
    else:
        files = [src]
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        return None
    target, n = dest_dir, 0
    while any(os.path.exists(os.path.join(target, os.path.basename(f))) for f in files):
        n += 1  # same file name from another folder: keep both
        target = os.path.join(dest_dir, str(n))
    os.makedirs(target, exist_ok=True)
    for f in files:
        shutil.copy2(f, target)
    return os.path.join(target, base)


def gather_assets(folder):
    """Copy every external file the open scene uses into folder, pointing datablocks at the copies.

    Returns the source paths that could not be found.
    """
    copied = {}  # source -> copy, so a file shared by several datablocks is copied once
    for attr, sub in ASSET_DIRS.items():
        for block in getattr(bpy.data, attr):
            if block.library or getattr(block, "packed_file", None) or block.filepath in ("", "<builtin>"):
                continue  # linked from a library, already inside the .blend, or generated
            if attr == "images" and block.is_dirty:
                block.pack()  # unsaved paint strokes: keep them inside the .blend
                continue
            src = os.path.normpath(bpy.path.abspath(block.filepath))
            if src not in copied:
                multi = attr == "images" and block.source in {'SEQUENCE', 'TILED'}
                # a copied library's own external files are not gathered
                copied[src] = copy_asset(src, os.path.join(folder, sub), multi)
            if copied[src]:
                block.filepath = copied[src]
    return [src for src, dst in copied.items() if dst is None]


def require_clean():
    if git("status", "--porcelain"):
        raise GitError("You have uncommitted changes: commit them first")


def reload_file():
    bpy.ops.wm.revert_mainfile()


def extract(rev):
    """Unpack the whole tree of `rev` (textures included) into the cache; return the .blend path."""
    sha = git("rev-parse", rev + "^{commit}")
    dest = os.path.join(CACHE, sha)
    if not os.path.isdir(dest):
        tmp = dest + ".tmp"
        with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", sha, raw=True))) as tar:
            tar.extractall(tmp, filter="data")
        os.replace(tmp, dest)
    path = os.path.join(dest, os.path.basename(bpy.data.filepath))
    if not os.path.isfile(path):
        raise GitError(f"{os.path.basename(path)} does not exist in that version")
    return path


def ensure_thumb(sha):
    if _previews is None or sha in _previews:
        return
    try:
        # extracts the full tree per selected commit, fine until repos reach GBs
        _previews.load(sha, extract(sha), 'BLEND')
    except GitError:
        pass


def state():
    return bpy.context.window_manager.bgit


def refresh():
    s = state()
    s.commits.clear()
    _branch_items.clear()
    if not in_repo():
        return
    try:
        s.branch = git("branch", "--show-current") or "(detached)"
        s.dirty = bool(git("status", "--porcelain"))
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
    elif s.commits:
        ensure_thumb(s.commits[s.index].sha)


@persistent
def on_file_event(*_):
    refresh()


def branch_items(self, context):
    return _branch_items or [("NONE", "(no branches)", "")]


def on_select(self, context):
    if 0 <= self.index < len(self.commits):
        ensure_thumb(self.commits[self.index].sha)


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


class BGIT_State(bpy.types.PropertyGroup):
    commits: CollectionProperty(type=BGIT_Commit)
    index: IntProperty(update=on_select)
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


class GitOp:
    bl_options = {'REGISTER'}
    confirm = ""  # non-empty: ask before running

    def invoke(self, context, event):
        if not self.confirm:
            return self.execute(context)
        msg = self.confirm + ("\nUnsaved changes in Blender will be lost." if bpy.data.is_dirty else "")
        return context.window_manager.invoke_confirm(self, event, title=self.bl_label, message=msg)

    def execute(self, context):
        try:
            done = self.run(context)
        except GitError as e:
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
        subprocess.Popen([bpy.app.binary_path, extract(self.rev)])
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
        # blocks the UI while pushing, move to a thread if big pushes get annoying
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


class VIEW3D_PT_blendgit(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "BlendGit"
    bl_label = "Versions"

    def draw(self, context):
        layout = self.layout
        if not in_repo():
            layout.operator("bgit.new_project", icon='NEWFOLDER')
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

        box = layout.box()
        box.label(text="GitHub", icon='URL')
        box.prop(s, "remote", text="", placeholder="https://github.com/you/repo.git")
        box.operator("bgit.push", icon='EXPORT')
        layout.operator("file.pack_all", text="Pack External Files Into .blend", icon='PACKAGE')


classes = (BGIT_Commit, BGIT_State, BGIT_Prefs, BGIT_OT_init, BGIT_OT_new_project, BGIT_OT_commit, BGIT_OT_preview,
           BGIT_OT_switch, BGIT_OT_branch, BGIT_OT_restore, BGIT_OT_push, BGIT_OT_refresh,
           BGIT_UL_commits, VIEW3D_PT_blendgit)
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
