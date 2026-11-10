"""Build dist/zotero-script-bridge-<version>.xpi from plugin/.

    build_xpi.py [--plugin-dir DIR] [--out-dir DIR]

Prints the output path and its SHA-256. Entries are stored, not compressed, so
the bytes depend only on the input files, not on the zlib the interpreter was
built with: the same tree gives the same bytes on any machine. When the
manifest's update_url is the expected one, also writes updates.json beside the
.xpi (the update manifest an installed copy fetches, naming this build's URL
and hash) and prints its path as a third line. A manifest without that exact
update_url is refused. Exit codes: 0 built; 1 build refused or failed
(unexpected, missing, linked or malformed files, a missing or unexpected
update_url, or a file-system error); 2 bad arguments.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A whitelist rather than "everything under plugin/": an editor backup or a
# scratch note dropped there would otherwise ship inside the add-on. A new
# plugin file must be added here on purpose, and the build fails until it is.
EXPECTED_FILES = ("bootstrap.js", "manifest.json")

# Names and flags the archive would otherwise take from the build machine.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_MODE = 0o644 << 16
UNIX = 3  # ZipInfo.create_system defaults to 0 on Windows and 3 elsewhere

# The version becomes part of a file name, so it must not carry a path.
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")

# A fork changes both EXPECTED_ID and EXPECTED_UPDATE_URL, and nothing else.
EXPECTED_ID = "zotero-script-bridge@zotero-script-bridge"

# The one update_url this build accepts, compared whole. A pattern with an
# open owner or repository part let through URLs that look like this
# repository but resolve elsewhere once a browser-style parser has dropped
# backslashes, dot segments, tabs and newlines. Any other URL would be packaged
# silently into every installed copy, which would then take its updates from
# somewhere else. The link to the versioned .xpi is built from this constant,
# never from the manifest.
EXPECTED_UPDATE_URL = (
    "https://github.com/miguelppais/zotero-script-bridge/releases/latest/download/updates.json"
)
UPDATE_URL_TAIL = "/latest/download/updates.json"
UPDATES_NAME = "updates.json"

# Windows marks symlinks, junctions and other redirecting entries with this
# attribute; the constant is missing from older `stat` modules.
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class BuildError(Exception):
    pass


def is_link(path):
    """True for a symlink, or on Windows for any reparse point (a junction
    looks like a plain folder to older Pythons)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def check_out_dir(plugin_dir, out_dir):
    # A linked output folder would send the temporary file and the archive to
    # wherever it points. The check stops at the folder the output shares with
    # the plugin folder: links above it belong to the machine's own layout (a
    # temporary folder under a symlink on macOS, for instance), not to the
    # tree being built. The paths are not normalised, so a `..` after a link
    # cannot hide the link.
    out, plugin = Path(out_dir).absolute(), Path(plugin_dir).absolute()
    try:
        shared = Path(os.path.commonpath([out, plugin]))
    except ValueError:
        shared = None  # different drives: check the whole path
    for folder in (out, *out.parents):
        if folder == shared:
            break
        if is_link(folder):
            raise BuildError(f"output folder is, or sits under, a link: {str(folder)!r}")


def check_plugin_dir(plugin_dir):
    # A link could point an input at a file outside plugin/, and the archive
    # would carry that file's content under a trusted name.
    if is_link(plugin_dir):
        raise BuildError(f"plugin folder is a symlink: {str(plugin_dir)!r}")
    if not plugin_dir.is_dir():
        raise BuildError(f"plugin folder not found: {str(plugin_dir)!r}")
    entries = list(plugin_dir.rglob("*"))
    links = sorted(p.relative_to(plugin_dir).as_posix() for p in entries if is_link(p))
    if links:
        raise BuildError("symlinks in plugin folder: " + ", ".join(map(repr, links)))
    present = {p.relative_to(plugin_dir).as_posix() for p in entries}
    extra = sorted(present - set(EXPECTED_FILES))
    missing = sorted(set(EXPECTED_FILES) - present)
    if extra:
        raise BuildError("unexpected entries in plugin folder: " + ", ".join(map(repr, extra)))
    if missing:
        raise BuildError("missing files in plugin folder: " + ", ".join(map(repr, missing)))


def read_input(plugin_dir, name):
    """Read one expected file, once, through a handle opened without
    following a link where the platform allows it.

    The file is not looked at again by name after this point. On POSIX,
    O_NOFOLLOW, the regular-file check and the link count are meant to close
    the window between the folder scan and the open (inferred, not run on
    POSIX). Windows has no O_NOFOLLOW and its open follows a link, so the
    handle describes the target; the only link guard there is the earlier
    scan, which leaves a window between the scan and the open.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(plugin_dir / name, flags)
    except OSError as exc:
        raise BuildError(f"cannot open {name!r}: {exc!r}")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BuildError(f"not a regular file: {name!r}")
        # On Windows the handle describes what the open followed, so this
        # probably cannot see a link there (inferred, not testable here); it
        # stays as a cheap extra check.
        if getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
            raise BuildError(f"symlink in plugin folder: {name!r}")
        # Another name for the same file means its content can change from
        # outside plugin/.
        if info.st_nlink > 1:
            raise BuildError(f"file has more than one hard link: {name!r}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise BuildError(f"cannot read {name!r}: {exc!r}")
    finally:
        os.close(fd)


def check_manifest(data):
    """Return (version, the manifest's zotero block), after checking its
    add-on identity and its update_url."""
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise BuildError(f"cannot read manifest.json: {exc!r}")
    if not isinstance(manifest, dict):
        manifest = {}
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise BuildError(f"manifest.json has no usable version: {version!r}")
    # The ID is permanent once released: installed copies update by it.
    applications = manifest.get("applications")
    zotero = applications.get("zotero") if isinstance(applications, dict) else None
    if not isinstance(zotero, dict):
        zotero = {}
    if zotero.get("id") != EXPECTED_ID:
        raise BuildError(
            f"manifest.json add-on id is {zotero.get('id')!r}, expected {EXPECTED_ID!r}"
        )
    for key in ("strict_min_version", "strict_max_version"):
        value = zotero.get(key)
        if not isinstance(value, str) or not value.strip():
            raise BuildError(f"manifest.json has no usable {key}: {value!r}")
    # Required, not optional: a copy built without it would never learn of an
    # update, security fixes included.
    update_url = zotero.get("update_url")
    if update_url != EXPECTED_UPDATE_URL:
        raise BuildError(
            f"manifest.json update_url is {update_url!r}, expected {EXPECTED_UPDATE_URL!r}"
        )
    return version, zotero


def render_updates(version, zotero, digest):
    """Return the bytes of updates.json for this build.

    The entry's version, ID and version range are copied from the manifest and
    the hash is the archive's own: Zotero refuses an update whose hash or
    version differs from the downloaded file's. The link comes from the
    expected update_url constant only.
    """
    releases = EXPECTED_UPDATE_URL.removesuffix(UPDATE_URL_TAIL)
    link = f"{releases}/download/v{version}/zotero-script-bridge-{version}.xpi"
    document = {
        "addons": {
            zotero["id"]: {
                "updates": [
                    {
                        "version": version,
                        "update_link": link,
                        "update_hash": "sha256:" + digest,
                        "applications": {
                            "zotero": {
                                "strict_min_version": zotero["strict_min_version"],
                                "strict_max_version": zotero["strict_max_version"],
                            }
                        },
                    }
                ]
            }
        }
    }
    # Sorted keys and fixed indent so two builds give the same bytes; encoded
    # here so the file is LF on Windows too.
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_archive(fileobj, files):
    with zipfile.ZipFile(fileobj, "w") as archive:
        for name in sorted(files):
            # Git's autocrlf makes the same commit read as CRLF on one
            # checkout and LF on another; the add-on is the same either way.
            # Text files only: a binary file added to EXPECTED_FILES must
            # bypass this replace, or it would be corrupted.
            data = files[name].replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.create_system = UNIX
            info.external_attr = FIXED_MODE
            # Stored, not deflated: compressed bytes vary with the zlib build.
            archive.writestr(info, data, zipfile.ZIP_STORED)


def set_mode(fd, path):
    # mkstemp creates the file 0600; give it the mode a normal write would
    # have had. The umask can only be read by setting it. On Windows chmod
    # only toggles read-only, so this changes nothing there. By handle where
    # the platform allows it, so a path swapped for a link is not followed.
    umask = os.umask(0)
    os.umask(umask)
    mode = 0o644 & ~umask
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(fd, mode)
    else:
        os.chmod(path, mode)


def write_file(out_dir, out, data):
    """Write data to out through a temporary file in the same folder, so a
    failed write never leaves a half-written file where a good one was."""
    if is_link(out):
        raise BuildError(f"output path is a symlink: {str(out)!r}")
    fd, tmp_name = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            set_mode(handle.fileno(), tmp_name)
        os.replace(tmp_name, out)
    finally:
        try:
            os.remove(tmp_name)
        except OSError:
            pass


def build_archive(plugin_dir, out_dir):
    check_out_dir(plugin_dir, out_dir)
    check_plugin_dir(plugin_dir)
    files = {name: read_input(plugin_dir, name) for name in EXPECTED_FILES}
    version, zotero = check_manifest(files["manifest.json"])
    out = out_dir / f"zotero-script-bridge-{version}.xpi"
    # os.replace swaps the name rather than writing through it, so this only
    # refuses early; the folder check above is what keeps the write inside.
    if is_link(out):
        raise BuildError(f"output path is a symlink: {str(out)!r}")
    updates_out = out_dir / UPDATES_NAME
    if is_link(updates_out):
        raise BuildError(f"output path is a symlink: {str(updates_out)!r}")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Written beside the target and moved into place, so a failed build never
    # leaves a half-written .xpi where a good one was. The handle from
    # mkstemp is kept: reopening the temporary file by name would follow a
    # link swapped in under it.
    fd, tmp_name = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w+b") as handle:
            write_archive(handle, files)
            handle.flush()
            set_mode(handle.fileno(), tmp_name)
            handle.seek(0)
            digest = hashlib.sha256(handle.read()).hexdigest()
        # The old updates.json names the old .xpi's hash. It goes before the
        # new .xpi is swapped in, so a failure from here on leaves no
        # updates.json rather than one that does not match the .xpi beside it.
        try:
            os.remove(updates_out)
        except FileNotFoundError:
            pass
        os.replace(tmp_name, out)
    finally:
        # Gone already after a successful replace; a failing cleanup must not
        # replace the error that brought us here.
        try:
            os.remove(tmp_name)
        except OSError:
            pass
    updates = render_updates(version, zotero, digest)
    write_file(out_dir, updates_out, updates)
    return out, digest, updates_out


def build(plugin_dir, out_dir):
    """Return (path, sha256 hex, updates.json path) of the build."""
    try:
        return build_archive(Path(plugin_dir), Path(out_dir))
    except (OSError, RecursionError) as exc:
        raise BuildError(f"build failed: {exc!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the Zotero Script Bridge .xpi.")
    parser.add_argument("--plugin-dir", default=ROOT / "plugin")
    parser.add_argument("--out-dir", default=ROOT / "dist")
    args = parser.parse_args(argv)
    try:
        path, digest, updates_path = build(args.plugin_dir, args.out_dir)
    except BuildError as exc:
        print(f"build_xpi: {exc}", file=sys.stderr)
        return 1
    print(path)
    print(f"sha256 {digest}")
    print(updates_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
