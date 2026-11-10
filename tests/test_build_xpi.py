import contextlib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_xpi  # noqa: E402


def make_manifest(version="1.2.3", **zotero):
    fields = {
        "id": build_xpi.EXPECTED_ID,
        "strict_min_version": "6.999",
        "strict_max_version": "10.*",
        "update_url": build_xpi.EXPECTED_UPDATE_URL,
    }
    fields.update(zotero)
    # None means "leave the key out".
    fields = {k: v for k, v in fields.items() if v is not None}
    return {"version": version, "applications": {"zotero": fields}}


def make_plugin(folder, version="1.2.3", bootstrap=b"var a = 1;\n"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(make_manifest(version)), encoding="utf-8")
    (folder / "bootstrap.js").write_bytes(bootstrap)
    return folder


def make_symlink(link, target):
    # Creating one on Windows needs Developer Mode or an elevated shell.
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        raise unittest.SkipTest(f"cannot create a symlink here: {exc}")


def run_main(plugin_dir, out_dir):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build_xpi.main(["--plugin-dir", str(plugin_dir), "--out-dir", str(out_dir)])
    return code, out.getvalue(), err.getvalue()


class BuildXpiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plugin = make_plugin(self.tmp / "plugin")

    def xpi(self, out_name="out"):
        return self.tmp / out_name / "zotero-script-bridge-1.2.3.xpi"

    def test_main_two_builds_are_byte_identical(self):
        self.assertEqual(run_main(self.plugin, self.tmp / "a")[0], 0)
        self.assertEqual(run_main(self.plugin, self.tmp / "b")[0], 0)
        self.assertEqual(self.xpi("a").read_bytes(), self.xpi("b").read_bytes())

    def test_main_rebuild_into_same_folder_is_byte_identical(self):
        run_main(self.plugin, self.tmp / "out")
        first = self.xpi().read_bytes()
        run_main(self.plugin, self.tmp / "out")
        self.assertEqual(first, self.xpi().read_bytes())

    def test_main_file_modification_times_do_not_change_bytes(self):
        run_main(self.plugin, self.tmp / "a")
        for name in build_xpi.EXPECTED_FILES:
            os.utime(self.plugin / name, (1_000_000_000, 1_000_000_000))
        run_main(self.plugin, self.tmp / "b")
        self.assertEqual(self.xpi("a").read_bytes(), self.xpi("b").read_bytes())

    def test_main_line_endings_do_not_change_bytes(self):
        lf = make_plugin(self.tmp / "lf", bootstrap=b"a\nb\n")
        crlf = make_plugin(self.tmp / "crlf", bootstrap=b"a\r\nb\r\n")
        run_main(lf, self.tmp / "a")
        run_main(crlf, self.tmp / "b")
        self.assertEqual(self.xpi("a").read_bytes(), self.xpi("b").read_bytes())

    def test_main_changed_content_changes_bytes(self):
        run_main(self.plugin, self.tmp / "a")
        (self.plugin / "bootstrap.js").write_bytes(b"var a = 2;\n")
        run_main(self.plugin, self.tmp / "b")
        self.assertNotEqual(self.xpi("a").read_bytes(), self.xpi("b").read_bytes())

    def test_main_archive_has_manifest_at_root_and_fixed_entry_fields(self):
        run_main(self.plugin, self.tmp / "out")
        with zipfile.ZipFile(self.xpi()) as archive:
            self.assertIsNone(archive.testzip())
            infos = archive.infolist()
            names = [i.filename for i in infos]
            self.assertIn("manifest.json", names)
            self.assertEqual(names, sorted(names))
            self.assertFalse([n for n in names if n.endswith("/")])
            for info in infos:
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.external_attr, 0o644 << 16)
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(info.file_size, info.compress_size)
                self.assertEqual(info.extra, b"")
            self.assertEqual(json.loads(archive.read("manifest.json"))["version"], "1.2.3")

    def test_main_prints_path_and_sha256_of_the_file(self):
        code, out, _ = run_main(self.plugin, self.tmp / "out")
        digest = hashlib.sha256(self.xpi().read_bytes()).hexdigest()
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [str(self.xpi()), "sha256 " + digest, str(self.tmp / "out" / "updates.json")],
        )

    def test_main_refuses_unexpected_file(self):
        (self.plugin / "notes.txt").write_text("x")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("notes.txt", err)
        self.assertFalse(self.xpi().exists())

    def test_main_refuses_unexpected_subfolder(self):
        (self.plugin / "locale").mkdir()
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("locale", err)

    def test_main_refuses_missing_file(self):
        (self.plugin / "bootstrap.js").unlink()
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("bootstrap.js", err)

    def test_main_refuses_missing_plugin_folder(self):
        code, _, err = run_main(self.tmp / "nope", self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_main_refuses_unusable_version(self):
        for bad in ("../evil", "", "1 2", None, 3):
            with self.subTest(version=bad):
                (self.plugin / "manifest.json").write_text(json.dumps(make_manifest(bad)))
                code, _, err = run_main(self.plugin, self.tmp / "out")
                self.assertEqual(code, 1)
                self.assertIn("version", err)
        self.assertFalse((self.tmp / "out").exists())

    def write_manifest(self, manifest):
        (self.plugin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_main_refuses_wrong_addon_id(self):
        self.write_manifest(make_manifest(id="other@example.org"))
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("add-on id", err)
        self.assertIn("other@example.org", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_missing_addon_id(self):
        self.write_manifest(make_manifest(id=None))
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("add-on id", err)

    def test_main_refuses_manifest_without_applications_block(self):
        for manifest in ({"version": "1.2.3"}, {"version": "1.2.3", "applications": []},
                         {"version": "1.2.3", "applications": {"zotero": "x"}}):
            with self.subTest(manifest=manifest):
                self.write_manifest(manifest)
                code, _, err = run_main(self.plugin, self.tmp / "out")
                self.assertEqual(code, 1)
                self.assertIn("add-on id", err)

    def test_main_refuses_missing_or_empty_version_range(self):
        for key in ("strict_min_version", "strict_max_version"):
            for bad in (None, "", "  ", 10):
                with self.subTest(key=key, value=bad):
                    if bad is None:
                        self.write_manifest(make_manifest(**{key: None}))
                    else:
                        self.write_manifest(make_manifest(**{key: bad}))
                    code, _, err = run_main(self.plugin, self.tmp / "out")
                    self.assertEqual(code, 1)
                    self.assertIn(key, err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_symlinked_bootstrap(self):
        real = self.tmp / "elsewhere.js"
        real.write_bytes(b"var evil = 1;\n")
        (self.plugin / "bootstrap.js").unlink()
        make_symlink(self.plugin / "bootstrap.js", real)
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertIn("bootstrap.js", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_symlinked_extra_entry(self):
        real = self.tmp / "elsewhere.txt"
        real.write_text("x")
        make_symlink(self.plugin / "notes.txt", real)
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertIn("notes.txt", err)

    def test_main_refuses_symlinked_plugin_folder(self):
        link = self.tmp / "plugin-link"
        make_symlink(link, self.plugin)
        code, _, err = run_main(link, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_symlink_at_output_path_and_leaves_target_alone(self):
        target = self.tmp / "precious.txt"
        target.write_bytes(b"keep me")
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        make_symlink(self.xpi(), target)
        code, _, err = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(target.read_bytes(), b"keep me")
        self.assertEqual(list(out_dir.glob("*.tmp")), [])

    # The tests above need symlink rights, which a plain Windows shell lacks;
    # these reach the same refusals by marking a path as a link, so the logic
    # is exercised everywhere.
    def refuse_as_link(self, link_name):
        real_is_link = build_xpi.is_link
        return mock.patch.object(
            build_xpi, "is_link", lambda p: p.name == link_name or real_is_link(p)
        )

    def test_main_refuses_an_expected_file_that_reads_as_a_link(self):
        with self.refuse_as_link("bootstrap.js"):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertIn("bootstrap.js", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_the_plugin_folder_when_it_reads_as_a_link(self):
        with self.refuse_as_link("plugin"):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_the_output_path_when_it_reads_as_a_link(self):
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        with self.refuse_as_link(self.xpi().name):
            code, _, err = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_main_failed_write_leaves_no_tmp_file_and_keeps_the_old_archive(self):
        run_main(self.plugin, self.tmp / "out")
        good = self.xpi().read_bytes()
        with mock.patch.object(build_xpi, "write_archive", side_effect=OSError("disk full")):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertEqual(list((self.tmp / "out").glob("*.tmp")), [])
        self.assertEqual(self.xpi().read_bytes(), good)

    def test_main_refused_build_leaves_no_tmp_file(self):
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        (self.plugin / "notes.txt").write_text("x")
        code, _, _ = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_main_refuses_an_output_parent_that_reads_as_a_link(self):
        with self.refuse_as_link("linkdir"):
            code, _, err = run_main(self.plugin, self.tmp / "linkdir" / "out")
        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertFalse((self.tmp / "linkdir").exists())

    def test_main_refuses_an_output_folder_that_reads_as_a_link(self):
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        with self.refuse_as_link("out"):
            code, _, err = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertIn("link", err)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_main_ignores_links_above_the_folder_shared_with_the_plugin(self):
        with self.refuse_as_link(self.tmp.name):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)

    def test_is_link_reads_the_windows_reparse_attribute(self):
        folder = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        plain = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x10)
        with mock.patch.object(build_xpi.os, "lstat", return_value=folder):
            self.assertTrue(build_xpi.is_link(self.plugin))
        with mock.patch.object(build_xpi.os, "lstat", return_value=plain):
            self.assertFalse(build_xpi.is_link(self.plugin))
        self.assertFalse(build_xpi.is_link(self.tmp / "nope"))

    def test_read_input_refuses_a_reparse_point_seen_through_the_handle(self):
        fake = types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400, st_nlink=1)
        with mock.patch.object(build_xpi.os, "fstat", return_value=fake):
            with self.assertRaises(build_xpi.BuildError):
                build_xpi.read_input(self.plugin, "bootstrap.js")

    def test_read_input_refuses_a_folder_and_a_missing_file(self):
        (self.plugin / "locale").mkdir()
        for name in ("locale", "gone.js"):
            with self.subTest(name=name):
                with self.assertRaises(build_xpi.BuildError):
                    build_xpi.read_input(self.plugin, name)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "no O_NOFOLLOW on this platform")
    def test_read_input_refuses_a_symlink_at_open_time(self):
        real = self.tmp / "elsewhere.js"
        real.write_bytes(b"x")
        make_symlink(self.plugin / "linked.js", real)
        with self.assertRaises(build_xpi.BuildError):
            build_xpi.read_input(self.plugin, "linked.js")

    def test_main_refuses_a_hard_linked_input(self):
        try:
            os.link(self.plugin / "bootstrap.js", self.tmp / "alias.js")
        except (OSError, NotImplementedError, AttributeError) as exc:
            self.skipTest(f"cannot create a hard link here: {exc}")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("hard link", err)
        self.assertIn("bootstrap.js", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_reads_each_input_once(self):
        with mock.patch.object(build_xpi, "read_input", wraps=build_xpi.read_input) as spy:
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        self.assertEqual(spy.call_count, len(build_xpi.EXPECTED_FILES))

    def test_main_archives_the_manifest_bytes_it_validated(self):
        real_check = build_xpi.check_manifest

        def check_then_swap(data):
            version = real_check(data)
            self.write_manifest(make_manifest(id="swapped@example.org"))
            return version

        with mock.patch.object(build_xpi, "check_manifest", check_then_swap):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        with zipfile.ZipFile(self.xpi()) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["applications"]["zotero"]["id"], build_xpi.EXPECTED_ID)

    def test_main_writes_through_the_open_handle_not_a_path(self):
        seen = []
        real = build_xpi.write_archive

        def spy(fileobj, files):
            seen.append(fileobj)
            return real(fileobj, files)

        with mock.patch.object(build_xpi, "write_archive", spy):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        self.assertTrue(hasattr(seen[0], "write"))
        self.assertNotIsInstance(seen[0], (str, Path))

    def test_main_prints_the_hash_of_the_bytes_written_not_of_what_is_there_later(self):
        run_main(self.plugin, self.tmp / "ref")
        expected = hashlib.sha256(self.xpi("ref").read_bytes()).hexdigest()
        real_replace = os.replace

        def swap_then_replace(src, dst):
            Path(src).write_bytes(b"swapped")
            real_replace(src, dst)

        with mock.patch.object(build_xpi.os, "replace", swap_then_replace):
            code, out, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[1], "sha256 " + expected)

    def test_set_mode_uses_the_handle_when_fchmod_exists(self):
        with mock.patch.object(os, "fchmod", create=True) as fchmod, \
                mock.patch.object(os, "chmod") as chmod:
            build_xpi.set_mode(7, "some.tmp")
        fchmod.assert_called_once()
        self.assertEqual(fchmod.call_args[0][0], 7)
        chmod.assert_not_called()

    def test_set_mode_falls_back_to_chmod_by_name_without_fchmod(self):
        with mock.patch.object(os, "fchmod", None, create=True), \
                mock.patch.object(os, "chmod") as chmod:
            build_xpi.set_mode(7, "some.tmp")
        chmod.assert_called_once()
        self.assertEqual(chmod.call_args[0][0], "some.tmp")

    def test_main_stores_entries_uncompressed_so_zlib_cannot_change_the_bytes(self):
        with mock.patch.object(zipfile.zlib, "compressobj", side_effect=AssertionError("deflate used")):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)

    def test_main_reports_an_output_folder_that_is_a_file(self):
        (self.tmp / "out").write_text("in the way")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("build_xpi:", err)

    def test_main_reports_a_scan_that_fails(self):
        with mock.patch.object(Path, "rglob", side_effect=OSError("folder vanished")):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("folder vanished", err)

    def test_main_reports_a_temp_file_that_cannot_be_made(self):
        with mock.patch.object(build_xpi.tempfile, "mkstemp", side_effect=PermissionError("denied")):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("denied", err)

    def test_main_refuses_a_manifest_nested_deeper_than_the_parser_allows(self):
        (self.plugin / "manifest.json").write_text("[" * 200_000)
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("manifest.json", err)

    def test_main_refuses_a_manifest_that_is_not_utf8(self):
        (self.plugin / "manifest.json").write_bytes(b"\xff\xfe{}")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("manifest.json", err)

    def test_main_keeps_the_real_error_when_removing_the_temp_file_fails(self):
        with mock.patch.object(build_xpi, "write_archive", side_effect=OSError("disk full")), \
                mock.patch.object(build_xpi.os, "remove", side_effect=PermissionError("locked")):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertNotIn("locked", err)

    def test_main_prints_file_names_escaped_in_error_messages(self):
        bidi = chr(0x202E)
        name = "x" + bidi + ".txt"
        try:
            (self.plugin / name).write_text("x")
        except OSError as exc:
            self.skipTest(f"cannot create that file name here: {exc}")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertNotIn(bidi, err)
        self.assertIn("\\u202e", err)

    @unittest.skipIf(os.name == "nt", "chmod only toggles read-only on Windows")
    def test_main_archive_mode_is_0644_minus_the_umask(self):
        old = os.umask(0o027)
        try:
            run_main(self.plugin, self.tmp / "out")
        finally:
            os.umask(old)
        self.assertEqual(self.xpi().stat().st_mode & 0o777, 0o640)

    def test_main_successful_build_leaves_only_the_archive_and_updates_json(self):
        run_main(self.plugin, self.tmp / "out")
        self.assertEqual(
            sorted(p.name for p in (self.tmp / "out").iterdir()),
            sorted([self.xpi().name, "updates.json"]),
        )

    def test_main_refuses_invalid_manifest_json(self):
        (self.plugin / "manifest.json").write_text("{not json")
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("manifest.json", err)

    def test_main_builds_the_real_plugin_folder(self):
        code, _, _ = run_main(REPO / "plugin", self.tmp / "out")
        version = json.loads((REPO / "plugin" / "manifest.json").read_text(encoding="utf-8"))["version"]
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "out" / f"zotero-script-bridge-{version}.xpi").is_file())


UPDATE_URL = build_xpi.EXPECTED_UPDATE_URL
OTHER_URL = "https://github.com/example-owner/example-repo/releases/latest/download/updates.json"
BACKSLASH = chr(92)


class UpdatesJsonTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plugin = make_plugin(self.tmp / "plugin")
        self.write_manifest(make_manifest())

    def write_manifest(self, manifest):
        (self.plugin / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def xpi(self, out_name="out"):
        return self.tmp / out_name / "zotero-script-bridge-1.2.3.xpi"

    def updates(self, out_name="out"):
        return self.tmp / out_name / "updates.json"

    def test_main_writes_updates_json_and_prints_its_path_third(self):
        code, out, _ = run_main(self.plugin, self.tmp / "out")
        digest = hashlib.sha256(self.xpi().read_bytes()).hexdigest()
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(), [str(self.xpi()), "sha256 " + digest, str(self.updates())]
        )
        self.assertTrue(self.updates().is_file())

    def test_main_updates_json_names_the_tag_the_xpi_and_its_hash(self):
        _, out, _ = run_main(self.plugin, self.tmp / "out")
        digest = out.splitlines()[1].split()[1]
        document = json.loads(self.updates().read_text(encoding="utf-8"))
        entries = document["addons"][build_xpi.EXPECTED_ID]["updates"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], "1.2.3")
        self.assertEqual(
            entries[0]["update_link"],
            "https://github.com/miguelppais/zotero-script-bridge/releases/download/v1.2.3/"
            "zotero-script-bridge-1.2.3.xpi",
        )
        self.assertEqual(entries[0]["update_hash"], "sha256:" + digest)

    def test_main_update_link_comes_from_the_constant_not_the_manifest(self):
        # A fork changes the constant; the manifest only has to equal it.
        self.write_manifest(make_manifest(update_url=OTHER_URL))
        with mock.patch.object(build_xpi, "EXPECTED_UPDATE_URL", OTHER_URL):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        entry = json.loads(self.updates().read_text(encoding="utf-8"))["addons"][
            build_xpi.EXPECTED_ID
        ]["updates"][0]
        self.assertEqual(
            entry["update_link"],
            "https://github.com/example-owner/example-repo/releases/download/v1.2.3/"
            "zotero-script-bridge-1.2.3.xpi",
        )

    def test_main_updates_json_copies_the_version_range_from_the_manifest(self):
        self.write_manifest(
            make_manifest(strict_min_version="7.0", strict_max_version="9.*")
        )
        run_main(self.plugin, self.tmp / "out")
        document = json.loads(self.updates().read_text(encoding="utf-8"))
        entry = document["addons"][build_xpi.EXPECTED_ID]["updates"][0]
        self.assertEqual(
            entry["applications"],
            {"zotero": {"strict_min_version": "7.0", "strict_max_version": "9.*"}},
        )

    def test_main_updates_json_is_sorted_indented_lf_with_trailing_newline(self):
        run_main(self.plugin, self.tmp / "out")
        raw = self.updates().read_bytes()
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            raw, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"}\n"))

    def test_main_two_builds_give_byte_identical_updates_json(self):
        run_main(self.plugin, self.tmp / "a")
        run_main(self.plugin, self.tmp / "b")
        self.assertEqual(self.updates("a").read_bytes(), self.updates("b").read_bytes())

    def test_main_rebuild_into_same_folder_replaces_updates_json(self):
        run_main(self.plugin, self.tmp / "out")
        (self.plugin / "bootstrap.js").write_bytes(b"var a = 2;\n")
        _, out, _ = run_main(self.plugin, self.tmp / "out")
        digest = out.splitlines()[1].split()[1]
        entry = json.loads(self.updates().read_text(encoding="utf-8"))["addons"][
            build_xpi.EXPECTED_ID
        ]["updates"][0]
        self.assertEqual(entry["update_hash"], "sha256:" + digest)
        self.assertEqual(list((self.tmp / "out").glob("*.tmp")), [])

    def test_main_without_update_url_is_refused_and_writes_nothing(self):
        self.write_manifest(make_manifest(update_url=None))
        code, out, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("update_url", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_every_build_prints_three_lines(self):
        code, out, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)

    def test_main_refuses_an_update_url_other_than_the_expected_one_and_writes_nothing(self):
        real = build_xpi.EXPECTED_UPDATE_URL
        owner_repo = "miguelppais/zotero-script-bridge"
        bad_urls = (
            "https://localhost/zotero-script-bridge-updates.json",
            "https://example.org/" + owner_repo + "/releases/latest/download/updates.json",
            "https://github.com/" + owner_repo + "/releases/download/v1.2.3/updates.json",
            "http://github.com/" + owner_repo + "/releases/latest/download/updates.json",
            "https://evil.example/?" + real,
            # A different owner, and a different repository under the same owner.
            OTHER_URL,
            real.replace("zotero-script-bridge/releases", "other-repo/releases"),
            # Forms a browser-style URL parser would resolve to another path.
            real.replace(
                owner_repo + "/",
                # Built with chr(92) so the source holds no literal UNC-looking path.
                owner_repo + BACKSLASH + ".." + BACKSLASH + ".." + BACKSLASH + "attacker" + BACKSLASH + "repo/",
            ),
            real.replace(owner_repo + "/", owner_repo + "/%2e%2e/attacker/repo/"),
            real.replace(owner_repo + "/", owner_repo + "/%2E/"),
            real.replace("miguelppais/", "miguelppais\t/"),
            real.replace("miguelppais/", "miguel\nppais/"),
            real.replace("miguelppais/", "miguelppais\r/"),
            real.replace("github.com", "github.com\\@evil.example"),
            # Trailing and leading additions.
            real + "?x=1",
            real + "#x",
            real + "/",
            real + " ",
            " " + real,
            real.upper(),
            "",
            7,
        )
        for bad in bad_urls:
            with self.subTest(update_url=bad):
                self.write_manifest(make_manifest(update_url=bad))
                code, out, err = run_main(self.plugin, self.tmp / "out")
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("update_url", err)
                self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_a_null_update_url(self):
        manifest = make_manifest()
        manifest["applications"]["zotero"]["update_url"] = None
        self.write_manifest(manifest)
        code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("update_url", err)
        self.assertFalse((self.tmp / "out").exists())

    def test_main_refuses_updates_json_path_that_reads_as_a_link(self):
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        real_is_link = build_xpi.is_link
        with mock.patch.object(
            build_xpi, "is_link", lambda p: p.name == "updates.json" or real_is_link(p)
        ):
            code, _, err = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertIn("symlink", err)
        self.assertEqual(list(out_dir.iterdir()), [])

    def test_main_failed_updates_write_leaves_no_tmp_file(self):
        real_replace = os.replace
        calls = []

        def replace_then_fail(src, dst):
            # The first call moves the .xpi into place; the second is updates.json.
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            real_replace(src, dst)

        with mock.patch.object(build_xpi.os, "replace", replace_then_fail):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertFalse(self.updates().exists())
        self.assertEqual(list((self.tmp / "out").glob("*.tmp")), [])

    def test_main_failed_updates_write_after_a_rebuild_leaves_no_stale_updates_json(self):
        run_main(self.plugin, self.tmp / "out")
        self.assertTrue(self.updates().is_file())
        (self.plugin / "bootstrap.js").write_bytes(b"var a = 2;\n")
        real_replace = os.replace
        calls = []

        def replace_then_fail(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            real_replace(src, dst)

        with mock.patch.object(build_xpi.os, "replace", replace_then_fail):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        # The new .xpi is in place, so the old updates.json (old hash) must be gone.
        self.assertTrue(self.xpi().is_file())
        self.assertFalse(self.updates().exists())
        self.assertEqual(list((self.tmp / "out").glob("*.tmp")), [])

    def test_main_failed_xpi_swap_leaves_no_updates_json(self):
        run_main(self.plugin, self.tmp / "out")
        with mock.patch.object(build_xpi.os, "replace", side_effect=OSError("locked")):
            code, _, err = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 1)
        self.assertIn("locked", err)
        self.assertFalse(self.updates().exists())
        self.assertEqual(list((self.tmp / "out").glob("*.tmp")), [])

    def test_main_removes_the_old_updates_json_before_the_new_xpi_is_swapped_in(self):
        run_main(self.plugin, self.tmp / "out")
        real_replace = os.replace
        seen = []

        def spy(src, dst):
            seen.append((Path(dst).name, self.updates().exists()))
            real_replace(src, dst)

        with mock.patch.object(build_xpi.os, "replace", spy):
            code, _, _ = run_main(self.plugin, self.tmp / "out")
        self.assertEqual(code, 0)
        self.assertEqual(seen[0], (self.xpi().name, False))
        self.assertTrue(self.updates().is_file())

    def test_main_refuses_when_an_old_updates_json_cannot_be_removed(self):
        out_dir = self.tmp / "out"
        out_dir.mkdir()
        self.updates().mkdir()  # a folder in the way cannot be removed as a file
        code, _, err = run_main(self.plugin, out_dir)
        self.assertEqual(code, 1)
        self.assertIn("build_xpi:", err)
        self.assertFalse(self.xpi().exists())
        self.assertEqual(list(out_dir.glob("*.tmp")), [])

    def test_real_manifest_builds_with_the_decided_update_url(self):
        code, out, _ = run_main(REPO / "plugin", self.tmp / "real")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 3)
        manifest = json.loads((REPO / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["applications"]["zotero"]["update_url"], build_xpi.EXPECTED_UPDATE_URL
        )
        self.assertTrue((self.tmp / "real" / "updates.json").is_file())


if __name__ == "__main__":
    unittest.main()
