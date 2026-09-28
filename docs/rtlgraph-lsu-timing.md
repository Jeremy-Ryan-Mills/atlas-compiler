# Conditional LSU timing from CIRCT

The [timing query](../scripts/rtlgraph_lsu_timing.py#L138-L161) derives the `EE290SimConfig` LSU request stream from typed state/counter transitions. With a one-cycle source-memory response contract, it also derives the existing write and release ages. The response query below establishes that contract under explicit physical-bank exclusions. Neither introduces a compiler timing override or a new VPU latency claim.

The evidence report (`build/rtlgraph-mixed-dma/timing/local-1/lsu-timing.json`) retains the hardware IR hash, typed artifact, source locations, checked cones, exact assumptions, and bounded trajectory. Its input is the prior LSU local evidence (`build/rtlgraph-lsu/local-2/lsu.json`), which is freshly rechecked rather than accepted by its status string. Existing extractors and historical evidence remain unchanged.

## What is established internally

The query checks every combination of each path's two-bit state, six-bit counter, and command bit: **512 combinations per path, 1,024 total**. It includes invalid/unreachable state encodings in these local transition checks. The resulting transition rules are:

- `IDLE` with a command enters `RUN` and clears the counter.
- `RUN` issues one source-row request each edge and increments the counter. Counter 31 enters `DRAIN`.
- `DRAIN` enters `IDLE` on the following edge.
- These request and next-state cones have no response-valid or backpressure input.

The [row-pipeline checks](../scripts/rtlgraph_lsu_timing.py#L164-L201) independently verify the counter's low-five-bit row selection, zero-extended base-plus-counter line arithmetic, row/address capture enables, and held values. They compose with the earlier checked pending-register recurrences and busy predicates. The source counterpart is the original hardware checkout's [LSU vector path](/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/src/main/scala/atlas/lsu/LSU.scala#L163-L330); the query evaluates CIRCT operations and register connections, not source text or a compiler timing table.

Thus an admitted command into an idle, empty path produces **32 source-row requests at ages 1–32**, independently of whether an external source responds. A request is an asserted interface request; actual SRAM access and collision freedom remain separate obligations.

## Conditional destination and release timing

The [bounded composition](../scripts/rtlgraph_lsu_timing.py#L215-L292) evaluates the extracted register-input cones over 37 sampled edges. Age zero is the LSU command-valid edge. All registers are evaluated from the same pre-edge state and updated together, matching the checked synchronous registers.

The temporal query assumes each asserted source-row request receives one valid response at the **next sampled edge**, without omissions, extra responses, or reordering. The separate response query below establishes that contract under explicit arbitration exclusions.

| Event relative to LSU command | `VLOAD` | `VSTORE` | Evidence scope |
| --- | --- | --- | --- |
| Source-row requests | VMEM ages 1–32 | MREG ages 1–32 | Derived internal recurrence |
| Source responses | Ages 2–33 | Ages 2–33 | Memory contract checked below under exclusions |
| Destination-row writes | MREG ages 3–34 | VMEM ages 3–34 | Derived under that contract |
| First edge with busy deasserted after activity | Age 35 | Age 35 | Derived under that contract |

The write/release ages are outputs of the composition, not constants asserted by the extractor. It repeats the bounded composition for all 64 possible stale idle-counter values; the accepted command clears them and produces the same event sequence. It also verifies that the request and destination streams contain each row 0–31 exactly once, in order. Busy is zero immediately before the issue edge; this does not eliminate the command's launch reservation.

The load/store control trajectories are composed independently. The report's simultaneous command inputs are a way to evaluate both paths together, not a claim that the scalar frontend can issue two commands in one cycle or that their physical addresses may alias.

## Source-response contract from memory logic

The [response query](../scripts/rtlgraph_lsu_response.py#L93-L158) checks all **32 MREG banks and six VMEM banks**, including LSU request-bank decode, arbitration, memory enables, registered valid/client selection, and the complete response-valid OR trees. It checks **24,224 local assignments**, with each domain required to cover the full typed input width. Both memories declare one-cycle reads in `seq.firmem`; wrapper checks establish a common clock/reset and direct request/response wiring.

- For `VSTORE`, each MREG request returns valid at the next edge when LSU is the **sole reader of that physical bank**. Registers `m_i` and `m_{i+32}` alias the same bank. Fixed priority does not make simultaneous reads legal.
- For `VLOAD`, each in-range VMEM vector request returns valid at the next edge if no LSU scalar write, vector write, or scalar read targets that bank. DMA and TileLink requests are lower priority. VMEM banks 6–7 decode to no request and are excluded.

These laws discharge the previous response-valid timing assumption under the stated exclusions. They do not establish response payloads, row/address routing, or same-row read/write visibility. The query freshly exports all four modules from the same hashed hardware IR, reruns the LSU checks, and retains evidence in `build/rtlgraph-lsu-response/local-3/lsu-response.json`. Source counterparts are [MREG arbitration and responses](/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/src/main/scala/atlas/mreg/MregFile.scala#L203-L307) and [VMEM access and response routing](/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/src/main/scala/diplomatic/memory/VMEM.scala#L249-L351).

## Scope and remaining work

The result strengthens the previous [local LSU checks and finite waveform observations](rtlgraph-lsu.md#typed-circt-checks). It supports the existing numerical profile under explicit assumptions. It is a deterministic bounded evaluation of extracted recurrences, **not a `circt-bmc` run or an unbounded whole-RTL proof**.

The transfer begins with idle state and empty LSU and memory-response stages; reset remains low and no second command is issued on the same path during the interval. Addresses must be aligned, in range, nonwrapping, and free of physical conflicts. Neither query proves payload correctness, complete row/address routing, scalar decoding or issue legality, or whole-kernel scheduling safety. Existing wrapper identity checks bind the LSU command interface to the scalar LSU-command output; previous traces provide finite instruction-to-command correspondence.

The next evidence step is selected row/payload routing and destination-write acceptance, followed by checking the physical exclusion conditions over complete schedules. This remains conditional timing evidence, not a universal fixed-latency proof.

## Reproduce

From the compiler repository, with the pinned existing artifacts:

```bash
python3 -B scripts/rtlgraph_lsu_timing.py \
  --lsu-evidence build/rtlgraph-lsu/local-2/lsu.json \
  --output build/rtlgraph-mixed-dma/timing/local-NEW

python3 -B scripts/tests/test_rtlgraph_lsu_timing.py \
  build/rtlgraph-lsu/local-2/typed.json

python3 -B scripts/rtlgraph_lsu_response.py \
  --lsu-evidence build/rtlgraph-lsu/local-2/lsu.json \
  --output build/rtlgraph-lsu-response/local-NEW

python3 -B scripts/tests/test_rtlgraph_lsu_response.py \
  build/rtlgraph-lsu-response/local-NEW/typed.json
```

The 19 timing tests reject changed counters, reset/clock behavior, row capture, progression, and pending stages. The 16 response tests cover changed memory latency, bank decode, arbitration, mode/enables, response routing, clock wiring, unsupported attributes, and incomplete input domains. Positive tests compare derived ages with the existing profile; those comparisons are outside the extractor.
