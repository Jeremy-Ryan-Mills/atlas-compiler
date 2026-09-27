# LSU/VPU overlap evidence

The `EE290SimConfig` memory-overlap experiment schedules independent `VLOAD`, VPU, and `VSTORE` work together. It preserves logical register reservations and physical port constraints. It does not stream a consumer into a partially written register. Performance comparisons and the generated kernel are recorded in the [memory-overlap experiment](rtlgraph-memory-overlap.md).

## Hardware obligations

The [LSU](../../src/main/scala/atlas/lsu/LSU.scala#L163-L215) has separate load and store state, pending stages, and busy signals. `VLOAD` and `VSTORE` commands require their respective path to be idle and a 1 KiB aligned transfer contained in one VMEM bank. The [stream logic](../../src/main/scala/atlas/lsu/LSU.scala#L237-L330) generates 32-row requests, registers incoming responses, and keeps the corresponding logical MREG reservation active while that path remains busy.

The [scalar hazard predicates](../../src/main/scala/atlas/scalar/ScalarCore.scala#L123-L170) prohibit VPU reads while either source register has a pending writer and prohibit VPU writes while the destination has a pending reader or writer. `VSTORE` requires its source writer to finish. `VLOAD` has the existing special rule that checks a pending writer but permits writing a register being read; this experiment does not use that exception to overlap dependent data. The [scalar issue logic](../../src/main/scala/atlas/scalar/ScalarCore.scala#L248-L249) stalls for `DELAY` and `DMA.WAIT`; [MREG and LSU busy violations](../../src/main/scala/atlas/scalar/ScalarCore.scala#L285-L307) cause assertions rather than an automatic scheduling stall.

Distinct logical registers can still share a physical port. The [bank/row mapping](../../src/main/scala/atlas/common/MregParams.scala#L72-L78) places `m_i` and `m_{i+32}` in the same physical SRAM bank. [MREG assertions](../../src/main/scala/atlas/mreg/MregFile.scala#L211-L217) enforce at most one reader and one writer per bank per cycle; [the memory and return tags](../../src/main/scala/atlas/mreg/MregFile.scala#L253-L295) implement the synchronous read interface. The temporal monitor also rejects simultaneous reads and writes of the same physical row because this experiment has no same-cycle visibility contract.

All deterministic [LSU VMEM request pairs](../../src/main/scala/atlas/lsu/LSU.scala#L332-L354), including a read and a write, must use different VMEM banks when they overlap. The [VMEM arbitration tree](../../src/main/scala/diplomatic/memory/VMEM.scala#L267-L305) gives those LSU requests priority over DMA and TileLink traffic. A DMA request without its grant has not performed a memory access. These source references explain the obligations; only the selected functions described below have been checked by the new typed extractor.

## Typed CIRCT checks

[The extractor](../scripts/rtlgraph_lsu.py#L21-L123) consumes the existing `EE290SimConfig` S0 hardware IR through the typed C++ exporter. It records source locations, SSA cones, input hashes, command arguments, and the resulting typed artifact. The recorded [local evidence](../build/rtlgraph-lsu/local-2/lsu.json) passed 78 finite cutpoint assignments, five resettable control recurrences, and twelve direct `AtlasCore` wiring paths, with the expected module bindings checked:

| Checked fact | Scope |
| --- | --- |
| Load/store command predicates | Command valid and source-correlated opcode select the corresponding path. |
| Request-valid predicates | Each path issues reads exactly when its own state cutpoint equals `RUN`. |
| Busy predicates | Each path combines its non-idle state and its two pending stages; the other path and VPU are absent from these cones. |
| Five control registers | One-bit `seq.firreg` recurrences share the module clock, synchronously reset to zero, and carry the checked request/response-valid expressions. |
| Output/reservation predicates | MREG and VMEM write-valid expressions and active logical reservation signals agree with the selected pending/busy cutpoints. |
| Wrapper wiring | Scalar commands, MREG/VMEM valid interfaces, and scalar LSU busy inputs follow direct identity connections. |

These are exact local function and register facts under the stated clock/reset semantics. They do not establish FSM reachability, counter progression, address/payload alignment, memory response latency, or universally safe instruction ages. In particular, independent busy/request logic does not permit a schedule to violate the shared-bank or logical-reservation obligations. No shorter LSU timing constant or new machine-profile override is introduced.

## Observed row monitor

The [100-signal capture map](../scripts/rtlgraph_lsu_vcd.py#L8-L47) includes the existing 56 VPU signals, LSU command and row interfaces, path busy signals, logical MREG masks, and DMA requests/grants. It samples settled values immediately before rising edges. The [monitor](../scripts/rtlgraph_lsu_trace.py#L40-L176) binds scalar instruction words to LSU commands, attributes reads and writes by independently counted row addresses, checks response validity against the previous observed request, and requires each write to follow its registered source response. It records issue-relative read, response, write, and first-not-busy ages after observing them. Tests deliberately move an otherwise consistent stream later to ensure the monitor does not merely assert the compiler's assumed latency table.

The same pass checks VPU/LSU logical issue hazards, LSU/VPU physical-bank collisions including register aliases, deterministic LSU VMEM bank conflicts, and granted DMA requests against concurrent LSU accesses. The existing VPU event monitor checks VPU command ownership and row streams. The replay completion checker binds the full straight-line instruction stream through `DBG0`, and full-tensor goldens establish numerical correctness independently of the timing model.

These checks cover the recorded executions. They do not establish timing for every state, memory environment, or interference pattern. LSU command addresses are measured rather than independently reconstructed from scalar registers; payloads, other engines' physical ports, and TileLink arbitration are not fully captured. The cached simulator's source-to-binary build linkage remains unverified.

The controlled [handwritten](../build/rtlgraph-memory/runs/original_1/lsu-events-1.json), [compute-only schedule](../build/rtlgraph-memory/runs/exact_1/lsu-events-1.json), and [memory-overlap schedule](../build/rtlgraph-memory/runs/overlap_1/lsu-events-1.json) all passed the monitor. A second host layout with 83 instruction words passed for both the [handwritten](../build/rtlgraph-memory/runs/original_83_1/lsu-events-1.json) and [overlap](../build/rtlgraph-memory/runs/overlap_83_1/lsu-events-1.json) programs. Each replay contains six `VLOAD`s, six `VSTORE`s, sixteen VPU commands, and 1,536 passing golden-word comparisons. All 60 observed LSU transfers agree with the inherited model:

| Event relative to scalar issue | `VLOAD` | `VSTORE` |
| --- | --- | --- |
| Source row requests | VMEM ages 1–32 | MREG ages 1–32 |
| Source responses | Ages 2–33 | Ages 2–33 |
| Destination row writes | MREG ages 3–34 | VMEM ages 3–34 |
| First edge with busy deasserted | Age 35 | Age 35 |

The handwritten and compute-only schedules have no observed LSU/VPU overlap. The memory-overlap schedule has 277 edges with an outstanding LSU transaction and a VPU row event, including 255 edges where both engines actually access MREG, in each host layout. The observed logical reservations, physical ports, and deterministic VMEM requests remain conflict-free. These executions have no captured DMA request denied its grant. The timing agreement corroborates the existing profile for these cases; the typed local checks above do not turn it into a universal timing proof.

## Reproduction

Use a fresh output directory for each extractor or replay. In this worktree the preserved S0 manifest is in the original hardware checkout; a new elaboration can instead supply its own recorded `EE290SimConfig` manifest.

```sh
python3 -B scripts/rtlgraph_lsu.py \
  --s0-manifest /tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-lsu/local-new

python3 -B scripts/tests/test_rtlgraph_lsu.py \
  build/rtlgraph-s0/query/rtlgraph_export
python3 -B scripts/tests/test_rtlgraph_lsu_trace.py
```

Add `--capture-lsu` to the [performance replay driver](../scripts/rtlgraph_perf.py), retaining its ordinary golden fixture and completion checks. Then monitor the successful replay:

```sh
python3 -B scripts/rtlgraph_lsu_trace.py \
  --manifest build/rtlgraph-memory/runs/overlap_1/manifest.json \
  --output build/rtlgraph-memory/runs/overlap_1/lsu-events.json
```

The typed fixture suite contains five checks, including rejected opcode, reset, register-shape, and recurrence mutations. The temporal suite contains fifteen checks covering scalar binding, corrupt or truncated row/response streams, premature release, VPU/LSU logical hazards, bank aliases, same-row visibility rejection, independent overlap, and request-versus-grant semantics.
