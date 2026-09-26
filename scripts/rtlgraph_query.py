#!/usr/bin/env python3
"""Build a typed CIRCT exporter and query local, combinational SSA reachability.

No printed MLIR is parsed. The companion C++ exporter uses CIRCT's parser and
MLIR Value identities, preserving output/result indices across instances.
The result establishes structural connectivity, not logical implication,
instruction acceptance, latency, or universal scheduling safety.
"""

import argparse
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from rtlgraph_s0 import artifact, checked_path


def digest(path):
    path = checked_path(path)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(data)
    return hasher.hexdigest()


def run(command, **kwargs):
    completed = subprocess.run(command, text=True, capture_output=True, **kwargs)
    if completed.returncode:
        raise RuntimeError(f"command failed ({completed.returncode}): {command!r}\n"
                           f"{completed.stdout}\n{completed.stderr}")
    return completed


def op_summary(op):
    return {key: op[key] for key in ("id", "kind", "location", "attributes")}


def local_path(module, source_name, sink_name):
    """Traverse only single-result, region-free combinational operations.

    Sequential ops, module instances, unknown ops and multi-result ops are
    explicit cutpoints. Named HW wires are identities. SV inout reads and
    assignments require a driver analysis and are not traversed.
    """
    operations = module["operations"]
    candidates = [op for op in operations
                  if any(op["attributes"].get(key) == source_name
                         for key in ("name", "sv.namehint"))]
    sinks = [port for port in module["ports"]
             if port["direction"] == "output" and port["name"] == sink_name]
    result = {"module": module["name"], "source_name": source_name,
              "sink_port": sink_name, "status": "unresolved"}
    if len(candidates) != 1 or len(sinks) != 1:
        result["reason"] = "source or output port is missing or ambiguous"
        result["source_candidates"] = [op_summary(op) for op in candidates]
        result["sink_candidates"] = sinks
        return result
    source = candidates[0]
    if len(source["results"]) != 1:
        result["reason"] = "named source does not have exactly one result"
        return result
    start, goal = source["results"][0], sinks[0]["value"]
    result.update(source=op_summary(source), source_value=start, sink=sinks[0])
    uses = defaultdict(list)
    for op in operations:
        for index, value in enumerate(op["operands"]):
            uses[value].append((op, index))
    pending = deque([start])
    previous = {start: None}
    cutpoints = {}
    while pending and goal not in previous:
        value = pending.popleft()
        for op, index in uses[value]:
            safe = (len(op["results"]) == 1 and not op["has_regions"]
                    and "instance" not in op and not op["kind"].startswith("seq.")
                    and (op["combinational"] or op["identity_wire"]))
            if not safe:
                if op["kind"] != "hw.output":
                    cutpoints[op["id"]] = op_summary(op)
                continue
            successor = op["results"][0]
            if successor not in previous:
                previous[successor] = (value, op, index)
                pending.append(successor)
    result["encountered_cutpoints"] = list(cutpoints.values())
    if goal not in previous:
        result["reason"] = "no path in the supported local combinational subset"
        return result
    steps = []
    cursor = goal
    while previous[cursor] is not None:
        value, op, index = previous[cursor]
        steps.append({"from_value": value, "to_value": cursor,
                      "operand_index": index, "operation": op_summary(op)})
        cursor = value
    result.update(status="structural_path_found", path=list(reversed(steps)))
    return result


def find_instance(module, instance_name):
    matches = [op for op in module["operations"]
               if op.get("instance", {}).get("name") == instance_name]
    if len(matches) != 1:
        raise ValueError(f"instance {instance_name!r} missing or ambiguous")
    return matches[0]


def instance_value(op, port_name, direction):
    names = op["instance"][f"{direction}_names"]
    if names.count(port_name) != 1:
        raise ValueError(f"{direction} port {port_name!r} missing or ambiguous")
    values = op["operands"] if direction == "input" else op["results"]
    return values[names.index(port_name)]


def identity_chain(module, source, sink):
    """Trace backward only through typed HW wires, retaining every SSA edge."""
    definitions = {value: op for op in module["operations"] for value in op["results"]}
    cursor, reverse, seen = sink, [], set()
    while cursor != source and cursor not in seen:
        seen.add(cursor)
        op = definitions.get(cursor)
        if not op or not op["identity_wire"] or len(op["operands"]) != 1:
            return None
        previous = op["operands"][0]
        reverse.append({"from_value": previous, "to_value": cursor,
                        "operation": op_summary(op)})
        cursor = previous
    return list(reversed(reverse)) if cursor == source else None


def atlas_launch_wiring(modules):
    """Check two named source-level wiring expectations by exact SSA identity."""
    checks = []
    try:
        top = modules["AtlasCore"]
        scalar, mxu = find_instance(top, "scalar"), find_instance(top, "mxu1")
        source = instance_value(scalar, "io_mxu1Cmd_valid", "output")
        sink = instance_value(mxu, "io_cmd_valid", "input")
        checks.append({"module": "AtlasCore", "source": "scalar.io_mxu1Cmd_valid",
                       "sink": "mxu1.io_cmd_valid", "source_value": source,
                       "sink_value": sink, "same_ssa_value": source == sink,
                       "identity_path": identity_chain(top, source, sink),
                       "source_location": scalar["location"], "sink_location": mxu["location"]})
        wrapper = modules[mxu["instance"]["module"]]
        seq = find_instance(wrapper, "seq")
        inputs = [p for p in wrapper["ports"]
                  if p["name"] == "io_cmd_valid" and p["direction"] == "input"]
        if len(inputs) != 1:
            raise ValueError("wrapper cmd.valid input missing or ambiguous")
        source = inputs[0]["value"]
        sink = instance_value(seq, "io_cmd_valid", "input")
        checks.append({"module": wrapper["name"], "source": "io_cmd_valid",
                       "sink": "seq.io_cmd_valid", "source_value": source,
                       "sink_value": sink, "same_ssa_value": source == sink,
                       "identity_path": identity_chain(wrapper, source, sink),
                       "sink_location": seq["location"]})
        return {"status": "direct_wiring_confirmed" if all(c["identity_path"] is not None for c in checks)
                else "unresolved", "checks": checks,
                "note": "Direct command-valid wiring only; acceptance guards are not proven."}
    except (KeyError, ValueError) as error:
        return {"status": "unresolved", "checks": checks, "reason": str(error)}


def inventory(graph):
    census = graph["census"]
    sites = census["instance_sites"]
    declarations = census["external_or_generated_modules"]
    for declaration in declarations:
        declaration["instance_sites"] = [site for site in sites
                                         if site["module"] == declaration["name"]]
        declaration["interpretation"] = "Behavior is not defined by a HW module body in this artifact."
    children = defaultdict(list)
    for site in sites:
        children[site["parent_module"]].append(site)
    paths = []
    pending = deque([("TestHarness", [], frozenset())])
    while pending:
        name, path, ancestors = pending.popleft()
        if name == "AtlasCore":
            paths.append(path)
        elif name not in ancestors:
            for site in children[name]:
                pending.append((site["module"], path + [site], ancestors | {name}))
    return dict(census,
                internal_module_count=len(graph["available_modules"]),
                external_module_count=sum(d["kind"] == "hw.module.extern" for d in declarations),
                generated_module_count=sum(d["kind"] == "hw.module.generated" for d in declarations),
                instance_site_count=len(sites),
                instance_count_scope="Syntactic instance sites in module definitions; not an expanded hierarchy count.",
                testharness_to_atlas_paths=paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--toolchain", type=Path, required=True,
                        help="CIRCT installation prefix with include/lib/bin")
    parser.add_argument("--build-dir", type=Path,
                        default=checked_path(__file__).parents[1] / "build/rtlgraph-s0/query")
    parser.add_argument("--module", default="ScalarCore")
    parser.add_argument("--source-name", default="s1_fire")
    parser.add_argument("--sink-port", default="io_mxu1Cmd_valid")
    parser.add_argument("--export-json", type=Path,
                        help="optional full typed SSA export for selected modules")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--local-only", action="store_true",
                        help="Do not require Atlas command-valid wiring for success (e.g. fixtures)")
    args = parser.parse_args()
    scripts = checked_path(__file__).parent
    toolchain = checked_path(args.toolchain)
    circt_opt = checked_path(toolchain / "bin/circt-opt")
    build = checked_path(args.build_dir)
    source_ir = checked_path(args.input)
    exporter = checked_path(build / "rtlgraph_export")
    build_record = checked_path(build / "rtlgraph-build.json")
    cache = checked_path(build / "CMakeCache.txt")
    build_inputs = {
        "sources": {name: digest(scripts / name) for name in ("rtlgraph_query.cpp", "CMakeLists.txt")},
        "toolchain_prefix": str(toolchain),
        "cmake_packages": {name: digest(toolchain / f"lib/cmake/{name}/{config}")
                           for name, config in (("circt", "CIRCTConfig.cmake"),
                                                ("mlir", "MLIRConfig.cmake"),
                                                ("llvm", "LLVMConfig.cmake"))},
        "selected_static_libraries": {name: digest(toolchain / f"lib/lib{name}.a")
                                      for name in ("CIRCTHW", "CIRCTComb", "CIRCTSeq", "CIRCTSV",
                                                   "CIRCTVerif", "CIRCTLTL", "CIRCTEmit", "CIRCTOM",
                                                   "CIRCTSim", "MLIRParser", "MLIRIR", "LLVMSupport")},
    }
    if not args.skip_build:
        run(["cmake", "-S", str(scripts), "-B", str(build),
             f"-DCIRCT_DIR={toolchain / 'lib/cmake/circt'}",
             f"-DMLIR_DIR={toolchain / 'lib/cmake/mlir'}",
             f"-DLLVM_DIR={toolchain / 'lib/cmake/llvm'}",
             "-DCMAKE_BUILD_TYPE=Release"], timeout=120)
        run(["cmake", "--build", str(build), "-j2"], timeout=300)
        build_record.write_text(json.dumps({"inputs": build_inputs,
                                           "exporter_sha256": digest(exporter),
                                           "cmake_cache_sha256": digest(cache)},
                                          sort_keys=True, indent=2) + "\n")
    provenance = {"exporter": artifact(exporter), "build_requested": not args.skip_build,
                  "build_linkage": "UNVERIFIED", "current_build_inputs": build_inputs,
                  "scope": "Selected package/library/source hashes and CMake cache; not a hermetic toolchain capture."}
    if build_record.is_file() and cache.is_file():
        saved = json.loads(build_record.read_text())
        matched = (saved.get("inputs") == build_inputs
                   and saved.get("exporter_sha256") == digest(exporter)
                   and saved.get("cmake_cache_sha256") == digest(cache))
        provenance.update(build_linkage="recorded_build_matches_inputs" if matched else "UNVERIFIED",
                          build_manifest=artifact(build_record), cmake_cache=artifact(cache))
    selected = sorted({args.module, "AtlasCore", "InnerProductTreesTop", "InnerProductTreesSequencer"})
    exported = run([str(exporter), str(source_ir), *selected], timeout=120)
    graph = json.loads(exported.stdout)
    if args.export_json:
        checked_path(args.export_json).write_text(json.dumps(graph, sort_keys=True, indent=2) + "\n")
    modules = {module["name"]: module for module in graph["modules"]}
    query = (local_path(modules[args.module], args.source_name, args.sink_port)
             if args.module in modules else {"status": "unresolved", "reason": "query module missing"})
    wiring = atlas_launch_wiring(modules)
    success = (query["status"] == "structural_path_found"
               and (args.local_only or wiring["status"] == "direct_wiring_confirmed"))
    report = {
        "schema_version": 1,
        "status": "structural_checks_passed" if success else "unresolved",
        "atlas_wiring_required": not args.local_only,
        "evidence": "structural SSA connectivity; no timing or Boolean implication proof",
        "input": {"path": str(source_ir), "sha256": digest(source_ir)},
        "exporter_build": provenance,
        "toolchain": {"prefix": str(toolchain),
                      "circt_opt_version": run([str(circt_opt), "--version"], timeout=20).stdout.strip()},
        "query_sources_sha256": {name: digest(scripts / name)
                                 for name in ("rtlgraph_query.py", "rtlgraph_query.cpp", "CMakeLists.txt", "rtlgraph_s0.py")},
        "missing_modules": graph["missing_modules"],
        "query": query,
        "atlas_command_valid_wiring": wiring,
        "census": inventory(graph),
        "hierarchy": {name: [dict(op["instance"], location=op["location"])
                             for op in module["operations"] if "instance" in op]
                      for name, module in modules.items()},
        "limitations": [
            "A structural path can be masked or cancelled by other inputs.",
            "State, instances, multi-result operations and unsupported operations cut local traversal.",
            "Named-value lookup is for discovery; SSA identities establish connectivity.",
            "HW wire identities assume no external force overrides; SV inout/assignment behavior is not inferred.",
            "This query does not validate issue legality, engine acceptance, resource timing, or scheduling safety."]}
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"rtlgraph_query: {error}", file=sys.stderr)
        sys.exit(2)
