# Conditional LSU timing from CIRCT

The [new timing query](../scripts/rtlgraph_lsu_timing.py#L130-L153) derives the `EE290SimConfig` LSU request stream from typed state/counter transitions. With an explicitly assumed one-cycle source-memory response contract, it also derives the existing write and release ages. It introduces no compiler timing override and makes no new VPU latency claim.

The evidence report (`build/rtlgraph-mixed-dma/timing/local-1/lsu-timing.json`) retains the hardware IR hash, typed artifact, source locations, checked cones, exact assumptions, and bounded trajectory. Its input is the prior LSU local evidence (`build/rtlgraph-lsu/local-2/lsu.json`), which is freshly rechecked rather than accepted by its status string. Existing extractors and historical evidence remain unchanged.

## What is established internally

The query checks every combination of each path's two-bit state, six-bit counter, and command bit: **512 combinations per path, 1,024 total**. It includes invalid/unreachable state encodings in these local transition checks. The resulting transition rules are:

- `IDLE` with a command enters `RUN` and clears the counter.
- `RUN` issues one source-row request each edge and increments the counter. Counter 31 enters `DRAIN`.
- `DRAIN` enters `IDLE` on the following edge.
- These request and next-state cones have no response-valid or backpressure input.

The [row-pipeline checks](../scripts/rtlgraph_lsu_timing.py#L156-L193) independently verify the counter's low-five-bit row selection, zero-extended base-plus-counter line arithmetic, row/address capture enables, and held values. They compose with the earlier checked pending-register recurrences and busy predicates. The source counterpart is the original hardware checkout's [LSU vector path](/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/src/main/scala/atlas/lsu/LSU.scala#L163-L330); the new query evaluates CIRCT operations and register connections, not source text or a compiler timing table.

Thus an admitted command into an idle, empty path produces **32 source-row requests at ages 1–32**, independently of whether an external source responds. A request is an asserted interface request; actual SRAM access and collision freedom remain separate obligations.

## Conditional destination and release timing

The [bounded composition](../scripts/rtlgraph_lsu_timing.py#L207-L284) evaluates the extracted register-input cones over 37 sampled edges. Age zero is the LSU command-valid edge. All registers are evaluated from the same pre-edge state and updated together, matching the checked synchronous registers.

The external assumption is precise: each asserted source-row request receives one valid response at the **next sampled edge**, without omissions, extra responses, or reordering. The query supplies that contract at the VMEM and MREG response interfaces; it does **not** prove that the memories satisfy it.

| Event relative to LSU command | `VLOAD` | `VSTORE` | Evidence scope |
| --- | --- | --- | --- |
| Source-row requests | VMEM ages 1–32 | MREG ages 1–32 | Derived internal recurrence |
| Source responses | Ages 2–33 | Ages 2–33 | Assumed one-cycle response contract |
| Destination-row writes | MREG ages 3–34 | VMEM ages 3–34 | Derived under that contract |
| First edge with busy deasserted after activity | Age 35 | Age 35 | Derived under that contract |

The write/release ages are outputs of the composition, not constants asserted by the extractor. It repeats the bounded composition for all 64 possible stale idle-counter values; the accepted command clears them and produces the same event sequence. It also verifies that the request and destination streams contain each row 0–31 exactly once, in order. Busy is zero immediately before the issue edge; this does not eliminate the command's launch reservation.

The load/store control trajectories are composed independently. The report's simultaneous command inputs are a way to evaluate both paths together, not a claim that the scalar frontend can issue two commands in one cycle or that their physical addresses may alias.

## Scope and remaining work

The result strengthens the previous [local LSU checks and finite waveform observations](rtlgraph-lsu.md#typed-circt-checks). It supports the existing numerical profile under explicit assumptions. It is a deterministic bounded evaluation of extracted recurrences, **not a `circt-bmc` run or an unbounded whole-RTL proof**.

The transfer begins with idle state and no pending response/write stages; reset remains low and no second command is issued on the same path during the interval. Addresses must be aligned, in range, nonwrapping, and free of physical conflicts. The query does not prove SRAM latency, grants, payload correctness, complete bank/address routing, scalar decoding or issue legality, or whole-kernel scheduling safety. Existing wrapper identity checks bind the LSU command interface to the scalar LSU-command output; previous traces provide finite instruction-to-command correspondence.

The next evidence step is to discharge the one-cycle response assumption through the actual MREG and VMEM arbitration/read-valid paths, retaining the physical conflict conditions. Until then, the conditional timing evidence must not be presented as a universal fixed-latency proof.

## Reproduce

From the compiler repository, with the pinned existing artifacts:

```bash
python3 -B scripts/rtlgraph_lsu_timing.py \
  --lsu-evidence build/rtlgraph-lsu/local-2/lsu.json \
  --output build/rtlgraph-mixed-dma/timing/local-NEW

python3 -B scripts/tests/test_rtlgraph_lsu_timing.py \
  build/rtlgraph-lsu/local-2/typed.json
```

The 19 focused tests reject altered counter limits/increments, missing counter reset, a stuck drain state, response-gated requests or progression, changed clock/reset semantics, incomplete lookup arrays, wrong row slices or capture enables, and broken pending-stage recurrences. Zero- and two-cycle response assumptions are explicitly rejected for this claim. The positive test compares the **derived** ages with the existing profile; that comparison is outside the extractor.
