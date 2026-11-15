"""Run: blender -b --factory-startup --python-exit-code 1 --python tests/test_blendgit.py"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blendgit

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(project_root, "blendgit", "blender_manifest.toml"), "rb") as f:
    version = tomllib.load(f)["version"]
assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version), f"{version} is not X.Y.Z"
with open(os.path.join(project_root, "CHANGELOG.md"), encoding="utf-8") as f:
    assert re.search(rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}}|Unreleased)$", f.read(), re.M), \
        f"CHANGELOG.md has no section for {version}"

blendgit.register()
root = tempfile.mkdtemp()
d = os.path.join(root, "My Car")
path = os.path.join(d, "My Car.blend")

for sub in ("a", "b"):
    os.makedirs(os.path.join(root, "elsewhere", sub))
    img = bpy.data.images.new(sub, 4, 4)
    img.filepath_raw = os.path.join(root, "elsewhere", sub, "wood.png")
    img.file_format = 'PNG'
    img.save()
    img.use_fake_user = True


def sh(*args):
    return subprocess.run(["git", *args], cwd=d, capture_output=True, check=True).stdout


def s():
    return bpy.context.window_manager.bgit


bpy.ops.bgit.new_project(name="My Car", directory=root)
assert bpy.data.filepath == path
sha1 = sh("rev-parse", "HEAD").decode().strip()
assert open(os.path.join(d, ".gitignore")).read() == blendgit.GITIGNORE + "/My Car.blend\n"
assert sorted(i.filepath for i in bpy.data.images if i.filepath) == ["//textures/1/wood.png", "//textures/wood.png"]
tracked = sh("ls-files").decode().splitlines()
assert {"textures/wood.png", "textures/1/wood.png", "blocks/My Car/manifest.json"} <= set(tracked), tracked
assert "My Car.blend" not in tracked, "the whole file must not be stored anymore"
first_bytes = blendgit.extract(sha1)
assert os.path.isfile(os.path.join(root, "elsewhere", "a", "wood.png")), "originals must be left in place"

data = bytearray(open(path, "rb").read())
dna = next(b for b in blendgit.blocks._walk(data) if b[0] == b"DNA1")
volatile, _ = blendgit.blocks._volatile_layout(bytes(data[dna[4]:dna[5]]))
assert volatile and all(o + n <= size for size, spans in volatile.values() for o, n in spans)

bpy.data.objects["Cube"].location.x = 5
s().message = "moved cube"
bpy.ops.bgit.commit()
assert [c.message for c in s().commits] == ["moved cube", "Initial version"]

changed = sh("diff", "--name-only", "HEAD~1", "HEAD", "--", "blocks").decode().splitlines()
assert any("/OB/Cube-" in p for p in changed), changed
model = [p for p in changed if p.split("/")[2] in blendgit.ROLLBACK_TYPES]
assert len(model) == 1, model
assert [(c.code, c.name) for c in s().changes] == [("OB", "Cube")]

bpy.ops.wm.revert_mainfile()
blendgit.commit("no model change")
changed = sh("diff", "--name-only", "HEAD~1", "HEAD", "--", "blocks").decode().splitlines()
assert not [p for p in changed if p.split("/")[2] in blendgit.ROLLBACK_TYPES], changed
sh("reset", "--hard", "-q", "HEAD~1")
blendgit.reload_file()

bpy.data.objects["Cube"].location.y = 2
bpy.ops.wm.save_mainfile()
blendgit.refresh()
assert s().dirty
try:
    blendgit.require_clean()
    raise SystemExit("saved work was not detected")
except blendgit.GitError:
    pass
bpy.data.objects["Cube"].location.y = 0
blendgit.commit("back")
sh("reset", "--hard", "-q", "HEAD~1")
blendgit.reload_file()

s().new_branch = "experiment"
bpy.ops.bgit.branch(from_selected=False)
assert s().branch == "experiment"

s().index = 1
bpy.ops.bgit.restore()
assert open(path, "rb").read() == open(first_bytes, "rb").read(), "restore is not byte-exact"
assert bpy.data.objects["Cube"].location.x == 0
assert len(s().commits) == 3 and s().commits[0].message.startswith("Restore to")

s().target = "main"
bpy.ops.bgit.switch()
assert s().branch == "main" and bpy.data.objects["Cube"].location.x == 5

for code, (attr, icon) in blendgit.ROLLBACK_TYPES.items():
    assert hasattr(bpy.data, attr), attr
cube, lamp = bpy.data.objects["Cube"], bpy.data.objects["Light"]
cube.shape_key_add(name="Basis")
bpy.data.materials["Material"].use_fake_user = True
blendgit.commit("shape keys")
good = sh("rev-parse", "HEAD").decode().strip()
bpy.data.objects["Cube"].location.x = 9
bpy.data.meshes["Cube"].vertices[0].co.x = 7
bpy.data.objects["Light"].location.x = 3
bpy.data.materials["Material"].roughness = 0.123
blendgit.commit("later edits")
counts = {a: len(getattr(bpy.data, a)) for a in ("objects", "meshes", "materials", "images", "shape_keys")}
assert bpy.ops.bgit.rollback(rev=good, code="OB", block_name="Cube") == {'FINISHED'}
assert bpy.data.objects["Cube"].location.x == 5
assert bpy.data.meshes["Cube"].vertices[0].co.x != 7, "the mesh must roll back with its object"
assert bpy.data.objects["Light"].location.x == 3, "other objects must not move"
assert abs(bpy.data.materials["Material"].roughness - 0.123) < 1e-6, "shared data keeps its current state"
assert bpy.data.objects["Cube"].data.materials[0] == bpy.data.materials["Material"]
assert bpy.data.objects["Cube"].name in bpy.context.scene.collection.all_objects
assert {a: len(getattr(bpy.data, a)) for a in counts} == counts, "rollback left duplicates behind"
assert not bpy.data.libraries, "the old version must not stay linked"

bpy.ops.object.text_add()
blendgit.commit("text")
good = sh("rev-parse", "HEAD").decode().strip()
bpy.data.objects["Text"].location.x = 4
bpy.data.materials["Material"].roughness = 0.9
blendgit.commit("text moved")
fonts = len(bpy.data.fonts)
assert bpy.ops.bgit.rollback(rev=good, code="OB", block_name="Text") == {'FINISHED'}
assert bpy.ops.bgit.rollback(rev=good, code="MA", block_name="Material") == {'FINISHED'}
assert bpy.data.objects["Text"].location.x == 0 and abs(bpy.data.materials["Material"].roughness - 0.123) < 1e-6
assert len(bpy.data.fonts) == fonts, "the built-in font must not be duplicated"

blendgit.commit("rolled back")
bpy.data.objects.remove(bpy.data.objects["Light"])
blendgit.commit("deleted the light")
blendgit.refresh()
s().index = 0
deleted = [c for c in s().changes if c.name == "Light"][0]
assert deleted.status == "D" and deleted.code == "OB"
bpy.ops.bgit.rollback(rev=deleted.rev, code="OB", block_name="Light")
assert bpy.data.objects["Light"].name in bpy.context.scene.collection.all_objects
blendgit.commit("light is back")

bpy.data.objects.remove(bpy.data.objects["Camera"])
assert bpy.context.scene.camera is None
blendgit.commit("deleted the camera")
bpy.ops.bgit.rollback(rev="HEAD^", code="OB", block_name="Camera")
assert bpy.context.scene.camera == bpy.data.objects["Camera"], "a restored camera must be the scene camera again"
assert [c.name for c in bpy.data.objects["Camera"].users_collection] == ["Collection"], \
    "a restored object must go back to its collection"
assert not bpy.data.libraries and len(bpy.data.objects) == 4 and len(bpy.data.collections) == 1, \
    "looking up the collection left data behind"
blendgit.commit("camera is back")

cached = blendgit.extract("HEAD")
before = open(cached, "rb").read()
command = blendgit.preview_command(cached)
assert command[1] != cached and os.path.dirname(command[1]) == os.path.dirname(cached), "preview must open a copy"
assert open(command[1], "rb").read() == before and blendgit.PREVIEW_SETUP in command

clone = os.path.join(root, "clone")
subprocess.run(["git", "clone", "-q", d, clone], check=True)
assert not os.path.exists(os.path.join(clone, "My Car.blend"))
assert bpy.ops.bgit.open_project(directory=clone) == {'FINISHED'}
assert bpy.data.filepath == os.path.join(clone, "My Car.blend")
assert bpy.data.objects["Cube"].location.x == 5 and "Light" in bpy.data.objects
assert not s().dirty and len(s().commits) > 3
bpy.ops.wm.open_mainfile(filepath=path)

evil = os.path.join(root, "evil")
os.makedirs(evil)
with open(os.path.join(root, "secret.txt"), "w") as f:
    f.write("private")
os.symlink(os.path.join(root, "secret.txt"), os.path.join(evil, "link.block"))
target = os.path.join(root, "evil.blend")
open(os.path.join(evil, "a.block"), "wb").close()
for entries in ('[["", "x", "../secret.txt"]]', '[["", "x", "link.block"]]', "not json",
                '[["", "x", "a.block"], ["", "x", "a.block"]]'):
    with open(os.path.join(evil, blendgit.blocks.MANIFEST), "w") as f:
        f.write(entries)
    try:
        blendgit.blocks.assemble(evil, target)
        raise SystemExit("assemble accepted a bad manifest: " + entries)
    except blendgit.blocks.BlockError:
        pass
    assert not os.path.exists(target) and not os.path.exists(target + ".tmp")

victim = os.path.join(root, "victim")
os.makedirs(victim)
keep = os.path.join(victim, "keep.txt")
with open(keep, "w") as f:
    f.write("private")
trap = os.path.join(root, "trap")
os.makedirs(os.path.join(trap, "blocks", "T"))
blend = os.path.join(trap, "T.blend")
shutil.copy(path, blend)
pieces = os.path.join(trap, "blocks", "T")

blendgit.blocks.split(blend, pieces)
os.symlink(keep, blend + ".tmp")
blendgit.blocks.assemble(pieces, blend)
assert open(keep).read() == "private" and not os.path.lexists(blend + ".tmp")

piece = next(p for p in glob.glob(os.path.join(pieces, "OB", "*")))
os.remove(piece)
os.symlink(keep, piece)
blendgit.blocks.split(blend, pieces)
assert open(keep).read() == "private" and not os.path.islink(piece) and os.path.getsize(piece)

shutil.rmtree(os.path.join(trap, "blocks"))
os.symlink(victim, os.path.join(trap, "blocks"))
for action in (blendgit.blocks.split, lambda a, b: blendgit.blocks.assemble(b, a)):
    try:
        action(blend, pieces)
        raise SystemExit("a linked blocks folder was accepted")
    except blendgit.blocks.BlockError:
        pass
assert open(keep).read() == "private"
assert os.stat(blendgit.CACHE).st_mode & 0o077 == 0, "the cache must be private"

old = os.path.join(root, "Old")
os.makedirs(old)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(old, "Old.blend"), compress=False)
d = old
sh("init", "-q", "-b", "main")
with open(os.path.join(old, ".gitignore"), "w") as f:
    f.write(blendgit.GITIGNORE)
sh("add", "-A")
sh("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "from 1.0")
blendgit.refresh()
assert not s().dirty
bpy.data.objects["Cube"].location.x = 11
blendgit.commit("first with pieces")
assert "Old.blend" not in sh("ls-files").decode().splitlines()
assert "blocks/Old/manifest.json" in sh("ls-files").decode().splitlines()
assert os.path.isfile(blendgit.extract("HEAD~1")) and os.path.isfile(blendgit.extract("HEAD"))
blendgit.refresh()
s().index = 1
bpy.ops.bgit.restore()
assert bpy.data.objects["Cube"].location.x == 5, "restoring a 1.0 version must work"
bpy.data.objects["Cube"].location.x = 12
blendgit.commit("pieces again")
assert "Old.blend" not in sh("ls-files").decode().splitlines()

d = os.path.dirname(path)
bpy.ops.wm.open_mainfile(filepath=path)
assert os.path.isfile(blendgit.extract(sha1))
assert not os.path.exists(os.path.join(d, "My Car.blend1")) or "My Car.blend1" not in sh("ls-files").decode()
print("BLENDGIT TESTS PASSED")
