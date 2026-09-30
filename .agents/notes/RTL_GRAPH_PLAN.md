# atlas-rtlgraph: deriving an instruction scheduling model from RTL

Updated 2026-09-30. This document describes the durable design and current
boundary of the RTL-graph work. The detailed extraction scripts, mutation tests,
replay commands, and chronological evidence remain available at
[`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765).

The goal is to derive the timing and hazard information a compiler needs from a
pinned hardware configuration: when an instruction reads and writes each
operand, which resources it occupies, which launch conditions must hold, and
which completion events cannot be represented by a fixed latency. The resulting
lookup tables concern instruction scheduling, not FPGA lookup-table cells.

## Current checkpoint

The extraction and validation target is `chipyard.EE290SimConfig`, selected by
the Atlas baremetal Makefile through `chipyard.harness.TestHarness`. It composes
the Atlas tile with the course Chipyard, Shuttle, Saturn, TestChipIP, and Rocket
Chip revisions recorded by the enclosing checkout's `.gitmodules`. Artifacts
from `AtlasShuttleVectorConfig` are historical and are not mixed with this
profile.

Fresh elaboration and CIRCT verification produced one shared hardware-IR
identity. Typed structural analyses and finite RTL traces support five optional
partial profiles:

| Profile | Derived compiler-facing facts |
| --- | --- |
| MXU0 | Overwrite-only `VMATMUL.MXU0` does not reserve an accumulator read |
| MXU1 | First accumulator write is age 3; overwrite-only `VMATMUL.MXU1` does not reserve an accumulator read |
| DMA | Operands and configuration are captured at issue; completion is released by explicit `DMA.WAIT`; configured 37-bit DRAM ranges and VMEM/bank constraints are tracked |
| LSU | `VLOAD` and `VSTORE` read rows at ages 1–32, write rows at ages 3–34, and first release their path at age 35 |
| XLU | Under idle entry and one-cycle MREG responses, `VTRPOSE.XLU` reads at ages 1–32, writes at 34–65, and first releases the engine at age 66; symbolic routing checks the byte transpose |

The checked-in projections and evidence are under
`profiles/EE290SimConfig/{mxu0,mxu1,dma,lsu,xlu}/`. Their loaders reject malformed
fields and refuse to compose profiles from different hardware IR. Unselected
rules retain the compiler's built-in behavior.

The scheduler has been validated on the eleven `perf_*.S` kernels with full
output goldens. The strongest controlled result reduces fused-attention
first-issue-to-completion time from 24,932 to 22,132 edges on MXU1 and from
25,445 to 22,132 on MXU0. These are finite executions under shared per-kernel
hosts, not universal speedup or safety proofs. The [model guide](../../docs/rtlgraph-model.md)
records the exact profile semantics, results, and limitations.

## Representations and consumers

```text
EE290SimConfig + pinned sources + environment assumptions
    -> elaborated CIRCT HW/Comb/Seq
    -> structural and temporal queries + evidence ledger
    -> resource/timing graph and instruction profiles
    -> atlas-opt + concrete operands and addresses
    -> kernel dependencies + resource reservations
    -> checked schedule
```

The hardware resource-timing graph contains storage, ports, engines, queues,
state, guards, and event timing. Instruction profiles project those facts into
operand-dependent accesses and resource holds. An instruction interaction graph
is useful for inspection, but pairwise distances alone cannot express bank
selection, repeated row accesses, alternative ports, capacity, or variable
completion. The concrete kernel graph and reservation table remain the scheduling
consumer.

The existing compiler interface is the compatibility target:

| Representation | Required meaning |
| --- | --- |
| `Access` | Storage identity, range, read/write direction, start age, row step, unknown-address aliasing, and completion lifetime |
| `Hold` | Resource identity and inclusive occupancy interval, including alternative resources |
| `Footprint` | An instruction's accesses, holds, logical reservations, release ages, and completion estimates |
| Dependency rules | RAW/WAR/WAW timing, logical-register lifetimes, sequencer rules, and explicit completion edges |
| `ReservationTable` | Physical MREG ports, VMEM banks, engine capacities, VPU slots, and reservations widened across DMA waits |

Profiles must be instantiated with actual operands and known scalar values.
Logical identity and physical contention are separate: for example, `m0` and
`m32` name different logical rows in the same MREG bank. Read-after-read creates
no data edge, while simultaneous reads may still conflict on a port.

## Evidence policy

Every admitted fact must identify its configuration, hardware IR, extraction
tool, assumptions, and unsupported cases. Evidence categories are deliberately
distinct:

- structural derivation follows typed CIRCT definitions, uses, hierarchy, and
  state-update cones;
- exhaustive local checks enumerate a bounded Boolean or bit-vector function;
- finite traces demonstrate a particular accepted execution and bind events to
  instruction identities;
- bounded temporal checks establish only the stated initial state, bound, and
  environment assumptions;
- inherited rules remain compiler assumptions and are not relabeled as RTL
  results.

A passing witness does not prove all states or interference patterns. A compiler
schedule passing its own checker establishes consistency with the selected model,
not independent RTL correctness. Numerical goldens and RTL event checks remain
separate obligations.

The current LSU proof illustrates the boundary. It checks operand capture,
32-row progression, bank/address selection, all payload bits, response routing,
destination write ports, and path release under idle entry, legal aligned
nonwrapping addresses, one-cycle memory response, and explicit arbitration
exclusions. It does not establish arbitrary simultaneous traffic, same-row
read/write visibility, scalar issue legality, or whole-kernel safety. DMA has no
fixed off-chip completion bound; correctness uses the matching `DMA.WAIT`.

## Integration boundary

`atlas-opt` accepts the five profiles independently and composes them only when
their hardware identity matches. It continues to build `Access`, `Hold`, and
`Footprint` objects and uses the existing dependency graph, list scheduler,
reservation table, and final checker. This branch requires explicit `DMA.WAIT`
instructions and `# atlas.release`; automatic wait insertion is a separate
integration task.

The [assembly and timing handoff](../../docs/rtlgraph-contract.md) packages an
emitter's `before.S`, selected profiles and evidence, operand-resolved
footprints, and compiler identity. This gives Merlin a usable boundary today:
Merlin can emit operations and buffer assignments, then invoke `atlas-opt` for
dependency and resource scheduling. A future native Merlin consumer should
retain row accesses, bank and port occupancy, capacities, and completion events
rather than collapsing them into mnemonic-level gaps.

The newer functional assembly contract on the compiler's `insert-dma-waits`
branch is deferred. When the branches meet, wait insertion must use the selected
model's issue-time operand capture, DRAM ranges, channel and ring reuse, and
completion rules. That compatibility work does not change the RTL-graph feature's
current extraction or scheduling function.

## External references

- The enclosing `.gitmodules` supplies the target's course forks:
  [TestChipIP](https://github.com/ucb-ee194-tapeout/testchipip),
  [Saturn `bf16_fp8`](https://github.com/ucb-ee194-tapeout/saturn-vectors/tree/bf16_fp8),
  [Shuttle](https://github.com/ucb-ee194-tapeout/shuttle), and
  [Rocket Chip](https://github.com/ucb-ee194-tapeout/rocket-chip). Branch names
  are tracking choices; evidence must record the actual parent gitlinks.
- The compiler baseline comes from [`npu_model`'s `rtl-match` branch](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match),
  including its [timing notes](https://github.com/ucb-ee194-tapeout/npu_model/blob/rtl-match/docs/rtl-timing.md).
- Merlin is an optional reference and future richer consumer, not a build
  dependency. The inspected comparison point is
  [`ucb-bar/merlin@81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb).
- [Radiance](https://github.com/ucb-bar/radiance) and its
  [Muon design](https://github.com/ucb-bar/radiance/blob/main/docs/muon.md) are
  portability targets for warp context, register mapping, occupancy, operand
  collection, and hardware-enforced hazards.
- [Vortex](https://github.com/vortexgpgpu/vortex) provides a SystemVerilog
  portability target; its [microarchitecture](https://github.com/vortexgpgpu/vortex/blob/master/docs/designs/microarchitecture.md)
  and [scoreboard](https://github.com/vortexgpgpu/vortex/blob/master/hw/rtl/core/VX_scoreboard.sv)
  expose readiness, operand collection, execution, and commit behavior.

The reusable component is the typed-query and evidence framework, not one Atlas
latency table. Radiance/Muon and Vortex must supply their own instruction
identity, acceptance, completion, resource, warp, and hardware-stall semantics.
For Vortex, begin with a small scoreboard/operand-collector slice through the
[CIRCT SystemVerilog frontend](https://circt.llvm.org/docs/Tools/circt-verilog/).

## Next milestone

The present branch is a coherent partial-model milestone. The next work should:

1. prove or conservatively model consecutive and overlapping commands, frontend
   acceptance, and additional shared-resource interference;
2. replace remaining inherited timing rules only when evidence is strong enough
   to change correctness scheduling;
3. feed a small MLP or attention kernel emitted by Merlin through the existing
   handoff, `atlas-opt`, RTL replay, and numerical validation;
4. define a versioned complete machine schema before replacing the current
   component-specific loaders; and
5. resolve source-to-simulator binary lineage and add scoped formal properties
   where finite traces are insufficient.

Complete whole-hardware timing proof, arbitrary variable-latency bounds, direct
Merlin model consumption, and full Radiance/Vortex imports remain outside this
milestone.
