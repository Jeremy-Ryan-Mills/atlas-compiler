#!/usr/bin/env python3
"""Check scoped build-input hashing without invoking SBT or hardware tools."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_build_inputs import snapshot


class BuildInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtlgraph-inputs-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.write("generators/chipyard/src/main/scala/Config.scala", "class Config")
        self.write("build.sbt", "scalaVersion := \"2.13.16\"")
        (self.root / "generators/sp26-atlas-acc").mkdir(parents=True)

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def test_content_hash_ignores_mtime_and_unrelated_outputs(self):
        before = snapshot(self.root)
        os.utime(self.source, (1, 1))
        self.write("generators/chipyard/target/scala-2.13/classes/Config.class", "output")
        self.write("generators/sp26-atlas-acc/baremetal/README.md", "outside scope")
        self.write("generators/sp26-atlas-acc/atlas-compiler-experiments/docs/example.md", "outside scope")
        self.assertEqual(before["sha256"], snapshot(self.root)["sha256"])
        self.source.write_text("class ChangedConfig")
        self.assertNotEqual(before["sha256"], snapshot(self.root)["sha256"])

    def test_resource_symlink_hashes_referenced_contents(self):
        resource = self.root / "generators/chipyard/src/main/resources"
        resource.mkdir(parents=True)
        backing = self.write("shared/model.sv", "module Model; endmodule")
        (resource / "model.sv").symlink_to(backing)
        before = snapshot(self.root)
        entry = next(item for item in before["files"] if item["path"].endswith("resources/model.sv"))
        self.assertEqual(entry["resolved_path"], "shared/model.sv")
        backing.write_text("module NewModel; endmodule")
        self.assertNotEqual(before["sha256"], snapshot(self.root)["sha256"])

    def test_generated_names_inside_declared_inputs_are_hashed(self):
        source = self.write("generators/chipyard/src/main/scala/build/Utility.scala", "class Utility")
        resource = self.write("generators/chipyard/src/main/resources/out/data.bin", "input")
        before = snapshot(self.root)
        paths = {item["path"] for item in before["files"]}
        self.assertIn(source.relative_to(self.root).as_posix(), paths)
        self.assertIn(resource.relative_to(self.root).as_posix(), paths)
        resource.write_text("changed input")
        self.assertNotEqual(before["sha256"], snapshot(self.root)["sha256"])

    def test_empty_project_directory_does_not_change_inputs(self):
        before = snapshot(self.root)
        (self.root / "generators/tacit/target").mkdir(parents=True)
        self.assertEqual(before["sha256"], snapshot(self.root)["sha256"])

    def test_prohibited_child_rejected_without_inspecting_entry(self):
        original = os.scandir
        source_dir = self.source.parent

        class NameOnlyEntry:
            name = "restricted-HAMMER-name"

            def __getattr__(self, name):
                raise AssertionError("Prohibited entry was inspected")

        class SyntheticEntries:
            def __enter__(self):
                return iter((NameOnlyEntry(),))

            def __exit__(self, *args):
                return False

        def scan(path):
            return SyntheticEntries() if Path(path) == source_dir else original(path)

        with patch("rtlgraph_build_inputs.os.scandir", side_effect=scan):
            with self.assertRaisesRegex(ValueError, "prohibited child name"):
                snapshot(self.root)

    def test_resource_cycle_rejected(self):
        resource = self.root / "generators/chipyard/src/main/resources"
        resource.mkdir(parents=True)
        (resource / "loop").symlink_to(".", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink cycle"):
            snapshot(self.root)

    def test_unsafe_symlink_target_rejected_before_access(self):
        alias = self.source.parent / "Alias.scala"
        alias.symlink_to(self.source.name)
        original = os.readlink

        def link_target(path, *args, **kwargs):
            if Path(path) == alias:
                # Synthetic readlink result; no prohibited path is created.
                return "restricted-vlsi-tree/Source.scala"
            return original(path, *args, **kwargs)

        with patch("rtlgraph_s0.os.readlink", side_effect=link_target):
            with self.assertRaisesRegex(ValueError, "prohibited component"):
                snapshot(self.root)

    def test_optional_module_initialization_changes_fingerprint(self):
        before = snapshot(self.root)
        self.write("generators/saturn/.git", "gitdir: unused-in-this-test")
        after = snapshot(self.root)
        self.assertNotEqual(before["sha256"], after["sha256"])
        self.assertTrue(next(module for module in after["optional_modules"]
                             if module["path"] == "generators/saturn")["initialized"])


if __name__ == "__main__":
    unittest.main()
