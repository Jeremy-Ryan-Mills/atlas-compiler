# RTL graph S0: reproducible artifact and structural query

The selected configuration is `chipyard.AtlasShuttleVectorConfig`, with `chipyard.harness.TestHarness` as the elaborated top. The S0 helpers generate FIRRTL and CIRCT hardware IR from the existing Chipyard generator JAR, record provenance, and query typed SSA connections from scalar issue to the MXU1 command interface. Independent IR verification and the target structural query have passed. This is a build/query prototype; current-source equivalence, functional RTL simulation, and instruction timing remain unvalidated. The compiler's scheduling model and the finished RTL are unchanged.

## Configuration identity and provenance

The source configuration is [AtlasConfigs.scala](../../chipyard/config/AtlasConfigs.scala#L40-L48). It composes Atlas, one Shuttle core with Saturn `WithShuttleVectorUnit(256, 128, VectorParams.genParams)`, 256-bit SBUS, 16-byte Shuttle tile beats, 64-byte cache blocks, and `EE290BaseConfig`. The full-system environment and its limits are described in [RTL_GRAPH_PLAN.md §4.1](../.agents/notes/RTL_GRAPH_PLAN.md#41-reproducible-hardware-boundary).

The cached `.classpath_cache/chipyard.jar` was used for elaboration. Read-only inspection with `javap -p -c -classpath .classpath_cache/chipyard.jar chipyard.AtlasShuttleVectorConfig` confirmed that its compiled constructor composes those same named mixins and explicit arguments. Its manifest reports the generic Chipyard version `1.6`, with no Git revision. This establishes a useful configuration-identity check; it does not establish that every compiled class corresponds to the current working tree. Artifact provenance must retain the JAR hash, relevant input hashes, tool versions, command, and working directory. A future source rebuild must be recorded as a distinct input lineage.

The emitted file starts with `FIRRTL version 3.3.0` and preserves source locators into Atlas RTL, including ScalarCore, both MXU sequencers, and AtlasCore. Source locators identify the original source locations; they do not prove source freshness or semantic equivalence to the present files.

The manifest records 25 selected source files and 11 repository records, including the course-specific TestChipIP, Saturn, Shuttle, and Rocket Chip origins from the root `.gitmodules`. These are a selected provenance sample, not a complete source snapshot or a clean-worktree assertion. The CIRCT source checkout differs from the parent's gitlink in this session; the installed `firtool` and `circt-opt` executables are identified separately by version and SHA-256. A source-checkout revision must not be attributed to those installed binaries without a build record.

## Existing build route

Chipyard's local [simulation documentation](../../../../docs/Simulation/Software-RTL-Simulation.rst#L118-L170) explains the generator project, configuration package, and harness/design-top distinction. The actual elaboration invocation is in [common.mk](../../../../common.mk#L149-L157), with the direct Java wrapper in [variables.mk](../../../../variables.mk#L280-L285). The wrapper runs from the Chipyard repository root.

The following is the source-derived invocation shape; retain the exact command and environment from a particular run alongside its artifacts:

```bash
java -cp .classpath_cache/chipyard.jar chipyard.Generator \
  --target-dir generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/elaboration \
  --name chipyard.harness.TestHarness.AtlasShuttleVectorConfig \
  --top-module chipyard.harness.TestHarness \
  --legacy-configs chipyard:AtlasShuttleVectorConfig
```

Java heap/stack settings and environment-dependent inputs belong in the run record. This recipe uses the existing assembly; it does not compile the sources. The project is `chipyard`: [build.sbt](../../../../build.sbt#L220-L258) discovers initialized generator submodules, adds their dependencies, and includes their `chipyard` source directories. [Atlas project dependencies](../../../../build.sbt#L288-L291) define Atlas's project dependencies. No separate `chipyard_atlas` project is required by this checkout.

The selected stage explicitly requests CHIRRTL at [ChipyardStage.scala](../../../../tools/stage/src/main/scala/ChipyardStage.scala#L12-L34), then emits annotations and other artifacts at [artifact emission](../../../../tools/stage/src/main/scala/ChipyardStage.scala#L49-L58). FIRRTL emission is the appropriate boundary before a separate CIRCT lowering step.

Stock Make invocation is unsuitable under the active access restriction without first replacing its discovery behavior. [common.mk](../../../../common.mk#L104-L128) performs broad recursive source discovery with symlink following and without the mandatory prohibited-name exclusions. This can occur while Make expands prerequisites, including during a dry run. The direct Java route avoids those Make scans; it does not excuse checking the actual generator inputs and resource paths.

## FIRRTL and annotation handling

The emitted circuit begins with `circuit TestHarness :%[[`, carrying embedded annotations. Its sidecar `.anno.json` contains the corresponding 8,653 annotations: 8,028 deduplication groups, 273 inline black boxes, 113 enum components, 102 do-not-touch annotations, 72 inline annotations, 47 decoder tables, 10 enum vectors, and 8 enum definitions. The inspection found no file/path/resource fields requiring an external file read. Inline black-box source still requires explicit treatment when establishing the semantics of the resulting hardware model.

The replay driver inspects embedded annotations and rejects classes outside the reviewed set before lowering. It consumes the FIRRTL with its embedded annotations and does not also pass the sidecar with `--annotation-file`, which would duplicate this annotation input. The ordinary Verilog rule in [common.mk](../../../../common.mk#L217-L236) also replaces sequential memories and emits split Verilog; it should not be copied unchanged for an analysis-oriented hardware IR artifact.

## Lowering and structural evidence

The first `firtool --ir-hw` attempt used debug lowering, named-value preservation, source-location emission, and aggregate preservation. It failed on a clock aggregate with `--preserve-aggregate=all`. Removing that flag produced `build/rtlgraph-s0/atlas.hw.mlir` (53,988,506 bytes) from the 175,132,286-byte FIRRTL input. Independent `circt-opt` parsing/verification and the typed C++ exporter both accepted this artifact. The matching installed tools report `CIRCT firtool-1.75.0`.

The retry command shape is:

```bash
firtool elaboration/chipyard.harness.TestHarness.AtlasShuttleVectorConfig.fir \
  --ir-hw -O=debug --preserve-values=named --mlir-print-debuginfo \
  --disable-annotation-unknown --warn-on-unprocessed-annotations \
  -o atlas.hw.mlir
```

This lowering runs from the artifact directory; the driver records the absolute binary, arguments, working directory, versions, hashes, and logs. Unknown-annotation tolerance and warnings are not a completeness guarantee: classify any retained or discarded annotation and any unsupported or external module that could affect subsequent extraction claims. The observed lowering warnings concern deprecated printf-encoded verification operations; this task does not prove that assertion semantics are preserved for a later simulation backend.

The typed census contains 702 internal module definitions, 15 external module declarations, no generated-module declarations, and 2,631 syntactic instance sites. Instance sites are counted once in each module definition, not expanded into physical instance multiplicities. Residual dialects are `builtin`, `comb`, `emit`, `hw`, `om`, `seq`, and `sv`; the result is not exclusively HW/Comb/Seq. External declarations include `SimDRAM`, `SimTSI`, clock/clock-gating models, I/O cells, plusarg readers, and debug/serial harness models. The census preserves names, ports, attributes, source locations, and instance sites, but supplies no behavioral contracts for them.

The hierarchy query finds `TestHarness.chiptop0.system.domain.atlasTile.core` as an `AtlasCore` instance. Within `ScalarCore`, the local query finds `s1_fire` → `comb.and` → `is_mxu1_launch` → `io_mxu1Cmd_valid`. The AtlasCore connection uses scalar result index 13 as MXU1's `io_cmd_valid` input. The MXU1 wrapper connects its input to `seq.io_cmd_valid` through a typed `hw.wire` identity operation. These conclusions come from CIRCT/MLIR values and port indices, with name lookup used only to select the endpoints.

A fresh driver replay passed Java elaboration, `firtool` lowering, and independent `circt-opt` verification. Its FIRRTL and annotation sidecar match the original bytes exactly. Its hardware IR differs only in 201 source-location filename literals: replacing the absolute replay FIRRTL filename with the original relative filename makes every byte match. The [comparison record](../build/rtlgraph-s0-replay/artifact-comparison.json) documents this narrow normalization; it does not establish equivalence to current Scala sources. The fresh [manifest](../build/rtlgraph-s0-replay/s0-manifest.json) records all three successful commands.

The local traversal stops at state, module instances, region-bearing operations, SV inout reads, and unsupported or multi-result operations. Explicit instance-wiring checks separately preserve the correct result index. A structural path can still be masked or cancelled by other inputs. The query does not prove that every `s1_fire` launches MXU1, recover the sequencer's `acceptCompute` condition, establish cycle ages, or show that a schedule is safe.

## Replaying the tools

Run these commands from the compiler repository. The driver selects the enclosing Chipyard installation, verifies the Java/Espresso/CIRCT tools, and runs Java from the Chipyard root. It uses the cached assembly and does not invoke Make, SBT, or Mill.

```sh
python3 scripts/rtlgraph_s0.py --output build/rtlgraph-s0-replay
python3 scripts/rtlgraph_query.py build/rtlgraph-s0-replay/atlas.hw.mlir \
  --toolchain ../../../.conda-env/riscv-tools \
  --build-dir build/rtlgraph-s0/query \
  --export-json build/rtlgraph-s0-replay/atlas-typed.json \
  > build/rtlgraph-s0-replay/atlas-query.json
```

The [query helper](../scripts/rtlgraph_query.py#L198-L299) builds a small C++ executable against installed CIRCT/MLIR headers and libraries with CMake; Python supplies the CLI and graph traversal without parsing printed MLIR. Its report identifies the IR and exporter, selected build inputs, and build-cache provenance. `--skip-build` records whether the saved build manifest matches those inputs; a missing or mismatched record leaves build linkage explicitly unverified. This is a selected build record, not a hermetic toolchain archive.

To verify the original saved artifacts without re-elaborating:

```sh
python3 scripts/rtlgraph_s0.py \
  --existing-elaboration build/rtlgraph-s0/elaboration \
  --existing-hw-ir build/rtlgraph-s0/atlas.hw.mlir
```

Each artifact directory holds `s0-manifest.json` and stage logs. Adoption explicitly leaves the original JAR→FIRRTL and FIRRTL→IR command linkage unverified. A fresh replay records those executed commands but still cannot establish the cached JAR's correspondence to current Scala sources. Query exit code 0 means the requested structural checks passed, 1 means a requested path/wiring check remained unresolved, and 2 means tool execution failed. Generated artifacts stay under ignored `build/` directories.

The saved [original query](../build/rtlgraph-s0/atlas-query.json) and [fresh replay query](../build/rtlgraph-s0-replay/atlas-query.json) identify their input hashes and selected exporter build records. The [typed exporter](../scripts/rtlgraph_query.cpp#L32-L100) preserves SSA result and port indices; the [query fixtures](../scripts/tests/test_rtlgraph_query.py#L19-L54) passed positive-path, sequential/instance boundary, missing/ambiguous endpoint, and distinct multi-output-index checks. Replay preflight checks also rejected external-file annotations, unsafe inline filenames, malformed annotation input, and missing executables without marking the workflow complete. These are tool checks, not functional RTL simulations.

After building the exporter, rerun the structural fixtures with `python3 scripts/tests/test_rtlgraph_query.py build/rtlgraph-s0/query/rtlgraph_export`.

The full S0 stage in the plan remains open: a current-source generator lineage, explicit simulation/environment contracts, and a functional RTL smoke case are still needed. The next analysis task is to extend the selected slice from command-valid wiring into MXU1 acceptance guards and operand events, with independent simulation or proof before emitting scheduling facts.

## Compiler contract checklist

The structural query is an input to model extraction; it does not populate a scheduling profile. The consumer audit identifies these requirements before a schema or adapter can replace the current model. Every row below remains unextracted from the hardware artifact.

| Consumer requirement | Required hardware facts and serialization coverage | Current implementation |
| --- | --- | --- |
| Element accesses | Storage identity, explicit/implicit operands, row ranges, first access age, per-element step, unknown aliases, and completion-relative accesses | [Access](../src/core/machine.h#L17-L31), [operand/address mapping](../src/core/machine.cpp#L64-L147) |
| Resource occupancy | Inclusive acquire/release windows, resource index, alternatives, capacities, and overlapping in-flight operations | [Hold](../src/core/machine.h#L33-L55), [capacity](../src/core/machine.cpp#L12-L15), [reservation checks](../src/core/reservations.cpp#L5-L78) |
| Logical reservations | MREG read/write sets, release ages, the `vload` exception, and operand legality | [Footprint](../src/core/machine.h#L57-L68) |
| Physical ports | Logical-register to bank/row mapping, sharing rules, same-row visibility/conflicts, and resource use by a single instruction | [port requests and conflicts](../src/core/reservations.cpp#L20-L78) |
| Instruction dependencies | RAW/WAR/WAW visibility and sequencer predicates, separate from resource exclusion | [dependence](../src/core/machine.cpp#L418-L517), [graph construction](../src/core/depgraph.cpp#L58-L110) |
| VPU overlap | Slot lifetimes, two-slot operations, and incompatible operation groups | [overlap rules](../src/core/machine.cpp#L34-L60), [slot checks](../src/core/reservations.cpp#L35-L48) |
| Variable completion | DMA command capture, channel reuse, waits and resource uncertainty; keep cost estimates distinct from completion guarantees | [DMA graph rules](../src/core/depgraph.cpp#L75-L110), [reservation widening](../src/core/reservations.cpp#L100-L113) |
| Frontend and publication | Actual issue, `delay`, control-flow barriers, `halt`, and `atlas.release` obligations | [barriers](../src/core/machine.cpp#L29-L32), [publication and halt checking](../src/core/simulator.cpp#L120-L144) |

The model boundary extends beyond `Footprint`. A future adapter must preserve the accompanying functions and consumers or reject unsupported semantics explicitly. The compiler checker reuses the model's profiles and dependency functions, so compiler agreement is not independent RTL validation. Host launch/completion and IMEM ownership remain separate obligations described in the shared [host/device semantics](../../../../.agents/host-device-kernel-semantics.md).
