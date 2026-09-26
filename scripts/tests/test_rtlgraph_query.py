#!/usr/bin/env python3
"""Run the real typed exporter on small positive and deliberately blocked IR.

Usage: python scripts/tests/test_rtlgraph_query.py build/rtlgraph-s0/query/rtlgraph_export
"""

import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
from rtlgraph_query import (atlas_launch_wiring, find_instance, identity_chain,
                            instance_value, inventory, local_path)
from rtlgraph_s0 import checked_path


def main():
    exporter = checked_path(sys.argv[1])
    fixture = checked_path(Path(__file__).parent / "rtlgraph-query.mlir")
    selected = ["ScalarCore", "AtlasCore", "InnerProductTreesTop",
                "InnerProductTreesSequencer", "StateCutpoint", "InstanceCutpoint", "Ambiguous"]
    graph = json.loads(subprocess.check_output([str(exporter), str(fixture), *selected], text=True))
    modules = {mod["name"]: mod for mod in graph["modules"]}
    assert not graph["missing_modules"]
    positive = local_path(modules["ScalarCore"], "s1_fire", "io_mxu1Cmd_valid")
    assert positive["status"] == "structural_path_found", positive
    assert [step["operation"]["kind"] for step in positive["path"]] == ["comb.and", "hw.wire"]
    for name, kind in (("StateCutpoint", "seq.compreg"), ("InstanceCutpoint", "hw.instance")):
        blocked = local_path(modules[name], "s1_fire", "result")
        assert blocked["status"] == "unresolved", blocked
        assert kind in [op["kind"] for op in blocked["encountered_cutpoints"]]
    assert local_path(modules["Ambiguous"], "s1_fire", "result")["status"] == "unresolved"
    assert local_path(modules["ScalarCore"], "absent", "io_mxu1Cmd_valid")["status"] == "unresolved"
    assert local_path(modules["ScalarCore"], "s1_fire", "absent")["status"] == "unresolved"
    wiring = atlas_launch_wiring(modules)
    assert wiring["status"] == "direct_wiring_confirmed", wiring
    first, second = wiring["checks"]
    assert first["source_value"].endswith(".r1"), first
    assert first["same_ssa_value"] and first["identity_path"] == []
    assert not second["same_ssa_value"] and len(second["identity_path"]) == 1
    top = modules["AtlasCore"]
    scalar = find_instance(top, "scalar")
    wrong = instance_value(scalar, "unused", "output")
    assert identity_chain(top, wrong, first["sink_value"]) is None
    assert atlas_launch_wiring({})["status"] == "unresolved"
    census = inventory(graph)
    assert census["external_module_count"] == 1
    assert census["generated_module_count"] == 0
    opaque = census["external_or_generated_modules"][0]
    assert [p["name"] for p in opaque["ports"]] == ["issue", "unrelated", "result"]
    assert [p["index"] for p in opaque["ports"]] == [0, 0, 1]
    assert opaque["instance_sites"][0]["parent_module"] == "InstanceCutpoint"
    print("PASS: typed positive path, state/instance cutpoints, ambiguity, exact multi-output ports, wire identity, external census")


if __name__ == "__main__":
    main()
