#!/usr/bin/env python3
"""Build Chipyard's generator directly with SBT and record scoped provenance.

Uses the audited source layout of this checkout, not Make's source discovery.
The build is incremental; snapshots record inputs, not a hermetic build proof.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from rtlgraph_build_inputs import snapshot
from rtlgraph_s0 import artifact, checked_path, timestamp


def main():
    compiler = checked_path(__file__).parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chipyard-root", type=Path, default=compiler.parent.parent.parent)
    parser.add_argument("--output", type=Path, default=compiler / "build/rtlgraph-source-build")
    args = parser.parse_args()
    root = checked_path(args.chipyard_root)
    output = checked_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = checked_path(output / "build-manifest.json")
    jar = checked_path(output / "chipyard-current.jar")
    java = checked_path(root / ".conda-env/lib/jvm/bin/java")
    launcher = checked_path(root / "scripts/sbt-launch.jar")
    # A fresh global settings directory prevents loading user/global plugins.
    global_base = Path(tempfile.mkdtemp(prefix="sbt-global-", dir=output))
    # Unix-domain sockets require a much shorter path than this workspace.
    runtime = tempfile.TemporaryDirectory(prefix="atlas-sbt-", dir="/tmp")
    env = os.environ.copy()
    removed = ("USE_CHISEL7", "CONSTELLATION_STANDALONE", "JAVA_TOOL_OPTIONS",
               "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
    for key in removed:
        env.pop(key, None)
    env["XDG_RUNTIME_DIR"] = runtime.name
    env["JAVA_HOME"] = str(checked_path(root / ".conda-env/lib/jvm"))
    env["PATH"] = os.pathsep.join((str(checked_path(root / ".conda-env/riscv-tools/bin")),
                                   str(checked_path(root / ".conda-env/bin")),
                                   env.get("PATH", os.defpath)))
    # JSON string syntax safely quotes this filename in the Scala expression.
    task = ';project chipyard;set assembly / assemblyOutputPath := file(' + json.dumps(str(jar)) + ');assembly'
    command = [str(java), "-XX:-UsePerfData", "-Xmx12G", "-Xss8M", "-XX:ActiveProcessorCount=8",
               f"-Dsbt.ivy.home={checked_path(root / '.ivy2')}",
               f"-Dsbt.global.base={global_base}",
               f"-Dsbt.boot.directory={checked_path(root / '.sbt/boot')}",
               "-Dsbt.supershell=false", "-Dsbt.server.autostart=false", "-Dsbt.server.forcestart=true", "-Dsbt.ci=true",
               "-jar", str(launcher), task]
    manifest = {
        "schema_version": 1, "kind": "chipyard-generator-build", "status": "running",
        "started_utc": timestamp(), "chipyard_root": str(root), "commands": [],
        "environment": {"JAVA_HOME": env["JAVA_HOME"], "unset": list(removed),
                        "sbt_global_base": str(global_base), "XDG_RUNTIME_DIR": runtime.name,
                        "scope": "Other environment variables inherited; not hermetic"},
        "limitations": ["SBT incremental compilation and cached published dependencies are used",
                        "Source snapshots cover the audited roots; they are not a whole-tree or dependency cache archive",
                        "A completed build does not validate RTL functionality or instruction timing"],
    }

    def save():
        temporary = checked_path(manifest_path.with_suffix(".json.tmp"))
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(manifest_path)

    def capture(name):
        data = snapshot(root)
        path = checked_path(output / f"inputs-{name}.json")
        path.write_text(json.dumps(data, indent=2) + "\n")
        return data, {"sha256": data["sha256"], "manifest": artifact(path)}

    try:
        save()
        manifest["drivers"] = {"build": artifact(checked_path(__file__)),
                               "inputs": artifact(checked_path(__file__).with_name("rtlgraph_build_inputs.py")),
                               "shared": artifact(checked_path(__file__).with_name("rtlgraph_s0.py"))}
        manifest["tools"] = {"java": artifact(java), "sbt_launcher": artifact(launcher)}
        data, before = capture("before")
        manifest["source_snapshot"] = {"scope": data["scope"], "before": before}
        log = checked_path(output / "sbt-build.log")
        record = {"argv": command, "cwd": str(root), "log": str(log), "started_utc": timestamp()}
        manifest["commands"].append(record)
        save()
        print(f"SBT assembly: {log}", flush=True)
        with log.open("w") as stream:
            result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                    stdout=stream, stderr=subprocess.STDOUT)
        record.update(returncode=result.returncode, finished_utc=timestamp(), log_artifact=artifact(log))
        _, after = capture("after")
        manifest["source_snapshot"]["after"] = after
        save()
        if result.returncode:
            raise RuntimeError(f"SBT assembly failed ({result.returncode}); see {log}")
        if before["sha256"] != after["sha256"]:
            raise RuntimeError("Audited source inputs changed during the build; rerun before using this JAR")
        manifest["generator_jar"] = artifact(jar)
        manifest["status"] = "complete"
        return 0
    except KeyboardInterrupt:
        manifest.update(status="interrupted", error="Build interrupted")
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        manifest.update(status="failed", error=str(error))
        print(str(error), file=sys.stderr)
        return 1
    finally:
        manifest["finished_utc"] = timestamp()
        save()
        print(f"Build manifest: {manifest_path}", flush=True)
        runtime.cleanup()


if __name__ == "__main__":
    sys.exit(main())
