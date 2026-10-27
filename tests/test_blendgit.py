# Run: blender -b --factory-startup --python-exit-code 1 --python tests/test_blendgit.py
import os
import subprocess
import sys
import tempfile

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import blendgit  # noqa: E402

blendgit.register()
root = tempfile.mkdtemp()
d = os.path.join(root, "My Car")
path = os.path.join(d, "My Car.blend")

# unsaved scene using two different textures with the same name from outside folders
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
assert open(os.path.join(d, ".gitignore")).read() == blendgit.GITIGNORE
assert sorted(i.filepath for i in bpy.data.images if i.filepath) == ["//textures/1/wood.png", "//textures/wood.png"]
tracked = sh("ls-files").decode().splitlines()
assert {"textures/wood.png", "textures/1/wood.png", "My Car.blend"} <= set(tracked), tracked
assert os.path.isfile(os.path.join(root, "elsewhere", "a", "wood.png")), "originals must be left in place"

bpy.data.objects["Cube"].location.x = 5
s().message = "moved cube"
bpy.ops.bgit.commit()
assert [c.message for c in s().commits] == ["moved cube", "Initial version"]

s().new_branch = "experiment"
bpy.ops.bgit.branch(from_selected=False)
assert s().branch == "experiment"

s().index = 1  # the initial version
bpy.ops.bgit.restore()
assert open(path, "rb").read() == sh("show", f"{sha1}:My Car.blend"), "restore is not byte-exact"
assert bpy.data.objects["Cube"].location.x == 0
assert len(s().commits) == 3 and s().commits[0].message.startswith("Restore to")

s().target = "main"
bpy.ops.bgit.switch()
assert s().branch == "main" and bpy.data.objects["Cube"].location.x == 5

assert os.path.isfile(blendgit.extract(sha1))
assert not os.path.exists(os.path.join(d, "My Car.blend1")) or "My Car.blend1" not in sh("ls-files").decode()
print("BLENDGIT TESTS PASSED")
