"""Split a .blend into one file per datablock and rebuild it. Uses only the standard library."""

import hashlib
import json
import os
import re
import shutil
import struct
from urllib.parse import quote

MANIFEST = "manifest.json"

BLOCK_HEAD = struct.Struct("<4siQqq")
FILE_HEADER = re.compile(rb"BLENDER17-01v\d{4}")

VOLATILE_ID_FIELDS = {"session_uid", "recalc", "recalc_up_to_undo_push", "recalc_after_undo_push"}
VOLATILE_STRUCT_FIELDS = {"CurveProfile": "changed_timestamp"}


## Raised when a .blend or its pieces cannot be read, split or rebuilt.
class BlockError(Exception):
    pass


##
# Read one NUL-terminated string table from the DNA block.
#
# @param data the DNA block
# @param pos offset of the table
# @param tag the 4-byte tag the table must start with
# @return the strings, and the offset of the next section
# @throws BlockError if the tag does not match
def _read_names(data, pos, tag):
    if data[pos:pos + 4] != tag:
        raise BlockError("The .blend has an unexpected DNA layout")
    (count,) = struct.unpack_from("<i", data, pos + 4)
    pos += 8
    names = []
    for _ in range(count):
        end = data.index(0, pos)
        names.append(data[pos:end].decode())
        pos = end + 1
    return names, (pos + 3) & ~3


##
# Compute the fields of one DNA struct.
#
# @param members flat list of (type index, name index) pairs
# @param names the DNA name table
# @param types the DNA type table
# @param sizes the DNA type sizes
# @return generator of (field name, offset, size)
def _member_layout(members, names, types, sizes):
    offset = 0
    for member_type, member_name in zip(members[::2], members[1::2]):
        name = names[member_name]
        size = 8 if name.startswith(("*", "(*")) else sizes[member_type]
        for length in re.findall(r"\[(\d+)\]", name):
            size *= int(length)
        yield re.sub(r"\[.*|\W", "", name), offset, size
        offset += size


##
# Find the runtime-only fields, which change between saves even if the model did not.
#
# @param dna the DNA block
# @return ({struct index: (struct size, [(offset, size)])}, (ID name offset, ID name size))
def _volatile_layout(dna):
    names, pos = _read_names(dna, 4, b"NAME")
    types, pos = _read_names(dna, pos, b"TYPE")
    sizes = struct.unpack_from(f"<{len(types)}H", dna, pos + 4)
    pos = (pos + 4 + 2 * len(types) + 3) & ~3
    (count,) = struct.unpack_from("<i", dna, pos + 4)
    pos += 8

    structs = []
    for _ in range(count):
        type_index, member_count = struct.unpack_from("<hh", dna, pos)
        structs.append((type_index, struct.unpack_from(f"<{member_count * 2}h", dna, pos + 4)))
        pos += 4 + member_count * 4

    id_members = next(members for type_index, members in structs if types[type_index] == "ID")
    id_fields = {name: (offset, size) for name, offset, size in _member_layout(id_members, names, types, sizes)}
    id_volatile = [id_fields[field] for field in VOLATILE_ID_FIELDS if field in id_fields]

    volatile = {}
    for index, (type_index, members) in enumerate(structs):
        spans = []
        if members and types[members[0]] == "ID" and names[members[1]] == "id":
            spans += id_volatile
        wanted = VOLATILE_STRUCT_FIELDS.get(types[type_index])
        if wanted:
            spans += [(offset, size) for name, offset, size in _member_layout(members, names, types, sizes)
                      if name == wanted]
        if spans:
            volatile[index] = (sizes[type_index], spans)
    return volatile, id_fields["name"]


##
# Iterate over the blocks of a .blend.
#
# @param data the file contents
# @return generator of (code, struct index, element count, start, body start, end)
# @throws BlockError if the file is compressed or older than Blender 5.0
def _walk(data):
    match = FILE_HEADER.match(data)
    if not match:
        raise BlockError("Only uncompressed .blend files from Blender 5.0 or newer can be split")
    pos = match.end()
    while pos < len(data):
        code, sdna, _, length, count = BLOCK_HEAD.unpack_from(data, pos)
        body = pos + BLOCK_HEAD.size
        yield code, sdna, count, pos, body, body + length
        pos = body + length


##
# Tell whether a block starts a datablock. Linked data placeholders stay with their library.
#
# @param code the 4-byte block code
# @return True if the block starts a new piece
def _is_datablock(code):
    return code[2:] == b"\0\0" and code != b"ID\0\0"


##
# Cut a .blend into pieces, blanking runtime fields so unchanged datablocks stay identical.
#
# The whole file is held in memory.
#
# @param path path of the .blend
# @return generator of (type code, name, bytes), in file order
# @throws BlockError if the file has no DNA block
def _cut(path):
    with open(path, "rb") as f:
        data = bytearray(f.read())
    blocks = list(_walk(data))

    dna = next((b for b in blocks if b[0] == b"DNA1"), None)
    if dna is None:
        raise BlockError("The .blend has no DNA block")
    volatile, (name_offset, name_size) = _volatile_layout(bytes(data[dna[4]:dna[5]]))

    for _, sdna, count, _, body, _ in blocks:
        if sdna in volatile:
            struct_size, spans = volatile[sdna]
            for element in range(count):
                for offset, size in spans:
                    start = body + element * struct_size + offset
                    data[start:start + size] = bytes(size)

    start, code, name = 0, "", "header"
    for block_code, _, _, block_start, body, _ in blocks:
        if _is_datablock(block_code) or block_code == b"DNA1":
            yield code, name, bytes(data[start:block_start])
            start = block_start
            if block_code == b"DNA1":
                code, name = "", "footer"
            else:
                code = block_code[:2].decode()
                raw = data[body + name_offset:body + name_offset + name_size]
                name = raw[2:raw.index(0)].decode(errors="replace")
    yield code, name, bytes(data[start:])


##
# Build the path of a piece. The hash keeps it unique on case-insensitive disks.
#
# @param code the datablock type code
# @param name the datablock name
# @return path relative to the pieces folder
def _file_name(code, name):
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{code or '_'}/{quote(name, safe=' ')[:80]}-{digest}.block"


##
# Write a file, leaving it untouched if it already has this content.
#
# @param path path of the file
# @param content the bytes to write
def _write_if_changed(path, content):
    try:
        if os.path.getsize(path) == len(content):
            with open(path, "rb") as f:
                if f.read() == content:
                    return
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


##
# Refuse a pieces folder that a symlink moves outside the project.
#
# @param folder the pieces folder
# @param blend_path path of the .blend at the project root
# @throws BlockError if the folder resolves somewhere else
def _check_folder(folder, blend_path):
    project = os.path.dirname(blend_path)
    if os.path.realpath(folder) != os.path.join(os.path.realpath(project), os.path.relpath(folder, project)):
        raise BlockError(f"{folder} is a link to somewhere else, so BlendGit won't touch it")


##
# Delete the symlinks among the pieces, leaving their targets alone.
#
# @param folder the pieces folder
def _remove_links(folder):
    for root, folders, files in os.walk(folder):
        for name in folders + files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                os.remove(path)


##
# Write the pieces of a .blend, touching only the changed ones and deleting stale ones.
#
# @param blend_path path of the .blend
# @param folder the pieces folder
# @throws BlockError if the file cannot be split or the folder is unsafe
def split(blend_path, folder):
    _check_folder(folder, blend_path)
    _remove_links(folder)
    manifest = []
    for code, name, content in _cut(blend_path):
        relative = _file_name(code, name)
        _write_if_changed(os.path.join(folder, relative), content)
        manifest.append([code, name, relative])

    _write_if_changed(os.path.join(folder, MANIFEST),
                      json.dumps(manifest, indent=0, ensure_ascii=False).encode())

    wanted = {os.path.normpath(entry[2]) for entry in manifest} | {MANIFEST}
    for root, _, files in os.walk(folder):
        for file in files:
            path = os.path.join(root, file)
            if os.path.normpath(os.path.relpath(path, folder)) not in wanted:
                os.remove(path)


##
# Parse a manifest.
#
# @param text the manifest contents
# @return {piece path: (type code, name)}
def read_manifest(text):
    return {relative: (code, name) for code, name, relative in json.loads(text)}


##
# Rebuild a .blend from its pieces. The old file survives if this fails halfway.
#
# Manifests may come from other people, so paths leaving the folder and repeated pieces are rejected.
#
# @param folder the pieces folder
# @param blend_path path of the .blend to write
# @throws BlockError if the manifest is invalid or a piece cannot be read
def assemble(folder, blend_path):
    temp = blend_path + ".tmp"
    try:
        _check_folder(folder, blend_path)
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            pieces = json.load(f)
        if len({entry[2] for entry in pieces}) != len(pieces):
            raise BlockError("The manifest lists the same piece twice")
        base = os.path.realpath(folder)
        if os.path.lexists(temp):
            os.remove(temp)
        with open(temp, "xb") as out:
            for _, _, relative in pieces:
                path = os.path.realpath(os.path.join(base, relative))
                if os.path.commonpath([base, path]) != base:
                    raise BlockError(f"The manifest points outside the project: {relative}")
                with open(path, "rb") as piece:
                    shutil.copyfileobj(piece, out)
        os.replace(temp, blend_path)
    except (OSError, ValueError, TypeError, BlockError) as e:
        if os.path.lexists(temp):
            os.remove(temp)
        raise BlockError(f"Could not rebuild the .blend: {e}")
