"""Copy the client into the skill folder so `npx skills add` ships it beside SKILL.md.

    sync_skill_client.py [--check]

Copies client/zotero_script_bridge.py to skills/zotero-script-bridge/ as bytes
and prints both paths relative to the repository root. The copy is written to a
temporary file in the skill folder and renamed over the old one, so an
interrupted run leaves the old copy intact. With --check nothing is written: it
exits 1 if the two files differ or the copy is missing.

Exit codes: 0 copied (or identical, with --check); 1 the source is missing,
the source or the destination is a symbolic link (nothing is read or written
through it), or --check found a difference.
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "client" / "zotero_script_bridge.py"
DEST = ROOT / "skills" / "zotero-script-bridge" / "zotero_script_bridge.py"


def _relative(path):
    # Printed paths stay repository-relative so the output holds no machine path.
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def find_symlink(source=None, dest=None):
    """Return the first of source and dest that is a symbolic link, or None."""
    source = SOURCE if source is None else source
    dest = DEST if dest is None else dest
    for path in (source, dest):
        if path.is_symlink():
            return path
    return None


def is_in_sync(source=None, dest=None):
    """True when both files exist and hold the same bytes."""
    source = SOURCE if source is None else source
    dest = DEST if dest is None else dest
    if not source.is_file() or not dest.is_file():
        return False
    return source.read_bytes() == dest.read_bytes()


def sync(source=None, dest=None):
    """Copy source to dest as bytes. Raises FileNotFoundError if source is missing."""
    source = SOURCE if source is None else source
    dest = DEST if dest is None else dest
    # Bytes, never text mode: a text copy would rewrite line endings and break
    # the byte-identity the unit test checks.
    data = source.read_bytes()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # The temporary file sits in the destination's own folder so the rename
    # stays on one volume, where os.replace is atomic.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".sync-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp makes the file private (0600); a copy that others install
        # from should have the usual mode.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if the copy differs from the client")
    args = parser.parse_args(argv)

    # Before any read or write: a link could point outside the repository.
    link = find_symlink()
    if link is not None:
        print("error: " + _relative(link) + " is a symbolic link; refusing to use it",
              file=sys.stderr)
        return 1
    if not SOURCE.is_file():
        print("error: " + _relative(SOURCE) + " is missing", file=sys.stderr)
        return 1
    if args.check:
        if is_in_sync():
            print(_relative(SOURCE) + " and " + _relative(DEST) + " are identical")
            return 0
        print(_relative(DEST) + " differs from " + _relative(SOURCE)
              + "; run py scripts/sync_skill_client.py", file=sys.stderr)
        return 1
    sync()
    print(_relative(SOURCE))
    print(_relative(DEST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
