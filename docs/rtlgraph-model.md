# RTL-derived Atlas scheduling profiles

This guide describes the production surface retained from the RTL-graph
experiments. The profiles are partial, opt-in projections for
`chipyard.EE290SimConfig`; they augment the existing `atlas-opt` machine model
rather than replacing it. Earlier extractors, mutation tests, trace tools, and
chronological experiment notes remain available at
[`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765).

## Use the profiles

The checked-in layout is:

```text
profiles/EE290SimConfig/
    mxu0/atlas-mxu0.profile
    mxu1/atlas-mxu1.profile
    dma/atlas-dma.profile
    lsu/atlas-lsu.profile
    xlu/atlas-xlu.profile
    vpu/atlas-vpu.profile
```

Each directory also retains canonical evidence for the projection. Profile and
evidence hashes identify the CIRCT artifact and report; loading a file does not
rerun or certify the hardware analysis. The loader rejects unknown, missing,
duplicate, or malformed fields and profiles with different hardware-IR hashes.

```sh
build/atlas-opt before.S -o after.S \
  --experimental-mxu0-profile profiles/EE290SimConfig/mxu0/atlas-mxu0.profile \
  --experimental-mxu1-profile profiles/EE290SimConfig/mxu1/atlas-mxu1.profile \
  --rtl-dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --rtl-lsu-profile profiles/EE290SimConfig/lsu/atlas-lsu.profile \
  --rtl-xlu-profile profiles/EE290SimConfig/xlu/atlas-xlu.profile \
  --rtl-vpu-profile profiles/EE290SimConfig/vpu/atlas-vpu.profile
```

Omit a profile to retain the built-in behavior for that component. Selecting
RTL DMA requires robust DMA timing. Matching `DMA.WAIT` instructions may be
supplied or inserted with the optional `insert-dma-waits` pass; lifetimes may
cross known control-flow edges, including joins and loops. See the
[DMA integration guide](rtlgraph-dma-integration.md) for the pass sequence and
remaining restrictions.

## Profile semantics

| Component | Derived fields | Important inherited or unresolved rules |
| --- | --- | --- |
| MXU0 v2 | First-write age 63; `overwrite_acc_read_hold=0` for overwrite-only `VMATMUL.MXU0` | Reuse gaps and resource rules retained; connected control checks now cover all seven commands |
| MXU1 | `first_write_age=3`; `overwrite_acc_read_hold=0` for overwrite-only `VMATMUL.MXU1` | Accumulating operations retain their read hold; weight and other resource rules remain built in |
| DMA v2 | Issue-time command/config capture; 32-byte VMEM lines; eight channels and eight command slots; explicit-wait completion; LSU priority; configured 37-bit DRAM byte ranges | Off-chip completion has no fixed correctness bound; transfers above the admitted 4,096-byte subset are rejected |
| LSU v2 | Vector read/write/free ages 1/3/35; scalar memory request age 1, load writeback and next-load acceptance age 3 | Scalar result assumes a one-cycle VMEM response; logical MREG rules retained |
| XLU v2 | `VTRPOSE.XLU` reads at 1–32, writes at 34–65, next launch at 66; logical read/write reservation release ages 33/65 | Connected MREG response/arbitration and ScalarCore assertions checked; conflicting higher-priority traffic must be excluded |
| VPU | Operand access timing for 29 implemented commands, including row/column reductions, packing, unpacking and immediates | Logical reservation floors, slot capacities and lane exclusion rules retained |

The LSU ages describe streams: reads occur at ages 1–32 and writes at ages 3–34.
The load/store path hold therefore ends at age 34 inclusive. Unknown VMEM
addresses retain conservative all-bank holds and must be legal, aligned,
in-range, and nonwrapping at runtime.

XLU timing matches the built-in model. The extractor checks all 8,192 symbolic
transpose bits, connected response routing, tracker identities and frontend
hazard predicates. Busy commands are ignored. A downstream datapath can consume
early rows before the transpose finishes, but ScalarCore assertions still forbid
that launch while the destination is reserved. The compiler preserves that
constraint. Same-row same-cycle MREG read/write is undefined and remains
forbidden. See [connected XLU evidence](rtlgraph-xlu-connected.md).

Regenerate with `python3 scripts/rtlgraph_xlu.py --query RTLGRAPH_EXPORT --hardware-ir atlas.hw.mlir --output DIR`, using the
[retained typed exporter](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/dd7342ce6a3c4d051544bf719086edb59cb80765/scripts/rtlgraph_query.cpp).
Pass its `XluEngine` JSON to `scripts/tests/test_rtlgraph_xlu.py` to enable the
four hardware regressions that ordinary CTest skips without that artifact.

DMA operands are captured when the command issues. `DMA.CONFIG` updates the
global base at issue; subsequent transfers retain their saved addresses even if
scalar registers or the base later change. Completion is represented by the
matching wait rather than by the latency estimate used for scheduling priority.
Read/read DRAM ranges may overlap; conflicting writes require ordering unless
the physical byte ranges are proven disjoint.

The compiler resolves these templates for each concrete instruction. It emits
row-level `Access` records, inclusive resource `Hold`s, logical reservations,
and dependencies, then checks physical ports, bank capacity, VPU slots, engine
capacity, and DMA lifetimes through its existing reservation table. A scalar
latency per mnemonic cannot represent these rules.

## Evidence and assumptions

The profiles share one fresh `EE290SimConfig` CIRCT hardware-IR identity. The
analyses use typed HW/Comb/Seq operations and preserve source locations and
artifact hashes. Their evidence includes:

- exhaustive local Boolean checks for MXU accumulator-read enables, VPU issue
  masks, MREG bank mapping, DMA capture, arbitration, and selected routing;
- structural pipeline and memory-port checks;
- bounded LSU state/counter composition and all-bit payload routing across all
  32 MREG and six supported VMEM banks; and
- instruction-attributed RTL traces with numerical golden checks for selected
  kernels.

The LSU timing result assumes idle entry, empty response stages, reset low, no
second command on the same path, one-cycle memory response, legal addresses,
and exclusion of conflicting higher-priority accesses. Under those conditions,
the analysis checks command operand capture, row/address selection, response
payload selection, destination write enables, full VMEM masks, and release. It
does not prove arbitrary traffic or same-row read/write visibility.

The [instruction coverage report](rtlgraph-instruction-coverage.md) records the
connected VPU, scalar LSU and MXU control checks, overlap scenarios and mutations.
They do not prove arithmetic payloads, arbitrary initial state or every frontend
assertion. The new VPU and scalar LSU profiles corroborate current timing; they
do not claim a new speedup.

Evidence labels retain these distinctions:

- derived structural or bounded facts;
- finite simulation witnesses;
- inherited compiler behavior; and
- explicitly unproved assumptions.

The compiler checker consumes the same model as the scheduler, so passing it is
not independent RTL validation. A finite replay demonstrates that exact program,
input, and environment. The cached simulator's saved FIRRTL matches the fresh
source artifact, but its source-to-executable build linkage remains unverified.

## Validation results

Every distinct optimized program below passed its original full-output golden
and the applicable native and RTL event checks. Times are first Atlas issue
through successful `DBG0` after output-DMA completion, measured under one shared
host and environment per kernel.

| Kernel | Handwritten edges | Best profiled schedule | Reduction |
| --- | ---: | ---: | ---: |
| `perf_fused_attention_mxu0.S` | 25,445 | 22,132 | 13.02% |
| `perf_fused_attention_mxu1.S` | 24,932 | 22,132 | 11.23% |
| `perf_mm_dual_128x128x128.S` | 42,792 | 41,093 | 3.97% |
| `perf_mm_mxu0_64x64x64.S` | 12,189 | 11,941 | 2.03% |
| `perf_mm_mxu0_64x64x128.S` | 19,114 | 18,510 | 3.16% |
| `perf_mm_mxu1_64x64x64.S` | 12,189 | 11,941 | 2.03% |
| `perf_mm_mxu1_64x64x128.S` | 19,114 | 18,510 | 3.16% |
| `perf_softmax.S` | 4,491 | 4,437 | 1.20% |
| `perf_unary.S` | 10,181 | 9,083 | 10.78% |
| `perf_vec_layernorm_32x32.S` | 4,608 | 4,593 | 0.33% |
| `perf_vec_rmsnorm_softmax.S` | 8,459 | 7,554 | 10.70% |

The expanded corpus recorded 24 candidate executions and 35,328 golden-word
comparisons. A later four-profile LSU replay rechecked another 1,536 unary and
1,024 MXU0 words. Those emitted programs were byte-identical to the previously
optimized candidates, so the LSU profile preserved the improvements but added
no new speedup.

The [XLU Verilator harness](../scripts/tests/xlu_rtl_check.cpp) passed 256 cases
and 262,144 byte comparisons on the same CIRCT artifact: every source ID,
in-place/distinct destinations, consecutive launches, busy-command rejection,
and one/two-cycle responses. These isolated-module witnesses do not establish
system response latency, frontend routing, or a new kernel speedup.

Attribution matters. On fused attention, DMA-only critical scheduling completed
in 22,132 edges, while adding the MXU1 profile completed in 22,136 despite a
shorter compute window. Memory behavior offset the local model improvement.
Likewise, a fixed-operation unary search reduced its original CSR window by
eight cycles without improving its first-issue-to-completion interval. Local
cycle counts are not whole-kernel performance claims.

## Remaining limits

The production profiles are a partial machine description. They do not provide:

- a whole-RTL or unbounded temporal proof;
- complete frontend acceptance and retry semantics for every instruction;
- path-sensitive DMA alias proofs, unknown branch targets, DMA in branch delay
  slots, or a fixed external-memory bound;
- complete payload, visibility, and interference proofs for every engine;
- register renaming, tiling, algorithm changes, or numerical equivalence across
  different engine assignments; or
- a direct Merlin-native resource model.

Consecutive and overlapping engine checks are now included, with bounded scope.
Broader interference and remaining inherited rules still need evidence. Fresh simulator build provenance remains deferred. The [handoff guide](rtlgraph-contract.md) describes
how Merlin can use the profiles without first flattening them into lossy issue
gaps.
