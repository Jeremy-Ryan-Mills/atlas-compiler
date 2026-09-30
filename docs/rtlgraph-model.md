# RTL-derived Atlas scheduling profiles

The optional profiles augment `atlas-opt` for `chipyard.EE290SimConfig`, described in [bringup-chipyard](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26), using [Atlas NPU](https://github.com/ucb-bar/atlas-npu) sources. Each component directory under `profiles/EE290SimConfig/` contains a partial `.profile` and canonical extraction evidence. Recorded revisions and the shared CIRCT artifact hash identify the analyzed hardware; moving source links are references. Unselected rules retain built-in behavior.

## Use the profiles

```sh
build/atlas-opt before.S -o after.S \
  --experimental-mxu0-profile profiles/EE290SimConfig/mxu0/atlas-mxu0.profile \
  --experimental-mxu1-profile profiles/EE290SimConfig/mxu1/atlas-mxu1.profile \
  --rtl-dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --rtl-lsu-profile profiles/EE290SimConfig/lsu/atlas-lsu.profile \
  --rtl-xlu-profile profiles/EE290SimConfig/xlu/atlas-xlu.profile \
  --rtl-vpu-profile profiles/EE290SimConfig/vpu/atlas-vpu.profile
```

Omit unused profiles. Loaders reject unknown, missing, duplicate, malformed fields and mixed hardware identities. Loading does not rerun hardware extraction. RTL DMA requires robust timing, which is the default. Compare the default critical-path heuristic with `--schedule-priority input` using complete kernel runs; both retain the same legality checks and neither guarantees an optimum.

For branches depending on numerical loads, `--validation static` checks dependency distances, reservations, fixed-engine drains, and reachable DMA lifetimes without evaluating numerical branch conditions. It requires robust timing and an RTL DMA profile when DMA is present. It provides no dynamic latency estimate and cannot be combined with footprint export. Dynamic validation remains the default.

## Profile semantics

Age zero is an accepted engine command. Access records resolve actual operands into row streams; resource holds have inclusive endpoints. Dependency edges and the reservation table enforce logical hazards, physical ports/banks, engine/VPU capacities, and explicit completion lifetimes.

| Component | Derived facts | Retained rules or admission assumptions |
| --- | --- | --- |
| MXU0 | First accumulator write age 63; overwrite `VMATMUL.MXU0` has no accumulator-read hold | Reuse/resources retained; connected controls cover seven commands |
| MXU1 | First accumulator write age 3; overwrite `VMATMUL.MXU1` has no accumulator-read hold | Accumulating reads, weights, and other resources retained |
| DMA | Issue capture, 32-byte VMEM lines, eight channels/slots, explicit waits, LSU priority, 37-bit DRAM ranges | No fixed external completion bound; maximum admitted transfer 4,096 bytes |
| LSU | Vector reads 1–32, writes 3–34, next launch 35; scalar memory request 1, load writeback/next-load 3 | One-cycle VMEM response; logical MREG reservations retained |
| XLU | `VTRPOSE.XLU` reads 1–32, responses 2–33, writes 34–65, next launch 66; logical releases 33/65 | Frontend reservations; higher-priority conflicting traffic excluded |
| VPU | 29 commands: read age 0; ordinary write 2, row-sum 7, column reduction 66, pack/unpack 3, immediate 1 | Logical reservation floors, slot capacity, lane exclusions retained; reserved `fp8` command excluded |

VPU/MXU streams contain 32 rows per MREG. VPU pair operations select the second register according to the FSM; pack writes every other cycle, unpack writes 64 consecutive rows, pair-row sum writes both destinations together, and column reductions read the source pair twice. Unknown VMEM addresses hold all banks conservatively and must remain aligned, legal, in range, and nonwrapping at runtime.

XLU's connected routing checks all 8,192 transpose bits, physical arbitration/response routing, and frontend predicates for 64 register IDs. A launch at age 65 is ignored; age 66 is accepted. Early downstream row consumption can work numerically, but ScalarCore assertions require destination release through age 65. The compiler preserves that reservation. Same-row same-cycle MREG read/write is undefined; higher-priority reads can lose an XLU response without retry. Both conflicts remain excluded.

## DMA and control flow

DMA captures scalar operands and `DMA.CONFIG` base state at issue; later register/base changes do not alter launched transfers. Config changes immediately affect subsequent launches. Matching `DMA.WAIT` establishes completion; estimates only guide scheduling priority. Read/read DRAM ranges may overlap, while conflicting writes require ordering unless physical byte ranges are proven disjoint.

Wait insertion is opt-in:

```sh
build/atlas-opt before.S -o after.S \
  --rtl-dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --passes strip-artifacts,insert-dma-waits,fill-delay-slots,schedule
```

The pass propagates pending launch-site footprints per channel across known branches, joins, and loops, growing wait masks to a fixed point before dependency construction. It inserts waits for conflicting memory accesses, channel/ring reuse, `atlas.release`, halt, and falloff. An old transfer must complete before its slot can be overwritten by the eighth subsequent launch; counting pending channels alone is insufficient. Captured scalar reuse and immediate configuration need no wait. Disjoint work may move ahead of waits; joins do not fence every channel. Fixed-latency engines still drain at block boundaries.

Admission assumes idle entry and known branch targets, rejecting DMA in branch delay slots. Waits protecting slot operations precede the branch; necessary exit waits receive a synthetic exit block. Register constants and captured bases join conservatively, so lost branch correlations can add waits. The 4,096-byte maximum is an imposed validated subset, not the limit implied by the 13-bit size field. Larger sizes need burst/counter and VMEM-boundary validation. A standalone `buildGraph` without incoming CFG state retains block-local completion requirements. The [README](../README.md) specifies the exact `atlas.release` marker and publication rules.

## Evidence and assumptions

Typed HW/Comb/Seq queries follow hierarchy, def-use, enables, counters, and state transitions, retaining source locations and hashes. Connected control execution covers all seven commands for each MXU, 29 VPU commands with 841 ordered-pair overlap cases, and scalar LSU capture/request/writeback. VPU pair checks use distinct operands at the engine mask's earliest launch; they do not remove frontend reservations for aliased operands. MXU boundary cases distinguish streaming readiness from acceptance: accumulator push followed by MXU1 accumulating multiply fails at gap 32 and succeeds at 33. Mutation checks detect changed valid stages and reject payload-dependent timing cones.

Vector LSU evidence checks capture, address/bank progression, payload routing, destination writes, and release across 32 MREG and six supported VMEM banks. It assumes reset low, idle entry/empty response stages, legal aligned nonwrapping addresses, no second same-path command, one-cycle responses, and excluded higher-priority interference. Scalar LSU additionally uses accepted-command/decode/host-start cutpoints. Arbitrary contention and same-row visibility remain unproved.

Structural derivation, exhaustive local Boolean/routing checks, bounded control reasoning, inherited rules, and finite RTL witnesses have distinct scopes. The scheduler and checker share a model. Independent VCS whole-system replays and a Verilator XLU/MREG/LSU witness add numerical evidence; the latter deliberately excludes ScalarCore for its early-overlap experiment. A finite replay establishes its program/input/environment. The cached simulator's saved FIRRTL matches the analyzed artifact, but source-to-executable build linkage remains unverified; fresh simulator provenance is deferred. These projections establish neither whole-RTL/unbounded correctness, arbitrary DMA latency bounds, nor a native Merlin/PyTorch lowering interface.

See [the lowering walkthrough](rtlgraph-lowering.md) for extraction and compiler reproduction, [results](rtlgraph-perf-validation.md) for numerical/performance evidence, and [the contract](rtlgraph-contract.md) for schema identities and external-consumer bundles.

## Dispatcher compatibility

[Bringup-chipyard PR12](https://github.com/ucb-ee194-tapeout/bringup-chipyard/pull/12), inspected at head `0f8648a7859607a7facd0a5faeaedb5ed1daaaf7`, updates Atlas to `de0ec2a9e434b2972d040dbd74d1e4450040b259`. The [public source comparison](https://github.com/ucb-bar/atlas-npu/compare/2ae0bef209df6db78c3de18e8f651bb43855cce9...de0ec2a9e434b2972d040dbd74d1e4450040b259) leaves Atlas `src/main/scala` and `chipyard` trees identical, preserving engine timing facts. Dispatcher kernels still need a separate ABI adapter reserving x28–x31 and DBG0/DBG1, relocating slot targets, and establishing compute/store/DMA completion before `JALR x0, x31, 1; NOP`; [DONE publication does not drain engines](https://github.com/ucb-bar/atlas-npu/blob/de0ec2a9e434b2972d040dbd74d1e4450040b259/baremetal/dispatcher/dispatcher.S#L25-L35), and current optimization/CFG checks reject indirect returns. PR12 changes Rocket/Shuttle/Tacit revisions, so whole-system and performance claims need fresh target identity and execution evidence; Atlas source equality does not establish the existing CIRCT identity for that system.
