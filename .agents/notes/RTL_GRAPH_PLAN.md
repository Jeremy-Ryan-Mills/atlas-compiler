# atlas-rtlgraph: Deriving an Instruction Scheduling Model from RTL

Planning document, updated 2026-09-28. Current-source `chipyard.EE290SimConfig` elaboration and CIRCT verification passed. Typed queries and independently checked traces support partial DMA/MXU compiler profiles and conditional LSU timing. The assembly handoff packages those profiles with resolved compiler footprints. Complete model extraction and whole-hardware scheduling proofs remain unfinished; the cached simulator's source-to-binary linkage is unverified. Historical `AtlasShuttleVectorConfig` artifacts retain their original identities.

Source links target inspected passages where available. Local anchors refer to this checkout; external references should use immutable revisions when available. Unverified sources retain file-level links.

The goal is to derive a compiler's timing and hazard model from hardware evidence: which operands an instruction accesses, when those accesses occur, which resources it occupies, and which launch conditions software must satisfy. The lookup tables describe instruction scheduling, not FPGA lookup-table cells.

The initial consumer is the existing `atlas-opt` compiler. Its [PLAN.md](PLAN.md#L11-L20) describes a scheduler targeting [npu_model's rtl-match branch](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match); the [current machine interface](../../src/core/machine.h#L17-L117) is the compatibility baseline. That model already includes row-timed accesses, pipelined MXUs, logical reservations, and physical-resource checks. The project replaces or corroborates those facts with evidence from a pinned RTL configuration; it does not begin from a completion-only, nonpipelined model.

The selected extraction and baremetal-validation target is `chipyard.EE290SimConfig`, defined in [EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L41-L54). The [baremetal Makefile](../../../baremetal/Makefile#L1-L3) defaults `CONFIG` to `EE290SimConfig` and passes it to both [simulator construction](../../../baremetal/Makefile#L96-L102) and [test execution](../../../baremetal/Makefile#L119-L123); the [baremetal README](../../../baremetal/README.md#L13-L36) uses the same configuration. Record any per-run override. Fresh artifacts now exist for this target; historical `AtlasShuttleVectorConfig` artifacts remain separate. Use the enclosing Chipyard checkout's actual submodule origins (§2.4), including its course-specific TestChipIP, Saturn, Shuttle, and Rocket Chip repositories. Section 4.1 records the configuration composition and remaining environment assumptions.

## Current checkpoint

The [S0 build and query](../../docs/rtlgraph-s0.md) use a fresh `EE290SimConfig` elaboration, verified CIRCT IR, and typed structural checks. [MXU1](../../docs/rtlgraph-mxu1.md), [MXU0](../../docs/rtlgraph-mxu0.md), [VPU](../../docs/rtlgraph-vpu.md), [LSU](../../docs/rtlgraph-lsu.md), [DMA](../../docs/rtlgraph-dma-hardware.md), and [MREG banks](../../docs/rtlgraph-banks.md) have scoped structural or temporal evidence. The individual guides retain counts, traces, assumptions, and failures.

The compiler consumes optional [MXU profiles](../../docs/rtlgraph-profile.md) and a [native DMA profile](../../docs/rtlgraph-dma-native.md), including [configured DRAM ranges](../../docs/rtlgraph-dram-ranges.md). The [full-kernel comparisons](../../docs/rtlgraph-mixed-dma.md) cover all eleven performance kernels with full output goldens. Controlled fused-attention completion improved from **25,445 to 22,132 edges** on MXU0 and **24,932 to 22,132** on MXU1 against handwritten assembly. Results include output DMA; they are finite measurements under each kernel's shared environment. Adding the MXU1 profile at fixed critical priority made completion four edges worse than DMA-only scheduling, so a local timing improvement does not guarantee a faster whole kernel. Earlier [compute-window comparisons](../../docs/rtlgraph-corpus-scheduling.md) and [fixed-operation bounds](../../docs/rtlgraph-search.md) retain separate scopes.

[Conditional LSU timing](../../docs/rtlgraph-lsu-timing.md) derives source requests at ages 1–32, writes at 3–34, and release at 35. The response query now checks the one-cycle memory contract across all 32 MREG and six VMEM banks, under explicit arbitration exclusions. Payload/row routing and destination-write acceptance remain open. These results corroborate existing compiler ages. The complete machine schema, whole-hardware temporal proof, arbitrary DMA completion bound, and cached simulator source-to-binary linkage remain open. Generated IR, traces, and result manifests live under ignored `build/` paths, with reproduction commands in the linked guides.

## 1. Deliverable and the roles of the graphs

```text
EE290SimConfig + pinned RTL + environment assumptions
    -> elaborated CIRCT hardware IR
    -> structural and temporal analysis
    -> Hardware Resource-Timing Graph (RTG) + evidence ledger
    -> instruction access profiles, launch rules, resource constraints
    -> canonical machine description (proposed atlas.rtl.yaml)
         -> atlas-opt adapter
              + kernel operands, addresses, and control flow
              -> dependency graph + resource reservations -> schedule
         -> optional Merlin contract adapter
         -> interaction graphs, traces, and discrepancy reports
```

| Representation | Meaning | Use |
| --- | --- | --- |
| Hardware Resource-Timing Graph (RTG) | Storage, ports, engines, queues, and control state connected by data/control paths, with guards and event timing | Explain and trace extracted facts back to hardware |
| Instruction profiles and rules | Operand/configuration-dependent reads, writes, visibility, reservations, capacities, and acceptance/completion behavior | Canonical compiler-facing model |
| Instruction Interaction Graph (IIG) | A derived view of relationships between instruction classes under stated operand/resource predicates | Inspect dependencies and export representable pairwise rules |
| Kernel dependency graph and reservation table | Concrete instruction instances, resolved aliases, precedence distances, and per-cycle occupancy | Existing compiler scheduling machinery |

The IIG is a projection, not a sufficient replacement for the profiles and resource constraints. A table indexed only by mnemonic pairs loses operand aliasing, physical-bank relationships, repeated accesses, and resource capacity shared by more than two instructions. DOT output is a debugging view, not the machine description.

The initial scope is Atlas scheduling-model extraction and integration. Radiance/Muon and Vortex are portability targets (§8), beginning with small analysis/import experiments. Full dynamic kernel-graph reconstruction remains a later extension; event instrumentation is needed much earlier.

## 2. Current baseline and evidence hierarchy

### 2.1 What the compiler actually consumes

Begin with the [machine interface](../../src/core/machine.h#L17-L117), [reservation interface](../../src/core/reservations.h#L9-L43), [profile construction](../../src/core/machine.cpp#L151-L421), and [graph construction](../../src/core/depgraph.cpp#L55-L195). The table identifies narrower definitions and implementation rules.

| Current representation | Semantics the adapter must preserve | Source |
| --- | --- | --- |
| `Access` | Storage kind, element range, read/write direction, starting age and per-element step, unknown-address aliasing, completion lifetimes, and optional wide DRAM byte intervals | [machine.h](../../src/core/machine.h#L33-L50) |
| `Hold` | Resource identity, inclusive occupancy interval, and an alternative resource index when either resource may be selected | [machine.h](../../src/core/machine.h#L52-L74) |
| `Footprint` | Accesses and holds; logical MREG read/write sets and release ages; `vload`'s write-during-read exception; VPU lifetime; final fixed resource-use age; estimated DMA cycles; operand legality | [machine.h](../../src/core/machine.h#L76-L87) |
| `dependence()` | RAW/WAR/WAW timing plus logical-reservation and operand-sensitive sequencer rules | [machine.cpp](../../src/core/machine.cpp#L456-L560) |
| `ReservationTable` | Resource capacities, physical MREG ports and sharing rules, VPU slots, and widening reservations across variable DMA waits | [reservations.cpp](../../src/core/reservations.cpp#L5-L78), [wait widening](../../src/core/reservations.cpp#L100-L113) |
| Other machine behavior | VPU incompatible operation groups, bank mapping, address units, DMA channels/FIFO/waits, barriers, frontend timing, and completion obligations | [barriers/VPU](../../src/core/machine.cpp#L29-L60), [address mapping](../../src/core/machine.cpp#L117-L147), [DMA/frontend simulation](../../src/core/simulator.cpp#L172-L288) |

Profile templates must be instantiated using actual operands and known scalar values. Preserve implicit operands, register-pair expansion, repeated row streams, alignment checks, byte-versus-word addresses, and unknown-address behavior; see [access/hold construction and address mapping](../../src/core/machine.cpp#L64-L147). RAR creates no data-precedence edge, but simultaneous reads can still contend for a physical port.

The model boundary is distributed across code. `unitCapacity()` ([capacity rules](../../src/core/machine.cpp#L12-L15)), `vpuCanOverlap()` ([overlap rules](../../src/core/machine.cpp#L29-L60)), bank mapping, same-cycle visibility, MXU launch rules, and DMA/frontend semantics are not all fields of `Footprint`. The [partial model interface](../../src/core/machine.h#L17-L31) and [CLI selection](../../src/tool/main.cpp#L38-L99) now carry partial MXU1, MXU0, and DMA selections. There is still no general YAML loader or `--machine` option. Full integration must extend the distributed model boundary rather than treating these partial projections as a complete target model.

The [current README](../../README.md#L53-L73) documents explicit DMA waits and `# atlas.release` completion markers. Automatic DMA-wait insertion was removed from this branch's history; the historical explicit-overlap experiments use `build/rtlgraph-no-dma-waits/atlas-opt`, while native DMA experiments use a separately rebuilt `build/rtlgraph-dma-native/compiler-1/atlas-opt`. Preserve the release obligations in [registry.cpp](../../src/passes/registry.cpp#L28-L119), [schedule.cpp](../../src/passes/schedule.cpp#L104-L124), and [simulator.cpp](../../src/core/simulator.cpp#L130-L154). Historical binaries/manifests retain their original identities. The checker shares [footprint/dependence checking](../../src/core/simulator.cpp#L157-L170), and [resource checks](../../src/core/simulator.cpp#L235-L240), with the scheduler: agreement checks consistency with the model, not independent correctness of that model against RTL.

[PLAN.md](PLAN.md#L397-L402) mixes implemented behavior and future design: it describes list, exact, and modulo scheduling, while [schedule.cpp](../../src/passes/schedule.cpp#L16-L90) implements the current per-block list scheduler. Treat implementation as the source for interface compatibility and independently pinned RTL as the source for hardware claims.

### 2.2 Hardware observations that guide extraction

The following are static observations from the enclosing Atlas repository's `src/main/scala` tree. They do not establish exact extracted timing profiles, and must not silently be attributed to a different `third_party/atlas-npu` revision. The shared [architecture notes](../../../../../.agents/architecture.md#L3-L11) provide navigation and their own inspection baseline.

| Observation | Source and consequence |
| --- | --- |
| Frontend issue is distinct from engine acceptance | [ScalarCore.scala](../../../src/main/scala/atlas/scalar/ScalarCore.scala#L226-L249) defines `s1_fire` and the `delay`/`dma.wait` stall causes; [engine launch assignments](../../../src/main/scala/atlas/scalar/ScalarCore.scala#L489-L533) generate engine valids. Trace launch and acceptance separately when defining age zero |
| Gating a rejected command is not a retry mechanism | [SA interface](../../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L47-L63), and [acceptance/assertions](../../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L289-L333); [IPT interface](../../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L29-L64), and [acceptance/assertions](../../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L301-L363). Both gate acceptance and assert illegal launches without command backpressure/retry |
| MXUs stream rows and track multiple operations | SA: [port/FIFO/guard state](../../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L179-L301), [row reads](../../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L444-L459), [writeback](../../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L609-L640). IPT: [port/FIFO/guard state](../../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L170-L319), [row reads](../../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L478-L497), [writeback](../../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L647-L679). A single busy duration is insufficient |
| Logical register identity differs from physical contention | [MregParams.scala](../../../src/main/scala/atlas/common/MregParams.scala#L36-L42) defines the 64-register/32-bank geometry; [bank/row mapping functions](../../../src/main/scala/atlas/common/MregParams.scala#L72-L78) map `m_i` and `m_(i+32)` to distinct rows of the same bank |
| DMA captures command fields before completion | [ScalarCore.scala](../../../src/main/scala/atlas/scalar/ScalarCore.scala#L503-L509) forms resolved address/size/channel fields; [DMA.scala](../../../src/main/scala/diplomatic/memory/DMA.scala#L223-L245) queues the command and later uses saved fields. Reconcile the compiler/model's completion-time operand accesses, tracing the full command path before assigning an exact capture age |
| DMA completion and memory contention are event-dependent | [DMA.scala](../../../src/main/scala/diplomatic/memory/DMA.scala#L291-L320) retires beats and clears channel busy after dispatch/retirement conditions. [VMEM priority selection](../../../src/main/scala/diplomatic/memory/VMEM.scala#L168-L180) and [ordered arbitration/grants](../../../src/main/scala/diplomatic/memory/VMEM.scala#L267-L305) place LSU traffic ahead of DMA |

The existing model is a useful comparison baseline, including its RTL trace fixtures. Its assumptions are not extraction results. In particular, [PLAN.md](PLAN.md#L105-L110) treats MXU0/MXU1 as interchangeable for the compiler baseline, while the [shared architecture notes](../../../../../.agents/architecture.md#L38-L40) require preserving engine-specific arithmetic, quantization, and accumulation order. Timing extraction cannot prove numerical interchangeability. Begin RTL validation with schedules that preserve engine assignments and use appropriate functional references.

### 2.3 Relationship to Merlin

The original survey used `ucb-bar/merlin@refactor/merlin-phase-architecture-clean`, particularly its [Atlas schedule contract](https://github.com/ucb-bar/merlin/blob/refactor/merlin-phase-architecture-clean/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml) and [CIRCT fact extractor](https://github.com/ucb-bar/merlin/blob/refactor/merlin-phase-architecture-clean/src/merlin/targetgen/rtl/circt_introspect.py). That survey reported structural RTL facts and `minimum_issue_gap`/`register_dependency_gap` timing entries whose evidence pointed to Python execution units. These are branch-specific comparison references; their contents were not revalidated during this update, and must be pinned and checked before integration.

The current [assembly handoff](../../docs/rtlgraph-contract.md) bundles native `before.S`, selected profiles/evidence, resolved `Access`/`Hold`/`Footprint` data, and the exact compiler identity. It rechecks the footprint query before invoking `atlas-opt` and checking `after.S`. It requires idle engines and the compiler's zero-scalar-register entry contract; it supplies no dispatch ABI. Comparison against Merlin's pinned `81a585b` schedule contract explicitly reports lost row, capacity, port, and DMA-completion semantics. Direct consumption by a richer Merlin model remains future work. The original survey's capacity discrepancies also need revision-specific reconciliation; the [current Atlas compiler](../../src/core/machine.h#L12-L15) already models 1.5 MiB VMEM.

Merlin's [command-stream reorder design](https://github.com/ucb-bar/merlin/blob/refactor/merlin-phase-architecture-clean/docs/design/command_stream_reorder_emitter.md) is a secondary reference. Inspect its target's interlocking and command-acceptance semantics before transferring scheduling assumptions to Atlas.

An optional reference checkout at `third_party/merlin` would be useful for inspecting the fact extractor, contract schemas, and validation examples locally, comparing versions, and citing exact source ranges. Record its selected branch and immutable commit; the surveyed refactor branch above is the initial reference to evaluate. This is a proposed reference location, not an existing checkout or a compiler build/runtime dependency. Normal builds and tests must work without it, and reference initialization remains optional. Exclude timing-bearing reference material from blind extraction runs (§7).

### 2.4 External repositories and branches

The enclosing Chipyard root `.gitmodules` is authoritative for the configured origins of this system's source checkouts; the compiler's separate [.gitmodules](../../.gitmodules#L1-L4) declares only the NPU model reference submodule. The root manifest was inspected at `/tools/C/reednicolas/ee194-sp26-chipyard/.gitmodules`; the relative links below point to the same file in this workspace. Distinguish the enclosing Atlas target repository from any optional Atlas comparison checkout. The rewritten compiler history does not register `third_party/atlas-npu` as a submodule.

The URLs below are manifest mappings, not a claim that all repositories are used by every configuration. A `branch` entry is a tracking choice, not an immutable pin; an omitted branch does not imply `main` or `master`. Record the parent gitlink and actual checked-out revision for each relevant submodule, plus any local source changes, when producing extraction artifacts.

| Project | Repository or branch | Relevant documentation |
| --- | --- | --- |
| NPU model | [ucb-ee194-tapeout/npu_model — rtl-match](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match) | [RTL timing notes](https://github.com/ucb-ee194-tapeout/npu_model/blob/rtl-match/docs/rtl-timing.md), [RTL trace fixtures and validation](https://github.com/ucb-ee194-tapeout/npu_model/blob/rtl-match/tests/rtl/README.md) |
| Atlas target: `generators/sp26-atlas-acc` | `git@bwrcrepo.eecs.berkeley.edu:ee194-290c-sp26/sp26-atlas-acc.git`; no branch specified | [Root manifest](../../../../../.gitmodules#L164-L166); [selected config](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L41-L54) |
| Atlas optional comparison reference | [ucb-bar/atlas-npu](https://github.com/ucb-bar/atlas-npu) | Not a registered compiler submodule or build dependency; any selected revision must not be substituted for the enclosing Atlas target |
| TestChipIP: `generators/testchipip` | [ucb-ee194-tapeout/testchipip](https://github.com/ucb-ee194-tapeout/testchipip); no branch specified | [Root manifest](../../../../../.gitmodules#L56-L58) |
| Saturn: `generators/saturn` | [ucb-ee194-tapeout/saturn-vectors — bf16_fp8](https://github.com/ucb-ee194-tapeout/saturn-vectors/tree/bf16_fp8) | [Root manifest](../../../../../.gitmodules#L167-L170); the selected vector parameters are specified in §4.1 |
| Shuttle: `generators/shuttle` | [ucb-ee194-tapeout/shuttle](https://github.com/ucb-ee194-tapeout/shuttle); no branch specified | [Root manifest](../../../../../.gitmodules#L174-L176) |
| Rocket Chip: `generators/rocket-chip` | [ucb-ee194-tapeout/rocket-chip](https://github.com/ucb-ee194-tapeout/rocket-chip); no branch specified | [Root manifest](../../../../../.gitmodules#L177-L179) |
| Diplomacy: `generators/diplomacy` | [chipsalliance/diplomacy](https://github.com/chipsalliance/diplomacy) | [Root manifest](../../../../../.gitmodules#L19-L21) |
| HardFloat: `generators/hardfloat` | [ucb-bar/berkeley-hardfloat](https://github.com/ucb-bar/berkeley-hardfloat) | [Root manifest](../../../../../.gitmodules#L29-L31) |
| Inclusive cache: `generators/rocket-chip-inclusive-cache` | [chipsalliance/rocket-chip-inclusive-cache](https://github.com/chipsalliance/rocket-chip-inclusive-cache) | [Root manifest](../../../../../.gitmodules#L53-L55) |
| CDE: `tools/cde` | [chipsalliance/cde](https://github.com/chipsalliance/cde) | [Root manifest](../../../../../.gitmodules#L107-L109) |
| Radiance / Muon: `generators/radiance` | [ucb-bar/radiance — main](https://github.com/ucb-bar/radiance/tree/main) | [Root manifest](../../../../../.gitmodules#L152-L155); [Muon](https://github.com/ucb-bar/radiance/blob/main/docs/muon.md), [issue](https://github.com/ucb-bar/radiance/blob/main/docs/issue.md), [register mapping](https://github.com/ucb-bar/radiance/blob/main/docs/rename.md) |
| Vortex | [vortexgpgpu/vortex — master](https://github.com/vortexgpgpu/vortex/tree/master) | [Microarchitecture](https://github.com/vortexgpgpu/vortex/blob/master/docs/designs/microarchitecture.md), [scoreboard RTL](https://github.com/vortexgpgpu/vortex/blob/master/hw/rtl/core/VX_scoreboard.sv) |
| Merlin (optional local reference: `third_party/merlin`) | [ucb-bar/merlin — surveyed refactor branch](https://github.com/ucb-bar/merlin/tree/refactor/merlin-phase-architecture-clean) | [Atlas schedule contract](https://github.com/ucb-bar/merlin/blob/refactor/merlin-phase-architecture-clean/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml); see §2.3 for survey scope and reference-only use |
| CIRCT: `tools/circt` | [llvm/circt](https://github.com/llvm/circt) | [Root manifest](../../../../../.gitmodules#L110-L112); [Getting Started](https://circt.llvm.org/docs/GettingStarted/), [SystemVerilog frontend](https://circt.llvm.org/docs/Tools/circt-verilog/), [bounded model checking](https://circt.llvm.org/docs/Tools/circt-bmc/) |
| LLVM / MLIR | [llvm/llvm-project](https://github.com/llvm/llvm-project) | [MLIR IR traversal](https://mlir.llvm.org/docs/Tutorials/UnderstandingTheIRStructure/), [LLVM-MCA](https://llvm.org/docs/CommandGuide/llvm-mca.html); additional scheduling references are in §9 |

## 3. Facts to derive

These requirements replace the older plan's assumptions about completion-time reads, nonpipelined MXUs, and RAR conflicts. Each fact needs configuration/operand predicates and evidence scope.

| ID | Fact | Extraction target and consumer |
| --- | --- | --- |
| F1 | Decode, operand roles, and implicit state | Decode predicates and control signals; instantiated reads/writes, register pairs, weight/accumulator selection, and legality |
| F2 | Operand capture and read events | Age of every relevant row/element read, including repeated streams and queued-command capture; `Access` and WAR timing |
| F3 | Write events and visibility | Row/element write ages, forwarding/same-cycle rules, logical reservation release; RAW/WAW timing and completion accounting |
| F4 | Resource occupancy and throughput | Port windows, alternatives, capacities, in-flight limits, initiation conditions, and VPU compatibility; `Hold` and reservation checks |
| F5 | Hardware-enforced versus software-required conditions | Trace stall, accept, assert, reject, and retry paths. Distinguish correctness obligations from hardware-induced performance stalls |
| F6 | Physical storage/port topology | Bank and row mapping, port counts, read sharing, collision semantics, and arbitration, including DMA/LSU interaction |
| F7 | Engine identity and sharing | Establish separate or shared XLU/VPU/MXU/LSU paths from instances and control. Verify current assignments rather than assuming an old model disagreement |
| F8 | DMA and other variable-latency work | Queue/channel lifetimes, ordering, command capture, completion events, and environment-dependent progress. Separate cost estimates from justified bounds |
| F9 | Frontend timing | Exact issue event and `delay` behavior; translate chosen issue gaps to instruction encoding independently of storage visibility |
| F10 | Control flow | Branch/jump redirect and delay-slot behavior, barriers, and legality. Current Atlas has one architectural delay slot |
| F11 | Configuration, capacities, and address maps | Memory sizes, bank geometry, instruction parameters, and address domains for the selected elaboration |
| F12 | Completion boundaries | Hardware events needed to discharge waits, fixed-work completion, `halt`, and compiler publication obligations such as `atlas.release`; distinguish hardware facts from compiler policy |

The expected benefit is a traceable and maintainable model, with tighter schedules where justified. Row-timed overlap and MXU pipelining already exist in the consumer, so they are not new performance wins to claim in advance.

## 4. Extraction architecture

### 4.1 Reproducible hardware boundary

The selected full-system configuration is `chipyard.EE290SimConfig`, from [EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L41-L54), matching the baremetal Makefile default. The prior `AtlasShuttleVectorConfig` runs are retained as tooling checks with their original configuration identities. The completed fresh run records Chipyard HEAD `48c3a7d11f6de3f0cecb33ae6a31c24ec26ff9d8` and Atlas HEAD `2ae0bef209df6db78c3de18e8f651bb43855cce9`. These revisions do not assert clean working trees: the build manifest separately records the actual scoped source/resource hashes. Retain both source hashes and relevant submodule revisions when regenerating artifacts.

| Selected component | Configuration and source |
| --- | --- |
| Atlas tile | `WithAtlasTile()` uses `AtlasParams()`, SBUS attachment, and enabled monitors: [WithAtlasTile.scala](../../../chipyard/config/WithAtlasTile.scala#L12-L30). The default engine/storage parameter bundle is in [AtlasParams.scala](../../../src/main/scala/atlas/common/AtlasParams.scala#L3-L10) |
| Shuttle + Saturn | One Shuttle core with `WithShuttleVectorUnit(256, 128, VectorParams.mxParams)`. The arguments set `vLen=256` and `dLen=128`; the omitted `mLen` becomes 128, with `useScalarFPFMA=false`: [Saturn Configs.scala](../../../../saturn/src/main/scala/shuttle/Configs.scala#L13-L44). Widths are in bits: [Parameters.scala](../../../../saturn/src/main/scala/common/Parameters.scala#L387-L402). The one-core mixin is defined in [Shuttle Configs.scala](../../../../shuttle/src/main/scala/common/Configs.scala#L14-L50) |
| Fabric and cache geometry | The selected config sets SBUS width to 256 bits, Shuttle tile beats to 16 bytes, and cache blocks to 64 bytes: [EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L47-L53). See [SBUS width conversion](../../../../chipyard/src/main/scala/config/fragments/SubsystemFragments.scala#L17-L19), [Shuttle beat setting](../../../../shuttle/src/main/scala/common/Configs.scala#L172-L179), and [tile width widget](../../../../shuttle/src/main/scala/common/Tile.scala#L263-L268) |
| Coherence manager | `WithBroadcastManager` replaces the inherited L2 coherence manager with a broadcast manager: [selected mixin](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L47-L53), [implementation](../../../../chipyard/src/main/scala/config/fragments/SubsystemFragments.scala#L8-L11). Preserve this distinction when comparing system-memory and DMA behavior |
| Tracing | The selected config includes `WithTraceSinkDMA(1)`, `WithTraceSinkAlways(0)`, `WithTraceArbiterMonitor`, and `WithTacitEncoder`: [EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L41-L47). Pin the corresponding sources and inspect their elaborated interfaces before assigning environmental assumptions |
| Base system | `EE290BaseConfig` configures one external-memory channel, 64-bit edge data, 64 GiB external address capacity, and 500 MHz bus/harness frequency settings, then inherits `AbstractConfig`: [EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L124-L139). The selected config overrides its 32-byte cache blocks with 64-byte blocks. These are configuration values, not measured memory latency or achieved frequency |
| Inherited simulation interfaces | `AbstractConfig` includes SimDRAM/SimTSI-over-serial binders ([harness binders](../../../../chipyard/src/main/scala/config/AbstractConfig.scala#L18-L20)) and a TestChipIP serial TileLink client plus one AXI memory channel ([serial and memory configuration](../../../../chipyard/src/main/scala/config/AbstractConfig.scala#L73-L81)). Pin the actual harness, binders, and external-memory model before interpreting DMA timing |
| Atlas bus boundary | DMA attaches to the selected SBUS ([AtlasTile.scala](../../../src/main/scala/diplomatic/top/AtlasTile.scala#L202-L204)); IMEM/CSR attach to PBUS ([IMEM/CSR attachment](../../../src/main/scala/diplomatic/top/AtlasTile.scala#L206-L218)); VMEM attaches to SBUS ([VMEM attachment](../../../src/main/scala/diplomatic/top/AtlasTile.scala#L220-L225)). Preserve arbitration and width-adaptation behavior in the extracted environment |

Saturn's selected `mxParams` extends `genParams` with `useMxFPFMA=true` and `useMxConversion=true`: [Parameters.scala](../../../../saturn/src/main/scala/common/Parameters.scala#L44-L60). The earlier `AtlasShuttleVectorConfig` selects `genParams` instead and omits the explicit broadcast-manager and Tacit tracing mixins: [AtlasConfigs.scala](../../../chipyard/config/AtlasConfigs.scala#L40-L48). Both configs use default `WithAtlasTile()`, but their complete systems are not interchangeable. The repository's `bf16_fp8` branch name alone does not establish which arithmetic features a configuration enables.

The selected composition uses `EE290BaseConfig`; it does not include the separate `WithEE290TapeoutPeripherals` mixin that installs a serial off-chip-memory manager and disables the standard memory port ([EE290Configs.scala](../../../../chipyard/src/main/scala/config/EE290Configs.scala#L90-L122)). Distinguish the selected AXI-memory/serial-client environment from that other topology. Any unit harness used for the first extraction slice must state which full-system interfaces it abstracts and how their relevant behavior is preserved.

Pin this configuration, the source origins in §2.4, all external modules, tool revisions, elaboration parameters, clock/reset assumptions, and source/IR hashes. The [S0 guide](../../docs/rtlgraph-s0.md) documents the current-source build route and historical cached-JAR evidence separately. The elaboration driver now accepts configuration and generator-JAR arguments, defaults to `EE290SimConfig`, and writes configuration-specific output. Its optional build-manifest check requires a completed build, matching before/after scoped source hashes, and a matching JAR hash. The direct SBT helper avoids Make's broad source discovery and records its audited inputs; simulation setup remains a separate step. Do not relabel historical artifacts or carry over module counts and timing claims.

Use Chisel/FIRRTL lowering into a supported CIRCT hardware subset. Keep source locations and stable instance/symbol references where possible. Inventory residual dialects, SRAM models, black boxes, and unsupported constructs. An unresolved module affecting a timing claim makes that claim unresolved; preserving a name does not supply its semantics.

Produce simulation inputs through an explicit backend flow from the pinned design. `firtool` emits hardware IR/RTL; constructing an Arcilator executable is a separate lowering/build step. If analysis and simulation use different optimization pipelines, preserve mappings and validate that they represent the same behavior relevant to the claim.

### 4.2 Deterministic analyses

| Component | Responsibility |
| --- | --- |
| E1 census | Instance hierarchy, relevant storage, ports, state, capacities, and external-module assumptions; start with the selected instruction slice |
| E2 decode | Recover instruction predicates and operand/control mappings through masks, slices, comparisons, and Boolean structure; do not assume one full-word equality per instruction |
| E3 netgraph | Traverse typed MLIR values, definitions, uses, and instance connections; retain bit/field selection and source provenance |
| E4 event/path query | Connect issue/acceptance to reads, writes, acquisition/release, and completion, with enables, mux guards, clocks, and resets |
| E5 temporal analysis | Analyze counters, FIFO state, sequencer transitions, and launch predicates; use bounded properties or other supported proof methods for explicit claims |

Sequential depth is structural evidence, not an instruction latency by itself. Enables, stalls, feedback, SRAM behavior, and prior state determine when a path is exercised. The [HW](https://circt.llvm.org/docs/Dialects/HW/), [Comb](https://circt.llvm.org/docs/Dialects/Comb/), and [Seq](https://circt.llvm.org/docs/Dialects/Seq/) dialects provide the starting representation.

Try [Core-to-FSM](https://circt.llvm.org/docs/Passes/#-convert-core-to-fsm) on a small selected controller before designing a custom recovery engine. Its documented state-register selection is explicit or name-based; Atlas row counters, FIFOs, and concurrent port engines may require additional analysis. Check the implementation and limits in the pinned CIRCT revision rather than assuming automatic recovery of an instruction contract.

### 4.3 Agent role and independent validation

The deterministic core is proposed as C++ CIRCT analysis passes plus a query CLI; Python bindings can support prototypes. Agents navigate source/IR, propose mappings and hypotheses, construct experiments, and reconcile discrepancies. Reproducible tool results carry the evidence. Share the current opcode representation through an adapter where useful; the compiler has a C++ opcode table, not the old proposed `isa.def` X-macro.

Instrument events during the first timing slice: scalar issue, engine acceptance, operand row read, destination row write, resource acquire/release, completion, stall, and assertion. Attach instruction/transaction identity and instance/row information so multiple in-flight commands can be distinguished. [Arc](https://circt.llvm.org/docs/Dialects/Arc/) is a simulation-oriented state-transfer representation; its simulation scheduling is distinct from kernel instruction scheduling.

Monitors must check independently justified hardware obligations: port capacity, intended value/version visibility, accepted-command accounting, or a documented launch invariant. A monitor that merely asserts the proposed lookup-table distance cannot independently validate that distance. Avoid blanket prohibitions on reading any in-flight destination: a legal streamed overlap may consume rows already available.

Use [bounded model checking](https://circt.llvm.org/docs/Tools/circt-bmc/) for questions such as whether a source can be read after age k, whether an accepted command must write a row by age k, or whether legal launches can collide. State reset, initial-state, environment, and interference assumptions, the timestep bound, and whether the result establishes safety or progress. A timeout or unsupported operation is not a proof.

## 5. Canonical model, projection, and evidence

### 5.1 Schema requirements

The implemented prototype retains rich JSON evidence and strict partial compiler projections: [MXU1](../../docs/rtlgraph-profile.md#evidence-and-projection), [MXU0](../../docs/rtlgraph-mxu0.md), and [DMA](../../docs/rtlgraph-dma-native.md). Each records inherited rules and its evidence scope; together they still do not implement the full schema below. The proposed `atlas.rtl.yaml` must carry:

- Input identity, target configuration, age-zero event, clock domain, interval conventions, address units, and external/environment assumptions.
- Operand-sensitive access templates and their applicability predicates, including implicit state, repeated streams, unknown aliases, and completion-event accesses.
- Resource identities and capacities, physical mapping/sharing, occupancy intervals and alternatives, logical reservations, and release visibility.
- Launch/sequencer rules, visibility/bypass policies, hardware enforcement classification, frontend behavior, and completion obligations.
- Provenance and evidence for every hardware claim; explicit unresolved/conflicting facts and consumer capabilities needed to interpret them.

The following YAML is an intentionally incomplete format sketch, with an unresolved fact. It is not a loadable machine model or a measured Atlas timing result; other accesses, holds, and launch rules must accompany the completed profile.

```yaml
schema_version: 1
family: circt_timing               # proposed format, not a registered Merlin schema
deployable: false
inputs:
  rtl_revision: null
  configuration: chipyard.EE290SimConfig
  hardware_ir_sha256: null
  toolchain: {}
timing:
  age_zero: frontend_issue         # map separately to each engine's acceptance
  hold_end: inclusive
facts:
  mxu1_acc_write_stream:
    resolution: UNKNOWN
    value: null                   # must resolve first_age, step, and count
    applicability: null
    provenance: []
    evidence: []
profile_templates:
  VMATMUL_MXU1:
    writes:
      - resource: Acc
        selector: instruction_accumulator
        stream_fact: mxu1_acc_write_stream
consumer_requirements:
  - row_accesses
  - logical_reservations
  - resource_capacities
  - conditional_launch_rules
  - completion_events
```

The schema audit must cover the distributed implementation in §2.1. A `Footprint` round trip alone is insufficient. Unsupported semantics must produce an explicit rejection or a documented, justified conservative translation. The canonical format remains richer than any pairwise-distance export.

### 5.2 Dependency distances versus resource exclusion

For fixed-age accesses by instructions A then B, derive precedence over overlapping storage elements and the relevant access occurrences:

```text
RAW: d >= max_e(writeAge_A(e) - readAge_B(e)  + delta_RAW(e))
WAR: d >= max_e(readAge_A(e)  - writeAge_B(e) + delta_WAR(e))
WAW: d >= max_e(writeAge_A(e) - writeAge_B(e) + delta_WAW(e))
```

Use the required same-cycle ordering/visibility policy for each delta. Repeated reads or writes require considering the relevant occurrences, not just the first access. Combine these data terms with logical-reservation and launch-rule terms and the single-issue minimum gap of one. Unknown completion ages require event-based ordering instead of substitution into a fixed-age formula.

For illustration only, if A writes row r at age `4+r`, B reads it at age `r`, and visibility requires a strictly later cycle, the row-data constraint is `d >= 5`. Other reservations or launch rules may require a larger gap. This is not an Atlas measurement.

Keep exclusion constraints in resource reservations. If A uses a port at age 5 and B at age 2, they collide at issue distance 3; other nearby distances can be legal. Three instructions can also exceed a two-slot resource while every pair is legal. Neither case is fully captured by one pairwise minimum distance or a universal `d = II` rule.

Storage visibility and `delay` encoding are separate facts. The current compiler models A at t, `delay N` at t+1, and B at t+N+2; a gap g greater than one is emitted as `delay (g-2)`. See [simulator.cpp](../../src/core/simulator.cpp#L235-L240) for the modeled timing. Verify that frontend convention against the selected RTL independently of RAW/WAR/WAW visibility.

### 5.3 Evidence and unresolved facts

Keep fact resolution separate from evidence strength. Proposed resolution states are `CANDIDATE`, `RESOLVED`, `UNKNOWN`, and `CONFLICT`; `RESOLVED` means the claim has an explicit interpretation and scope, not universal safety. Consumer admission is a separate policy requiring suitable evidence for the intended use.

| Evidence category | What it establishes |
| --- | --- |
| Structural derivation | A reproducible query/derivation over identified RTL/IR under stated semantic assumptions |
| Simulation validation | The obligation held for listed inputs, states, traffic, configuration, and observed executions |
| Bounded proof | The property holds for the modeled executions within the recorded bound and assumptions |
| Unbounded proof | The stated property holds under recorded assumptions using an identified proof method; only claim this when actually established |

Each record needs the fact/property, applicability predicates, source instances/state/control paths, hashes and tool versions, assumptions, state/input/interference scope, result, and replayable query/test/proof artifacts. Preserve counterexamples and conflicts. Record model/spec values as comparison evidence, not as RTL derivation.

A passing witness at d establishes that execution. A failure at d-1 establishes a counterexample there. Together they do not prove safety for every state/operand or universal minimality, especially when legal resource gaps are nonmonotone. Record adjacent-gap experiments as such; use a stronger claim only when its full scope is established.

For `UNKNOWN` or `CONFLICT`, reject affected scheduling choices or retain an independently justified conservative policy. An arbitrary large delay cannot establish completion for an unbounded transaction. DMA latency estimates may guide performance decisions, but correctness must use completion events or a validated bound under enforceable environment assumptions. Preserve uncertainty across waits, including physical-resource reservations of fixed-latency work still in flight.

## 6. Stages and deliverables

The first end-to-end slice uses MXU1 computation and FP8 pops in K64, followed by held-out K128 validation, controlled bank-alias cases, fixed-model scheduling bounds, and faster fused attention. The corpus expansion adds an independently extracted MXU0 read-resource projection, MXU0 event checking, and VPU issue/resource corroboration against the linked compiler. All eleven kernels with full output goldens now have [full-kernel scheduling comparisons](../../docs/rtlgraph-mixed-dma.md). This reaches experimental S3 and broader, still partial S4/S5 coverage while S0 simulator build linkage and S2 temporal proof obligations remain open. The VPU follow-up adds local valid-pipeline proofs and finite launch/read/write/release observations; complete composition and BF16 visibility proofs remain open. Stages are coverage criteria, not a claim that every earlier obligation is complete. Next, broaden data/interference coverage and row/payload proofs, strengthen the branching probes beyond their original single-element checks, and exercise the [assembly handoff](../../docs/rtlgraph-contract.md) with a Merlin emitter.

| Stage | Work | Exit criterion |
| --- | --- | --- |
| S0 Consumer/build boundary | Audit the distributed consumer contract; pin `chipyard.EE290SimConfig`, the relevant repository revisions from §2.4, tools, reset/environment assumptions, and reproducible IR/simulation flow | Field/rule coverage checklist; hashed hardware artifact; explicit unsupported/external modules; one functional smoke case, without claiming timing proof |
| S1 Structural slice | E1/E2 over one instruction family and its relevant resource paths; identify scalar issue, engine acceptance, and storage identities | Reproducible decoder/path queries with source provenance and explicit coverage |
| S2 Temporal slice | E3–E5 plus early event instrumentation; derive reads, writes, visibility, release, and launch predicates | One profile with independent simulation checks and an appropriately scoped bounded property where supported; unresolved claims remain explicit |
| S3 Consumer integration | Specify schema/model interface and adapter; replace the chosen baseline profile and related rules in graph construction, scheduling, and checking | A kernel schedule consumes the profile without losing resource or completion semantics; RTL functional checks pass for that slice |
| S4 Generalization | Expand F1–F12, bank/port conflicts, operand aliases, multiple in-flight instructions, and completion-driven DMA | Coverage by instruction/configuration/rule family; every gap or conflict identified; no silently omitted correctness obligation |
| S5 Corpus validation | Run original and scheduled kernels from the pinned corpus, with varied operands, bank relationships, issue gaps, and interference | Report actual corpus/coverage counts, functional outcomes, assertion/monitor results, and prediction errors; distinguish baseline failures |
| S6 Portability probes | Small SystemVerilog import, then selected Radiance/Muon and Vortex slices (§8) | Supported IR subset, assumptions, target-specific enforcement, event traces, and unsupported constructs recorded |
| S7 Optional dynamic graph | Reconstruct observed kernel dependencies from tagged event traces and compare with compiler constraints | Explain observed missing/extra constraints for the tested executions; avoid claiming universal coverage from one trace |

The first demonstrator ends at S3: a hardware-derived profile reaches the scheduler and passes independent RTL checks. Demonstrate sensitivity using a synthetic timing fixture or an isolated experimental fixture whose timing can be changed: extraction and the consumer's schedule should respond to the relevant change. The finished Atlas RTL remains reference material.

Deliverables are the versioned model/schema, extractor queries, evidence manifest, adapter, scoped RTG/IIG visualizations, discrepancy report, and replayable witness/property corpus. Keep generated IR, binaries, traces, and run artifacts outside this context directory; retain references and reusable conclusions here.

## 7. Evaluation and risks

Compare against the pinned current compiler/`rtl-match` baseline, not the older completion-only model. Measure extraction coverage, unsupported cases, provenance completeness, replayability, simulation failures, proven property scope, compile/extraction cost, schedule cycles, and prediction errors. State the corpus and environment for every numerical result. A faster schedule is useful only with the required functional and timing checks.

For an agentic evaluation, compare deterministic tools alone, agent plus tools, agent plus tools and specifications, and the current manually maintained model. Blind extraction runs must exclude model/spec timing values and this document's baseline observations; the reconciler sees them afterward. An agent that has already read those values cannot serve as a blind experimental arm merely by omitting citations.

Use dependency-limited experiments separately from contention experiments. Vary aliases, physical-bank relationships, instruction spacing, reset/prior state, and concurrent traffic. Cross-check selected witnesses between simulation backends where supported. Mutation fixtures should exercise both extraction sensitivity and consumer behavior; stale timing should be detected when the mutation invalidates its assumptions.

| Risk | Response |
| --- | --- |
| Name loss or optimization changes provenance | Preserve source/symbol mappings, query semantics rather than names alone, and track extraction/simulation artifact identity |
| Correct-looking simulation under a wrong model | Independent obligations, distinguishing data patterns, varied interference, and scoped formal checks; the compiler's own checker is not an RTL oracle |
| Unsupported memories, external modules, or lowering | Record contracts and unresolved behavior; restrict the claim instead of assigning guessed timing |
| State-space growth in sequencers/FIFOs | Begin with a slice and explicit assumptions; report bounded results and inconclusive analyses honestly |
| DMA/link latency and arbitration variability | Model completion and backpressure; distinguish measured costs from correctness bounds |
| Rich semantics lost in a consumer adapter | Capability/schema checks and explicit precision-loss reports; retain reservation-table checks |
| Timing optimization changes numerical behavior | Preserve engine assignment/operation semantics initially; require a separate numerical contract for transformations such as MXU rebinding |
| Source or tool drift | Pin revisions/configurations; regenerate facts and compare evidence/model diffs before admitting an updated model |

## 8. Portability: Radiance/Muon and Vortex

The reusable component is the analysis and evidence framework. Each target supplies its own instruction identities, architectural storage, acceptance/completion events, resource topology, and enforcement semantics. A relation that requires explicit software spacing on Atlas may describe a hardware-induced stall on another machine.

### 8.1 Radiance/Muon

Current [Radiance documentation](https://github.com/ucb-bar/radiance/blob/main/README.md) identifies Muon as its SIMT core. Treat it as a distinct target profile from Vortex. The [Muon design](https://github.com/ucb-bar/radiance/blob/main/docs/muon.md) discusses register-demand-dependent warp occupancy; its [issue](https://github.com/ucb-bar/radiance/blob/main/docs/issue.md) and [register mapping](https://github.com/ucb-bar/radiance/blob/main/docs/rename.md) documents motivate extracting warp context, register allocation/mapping, operand collection, forwarding, and hardware hazard handling.

Those documents include tentative choices and TODOs. Their latency or occupancy examples are not verified facts about a selected implementation. Pin the Radiance commit/configuration, elaborate a small issue/register-access slice, and reconcile the design notes with its actual control paths. Record which conditions block a warp, an instruction, or a shared pipeline, and how register usage constrains resident work.

### 8.2 Vortex

Vortex's [microarchitecture overview](https://github.com/vortexgpgpu/vortex/blob/master/docs/designs/microarchitecture.md) describes per-warp instruction buffering, a register scoreboard, operand collection, execution, and scoreboard updates at commit. Its [scoreboard RTL](https://github.com/vortexgpgpu/vortex/blob/master/hw/rtl/core/VX_scoreboard.sv) is a starting point for checking readiness, resource congestion, and release on writeback in a pinned configuration.

Begin with a scoreboard/operand-collection slice and its real parameters, macros, interfaces, and dependencies. Preserve warp identity, relevant active-lane information, register readiness, queue/port capacity, and acceptance/writeback events. Extract which hazards hardware enforces and which obligations remain with software; do not transfer Atlas `delay` insertion or fixed issue-to-completion assumptions.

### 8.3 Common IR boundary and feasibility gates

```text
Atlas or Radiance Chisel -> FIRRTL -> firtool -----+
                                                  +-> supported HW/Comb/Seq subset
Vortex SystemVerilog -> circt-verilog lowering ----+
```

The [CIRCT SystemVerilog frontend](https://circt.llvm.org/docs/Tools/circt-verilog/) documents lowering to core hardware dialects, requires a Slang-enabled build, and has evolving language support. First import a small SystemVerilog module, inventory residual dialects/external modules, and compare event behavior with an independent simulator. Then attempt the selected Vortex slice. This plan does not establish a complete Vortex or Radiance import.

Portability success means the same query/evidence framework can recover a scoped target-specific profile and explain its enforcement semantics. It does not require replacing the existing Atlas scheduler or promising one universal latency table.

## 9. Focused reading and prototype sequence

Work backward from the consumer. External documentation links may track moving branches; pin tool/source revisions when using them in an experiment.

| Order | Reading | Concrete question or output |
| --- | --- | --- |
| 1 | [PLAN.md scheduling](PLAN.md#L397-L412), [machine.h](../../src/core/machine.h#L17-L117), [reservations.h](../../src/core/reservations.h#L9-L43), and the implementation ranges in §2.1 | What must an extracted replacement preserve, including rules outside `Footprint`? Produce the S0 checklist |
| 2 | [GCC processor pipeline descriptions](https://gcc.gnu.org/onlinedocs/gccint/Processor-pipeline-description.html), [LLVM scheduling definitions](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/Target/TargetSchedule.td), [LLVM-MCA](https://llvm.org/docs/CommandGuide/llvm-mca.html) | Separate dependency latency, operand timing/bypasses, and resource reservations. GCC's compiler RTL is a different representation from hardware RTL |
| 3 | [CIRCT Getting Started](https://circt.llvm.org/docs/GettingStarted/), [MLIR IR traversal](https://mlir.llvm.org/docs/Tutorials/UnderstandingTheIRStructure/) | Build a small pass and follow definitions/uses and hierarchy through APIs rather than parsing printed IR |
| 4 | [HW](https://circt.llvm.org/docs/Dialects/HW/), [Comb](https://circt.llvm.org/docs/Dialects/Comb/), [Seq](https://circt.llvm.org/docs/Dialects/Seq/), [instance/module graph passes](https://circt.llvm.org/docs/Passes/#-hw-print-module-graph) | Map modules, control predicates, state updates, and provenance for one instruction path |
| 5 | [Chisel ready/valid interfaces](https://www.chisel-lang.org/docs/explanations/interfaces-and-connections), [Core-to-FSM](https://circt.llvm.org/docs/Passes/#-convert-core-to-fsm) | Distinguish command presence, acceptance, and architectural issue; identify counters/FIFOs beyond named FSM state |
| 6 | [Arc](https://circt.llvm.org/docs/Dialects/Arc/), [CIRCT BMC](https://circt.llvm.org/docs/Tools/circt-bmc/) | Generate tagged event traces and an independently specified, explicitly scoped property |
| 7 | [LLVM-Exegesis](https://llvm.org/docs/CommandGuide/llvm-exegesis.html) | Borrow controlled-snippet methodology for dependency and contention experiments; it is not an Atlas RTL extractor |
| 8 | Merlin references (§2.3), [CIRCT scheduling infrastructure](https://circt.llvm.org/docs/Scheduling/), and target/frontend references (§8) | Audit adapter precision and explore a second target. CIRCT scheduling/SSP concerns the consuming side and does not replace extraction |

## 10. Decisions to resolve during implementation

1. For the selected `chipyard.EE290SimConfig`, which pinned toolchain, verified elaboration command, memory/environment contracts, and optional unit-harness abstractions define the first extraction run?
2. After K64/K128 bounds and the faster fused-attention witness, which accumulator visibility, VPU/MXU sharing, weight-version, and write-port rules should gain independent extraction or temporal proofs next?
3. What versioned model interface can express the distributed compiler rules without silently retaining stale hardcoded assumptions?
4. What evidence and scope are required before an extracted fact may affect correctness scheduling, and how are unsupported cases rejected or conservatively handled?
5. Beyond the assembly handoff and pinned Merlin contract comparison, which richer resource/completion fields should a direct Merlin consumer support?
6. Which agent runtime and restricted query tools support reproducible evaluation, and what extraction/proof budgets apply?
7. Which Radiance/Muon and Vortex configurations provide manageable initial portability slices? Full target import and dynamic whole-kernel graphs remain subsequent work.
