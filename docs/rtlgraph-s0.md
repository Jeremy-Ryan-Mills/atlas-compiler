# RTL graph S0: reproducible artifact and structural query

The current checkpoint builds the Chipyard generator from the checked-out sources, elaborates `chipyard.EE290SimConfig` with `chipyard.harness.TestHarness` as top, and lowers it to CIRCT hardware IR. On 2026-09-26, the source build, fresh elaboration/lowering, independent IR verification, and typed scalar-to-MXU1 structural query passed. The build record links the JAR to unchanged scoped inputs. The subsequent [MXU1 event and replay checkpoint](rtlgraph-mxu1.md) and [passive tracing](rtlgraph-mxu1-timing.md) cover both the initial single-compute witness and a finite K64 baseline with eight computes, four weight pushes, and four FP8 pops. The [kernel guide](rtlgraph-kernels.md) and [experimental partial profile](rtlgraph-profile.md) now connect that evidence to opt-in compiler scheduling. The default model and Atlas RTL remain unchanged; universal scheduling safety is not established.

The selected target is `chipyard.EE290SimConfig`, matching the [baremetal Makefile default](../../baremetal/Makefile#L1-L3) and [simulator invocation](../../baremetal/Makefile#L119-L123). Its [definition](../../../chipyard/src/main/scala/config/EE290Configs.scala#L41-L54) selects Saturn `mxParams`, a broadcast coherence manager, and Tacit tracing. The driver now accepts `--config` (default `EE290SimConfig`), `--generator-jar`, and `--generator-build-manifest`; its default output is `build/rtlgraph-s0/<config>`. Historical artifact counts and validation results below describe the completed `AtlasShuttleVectorConfig` prototype. No existing artifact has been relabeled.

## Current `EE290SimConfig` checkpoint

The [build manifest](../build/rtlgraph-source-build/build-manifest.json) records the successful direct SBT assembly, Java/launcher and driver hashes, environment choices, and input snapshots. The [input inventory](../build/rtlgraph-source-build/inputs-before.json) covers 8,804 files in 249 declared roots; the before/after SHA-256 is `de8ae7ee79f321e59eef4a210451ad7e1c30eb278d7e4e94bb61035ad407db9c` in both snapshots. The SBT log reports compilation of 1,726 Scala and six Java sources. Its published dependencies and incremental compilation machinery are still cached; the snapshot is scoped rather than a complete hermetic input archive.

The fresh [elaboration manifest](../build/rtlgraph-s0/EE290SimConfig/s0-manifest.json) records `RECORDED_BUILD_HASH_MATCH`, all three successful Java/`firtool`/`circt-opt` commands, and CIRCT `firtool-1.75.0`. The emitted FIRRTL is version 3.3.0. All 8,901 embedded annotations belong to the reviewed inline/non-file classes; 268 carry inline black-box source. Elaboration reports 128 warnings, and lowering reports deprecated printf-encoded verification operations. Preserve those logs when establishing later functional and assertion semantics.

| Artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| Current-source generator JAR | 433,585,494 | `617e8a38d4365f877ed02df0c1ad700d0b54c73a34ce6308c770886dc0400084` |
| `EE290SimConfig` FIRRTL | 178,739,543 | `197aab9e74d40e98d4291bd8ffb34e018c74e9e5146196384f1f3405c3c9e989` |
| [CIRCT hardware IR](../build/rtlgraph-s0/EE290SimConfig/atlas.hw.mlir) | 58,213,524 | `d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2` |

The new [typed query report](../build/rtlgraph-s0/EE290SimConfig/atlas-query.json) passes the requested structural checks. Its census contains 735 internal modules, 16 external declarations, no generated-module declarations, 2,698 syntactic instance sites, and 576,559 operations. Residual dialects are `builtin`, `comb`, `emit`, `hw`, `om`, `seq`, and `sv`. The external declarations include `TraceSinkMonitor` as well as memory, serial/debug, clock, I/O-cell, and plusarg models; their behavioral contracts remain open work.

The query independently confirms `s1_fire` → `comb.and` → `is_mxu1_launch` → `io_mxu1Cmd_valid`, scalar result index 13 feeding MXU1's command-valid input in `AtlasCore`, and the wrapper's typed wire connection to the sequencer. These are wiring facts from the new IR. This original query does not recover `acceptCompute`; the separate [event query](rtlgraph-mxu1.md#extracted-events-and-their-scope) now checks its local Boolean function and locates operand/result events. Neither establishes operand access ages or validates a legal instruction schedule.

## Historical configuration identity and provenance

The source configuration is [AtlasConfigs.scala](../../chipyard/config/AtlasConfigs.scala#L40-L48). It composes Atlas, one Shuttle core with Saturn `WithShuttleVectorUnit(256, 128, VectorParams.genParams)`, 256-bit SBUS, 16-byte Shuttle tile beats, 64-byte cache blocks, and `EE290BaseConfig`. The full-system environment and its limits are described in [RTL_GRAPH_PLAN.md §4.1](../.agents/notes/RTL_GRAPH_PLAN.md#41-reproducible-hardware-boundary).

The cached `.classpath_cache/chipyard.jar` was used for elaboration. Read-only inspection with `javap -p -c -classpath .classpath_cache/chipyard.jar chipyard.AtlasShuttleVectorConfig` confirmed that its compiled constructor composes those same named mixins and explicit arguments. Its manifest reports the generic Chipyard version `1.6`, with no Git revision. This establishes a useful configuration-identity check; it does not establish that every compiled class corresponds to the current working tree. Artifact provenance must retain the JAR hash, relevant input hashes, tool versions, command, and working directory. A future source rebuild must be recorded as a distinct input lineage.

The emitted file starts with `FIRRTL version 3.3.0` and preserves source locators into Atlas RTL, including ScalarCore, both MXU sequencers, and AtlasCore. Source locators identify the original source locations; they do not prove source freshness or semantic equivalence to the present files.

The manifest records 25 selected source files and 11 repository records, including the course-specific TestChipIP, Saturn, Shuttle, and Rocket Chip origins from the root `.gitmodules`. These are a selected provenance sample, not a complete source snapshot or a clean-worktree assertion. The CIRCT source checkout differs from the parent's gitlink in this session; the installed `firtool` and `circt-opt` executables are identified separately by version and SHA-256. A source-checkout revision must not be attributed to those installed binaries without a build record.

## Historical cached-JAR build route

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

In the historical `AtlasShuttleVectorConfig` run, the emitted circuit begins with `circuit TestHarness :%[[`, carrying embedded annotations. Its sidecar `.anno.json` contains the corresponding 8,653 annotations: 8,028 deduplication groups, 273 inline black boxes, 113 enum components, 102 do-not-touch annotations, 72 inline annotations, 47 decoder tables, 10 enum vectors, and 8 enum definitions. The inspection found no file/path/resource fields requiring an external file read. Inline black-box source still requires explicit treatment when establishing the semantics of the resulting hardware model.

The replay driver inspects embedded annotations and rejects classes outside the reviewed set before lowering. It consumes the FIRRTL with its embedded annotations and does not also pass the sidecar with `--annotation-file`, which would duplicate this annotation input. The ordinary Verilog rule in [common.mk](../../../../common.mk#L217-L236) also replaces sequential memories and emits split Verilog; it should not be copied unchanged for an analysis-oriented hardware IR artifact.

## Historical lowering and structural evidence

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

Run these commands from the compiler repository. The build helper runs SBT directly against the current sources, with a fresh global settings directory and a scoped input inventory before and after compilation. Its output is separate from the historical cached assembly. SBT incremental compilation and published dependency caches remain in use; this is a recorded build, not a hermetic build proof. The elaboration driver verifies the resulting JAR hash against that record, selects the enclosing Chipyard Java/Espresso/CIRCT tools, and runs Java from the Chipyard root. Neither helper invokes Make or Mill.

```sh
python3 scripts/rtlgraph_build.py
python3 scripts/rtlgraph_s0.py --config EE290SimConfig \
  --generator-jar build/rtlgraph-source-build/chipyard-current.jar \
  --generator-build-manifest build/rtlgraph-source-build/build-manifest.json
python3 scripts/rtlgraph_query.py build/rtlgraph-s0/EE290SimConfig/atlas.hw.mlir \
  --toolchain ../../../.conda-env/riscv-tools \
  --build-dir build/rtlgraph-s0/query \
  --export-json build/rtlgraph-s0/EE290SimConfig/atlas-typed.json \
  > build/rtlgraph-s0/EE290SimConfig/atlas-query.json
```

The environment recipe is the user's `ee194_env.sh`, found in this installation at `/bwrcq/home/reednicolas/bin/ee194_env.sh` (also available through `~/bin/ee194_env.sh`); the requested `/user/reednicolas/bin/ee194_env.sh` path was absent. The helpers select the project Conda Java and RISC-V tool paths directly. The source build keeps `USE_CHISEL7` and `CONSTELLATION_STANDALONE` unset to match the audited Chisel 6 route. VCS license setup, shell integration, and the recipe's `mill --version` check are unnecessary for this build/lowering stage. The [baremetal README](../../baremetal/README.md#L13-L36) supplies the compile/run workflow; the separate [replay guide](rtlgraph-mxu1.md#replay) applies the recipe's BWRC and Conda setup for VCS. The direct SBT settings retain Chipyard's [batch startup fallback](../../../../variables.mk#L267-L278); a short temporary runtime directory handles Unix-socket path limits.

The [source inventory](../scripts/rtlgraph_build_inputs.py) covers explicitly listed SBT metadata, source, resource, glue, and unmanaged-library roots. It records content hashes and initialized optional generators, follows checked resource symlinks, and excludes generated outputs. The [build manifest](../build/rtlgraph-source-build/build-manifest.json) records the command, logs, environment choices, tool hashes, and snapshot hashes. Only a successful build with matching before/after input hashes and a matching JAR hash is accepted as `RECORDED_BUILD_HASH_MATCH`. This status describes the supplied build record's scope; it does not establish timing correctness or archive every resolved dependency.

The [query helper](../scripts/rtlgraph_query.py#L198-L299) builds a small C++ executable against installed CIRCT/MLIR headers and libraries with CMake; Python supplies the CLI and graph traversal without parsing printed MLIR. Its report identifies the IR and exporter, selected build inputs, and build-cache provenance. `--skip-build` records whether the saved build manifest matches those inputs; a missing or mismatched record leaves build linkage explicitly unverified. This is a selected build record, not a hermetic toolchain archive.

To verify the original saved artifacts without re-elaborating:

```sh
python3 scripts/rtlgraph_s0.py \
  --config AtlasShuttleVectorConfig \
  --output build/rtlgraph-s0-adopted \
  --existing-elaboration build/rtlgraph-s0/elaboration \
  --existing-hw-ir build/rtlgraph-s0/atlas.hw.mlir
```

Each artifact directory holds `s0-manifest.json` and stage logs. Adoption explicitly leaves the original JAR→FIRRTL and FIRRTL→IR command linkage unverified. A fresh replay records those executed commands but still cannot establish the cached JAR's correspondence to current Scala sources. Query exit code 0 means the requested structural checks passed, 1 means a requested path/wiring check remained unresolved, and 2 means tool execution failed. Generated artifacts stay under ignored `build/` directories.

The saved [original query](../build/rtlgraph-s0/atlas-query.json) and [fresh replay query](../build/rtlgraph-s0-replay/atlas-query.json) identify their input hashes and selected exporter build records. The [typed exporter](../scripts/rtlgraph_query.cpp#L32-L100) preserves SSA result and port indices; the [query fixtures](../scripts/tests/test_rtlgraph_query.py#L19-L54) passed positive-path, sequential/instance boundary, missing/ambiguous endpoint, and distinct multi-output-index checks. Replay preflight checks also rejected external-file annotations, unsafe inline filenames, malformed annotation input, and missing executables without marking the workflow complete. These are tool checks, not functional RTL simulations.

After building the exporter, rerun the structural fixtures with `python3 scripts/tests/test_rtlgraph_query.py build/rtlgraph-s0/query/rtlgraph_export`.

The build and structural-query portion now has a recorded current-source lineage for `EE290SimConfig`. The [event/replay checkpoint](rtlgraph-mxu1.md) and [timing trace](rtlgraph-mxu1-timing.md#k64-baseline) also record finite accumulation, overlapping commands, scalar-issue alignment, and complete row streams for the K64 baseline, with all 1,024 golden output words passing. These observations support an [experimental two-field profile](rtlgraph-profile.md), not extraction of the complete machine model. Full S0 remains open for explicit simulation/environment contracts and source-to-executable build linkage. Broader interference tests, same-cycle visibility, and bounded temporal properties remain open work.

## Compiler contract checklist

The original structural query is an input to model extraction; it does not itself populate a scheduling profile. The separate [partial-profile extractor](rtlgraph-profile.md) now derives the core valid-register chain and accumulator-read guard, corroborates scalar-relative row ages in a finite trace, and projects two MXU1 fields. The consumer audit below still describes the broader requirements for replacing the complete model; the remaining fields and rules are inherited rather than extracted.

| Consumer requirement | Required hardware facts and serialization coverage | Current implementation |
| --- | --- | --- |
| Element accesses | Storage identity, explicit/implicit operands, row ranges, first access age, per-element step, unknown aliases, and completion-relative accesses | [Access](../src/core/machine.h#L31-L42), [operand/address mapping](../src/core/machine.cpp#L64-L147) |
| Resource occupancy | Inclusive acquire/release windows, resource index, alternatives, capacities, and overlapping in-flight operations | [Hold](../src/core/machine.h#L59-L66), [capacity](../src/core/machine.cpp#L12-L15), [reservation checks](../src/core/reservations.cpp#L5-L78) |
| Logical reservations | MREG read/write sets, release ages, the `vload` exception, and operand legality | [Footprint](../src/core/machine.h#L68-L79) |
| Physical ports | Logical-register to bank/row mapping, sharing rules, same-row visibility/conflicts, and resource use by a single instruction | [port requests and conflicts](../src/core/reservations.cpp#L20-L78) |
| Instruction dependencies | RAW/WAR/WAW visibility and sequencer predicates, separate from resource exclusion | [dependence](../src/core/machine.cpp#L418-L517), [graph construction](../src/core/depgraph.cpp#L58-L110) |
| VPU overlap | Slot lifetimes, two-slot operations, and incompatible operation groups | [overlap rules](../src/core/machine.cpp#L34-L60), [slot checks](../src/core/reservations.cpp#L35-L48) |
| Variable completion | DMA command capture, channel reuse, waits and resource uncertainty; keep cost estimates distinct from completion guarantees | [DMA graph rules](../src/core/depgraph.cpp#L75-L110), [reservation widening](../src/core/reservations.cpp#L100-L113) |
| Frontend and publication | Actual issue, `delay`, control-flow barriers, `halt`, and `atlas.release` obligations | [barriers](../src/core/machine.cpp#L29-L32), [publication and halt checking](../src/core/simulator.cpp#L120-L144) |

The model boundary extends beyond `Footprint`. A future adapter must preserve the accompanying functions and consumers or reject unsupported semantics explicitly. The compiler checker reuses the model's profiles and dependency functions, so compiler agreement is not independent RTL validation. Host launch/completion and IMEM ownership remain separate obligations described in the shared [host/device semantics](../../../../.agents/host-device-kernel-semantics.md).
