#!/usr/bin/env python3
"""Replay the pinned Atlas S0 elaboration/lowering flow and record its provenance.

The cached Chipyard JAR is an input artifact. Hashing current Scala sources does
not establish that the JAR was compiled from those sources.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


TARGET = "chipyard.harness.TestHarness.AtlasShuttleVectorConfig"
TOOLCHAIN_VERSION = "firtool-1.75.0"
FORBIDDEN = ("hammer", "vlsi")
SOURCE_FILES = (
    ".gitmodules",
    "build.sbt",
    "project/build.properties",
    "tools/stage/src/main/scala/ChipyardStage.scala",
    "tools/stage/src/main/scala/phases/GenerateFirrtlAnnos.scala",
    "generators/chipyard/src/main/scala/config/EE290Configs.scala",
    "generators/chipyard/src/main/scala/harness/TestHarness.scala",
    "generators/sp26-atlas-acc/chipyard/config/AtlasConfigs.scala",
    "generators/sp26-atlas-acc/src/main/scala/diplomatic/top/AtlasCore.scala",
    "generators/sp26-atlas-acc/src/main/scala/diplomatic/top/AtlasTile.scala",
    "generators/sp26-atlas-acc/src/main/scala/atlas/scalar/ScalarCore.scala",
    "generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala",
    "generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesTop.scala",
    "generators/sp26-atlas-acc/src/main/scala/atlas/common/MregParams.scala",
    "generators/sp26-atlas-acc/src/main/scala/atlas/common/VmemParams.scala",
    "generators/saturn/src/main/scala/shuttle/Configs.scala",
    "generators/saturn/src/main/scala/common/Parameters.scala",
    "generators/shuttle/src/main/scala/common/Configs.scala",
    "generators/shuttle/src/main/scala/common/Parameters.scala",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/machine.h",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/machine.cpp",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/reservations.h",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/reservations.cpp",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/depgraph.cpp",
    "generators/sp26-atlas-acc/atlas-compiler-experiments/src/core/simulator.cpp",
)
REPOSITORIES = (
    ".", "generators/sp26-atlas-acc", "generators/saturn", "generators/shuttle",
    "generators/testchipip", "generators/rocket-chip", "generators/diplomacy",
    "generators/hardfloat", "tools/cde", "tools/circt",
    "generators/sp26-atlas-acc/atlas-compiler-experiments",
)
# This checkpoint supports the annotations actually emitted by the selected
# cached generator. In particular, do not admit file/resource annotations merely
# because firtool's unknown-annotation tolerance is enabled.
INLINE_ANNOTATIONS = frozenset((
    "firrtl.transforms.DedupGroupAnnotation",
    "firrtl.transforms.BlackBoxInlineAnno",
    "firrtl.transforms.DontTouchAnnotation",
    "firrtl.passes.InlineAnnotation",
    "chisel3.util.experimental.decode.DecodeTableAnnotation",
    "chisel3.experimental.EnumAnnotations$EnumComponentAnnotation",
    "chisel3.experimental.EnumAnnotations$EnumVecAnnotation",
    "chisel3.experimental.EnumAnnotations$EnumDefAnnotation",
))


def checked_path(value: str | Path) -> Path:
    """Reject forbidden components before following any filesystem symlink."""
    original = Path(value).expanduser()
    if not original.is_absolute():
        original = Path.cwd() / original
    pending = deque(original.parts[1:])
    current = Path(original.anchor)
    links = 0
    while pending:
        part = pending.popleft()
        if any(word in part.lower() for word in FORBIDDEN):
            raise ValueError("A supplied path contains a prohibited component")
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise ValueError("Too many filesystem symlinks")
            target = Path(os.readlink(candidate))
            if any(word in component.lower() for component in target.parts for word in FORBIDDEN):
                raise ValueError("A symlink points through a prohibited component")
            if target.is_absolute():
                current = Path(target.anchor)
                pending.extendleft(reversed(target.parts[1:]))
            else:
                pending.extendleft(reversed(target.parts))
        else:
            current = candidate
    return current


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact(path: Path) -> dict:
    path = checked_path(path)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path),
        "bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": digest.hexdigest(),
    }


def git_output(repo: Path, *arguments: str, optional: bool = False) -> str | None:
    result = subprocess.run(["git", "-C", str(repo), *arguments], text=True,
                            capture_output=True, timeout=30, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if optional and result.returncode == 1:
        return None
    raise RuntimeError(f"Git metadata query failed for {repo}: {result.stderr.strip()}")


def repository_info(root: Path, relative: str) -> dict:
    repo = checked_path(root / relative)
    git_entry = checked_path(repo / ".git")
    if not git_entry.exists():
        return {"initialized": False}
    if git_entry.is_file():
        prefix, separator, target = git_entry.read_text().strip().partition(": ")
        if prefix != "gitdir" or not separator:
            raise ValueError(f"Invalid Git directory indirection at {git_entry}")
        checked_path(repo / target)
    top = git_output(repo, "rev-parse", "--show-toplevel")
    if checked_path(top) != repo:
        raise ValueError(f"Expected a distinct repository at {repo}")
    info = {
        "initialized": True,
        "head": git_output(repo, "rev-parse", "HEAD"),
        "origin": git_output(repo, "config", "--get", "remote.origin.url", optional=True),
    }
    if relative != ".":
        entry = git_output(root, "ls-tree", "HEAD", "--", relative)
        info["root_gitlink"] = entry.split()[2] if entry.startswith("160000 commit ") else None
        info["matches_root_gitlink"] = (
            info["head"] == info["root_gitlink"] if info["root_gitlink"] else None
        )
        for key in ("url", "branch"):
            info[f"gitmodules_{key}"] = git_output(
                root, "config", "--file", str(checked_path(root / ".gitmodules")),
                "--get", f"submodule.{relative}.{key}", optional=True,
            )
    return info


def embedded_annotations(fir: Path) -> dict:
    """Inspect JSON annotations without interpreting any referenced file path."""
    decoder = json.JSONDecoder()
    with fir.open() as stream:
        version = stream.readline().strip()
        circuit = stream.readline()
        if not circuit.startswith("circuit TestHarness :%["):
            raise ValueError("Expected TestHarness FIRRTL with embedded JSON annotations")
        buffer = circuit.partition("%[")[2]
        while True:
            try:
                annotations, end = decoder.raw_decode(buffer)
                if not buffer[end:].strip():
                    buffer += stream.read(4096)
                if not buffer[end:].lstrip().startswith("]"):
                    raise ValueError("Missing embedded annotation wrapper terminator")
                break
            except json.JSONDecodeError:
                block = stream.read(1024 * 1024)
                if not block or len(buffer) > 64 * 1024 * 1024:
                    raise ValueError("Invalid or excessive embedded annotation JSON") from None
                buffer += block
    if not isinstance(annotations, list) or any(not isinstance(annotation, dict) for annotation in annotations):
        raise ValueError("Embedded annotations must be an array of JSON objects")
    classes = Counter(annotation.get("class") for annotation in annotations)
    unsupported = set(classes) - INLINE_ANNOTATIONS
    if unsupported:
        raise ValueError(f"Unreviewed embedded annotation classes: {sorted(map(str, unsupported))}")
    for annotation in annotations:
        if annotation["class"] == "firrtl.transforms.BlackBoxInlineAnno":
            # Inline black-box names become emitted filenames. Check the name
            # without opening it, and reject directory components altogether.
            name = annotation.get("name", "")
            if not name or Path(name).name != name or any(word in name.lower() for word in FORBIDDEN):
                raise ValueError("Unsafe inline black-box filename")
    return {"firrtl_version": version, "count": len(annotations), "classes": dict(sorted(classes.items())),
            "policy": "Only explicitly reviewed inline/non-file annotation classes accepted; no sidecar passed to firtool"}


def main() -> int:
    compiler = checked_path(__file__).parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chipyard-root", type=Path, default=compiler.parent.parent.parent)
    parser.add_argument("--output", type=Path, default=compiler / "build" / "rtlgraph-s0")
    parser.add_argument("--existing-elaboration", type=Path, help="Adopt an existing target directory; do not run Java")
    parser.add_argument("--existing-hw-ir", type=Path, help="Adopt existing HW IR; still verify it with circt-opt")
    args = parser.parse_args()
    if args.existing_hw_ir and not args.existing_elaboration:
        parser.error("--existing-hw-ir requires --existing-elaboration; do not mix fresh FIRRTL with unrelated adopted IR")
    root = checked_path(args.chipyard_root)
    output = checked_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = checked_path(output / "s0-manifest.json")
    elaboration = checked_path(args.existing_elaboration or output / "elaboration")
    fir = checked_path(elaboration / f"{TARGET}.fir")
    hw = checked_path(args.existing_hw_ir or output / "atlas.hw.mlir")
    jar = checked_path(root / ".classpath_cache/chipyard.jar")
    java = checked_path(root / ".conda-env/lib/jvm/bin/java")
    firtool = checked_path(root / ".conda-env/riscv-tools/bin/firtool")
    circt_opt = checked_path(root / ".conda-env/riscv-tools/bin/circt-opt")
    espresso = checked_path(root / ".conda-env/riscv-tools/bin/espresso")
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((
        str(checked_path(root / ".conda-env/riscv-tools/bin")),
        str(checked_path(root / ".conda-env/bin")),
        env.get("PATH", os.defpath),
    ))
    env["JAVA_HOME"] = str(checked_path(root / ".conda-env/lib/jvm"))
    manifest = {
        "schema_version": 2,
        "started_utc": timestamp(),
        "status": "running",
        "status_scope": "Artifact workflow only; complete does not mean the full S0 milestone or a validated scheduling model",
        "target": TARGET,
        "chipyard_root": str(root),
        "output": str(output),
        "cached_jar_source_equivalence": "UNVERIFIED",
        "scope": "S0 artifact generation and IR verification; no RTL timing or functional proof",
        "source_hash_scope": "Selected files only; neither a complete source snapshot nor a dirty-tree check",
        "repository_scope": "Selected checkouts and root gitlinks only; no recursive submodule or source scan",
        "environment": {
            "JAVA_HOME": env["JAVA_HOME"],
            "path_prefix": [str(checked_path(root / ".conda-env/riscv-tools/bin")),
                            str(checked_path(root / ".conda-env/bin"))],
            "inheritance": "Other variables and trailing PATH inherited; environment is not hermetic",
        },
        "limitations": [
            "Cached JAR source correspondence is UNVERIFIED even when configuration names match",
            "IR parsing/verification establishes structural validity, not functional or timing correctness",
            "External modules and annotation semantics require separate analysis before scheduling claims",
        ],
        "commands": [],
        "tools": {},
        "sources": {},
        "repositories": {},
        "artifacts": {},
    }

    def save() -> None:
        temporary = checked_path(manifest_path.with_suffix(".json.tmp"))
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(manifest_path)

    def run(command: list[str], name: str, cwd: Path = output) -> None:
        log = checked_path(output / f"s0-{name}.log")
        record = {"argv": command, "cwd": str(cwd), "log": str(log), "started_utc": timestamp()}
        manifest["commands"].append(record)
        save()
        print(f"{name}: {log}", flush=True)
        with log.open("w") as stream:
            result = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        record.update(returncode=result.returncode, finished_utc=timestamp())
        record["log_artifact"] = artifact(log)
        save()
        if result.returncode:
            raise RuntimeError(f"{name} failed with exit code {result.returncode}; see {log}")

    try:
        save()
        manifest["driver"] = artifact(checked_path(__file__))
        for executable in (java, firtool, circt_opt, espresso):
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise RuntimeError(f"Required executable is unavailable: {executable}")
        manifest["tools"]["espresso"] = artifact(espresso)
        manifest["tools"]["espresso"]["preflight"] = "Executable present in first PATH directory; required by Chisel decoder minimization"
        for name, executable in (("java", java), ("firtool", firtool), ("circt-opt", circt_opt)):
            info = artifact(executable)
            version = subprocess.run([str(executable), "-version" if name == "java" else "--version"],
                                     cwd=output, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     timeout=30, check=True)
            info["version"] = version.stdout.strip()
            manifest["tools"][name] = info
            if name != "java" and TOOLCHAIN_VERSION not in info["version"]:
                raise RuntimeError(f"{name} must report {TOOLCHAIN_VERSION}; found {info['version']}")
        manifest["artifacts"]["cached_jar"] = artifact(jar)
        for relative in REPOSITORIES:
            manifest["repositories"][relative] = repository_info(root, relative)
        for relative in SOURCE_FILES:
            source = checked_path(root / relative)
            if not source.is_file():
                manifest["sources"][relative] = {"missing": True}
                raise RuntimeError(f"Selected provenance source is missing: {source}")
            manifest["sources"][relative] = artifact(source)
        if args.existing_elaboration:
            manifest["elaboration"] = {"mode": "adopted", "path": str(elaboration), "command_provenance": "not established by this run"}
        else:
            elaboration.mkdir(parents=True, exist_ok=True)
            manifest["elaboration"] = {"mode": "generated", "path": str(elaboration)}
            run([str(java), "-XX:-UsePerfData", "-Xmx16G", "-Xss8M", "-cp", str(jar), "chipyard.Generator",
                 "--target-dir", str(elaboration), "--name", TARGET, "--top-module", "chipyard.harness.TestHarness",
                 "--legacy-configs", "chipyard:AtlasShuttleVectorConfig"], "java", cwd=root)
        manifest["artifacts"]["firrtl"] = artifact(fir)
        manifest["annotations"] = embedded_annotations(fir)
        annotations = checked_path(elaboration / f"{TARGET}.anno.json")
        if annotations.is_file():
            manifest["artifacts"]["annotation_sidecar"] = artifact(annotations)
        if args.existing_hw_ir:
            manifest["lowering"] = {"mode": "adopted", "input_equivalence": "UNVERIFIED",
                                    "command_provenance": "not established by this run"}
            manifest["limitations"].append("Adopted FIRRTL/JAR and HW-IR/FIRRTL input linkage is not established by this run")
        else:
            manifest["lowering"] = {"mode": "generated", "annotations": "embedded FIRRTL annotations; no duplicate sidecar input"}
            run([str(firtool), str(fir), "--ir-hw", "-O=debug", "--preserve-values=named",
                 "--mlir-print-debuginfo", "--disable-annotation-unknown",
                 "--warn-on-unprocessed-annotations", "-o", str(hw)], "firtool")
        manifest["artifacts"]["hardware_ir"] = artifact(hw)
        run([str(circt_opt), str(hw), "--verify-each", "-o", os.devnull], "circt-opt")
        manifest["status"] = "complete"
        return 0
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        manifest["error"] = "Interrupted before all artifact checks completed"
        print(manifest["error"], file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        print(str(error), file=sys.stderr)
        return 1
    finally:
        manifest["finished_utc"] = timestamp()
        save()
        print(f"Manifest: {manifest_path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
