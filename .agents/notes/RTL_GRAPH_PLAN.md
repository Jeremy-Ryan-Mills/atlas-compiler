# atlas-rtlgraph: deriving an instruction scheduling model from RTL

Updated 2026-09-30. RTL-graph derives the timing and hazard information a compiler needs from a pinned hardware configuration: operand reads and writes, resource occupancy, launch conditions, and completion events. Its lookup tables describe instruction scheduling, not FPGA cells. [The model guide](../../docs/rtlgraph-model.md) records profile semantics, measured results, and limits; earlier experiments remain at [`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765).

## Current checkpoint

The target is `chipyard.EE290SimConfig`, selected by the Atlas baremetal Makefile through `chipyard.harness.TestHarness`. Its current public definition is in [bringup-chipyard](https://github.com/ucb-ee194-tapeout/bringup-chipyard), at [`EE290Configs.scala`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26). This is a source reference, not a claim that moving `main` reproduces the checked-in profiles: their recorded revisions, elaboration inputs, and CIRCT artifact hash identify the analyzed hardware. Historical `AtlasShuttleVectorConfig` artifacts are not mixed with these profiles.

Fresh elaboration and CIRCT verification produced one shared hardware-IR identity. Typed structural analyses and finite executions support six optional partial profiles:

| Profile | Derived compiler-facing facts |
| --- | --- |
| MXU0 | First accumulator write at age 63; overwrite-only `VMATMUL.MXU0` does not reserve an accumulator read |
| MXU1 | First accumulator write at age 3; overwrite-only `VMATMUL.MXU1` does not reserve an accumulator read |
| DMA | Issue-time operand/configuration capture, explicit `DMA.WAIT` completion, 37-bit DRAM ranges, and VMEM/bank constraints |
| LSU | Vector read/write/free ages 1/3/35; scalar memory request at age 1 and writeback/next-load acceptance at age 3 |
| XLU | `VTRPOSE.XLU` reads at 1–32, writes at 34–65, and first releases the engine at 66; connected frontend checks retain logical reservation release ages 33/65 |
| VPU | Access timing for 29 implemented commands, with 841 ordered-pair control overlap checks |

The projections and evidence live under `profiles/EE290SimConfig/{mxu0,mxu1,dma,lsu,xlu,vpu}/`. Loaders reject malformed fields and mixed hardware identities; unselected rules retain built-in behavior. The [fresh kernel regression](../../docs/rtlgraph-perf-validation.md) covers eleven original `perf_*.S` kernels with full-output goldens, plus [three separately instrumented probes](../../docs/rtlgraph-review.md). Real-kernel wait insertion, memory-dependent branches through static CFG checks, and both scheduling priorities are exercised. The results include gains and a host-control-sensitive regression; finite execution does not establish universal speedup or safety.

## Lowering and consumers

```text
EE290SimConfig + pinned Chisel sources + elaboration inputs
    -> Chisel elaboration -> FIRRTL -> firtool
    -> CIRCT HW/Comb/Seq, with remaining SV constructs
    -> typed structural and bounded control queries + evidence
    -> operand access profiles, resource holds, completion rules
    -> atlas-opt + kernel operands and addresses
    -> dependency graph + reservation table -> checked schedule
```

Extraction follows module connections, def-use chains, enables, counters, and state transitions in elaborated hardware. Counting pipeline registers alone cannot establish acceptance, arbitration, or variable-latency completion. The hardware resource-timing graph is the conceptual analysis model; the retained implementation uses targeted typed queries and per-unit profiles, not a complete automatically recovered machine description. [CIRCT's dialect documentation](https://circt.llvm.org/docs/Dialects/) describes the underlying IR.

The compiler interface remains the compatibility target:

| Representation | Required meaning |
| --- | --- |
| `Access` | Storage identity/range, direction, age, row step, unknown-address aliasing, and completion lifetime |
| `Hold` | Resource identity and inclusive occupancy, including alternative resources |
| `Footprint` | Accesses, holds, logical reservations, release ages, and completion estimates |
| Dependency rules | RAW/WAR/WAW timing, sequencer rules, logical lifetimes, and explicit completion edges |
| `ReservationTable` | Physical MREG ports, VMEM banks, engine/VPU capacities, and reservations widened across DMA waits |

Profiles instantiate actual operands and known scalar values. Logical identity and physical contention differ: `m0` and `m32` are distinct logical registers sharing a physical bank. Read-after-read creates no data edge, but simultaneous reads can conflict on a port. Pairwise minimum distances cannot express every row stream, capacity constraint, or isolated forbidden issue gap.

## Evidence policy

Every admitted fact identifies hardware configuration, IR, tools, assumptions, and unsupported cases. Structural derivation, exhaustive local Boolean checks, finite simulation, bounded temporal reasoning, and inherited compiler rules remain distinct. A successful trace demonstrates its inputs and environment. Passing the scheduler's own checker establishes consistency with the selected model, not independent RTL correctness.

For example, the vector LSU analysis checks capture, row/bank/address progression, all payload bits, response routing, write ports, and release under idle entry, legal aligned nonwrapping addresses, one-cycle response, and arbitration exclusions. It does not establish arbitrary interfering traffic or same-row read/write visibility. Connected XLU datapath overlap is numerically possible before logical release, but frontend assertions prohibit it; the compiler preserves those reservations. Off-chip DMA has no fixed correctness bound and requires the matching `DMA.WAIT`.

## Integration boundary

`atlas-opt` composes profiles only when their hardware identities match. Its dependency graph, list scheduler, reservation table, and checker consume the resulting footprints. Critical-path and input-order scheduling share those legality checks. Optional `--validation static` checks known CFGs when numerical branch conditions are unavailable to the timing simulator; RTL execution remains a separate obligation. The optional [model-aware DMA wait pass](../../docs/rtlgraph-dma-integration.md) handles known control-flow edges, joins, and loops; fixed-latency engines still drain at block boundaries. `# atlas.release` marks publication. The functional parser on the separate [`insert-dma-waits` branch](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/insert-dma-waits) remains a future convergence task.

The [assembly/timing handoff](../../docs/rtlgraph-contract.md) packages `before.S`, profiles, evidence, resolved footprints, and compiler identity for an external consumer. A future [Merlin](https://github.com/ucb-bar/merlin) adapter could preserve these as Phase 0 evidence, invoke an optional Atlas scheduling/checking step, and retain profile identity with performance measurements. No Merlin integration is implemented here. A native consumer should retain row accesses, ports, capacities, and completion events instead of flattening them into mnemonic gaps.

## External references and portability

- The enclosing [`.gitmodules`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/.gitmodules) identifies course forks of [TestChipIP](https://github.com/ucb-ee194-tapeout/testchipip), [Saturn `bf16_fp8`](https://github.com/ucb-ee194-tapeout/saturn-vectors/tree/bf16_fp8), [Shuttle](https://github.com/ucb-ee194-tapeout/shuttle), and [Rocket Chip](https://github.com/ucb-ee194-tapeout/rocket-chip). Evidence records actual gitlinks, not only tracking branches.
- The compiler baseline references [`npu_model`'s `rtl-match` branch](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match) and its [timing notes](https://github.com/ucb-ee194-tapeout/npu_model/blob/rtl-match/docs/rtl-timing.md).
- Merlin is an optional reference, not a build dependency; the handoff comparison is pinned to [`81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb).
- [Radiance](https://github.com/ucb-bar/radiance) and its [Muon design](https://github.com/ucb-bar/radiance/blob/main/docs/muon.md) are potential targets for warp context, register mapping, occupancy, operand collection, and hardware-enforced hazards.
- [Vortex](https://github.com/vortexgpgpu/vortex), its [microarchitecture](https://github.com/vortexgpgpu/vortex/blob/master/docs/designs/microarchitecture.md), and [scoreboard](https://github.com/vortexgpgpu/vortex/blob/master/hw/rtl/core/VX_scoreboard.sv) provide a SystemVerilog portability target. A small slice through [CIRCT's SystemVerilog frontend](https://circt.llvm.org/docs/Tools/circt-verilog/) would test feasibility before a full import.

The reusable component is the query/evidence framework. Each target needs its own instruction identity, acceptance, completion, resource, warp, and stall semantics. A rule can constrain legal software issue on Atlas while predicting a hardware stall elsewhere.

## Follow-up

The kernel regression, automatic-wait validation, static CFG checking, and tooling/documentation cleanup form the completed review checkpoint. [The review notes](../../docs/rtlgraph-review.md) retain additional findings and a draft PR description.

Extend frontend/interference coverage and replace remaining inherited rules only where evidence supports the change. Define a versioned complete machine schema before replacing component loaders. Fresh simulator build provenance remains deferred. Whole-hardware proofs, arbitrary variable-latency bounds, Merlin implementation, and full Radiance/Vortex imports are outside this checkpoint.
