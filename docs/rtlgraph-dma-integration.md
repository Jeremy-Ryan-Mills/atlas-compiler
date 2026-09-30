# Model-aware DMA waits

The optional `insert-dma-waits` pass uses the selected `MachineModel` and permits
DMA lifetimes to cross basic-block boundaries. Fixed-latency engines still drain
at block ends. Native assembly retains explicit branch delay slots and may keep
its existing waits.

```sh
build/atlas-opt before.S -o after.S \
  --rtl-dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --passes strip-artifacts,insert-dma-waits,fill-delay-slots,schedule
```

Wait insertion is opt-in. The default pipeline continues to require correctly
placed waits, but now validates them over the complete control-flow graph rather
than requiring idle DMA at every label. A standalone `buildGraph` call without
incoming control-flow state retains the stricter block-local requirement.

## What the selected model changes

| Concern | Built-in model | RTL DMA profile |
|---|---|---|
| Scalar operands | Live until completion | Captured at launch |
| `DMA.CONFIG` | Queued command | Immediate captured base state |
| Memory completion | Matching wait establishes completion | Matching wait establishes completion |
| DRAM aliasing | Existing model coverage | 64-bit projected ranges when the profile supports them; otherwise conservative |
| Command ring | Existing model behavior | Wait before a still-live slot could be overwritten by its eighth subsequent launch |

The pass does not insert waits for issue-captured scalar reuse or treat an RTL
`DMA.CONFIG` as an outstanding transfer. It inserts waits before conflicting
memory accesses, channel reuse, occupied-ring reuse, `atlas.release`, halt, and
program falloff. An estimated transfer latency only guides scheduling; no amount
of `DELAY` establishes completion.

## Control-flow analysis

Each DMA launch has a site identity and the footprint evaluated at that site.
The analysis propagates potentially pending sites per channel through branches,
joins, and backedges. A matching `DMA.WAIT` clears that channel. Register constants
and captured base values join conservatively before range evaluation.

For the RTL ring, each pending site also records the maximum number of later
launches, saturated at eight. This catches the case where one old transfer remains
live while another channel repeatedly launches and retires. Counting outstanding
channels alone cannot prove that the next ring slot is free.

Wait masks grow until the finite control-flow analysis reaches a fixed point.
The scheduler receives incoming footprints and binds dependent instructions to
the matching wait. Disjoint work may still move ahead of that wait. Branches to
end labels and final fallthrough receive a synthetic exit when a completion wait
is necessary. Waits protecting a delay-slot operation are placed before its
branch, preserving the native slot layout.

The analysis assumes idle DMA at kernel entry, known branch targets, and the
selected profile's existing address/size rules. It rejects DMA instructions in
branch delay slots. Joins retain all possible pending sites rather than proving
branch correlations; this can insert redundant waits. It does not infer a bound
on external DMA service time, reassign channels, or accept larger transfers than
the selected profile already supports. The 4,096-byte admission maximum is an
imposed subset, not a consequence of the 13-bit size field; larger aligned sizes
need separate burst/counter and VMEM-boundary validation.

## Relationship to `insert-dma-waits`

The latest inspected remote commit was
[`3ae2b5d`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/commit/3ae2b5d76909c529df14c89933f737f9ab19be8d).
That branch's [functional/executable assembly contract](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/3ae2b5d76909c529df14c89933f737f9ab19be8d/ASSEMBLY_CONTRACT.md)
changes the input interface and describes completion-time DMA operand reads.
This integration does not merge that branch or change native assembly syntax.
It addresses the shared implementation requirement: footprint construction,
wait placement, range conflicts, and scheduling must consume the same selected
hardware model. A future convergence can retain that model threading while
adopting the functional parser and completion spelling separately.

The profile identity and distinction between completion events and cost estimates
remain explicit for future consumers, including Merlin. No Merlin integration is
included here.

## Validation

`dma-wait-tests` covers issue-time versus completion-time operands, immediate
configuration, divergent base values, incoming lifetimes at joins, channel reuse
on loop backedges, occupied-ring wrap across blocks, DRAM aliases, publication,
and falloff. The emitted schedules are checked with the compiler dynamic
hazard checker at 0.001x, 1x, and 100x modeled DMA latency. These are compiler
regressions, not a new universal RTL completion proof or a performance claim.

For an independent hardware witness, `scripts/rtlgraph_dma_witness.py` emits native
input/output, restricted baremetal assembly, and a bit-exact DRAM golden. It copies
three 128-byte blocks while taking both branch outcomes and reusing pointer
registers immediately after capture. The final matching wait is inside its CSR
cycle bracket. The generated manifest records hashes; simulator execution and
source-to-binary provenance must be established by the replay separately.

The [cached RTL replay](../profiles/EE290SimConfig/dma/cfg-replay.json) passed all
96 DRAM word checks for 384 copied bytes, taking both branch outcomes across three
iterations. Its complete DMA-copy bracket measured 1,005 CSR cycles, including the
final wait. This is a correctness witness with a recorded duration, not a speedup
comparison. The replay verified the cached simulator and 780 runtime artifacts;
it does not establish a fresh source-to-executable build chain.
