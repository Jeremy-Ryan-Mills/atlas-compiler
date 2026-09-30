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
  --rtl-xlu-profile profiles/EE290SimConfig/xlu/atlas-xlu.profile
```

Omit a profile to retain the built-in behavior for that component. Selecting
RTL DMA requires robust DMA timing, explicit matching `DMA.WAIT` instructions,
and same-block completion in the supported scheduling subset. No automatic wait
insertion is added by this feature.

## Profile semantics

| Component | Derived fields | Important inherited or unresolved rules |
| --- | --- | --- |
| MXU0 | `overwrite_acc_read_hold=0` for overwrite-only `VMATMUL.MXU0` | First-write age 63, reuse gaps, acceptance, and other resources remain built in |
| MXU1 | `first_write_age=3`; `overwrite_acc_read_hold=0` for overwrite-only `VMATMUL.MXU1` | Accumulating operations retain their read hold; weight and other resource rules remain built in |
| DMA v2 | Issue-time command/config capture; 32-byte VMEM lines; eight channels and eight command slots; explicit-wait completion; LSU priority; configured 37-bit DRAM byte ranges | Off-chip completion has no fixed correctness bound; transfers above the admitted 4,096-byte subset are rejected |
| LSU | 32 rows at unit stride; source-read age 1; destination-write age 3; first-free age 35 for `VLOAD` and `VSTORE` | Scalar LSU timing, logical MREG reservations, same-cycle visibility, and frontend issue remain built in |
| XLU | `VTRPOSE.XLU` reads rows at ages 1–32, writes at 34–65, and permits the next launch at age 66 | Assumes idle entry and one-cycle MREG responses; logical reservation floors 33/65 and visibility rules remain inherited |

The LSU ages describe streams: reads occur at ages 1–32 and writes at ages 3–34.
The load/store path hold therefore ends at age 34 inclusive. Unknown VMEM
addresses retain conservative all-bank holds and must be legal, aligned,
in-range, and nonwrapping at runtime.

XLU timing matches the built-in model. From post-reset idle state, the extractor
checks all 8,192 symbolic transpose bits; unsupported or data-dependent control
is rejected. Busy commands are ignored, so software must prevent them. Delayed
responses shift completion; delayed profiles extend logical reservation floors.

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

The MXU profiles change only the fields listed above. Structural checks of a
local enable or valid chain do not prove every sequencer state, operand lifetime,
or numerical property. The VPU work corroborated current issue/resource rules
but produced no production VPU profile and no shortened RTL-derived latency.

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
- arbitrary cross-block DMA scheduling or a fixed external-memory bound;
- complete payload, visibility, and interference proofs for every engine;
- register renaming, tiling, algorithm changes, or numerical equivalence across
  different engine assignments; or
- a direct Merlin-native resource model.

The next evidence should cover consecutive and overlapping commands, broader
shared-resource interference, remaining inherited timing fields, and a recorded
source-to-simulator build. The [handoff guide](rtlgraph-contract.md) describes
how Merlin can use the profiles without first flattening them into lossy issue
gaps.
