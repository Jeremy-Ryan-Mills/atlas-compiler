# Conditional LSU timing from CIRCT

The [timing query](../scripts/rtlgraph_lsu_timing.py#L138-L161) derives the `EE290SimConfig` LSU request stream from typed state/counter transitions. The response and routing queries connect that stream to memory accesses under explicit physical-bank exclusions. An optional compiler profile now consumes the derived vector access and path timing; scalar LSU and VPU timing remain inherited.

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

The transfer begins with idle state and empty LSU and memory-response stages; reset remains low and no second command is issued on the same path during the interval. Addresses must be aligned, in range, nonwrapping, and free of physical conflicts. The timing and response queries alone do not prove payload routing or write acceptance; the routing query below checks those paths. Scalar decoding, issue legality, same-row read/write visibility, and whole-kernel safety remain outside this proof. Existing wrapper identity checks bind the LSU command interface to the scalar LSU-command output; previous traces provide finite instruction-to-command correspondence.

## Row routing and write acceptance

The [routing query](../scripts/rtlgraph_lsu_routing.py#L124-L234) checks command operand capture, physical row/address selection, response payload selection, LSU data registers, and destination SRAM write ports. Symbolic bit identities cover every payload value through the selected paths. The proof assumes the storage semantics of the checked `seq.firmem` operations. Fresh evidence is retained in `build/rtlgraph-lsu-routing/local-1/lsu-routing.json`.

For each VMEM bank it checks all 16 allowed lower-priority request combinations during a vector read and all 64 during a vector write, including DMA/TileLink interference. A vector write must enable the full 32-byte mask. MREG accesses require the LSU to be the sole reader or writer of the corresponding physical bank. The derived one-hot request/response selection connects the selected bank's payload to the row pipeline. These conditional facts do not prove that an arbitrary kernel satisfies the exclusions.

## Compiler consumption

[`rtlgraph_lsu_profile.py`](../scripts/rtlgraph_lsu_profile.py#L15-L103) reruns the typed analysis and emits `atlas-lsu-profile-v1` with canonical evidence. `atlas-opt --rtl-lsu-profile FILE` uses its ages for `VLOAD`/`VSTORE` row accesses, VMEM bank holds, and load/store path occupancy. Profiles compose only when they identify the same hardware IR. The derived profile and its evidence are in `build/rtlgraph-lsu-profile/profile-1`.

The current projection has read age 1, write age 3, 32 rows with unit stride, and first-free age 35. Thus the path hold ends at **34 inclusive**, not 35. Logical MREG reservation policy and same-cycle visibility remain inherited; reservation release follows the selected path lifetime. Unknown VMEM addresses retain conservative all-bank holds and require legal aligned runtime addresses.

The [Merlin-facing assembly handoff](rtlgraph-contract.md#merlin-comparison) accepts `--lsu-profile` and preserves these facts, their assumptions, and operand-resolved native footprints. An emitter supplies operations and buffer assignments; `atlas-opt` consumes the timing and checks the resulting schedule. A scalar completion latency would lose row-stream overlap and bank/path occupancy.

The sensitivity test adds a one-cycle busy tail to a typed-IR fixture: analysis derives first-free age 36 instead of 35, the native hold extends by one cycle, and the second independent `VLOAD` moves from cycle 35 to 36. The checker rejects the old spacing under the changed profile. `build/rtlgraph-lsu-profile/sensitivity-2/results.json` binds the analysis, compiler, profiles, and schedules. This is a controlled typed-IR test, not a modified-RTL build or performance result.

Fresh RTL replays with DMA, MXU0, MXU1, and LSU profiles passed all 1,536 unary and 1,024 MXU0 K64 output words. Completion remained 9,083 and 11,941 edges respectively, compared with handwritten baselines of 10,181 and 12,189. Both emitted assemblies match the previously optimized candidates byte-for-byte; selecting the LSU profile preserves those improvements without adding another speedup. `build/rtlgraph-lsu-profile/validation-1.json` binds the evidence, sensitivity test, four-profile handoff, compiler checks, and replay comparisons. The cached simulator's source-to-binary linkage remains unverified.

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

python3 -B scripts/rtlgraph_lsu_routing.py \
  --lsu-evidence build/rtlgraph-lsu/local-2/lsu.json \
  --output build/rtlgraph-lsu-routing/local-NEW

python3 -B scripts/rtlgraph_lsu_profile.py \
  --lsu-evidence build/rtlgraph-lsu-routing/local-NEW/lsu-routing.json \
  --output build/rtlgraph-lsu-profile/profile-NEW

python3 -B scripts/tests/test_rtlgraph_lsu_routing.py \
  build/rtlgraph-lsu-routing/local-NEW/typed.json

python3 -B scripts/tests/test_rtlgraph_lsu_profile_integration.py \
  build/rtlgraph-lsu-routing/local-NEW/typed.json \
  build/rtlgraph-lsu-profile/compiler-1/atlas-opt \
  --output build/rtlgraph-lsu-profile/sensitivity-NEW
```

The 19 timing tests reject changed counters, reset/clock behavior, row capture, progression, and pending stages. The 16 response tests cover changed memory latency, bank decode, arbitration, mode/enables, response routing, clock wiring, unsupported attributes, and incomplete input domains. The 16 routing tests cover operand capture, address/payload wiring, write masks, ports, and the controlled busy tail. Positive tests compare derived ages with the existing profile; those comparisons are outside the extractor.
