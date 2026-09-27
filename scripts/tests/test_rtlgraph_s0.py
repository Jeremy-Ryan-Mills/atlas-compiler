#!/usr/bin/env python3
"""Check configuration selection and build lineage without launching hardware tools."""

import argparse
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_s0 import (artifact, configuration_name, elaboration_command,
                         generator_lineage, parse_arguments, target_name)


class ConfigurationTests(unittest.TestCase):
    def test_default_config_has_separate_output(self):
        compiler = Path("/tmp/example/compiler")
        args = parse_arguments([], compiler=compiler)
        self.assertEqual(args.config, "EE290SimConfig")
        self.assertEqual(args.output, compiler / "build/rtlgraph-s0/EE290SimConfig")
        self.assertIsNone(args.generator_jar)

    def test_explicit_config_selects_command_and_artifact_name(self):
        for config in ("EE290SimConfig", "AtlasShuttleVectorConfig"):
            with self.subTest(config=config):
                args = parse_arguments(["--config", config, "--generator-jar", "/tmp/current.jar"],
                                       compiler=Path("/tmp/compiler"))
                command = elaboration_command(Path("/tmp/java"), args.generator_jar,
                                              Path("/tmp/elaboration"), args.config)
                self.assertEqual(command[command.index("-cp") + 1], "/tmp/current.jar")
                self.assertEqual(command[command.index("--name") + 1], target_name(config))
                self.assertEqual(command[command.index("--legacy-configs") + 1], f"chipyard:{config}")
                self.assertEqual(args.output.name, config)
                self.assertEqual(target_name(config), f"chipyard.harness.TestHarness.{config}")

    def test_explicit_output_and_adoption_preserved(self):
        args = parse_arguments(["--config", "AtlasShuttleVectorConfig", "--output", "/tmp/historical",
                                "--existing-elaboration", "/tmp/elaboration", "--existing-hw-ir", "/tmp/old.mlir"])
        self.assertEqual(args.output, Path("/tmp/historical"))
        self.assertEqual(args.existing_elaboration, Path("/tmp/elaboration"))
        self.assertEqual(args.existing_hw_ir, Path("/tmp/old.mlir"))

    def test_unlinked_ir_adoption_rejected(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_arguments(["--existing-hw-ir", "/tmp/old.mlir"])

    def test_config_requires_single_class_name(self):
        for invalid in ("", "../EE290SimConfig", "chipyard.EE290SimConfig", "chipyard:EE290SimConfig", "Bad Name", "--flag"):
            with self.subTest(config=invalid), self.assertRaises(argparse.ArgumentTypeError):
                configuration_name(invalid)


class BuildLineageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtlgraph-s0-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.jar = self.directory / "generator.jar"
        self.jar.write_bytes(b"test generator artifact")
        self.jar_info = artifact(self.jar)
        self.path = self.directory / "build-manifest.json"
        self.record = {
            "schema_version": 1,
            "kind": "chipyard-generator-build",
            "status": "complete",
            "generator_jar": self.jar_info,
            "source_snapshot": {
                "scope": "Test source and resource manifest only",
                "before": {"sha256": "a" * 64},
                "after": {"sha256": "a" * 64},
            },
            "commands": [{"argv": ["sbt", "chipyard/assembly"], "cwd": "/tmp/example", "returncode": 0}],
        }

    def lineage(self):
        self.path.write_text(json.dumps(self.record))
        return generator_lineage(self.jar_info, self.path)

    def test_no_manifest_is_unverified(self):
        self.assertEqual(generator_lineage(self.jar_info, None)["status"], "UNVERIFIED")

    def test_matching_completed_build_records_scoped_lineage(self):
        result = self.lineage()
        self.assertEqual(result["status"], "RECORDED_BUILD_HASH_MATCH")
        self.assertEqual(result["build_manifest"]["sha256"], artifact(self.path)["sha256"])
        self.assertEqual(result["source_snapshot"], self.record["source_snapshot"])

    def test_jar_hash_mismatch_rejected(self):
        self.record["generator_jar"] = {**self.jar_info, "sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "JAR SHA-256"):
            self.lineage()

    def test_mutated_source_snapshot_rejected(self):
        self.record["source_snapshot"]["after"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "before and after"):
            self.lineage()

    def test_failed_or_unrecorded_build_rejected(self):
        for commands in ([], [{"argv": ["sbt"], "returncode": 1}], [{"argv": [], "returncode": 0}]):
            with self.subTest(commands=commands), self.assertRaisesRegex(ValueError, "successful build commands"):
                self.record["commands"] = commands
                self.lineage()

    def test_incomplete_manifest_rejected(self):
        self.record["status"] = "running"
        with self.assertRaisesRegex(ValueError, "completed"):
            self.lineage()

    def test_unscoped_snapshot_rejected(self):
        del self.record["source_snapshot"]["scope"]
        with self.assertRaisesRegex(ValueError, "snapshot scope"):
            self.lineage()


if __name__ == "__main__":
    unittest.main()
