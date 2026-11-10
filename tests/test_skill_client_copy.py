import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import sync_skill_client  # noqa: E402

SKILL_DIR = REPO / "skills" / "zotero-script-bridge"
FIX = "run py scripts/sync_skill_client.py"


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync_skill_client.main(argv)
    return code, out.getvalue(), err.getvalue()


class SkillClientCopyTest(unittest.TestCase):
    def test_skill_copy_is_byte_identical_to_the_client(self):
        client = (REPO / "client" / "zotero_script_bridge.py").read_bytes()
        copy = (SKILL_DIR / "zotero_script_bridge.py").read_bytes()
        self.assertEqual(client, copy, "the skill's copy differs from the client: " + FIX)

    def test_skill_folder_holds_only_skill_md_and_the_client(self):
        names = sorted(p.name for p in SKILL_DIR.iterdir() if p.name != "__pycache__")
        self.assertEqual(names, ["SKILL.md", "zotero_script_bridge.py"])

    def test_skill_md_names_the_copy_beside_it(self):
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("zotero_script_bridge.py --ping", text)
        self.assertNotIn("python client/zotero_script_bridge.py", text)


class SyncScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = self.tmp / "client" / "zotero_script_bridge.py"
        self.dest = self.tmp / "skills" / "zotero-script-bridge" / "zotero_script_bridge.py"
        self.source.parent.mkdir()
        # Mixed line endings: a text-mode copy would change them.
        self.payload = b"print('a')\r\nprint('b')\n"
        self.source.write_bytes(self.payload)
        for name, value in (("SOURCE", self.source), ("DEST", self.dest), ("ROOT", self.tmp)):
            patcher = mock.patch.object(sync_skill_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sync_copies_the_bytes_unchanged_and_creates_the_folder(self):
        sync_skill_client.sync()
        self.assertEqual(self.dest.read_bytes(), self.payload)

    def test_sync_raises_when_the_source_is_missing(self):
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            sync_skill_client.sync()

    def test_is_in_sync_true_for_equal_files(self):
        sync_skill_client.sync()
        self.assertTrue(sync_skill_client.is_in_sync())

    def test_is_in_sync_false_for_differing_files(self):
        sync_skill_client.sync()
        self.dest.write_bytes(self.payload + b"#\n")
        self.assertFalse(sync_skill_client.is_in_sync())

    def test_is_in_sync_false_when_the_copy_is_missing(self):
        self.assertFalse(sync_skill_client.is_in_sync())

    def test_is_in_sync_false_when_the_source_is_missing(self):
        sync_skill_client.sync()
        self.source.unlink()
        self.assertFalse(sync_skill_client.is_in_sync())

    def test_main_copies_and_prints_both_paths_relative(self):
        code, out, err = run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["client/zotero_script_bridge.py",
                                            "skills/zotero-script-bridge/zotero_script_bridge.py"])
        self.assertEqual(err, "")
        self.assertEqual(self.dest.read_bytes(), self.payload)

    def test_main_returns_1_with_one_line_when_the_source_is_missing(self):
        self.source.unlink()
        code, out, err = run_main([])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertFalse(self.dest.exists())

    def test_main_check_returns_0_for_identical_files_and_writes_nothing(self):
        sync_skill_client.sync()
        mtime = self.dest.stat().st_mtime_ns
        code, _out, _err = run_main(["--check"])
        self.assertEqual(code, 0)
        self.assertEqual(self.dest.stat().st_mtime_ns, mtime)

    def test_main_check_returns_1_for_a_differing_copy_and_does_not_fix_it(self):
        sync_skill_client.sync()
        self.dest.write_bytes(b"different\n")
        code, _out, err = run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn(FIX.replace("run ", ""), err)
        self.assertEqual(self.dest.read_bytes(), b"different\n")

    def test_main_check_returns_1_when_the_copy_is_missing(self):
        code, _out, _err = run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertFalse(self.dest.exists())

    def test_main_check_returns_1_when_the_source_is_missing(self):
        self.source.unlink()
        code, _out, _err = run_main(["--check"])
        self.assertEqual(code, 1)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = self.tmp / "client" / "zotero_script_bridge.py"
        self.dest = self.tmp / "skills" / "zotero-script-bridge" / "zotero_script_bridge.py"
        self.source.parent.mkdir()
        self.dest.parent.mkdir(parents=True)
        self.payload = b"new\r\nbytes\n"
        self.source.write_bytes(self.payload)
        self.dest.write_bytes(b"old bytes\n")

    def test_sync_keeps_the_old_bytes_and_leaves_no_temporary_file_when_the_replace_fails(self):
        with mock.patch.object(sync_skill_client.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sync_skill_client.sync(self.source, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"old bytes\n")
        self.assertEqual([p.name for p in self.dest.parent.iterdir()], [self.dest.name])

    def test_sync_removes_the_temporary_file_when_the_write_fails(self):
        with mock.patch.object(sync_skill_client.os, "chmod", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                sync_skill_client.sync(self.source, self.dest)
        self.assertEqual(self.dest.read_bytes(), b"old bytes\n")
        self.assertEqual([p.name for p in self.dest.parent.iterdir()], [self.dest.name])

    def test_sync_replaces_from_a_temporary_file_in_the_destination_folder(self):
        real_replace = os.replace
        with mock.patch.object(sync_skill_client.os, "replace",
                               side_effect=real_replace) as replace:
            sync_skill_client.sync(self.source, self.dest)
        replace.assert_called_once()
        origin, target = replace.call_args.args
        self.assertEqual(Path(origin).parent, self.dest.parent)
        self.assertNotEqual(Path(origin), self.dest)
        self.assertEqual(Path(target), self.dest)
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertEqual([p.name for p in self.dest.parent.iterdir()], [self.dest.name])


class SymlinkRefusalTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.source = self.tmp / "client" / "zotero_script_bridge.py"
        self.dest = self.tmp / "skills" / "zotero-script-bridge" / "zotero_script_bridge.py"
        self.target = self.tmp / "elsewhere.py"
        self.source.parent.mkdir()
        self.dest.parent.mkdir(parents=True)
        self.target.write_bytes(b"outside\n")
        for name, value in (("SOURCE", self.source), ("DEST", self.dest), ("ROOT", self.tmp)):
            patcher = mock.patch.object(sync_skill_client, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_link(self, link):
        # Creating a symlink on Windows needs Developer Mode or admin rights.
        try:
            os.symlink(self.target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create a symlink here: " + str(exc))

    def report_as_symlink(self, linked):
        # Stands in for a real link where the machine cannot make one, so the
        # refusal logic still runs; the tests above use real links when they can.
        real = Path.is_symlink
        patcher = mock.patch.object(
            Path, "is_symlink", autospec=True,
            side_effect=lambda path: path == linked or real(path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_main_refuses_a_source_reported_as_a_symlink_in_both_modes(self):
        self.source.write_bytes(b"client\n")
        self.dest.write_bytes(b"client\n")
        self.report_as_symlink(self.source)
        for argv in ([], ["--check"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err.strip().splitlines(), [
                    "error: client/zotero_script_bridge.py is a symbolic link;"
                    " refusing to use it"])
        self.assertEqual(self.dest.read_bytes(), b"client\n")

    def test_main_refuses_a_destination_reported_as_a_symlink_in_both_modes(self):
        self.source.write_bytes(b"client\n")
        self.dest.write_bytes(b"old\n")
        self.report_as_symlink(self.dest)
        for argv in ([], ["--check"]):
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(err.strip().splitlines(), [
                    "error: skills/zotero-script-bridge/zotero_script_bridge.py is a"
                    " symbolic link; refusing to use it"])
        self.assertEqual(self.dest.read_bytes(), b"old\n")

    def test_find_symlink_returns_none_for_two_plain_files(self):
        self.source.write_bytes(b"a\n")
        self.dest.write_bytes(b"a\n")
        self.assertIsNone(sync_skill_client.find_symlink())

    def test_find_symlink_names_a_symlinked_source_before_a_symlinked_destination(self):
        self.make_link(self.source)
        self.make_link(self.dest)
        self.assertEqual(sync_skill_client.find_symlink(), self.source)

    def test_main_copy_refuses_a_symlinked_source_and_writes_nothing(self):
        self.make_link(self.source)
        code, out, err = run_main([])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip().splitlines(), [
            "error: client/zotero_script_bridge.py is a symbolic link; refusing to use it"])
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.target.read_bytes(), b"outside\n")

    def test_main_check_refuses_a_symlinked_source(self):
        self.make_link(self.source)
        self.dest.write_bytes(b"outside\n")
        code, _out, err = run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("client/zotero_script_bridge.py is a symbolic link", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_main_copy_refuses_a_symlinked_destination_and_writes_nothing(self):
        self.source.write_bytes(b"client\n")
        self.make_link(self.dest)
        code, out, err = run_main([])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip().splitlines(), [
            "error: skills/zotero-script-bridge/zotero_script_bridge.py is a symbolic link;"
            " refusing to use it"])
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(self.target.read_bytes(), b"outside\n")

    def test_main_check_refuses_a_symlinked_destination_even_with_equal_bytes(self):
        self.source.write_bytes(b"outside\n")
        self.make_link(self.dest)
        code, out, err = run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("is a symbolic link", err)


class RealFilesTest(unittest.TestCase):
    def test_main_check_passes_on_the_real_files(self):
        code, _out, err = run_main(["--check"])
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
