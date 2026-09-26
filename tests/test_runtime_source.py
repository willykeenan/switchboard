import hashlib
import tempfile
import unittest
from pathlib import Path

from runtime_source import snapshot, write_manifest, runtime_files


class RuntimeSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "board.py").write_text("# fixture board\n")
        (self.root / "agents").mkdir()
        (self.root / "agents" / "http.py").write_text("# fixture agents\n")
        (self.root / "inspector").mkdir()
        (self.root / "inspector" / "x.py").write_text("# fixture inspector\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_generate_and_verify(self):
        spec = write_manifest(self.root)
        self.assertIn("board.py", spec["files"])
        self.assertIn("agents/http.py", spec["files"])
        result = snapshot(self.root)
        self.assertEqual(spec["files"], result["files"])

    def test_missing_dependency_is_refused(self):
        write_manifest(self.root)
        (self.root / "board.py").unlink()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            snapshot(self.root)

    def test_added_unpinned_runtime_is_refused(self):
        write_manifest(self.root)
        (self.root / "new_dependency.py").write_text("# added")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            snapshot(self.root)

    def test_changed_runtime_bytes_are_refused(self):
        write_manifest(self.root)
        (self.root / "board.py").write_text("# changed")
        with self.assertRaisesRegex(ValueError, "drift"):
            snapshot(self.root)

    def test_private_runtime_not_traversed(self):
        write_manifest(self.root)
        for folder in ("runtime/managed", "backups", "evidence"):
            path = self.root / folder
            path.mkdir(parents=True)
            (path / "private.py").write_text("# private test only")
        names = {str(p.relative_to(self.root)) for p in runtime_files(self.root)}
        self.assertNotIn("runtime/managed/private.py", names)
        snapshot(self.root)

    def test_install_regenerates(self):
        first = write_manifest(self.root)
        (self.root / "extra.py").write_text("# extra")
        second = write_manifest(self.root)
        self.assertNotEqual(first["files"], second["files"])
        self.assertIn("extra.py", second["files"])
        self.assertEqual(
            hashlib.sha256((self.root / "extra.py").read_bytes()).hexdigest(),
            second["files"]["extra.py"],
        )


if __name__ == "__main__":
    unittest.main()
