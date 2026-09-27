#!/usr/bin/env python3
"""Hash the audited source/build inputs for the Chipyard SBT assembly.

This is a scoped input inventory, not a Git tree hash or a dependency lockfile.
Resolved Maven dependencies, Java, SBT, and build environment belong in the
calling build manifest. The roots deliberately cover more than EE290SimConfig:
SBT compiles the Chipyard project and its project dependencies as a whole.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from rtlgraph_s0 import FORBIDDEN, checked_path


# Roots declared in Chipyard's build.sbt, including optional initialized modules.
# Keeping the list explicit avoids traversing unrelated repository contents.
PROJECT_ROOTS = (
    "generators/chipyard", "generators/rocket-chip/macros",
    "generators/compress-acc", "generators/mempress",
    "generators/bar-fetchers", "generators/sp26-atlas-acc/dependencies/fpex",
    "generators/sp26-atlas-acc/dependencies/sp26-fp-units",
    "generators/sp26-atlas-acc", "generators/saturn", "generators/constellation",
    "generators/fft-generator", "generators/tracegen", "generators/icenet",
    "generators/shuttle", "generators/cva6", "generators/ara", "generators/ibex",
    "generators/vexiiriscv", "generators/riscv-sodor", "generators/radiance",
    "generators/nvdla", "generators/tacit", "generators/caliptra-aes-acc",
    "generators/rerocc", "generators/rocc-acc-utils", "tools/tapeout", "tools/cde",
    "generators/rocket-chip-blocks", "generators/rocket-chip-inclusive-cache",
    "fpga/fpga-shells", "fpga", "sims/firesim/sim/midas/targetutils",
    "sims/firesim/sim/firesim-lib", "generators/firechip/bridgeinterfaces",
    "generators/firechip/bridgestubs", "generators/firechip/chip",
)
FRESH_PROJECT_ROOTS = (
    "generators/testchipip",
    "generators/hardfloat/hardfloat", "generators/diplomacy/diplomacy",
    "generators/rocket-chip", "generators/boom", "generators/gemmini",
    "tools/fixedpoint", "tools/dsptools", "tools/rocket-dsp-utils",
    "tools/firrtl2", "tools/firrtl2/bridge",
)
OPTIONAL_GLUE = (
    "cva6", "ibex", "vexiiriscv", "riscv-sodor", "ara", "saturn", "tacit",
    "gemmini", "nvdla", "radiance", "caliptra-aes-acc", "compress-acc",
    "mempress", "fft-generator", "sp26-atlas-acc",
)
METADATA_EXCLUDED_NAMES = frozenset((
    "target", "build", "out", ".git", ".bloop", ".metals", ".bsp",
    "__pycache__", ".pytest_cache", ".cache", ".venv", "node_modules",
))
SOURCE_SUFFIXES = frozenset((".scala", ".java"))
METADATA_SUFFIXES = frozenset((".sbt", ".scala", ".properties"))


def _forbidden(name: str) -> bool:
    return any(word in name.lower() for word in FORBIDDEN)


def snapshot(root: Path) -> dict:
    """Return a deterministic inventory, rejecting unsafe symlink targets.

    Forbidden child names reject a source/resource inventory before stat or
    traversal, so a later build cannot silently include an unexamined subtree.
    Explicit paths and unsafe symlink targets are rejected by checked_path.
    Allowed resource symlinks are followed with cycle detection and represented
    by both their logical input path and their resolved input path.
    """
    root = checked_path(root)
    if not root.is_dir():
        raise ValueError("Chipyard root is not a directory")
    files: dict[str, dict] = {}
    roots: dict[str, dict] = {}

    def record(logical: Path, physical: Path) -> None:
        key = logical.relative_to(root).as_posix()
        if key in files:
            return
        digest = hashlib.sha256()
        size = 0
        with physical.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                digest.update(block)
        try:
            resolved = physical.relative_to(root).as_posix()
        except ValueError:
            # Explicitly identify out-of-tree resource inputs, if introduced.
            resolved = str(physical)
        files[key] = {"path": key, "resolved_path": resolved,
                      "bytes": size, "sha256": digest.hexdigest()}

    def walk(logical: Path, suffixes: frozenset | None, excluded: frozenset[str],
             ancestors: frozenset[Path] = frozenset()) -> None:
        physical = checked_path(logical)
        if physical.is_dir():
            if physical in ancestors:
                raise ValueError(f"Source/resource symlink cycle at {logical}")
            ancestors = ancestors | {physical}
            with os.scandir(physical) as entries:
                # Do not stat, resolve, or descend into forbidden entries.
                names = []
                for entry in entries:
                    if _forbidden(entry.name):
                        raise ValueError("A declared build-input tree contains a prohibited child name; build must not proceed")
                    if entry.name not in excluded:
                        names.append(entry.name)
            for name in sorted(names):
                walk(logical / name, suffixes, excluded, ancestors)
        elif physical.is_file():
            if suffixes is None or logical.suffix in suffixes:
                record(logical, physical)
        elif logical.is_symlink():
            raise ValueError(f"Dangling source/resource symlink at {logical}")

    def add(relative: str, kind: str, suffixes: frozenset | None) -> None:
        logical = root / relative
        physical = checked_path(logical)
        present = physical.exists()
        roots[relative] = {"path": relative, "kind": kind, "present": present}
        if present:
            # Declared source/resource roots may legitimately contain packages
            # named build/out/target. Only meta-build trees prune such outputs.
            # Git internals are never Scala sources or packaged resources.
            excluded = METADATA_EXCLUDED_NAMES if kind == "build-metadata" else frozenset((".git",))
            walk(logical, suffixes, excluded)
        elif logical.is_symlink():
            raise ValueError(f"Dangling input-root symlink at {logical}")

    def metadata(relative: str) -> None:
        # Record missing roots consistently even when the enclosing project
        # directory does not exist yet; SBT may create output directories there.
        add(str(Path(relative) / "project"), "build-metadata", METADATA_SUFFIXES)
        directory = checked_path(root / relative)
        if not directory.is_dir():
            return
        # SBT loads immediate .sbt files; never recursively scan project roots.
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries
                           if not _forbidden(entry.name) and entry.name.endswith(".sbt"))
        for name in names:
            add(str(Path(relative) / name), "build-metadata", METADATA_SUFFIXES)

    metadata(".")
    add(".gitmodules", "submodule-configuration", None)
    add("lib", "unmanaged-libraries", None)
    for project in PROJECT_ROOTS + FRESH_PROJECT_ROOTS:
        metadata(project)
        add(f"{project}/src/main/scala", "scala-java-source", SOURCE_SUFFIXES)
        add(f"{project}/src/main/java", "scala-java-source", SOURCE_SUFFIXES)
        add(f"{project}/src/main/resources", "resources", None)
    for project in FRESH_PROJECT_ROOTS:
        # freshProject uses <repo>/src as SBT base. Its Scala/resource roots are
        # overridden above; Java retains SBT's default relative to that base.
        metadata(f"{project}/src")
        add(f"{project}/src/src/main/java", "scala-java-source", SOURCE_SUFFIXES)
    for module in OPTIONAL_GLUE:
        add(f"generators/{module}/chipyard", "chipyard-glue", SOURCE_SUFFIXES)
    for relative in (
        "tools/stage/src/main/scala",
        "tools/cde/cde/src/chipsalliance/rocketchip",
        "generators/diplomacy/diplomacy/src/diplomacy",
        "generators/rocket-chip-inclusive-cache/design/craft",
    ):
        add(relative, "custom-source-root", SOURCE_SUFFIXES)
    add("tools/firrtl2/src/main/antlr4", "antlr-grammar", frozenset((".g4",)))

    inventory = [files[key] for key in sorted(files)]
    root_inventory = [roots[key] for key in sorted(roots)]
    optional_modules = [
        {"path": f"generators/{module}",
         "initialized": checked_path(root / f"generators/{module}/.git").exists()}
        for module in sorted(OPTIONAL_GLUE)
    ]
    digest_input = {"roots": root_inventory, "files": inventory,
                    "optional_modules": optional_modules}
    encoded = json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": 1,
        "scope": "Audited Chisel 6 Chipyard SBT project metadata, compile sources, resources, optional Chipyard glue, and unmanaged libraries; excludes generated outputs and externally resolved dependencies",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "file_count": len(inventory),
        "roots": root_inventory,
        "optional_modules": optional_modules,
        "files": inventory,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chipyard-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(snapshot(args.chipyard_root), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        destination = checked_path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded)


if __name__ == "__main__":
    main()
