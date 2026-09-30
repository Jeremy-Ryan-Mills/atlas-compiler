# RTL-derived Atlas scheduling profiles

These opt-in profiles augment the existing `atlas-opt` model for `chipyard.EE290SimConfig`. The current public configuration is [`EE290Configs.scala`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26) in [bringup-chipyard](https://github.com/ucb-ee194-tapeout/bringup-chipyard). That moving source link is a reference; recorded revisions and the shared CIRCT artifact hash identify the hardware behind these profiles. Earlier experiments remain at [`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765).

## Use the profiles

Hardware analysis runs per pinned configuration; kernels reuse the resulting profiles with their own operands and addresses. All six components have extracted facts; each remains a partial projection with inherited or unresolved rules. Each component directory contains a versioned `.profile` and canonical evidence. The current schemas are MXU0/DMA/LSU/XLU v2 and MXU1/VPU v1; the [JSON handoff](rtlgraph-contract.md) preserves these identities and resolved footprints. Hashes identify the CIRCT artifact and report; loading does not rerun or certify hardware analysis. The loader rejects unknown, missing, duplicate, or malformed fields and mixed hardware identities.

```sh
build/atlas-opt before.S -o after.S \
  --experimental-mxu0-profile profiles/EE290SimConfig/mxu0/atlas-mxu0.profile \
  --experimental-mxu1-profile profiles/EE290SimConfig/mxu1/atlas-mxu1.profile \
  --rtl-dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --rtl-lsu-profile profiles/EE290SimConfig/lsu/atlas-lsu.profile \
  --rtl-xlu-profile profiles/EE290SimConfig/xlu/atlas-xlu.profile \
  --rtl-vpu-profile profiles/EE290SimConfig/vpu/atlas-vpu.profile
```

Omit a profile to retain built-in behavior for that component. RTL DMA requires robust timing. Matching `DMA.WAIT` instructions may be supplied or inserted by the optional `insert-dma-waits` pass; lifetimes may cross known control-flow edges, joins, and loops. The CFG-aware [wait pass](../src/passes/insert_dma_waits.cpp#L36-L73) grows per-channel masks to a fixed point across joins and backedges, separately from dependency-graph construction. Pending transfers may cross a join; it does not impose a full DMA fence at every boundary. The [DMA guide](rtlgraph-dma-integration.md) specifies the pass sequence and restrictions.

For memory-dependent branches that the timing simulator cannot evaluate, `--validation static` checks block dependency distances, resource reservations, fixed-engine drains, and DMA lifetimes across reachable CFG paths. It requires robust timing and an RTL DMA profile when DMA is present. It does not evaluate numerical branch conditions or estimate dynamic latency; validate the resulting program against RTL and full-output goldens. Dynamic validation remains the default.

The default critical-path heuristic can be compared with `--schedule-priority input`, which prefers ready instructions in source order while retaining the same hazard/resource checks. Neither heuristic guarantees an optimal schedule; compare complete kernel runs.

## Profile semantics

| Component | Derived fields | Inherited or unresolved rules |
| --- | --- | --- |
| MXU0 v2 | First accumulator-write age 63; no accumulator-read hold for overwrite-only `VMATMUL.MXU0` | Reuse/resource rules retained; connected control checks cover all seven commands |
| MXU1 | First accumulator-write age 3; no accumulator-read hold for overwrite-only `VMATMUL.MXU1` | Accumulating reads, weight, and other resource rules retained |
| DMA v2 | Issue-time capture; 32-byte VMEM lines; eight channels/command slots; explicit-wait completion; LSU priority; 37-bit DRAM ranges | No fixed external completion bound; admitted transfers are at most 4,096 bytes |
| LSU v2 | Vector read/write/free ages 1/3/35; scalar memory request age 1, load writeback/next-load acceptance age 3 | One-cycle VMEM response assumption; logical MREG rules retained |
| XLU v2 | `VTRPOSE.XLU` reads at 1–32, writes at 34–65, next launch at 66; logical read/write release ages 33/65 | Frontend reservations retained; conflicting higher-priority traffic excluded |
| VPU | Access timing for 29 commands, including reductions, packing, unpacking, and immediates | Logical reservation floors, slot capacities, and lane exclusions retained |

Vector LSU reads stream at ages 1–32 and writes at 3–34; its path hold ends at 34 inclusive. Unknown VMEM addresses conservatively hold all banks and must be legal, aligned, in range, and nonwrapping at runtime.

XLU timing matches the built-in model. Connected analysis checks all 8,192 symbolic transpose bits, response routing, tracker identities, and frontend hazard predicates. Busy commands are ignored. Although a downstream datapath can consume early rows, ScalarCore assertions forbid launching it while the destination remains reserved. Same-row same-cycle MREG read/write is undefined and remains forbidden. See [connected XLU evidence and regeneration commands](rtlgraph-xlu-connected.md), including the required connected facts and independent RTL witness for v2 export.

DMA captures operands at issue. `DMA.CONFIG` immediately changes the global base; previously launched transfers retain saved addresses when registers or the base change. Matching waits establish completion; latency estimates only guide scheduling priority. Read/read DRAM ranges may overlap. Conflicting writes require ordering unless physical byte ranges are proven disjoint.

The compiler resolves profiles against concrete operands into row-level `Access` records, inclusive resource `Hold`s, logical reservations, and dependency edges. Its reservation table checks physical ports, banks, VPU slots, engines, and DMA lifetimes. A scalar latency per mnemonic cannot express all these constraints.

## Evidence and assumptions

The profiles share one fresh `EE290SimConfig` hardware-IR identity. Typed HW/Comb/Seq queries and finite control execution follow hierarchy, def-use, enables, counters, and state transitions, retaining locations and hashes. This targeted extraction recovers scheduling facts rather than inferring instruction timing by counting registers; the [lowering walkthrough](rtlgraph-lowering.md) traces the stages. Evidence includes local exhaustive Boolean/routing checks, pipeline and memory-port structure, bounded LSU state/counter composition across 32 MREG and six supported VMEM banks, and instruction-attributed RTL traces with numerical goldens.

Vector LSU reasoning assumes idle entry, empty response stages, reset low, no second command on the same path, one-cycle response, legal addresses, and exclusion of competing higher-priority accesses. It checks capture, addresses, response payloads, destination writes, VMEM masks, and release within those assumptions. It does not prove arbitrary interference or same-row visibility.

The [instruction coverage report](rtlgraph-instruction-coverage.md) covers connected VPU, scalar LSU, and MXU controls, overlap cases, and mutations. It distinguishes physical streams from frontend legality and numerical payload correctness. New VPU and scalar LSU timing corroborates existing values; it does not by itself establish a speedup.

Structural facts, bounded reasoning, finite witnesses, inherited behavior, and unproved assumptions remain separate. The compiler checker uses the same model as the scheduler; independent RTL execution and numerical goldens supply separate evidence. Whole-system kernel replays use VCS, while the connected XLU witness uses Verilator on a local RTL harness. Their scopes differ, and a finite replay demonstrates that program/input/environment. The cached simulator's saved FIRRTL matches the fresh artifact, but its source-to-executable linkage remains unverified. Fresh simulator build provenance remains deferred.

## Kernel validation

The [current kernel regression](rtlgraph-perf-validation.md) is the source for complete tables and controlled comparisons: eleven original kernels retain full-output goldens, while [three instrumented probes](rtlgraph-review.md) add observations missing from the original sources. The metric is first Atlas issue through successful `DBG0` after output-DMA completion, excluding host setup/checking. Both scheduling priorities preserve hazard checks; the empirically best choice varies by kernel.

Earlier results, including the full historical table, remain in [the previous model guide](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/b71003a/docs/rtlgraph-model.md#L127-L146). Those selected controls showed up to 13.02% improvement on fused attention. Current reruns also expose a limitation: layernorm changes from 4,803 to 4,830 edges with a 64-word host control, while the historical 128-word control reproduces 4,608 to 4,593. The phase audit shows output DMA launching 74 edges earlier in both controls, but taking 102 or 60 extra edges to finish; its memory-system cause remains unresolved. One controlled speedup is not a universal performance guarantee.

The expanded historical corpus recorded 24 candidate executions and 35,328 golden-word comparisons. A later four-profile LSU replay checked another 1,536 unary and 1,024 MXU0 words. Those schedules were byte-identical to earlier candidates, preserving the improvements without adding a speedup.

The [historical isolated XLU harness](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/b71003a/scripts/tests/xlu_rtl_check.cpp) passed 256 cases and 262,144 byte comparisons. The current connected harness adds 192 cases and 196,608 byte comparisons through actual XLU/MREG/LSU routing. Its early-overlap experiment deliberately excludes ScalarCore and violates that frontend's reservation assertion, so it is datapath evidence, not a legal compiler schedule. Details and negative cases are in the [XLU guide](rtlgraph-xlu-connected.md).

Attribution matters: fused-attention DMA-only critical scheduling took 22,132 edges, while adding MXU1 profiling took 22,136 despite a shorter compute window. Memory behavior offset the local improvement. Similarly, an eight-cycle unary CSR-window reduction did not improve its first-issue-to-completion interval. Local cycle counts are not whole-kernel claims, and list scheduling does not guarantee an optimum.

## Remaining limits

These partial profiles do not establish:

- whole-RTL or unbounded temporal correctness, every frontend acceptance/retry rule, or all payload/visibility/interference cases;
- path-sensitive DMA alias proofs, unknown branch targets, DMA in branch delay slots, or fixed external-memory completion;
- register allocation, tiling, algorithm changes, or numerical equivalence across engine assignments; or
- native [Merlin](https://github.com/ucb-bar/merlin) integration or an exact supported PyTorch operator set.

Broader interference and remaining inherited rules need additional evidence. The [handoff guide](rtlgraph-contract.md) preserves rich profiles and explicit completion events for future consumers without claiming a lossy minimum-gap projection is equivalent.
