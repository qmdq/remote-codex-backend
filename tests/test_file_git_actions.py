import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.files.service import FileService
from app.protocol.errors import ValidationError


class FileGitActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="remote-codex-files-"))
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.service = FileService()
        self.project = {"id": "prj_files", "normalized_path": str(self.root)}
        self._run_git("init")
        self._run_git("config", "user.name", "RemoteCodex Tests")
        self._run_git("config", "user.email", "tests@remote.codex")
        (self.root / "readme.md").write_text("before\n", encoding="utf-8")
        self._run_git("add", ".")
        self._run_git("commit", "-m", "initial")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_git(self, *arguments: str):
        subprocess.run(
            ["git", *arguments],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_diff_and_revert_restore_committed_content(self):
        (self.root / "readme.md").write_text("before\nafter\n", encoding="utf-8")

        diff = self.service.diff(self.project, "readme.md")
        reverted = self.service.revert(self.project, "readme.md")
        content = (self.root / "readme.md").read_text(encoding="utf-8")

        self.assertEqual(diff["path"], "readme.md")
        self.assertIn("+after", diff["diff"])
        self.assertEqual(diff["additions"], 1)
        self.assertEqual(diff["deletions"], 0)
        self.assertEqual(reverted["path"], "readme.md")
        self.assertEqual(content, "before\n")

    def test_diff_accepts_absolute_path_inside_project(self):
        (self.root / "readme.md").write_text("before\nafter\n", encoding="utf-8")

        diff = self.service.diff(self.project, str(self.root / "readme.md"))

        self.assertEqual(diff["path"], "readme.md")
        self.assertIn("+after", diff["diff"])

    def test_diff_counts_untracked_file(self):
        path = self.root / "new.md"
        path.write_text("one\ntwo\n", encoding="utf-8")

        diff = self.service.diff(self.project, "new.md")

        self.assertIn("+one", diff["diff"])
        self.assertEqual(diff["additions"], 2)
        self.assertEqual(diff["deletions"], 0)

    def test_diff_counts_deleted_file(self):
        (self.root / "readme.md").unlink()

        diff = self.service.diff(self.project, "readme.md")

        self.assertIn("-before", diff["diff"])
        self.assertEqual(diff["additions"], 0)
        self.assertEqual(diff["deletions"], 1)

    def test_revert_requires_uncommitted_change(self):
        with self.assertRaises(ValidationError):
            self.service.revert(self.project, "readme.md")

    def test_rejects_path_outside_project(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.assertRaises(Exception):
            self.service.diff(self.project, str(outside))

    def test_save_upload_writes_file_and_reports_size(self):
        result = self.service.save_upload(self.project, "uploaded.bin", b"hello upload")

        self.assertEqual(result["path"], "uploaded.bin")
        self.assertEqual(result["name"], "uploaded.bin")
        self.assertEqual(result["size"], len(b"hello upload"))
        self.assertEqual((self.root / "uploaded.bin").read_bytes(), b"hello upload")

    def test_save_upload_refuses_existing_file_without_overwrite(self):
        (self.root / "uploaded.bin").write_bytes(b"old")

        with self.assertRaises(ValidationError) as raised:
            self.service.save_upload(self.project, "uploaded.bin", b"new")

        self.assertEqual(getattr(raised.exception, "code", None), "file.exists")
        self.assertEqual((self.root / "uploaded.bin").read_bytes(), b"old")

    def test_save_upload_overwrites_when_requested(self):
        (self.root / "uploaded.bin").write_bytes(b"old")

        self.service.save_upload(self.project, "uploaded.bin", b"new", overwrite=True)

        self.assertEqual((self.root / "uploaded.bin").read_bytes(), b"new")

    def test_save_upload_rejects_path_outside_project(self):
        with self.assertRaises(Exception):
            self.service.save_upload(self.project, "../uploaded.bin", b"data")


if __name__ == "__main__":
    unittest.main()
