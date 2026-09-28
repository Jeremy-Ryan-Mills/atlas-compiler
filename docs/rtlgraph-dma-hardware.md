# DMA facts for explicit-wait overlap

This experiment uses the pinned `EE290SimConfig` hardware IR to establish selected DMA control facts needed for overlapping transfers with independent LSU/VPU work. It keeps explicit `DMA.WAIT` instructions. The removed automatic wait-insertion feature is neither required nor restored. DMA completion remains an observed event; no fixed completion bound is inferred.

## Instruction and address semantics

The [scalar command generation](../../src/main/scala/atlas/scalar/ScalarCore.scala#L489-L509) distinguishes three operations:

- `DMA.CONFIG` writes the scalar `dmaBaseReg` when the instruction fires. It does not enqueue a DMA command. The base supplies the upper 32 bits of later DRAM addresses.
- `DMA.LOAD` and `DMA.STORE` present a command when the scalar instruction fires. The [DMA command slots](../../src/main/scala/diplomatic/memory/DMA.scala#L216-L229) capture direction, channel, VMEM line address, DRAM byte address, and byte count at that edge. Subsequent scalar register writes do not change those saved fields.
- `DMA.WAIT` [stalls on its selected channel](../../src/main/scala/atlas/scalar/ScalarCore.scala#L224-L249). The [wrapper](../../src/main/scala/diplomatic/top/AtlasCore.scala#L185-L191) includes a launch-pending bit in the scalar busy input so a just-issued command remains visible before the engine busy register propagates.

The [wrapper address conversion](../../src/main/scala/diplomatic/top/AtlasCore.scala#L170-L183) uses word-addressed VMEM operands. In this configuration the typed operation is exactly `comb.extract` with low bit 3 and width 16:

```text
DMA VMEM line = (scalar_word_address >> 3) & 0xffff
DMA byte count = scalar_size & 0x1fff
```

The low three word-address bits select within a 32-byte line and are discarded. The experiment requires aligned addresses and byte counts divisible by 32; this truncation is not permission to accept misaligned or oversized transfers. The six physical banks each hold 8,192 lines of 256 bits, for 49,152 usable lines. Encoded bank indices 6 and 7 are outside that capacity. The [bank mapping](../../src/main/scala/atlas/common/VmemParams.scala#L36-L69) uses the upper three line-address bits for the bank and the lower thirteen for its row.

For the original `perf_unary` scalar register values, the relevant line starts are:

| Buffer | Scalar VMEM word address | First line | Lines in each DMA |
| --- | --- | --- | --- |
| Input/output A | `0x20000000` | 0 | 64 |
| Input B | `0x20000400` | 128 | 64 |
| Output B | `0x20001000` | 512 | 64 |
| Output C | `0x20001800` | 768 | 64 |

All four buffers occupy bank 0 with disjoint ranges. The output-C address accounts for the encoded 12-bit signed `ADDI` immediate; interpreting its printed `2048` as a positive immediate would incorrectly produce `0x20002800`.

## Completion and shared-memory obligations

The engine has eight channels and eight ring slots. The command interface is `Valid`, with no ready handshake. The [enqueue logic](../../src/main/scala/diplomatic/memory/DMA.scala#L223-L229) has no slot-free or duplicate-channel admission gate. A schedule must keep the next ring slot free and must not reuse a channel before its prior command completes. Limiting the total outstanding count alone does not prove that a particular ring slot is free when completions can arrive out of order.

The [TileLink request path](../../src/main/scala/diplomatic/memory/DMA.scala#L237-L276) dispatches beats from saved commands and captures per-source-ID metadata. The [response and retirement logic](../../src/main/scala/diplomatic/memory/DMA.scala#L283-L325) associates returning responses with their originating slots. A slot retires only after all its requests are dispatched and its outstanding-beat count reaches zero, accounting for simultaneous request and response acceptance. That event clears the saved command's channel busy flag. The typed checks below cover the local zero and active-slot functions; they do not prove the full metadata/counter/busy composition.

The [VMEM priority tree](../../src/main/scala/diplomatic/memory/VMEM.scala#L267-L305) selects, per bank:

```text
LSU scalar write > LSU vector write > LSU scalar read > LSU vector read
  > DMA write > DMA read > TileLink write > TileLink read
```

Consequently, same-bank DMA traffic cannot displace an LSU request at these arbitration cutpoints. The [DMA store read sequencer](../../src/main/scala/diplomatic/memory/DMA.scala#L190-L210) advances only on a granted VMEM read. A load response [waits for its VMEM write grant](../../src/main/scala/diplomatic/memory/DMA.scala#L283-L297); a store acknowledgement has no VMEM write side effect. A denied request is not a memory access.

This priority makes overlap possible even though the unary buffers share a bank. It does not make overlapping accesses to the same data safe. A load's wait must precede consumers of that input; a store may launch only after its full output range is ready; buffers and channels remain protected until completion. Independent LSU requests must also obey their own bank and MREG constraints. Arbitration can lengthen DMA completion, so software cannot replace the completion event with a guessed delay.

## Typed evidence and limits

The [extractor](../scripts/rtlgraph_dma.py#L97-L270) consumes typed SSA from `ScalarCore`, `DmaEngine`, `AtlasCore`, and `Vmem`. The recorded report (`build/rtlgraph-dma/local-1/dma.json`) includes input hashes, source locations, command arguments, exact checked cones, register structures, and the exported typed artifact.

| Check | Exhaustive cutpoint assignments | Additional structure |
| --- | ---: | --- |
| Scalar launch/config/wait | 64 | Selected-channel array and scalar base-register update |
| Eight slot zero/active/capture functions | 18,048 | 40 saved command-field recurrences |
| Six per-bank priority trees | 6,144 | Bank-bit selection and registered read-client/valid outputs |
| Scalar channel-busy wrapper | 32 | Direct command/grant connections and address slices |
| Load-response backpressure | 8 | Response tag selects one of 64 saved direction bits |
| **Total** | **24,296** | Six `8192 × 256` VMEM memory objects |

These finite checks establish exact local functions for all assignments to their stated cutpoints, including unreachable assignments. They do not establish a full DMA state invariant or prove that every cutpoint is produced correctly. In particular, the extractor does not prove raw instruction decoding, complete per-channel busy recurrence, source-ID uniqueness, beat-counter progression, store queue safety, TileLink error handling, VMEM address decode, row/payload selection, memory visibility, response routing, or environmental fairness. The per-bank arbitration proof starts at verified bit slices of one-hot request cutpoints. The channel launch-pending latch is included in the busy function, but its complete recurrence is not checked here.

The [typed fixture tests](../scripts/tests/test_rtlgraph_dma.py#L22-L85) include ten checks. Mutations reject an incorrect final-response count, reset, capture enable, live rather than saved command address, early release, DMA priority over LSU, bank selection, and read-client identity. Successful RTL replays and independent transaction monitors provide separate execution evidence; neither these tests nor one successful replay establish universal timing safety. The cached simulator's source-to-binary build linkage remains unverified.

## Difference from the inherited compiler model

The inherited, default [`machine.cpp` DMA profiles](../src/core/machine.cpp#L366-L389) describe scalar registers and DMA base accesses at completion. `DMA.CONFIG` has a queued completion-time base update and a modeled latency. The [VMEM footprint helper](../src/core/machine.cpp#L90-L108) interprets its input as a local byte address; the DMA caller supplies the raw scalar pointer, without the hardware's word-address mask and conversion.

Those assumptions follow the inherited software-model interface, which references the optional [`npu_model` `rtl-match` checkout](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match). They differ from the measured target's issue-captured command fields, scalar-only `DMA.CONFIG`, and word-addressed VMEM operands. This hardware analysis does not silently change that compiler model or use its DMA cycle estimates to certify overlap. The narrow DMA adapter must reconstruct actual scalar values and captured descriptors, retain explicit completion dependencies, and validate the transformed kernel against RTL and full output goldens. The subsequent [native compiler integration](rtlgraph-dma-native.md) reconciles these semantics through an explicit optional profile. Its [version-2 address extension](rtlgraph-dram-ranges.md) adds configured DRAM ranges and verifies the 37-bit TileLink projection. The original evidence and narrow experiment retain their recorded identities.

## Reproduction

Use a new output directory. The preserved S0 manifest is in the original hardware checkout; the exporter and new artifacts are in this worktree.

```sh
python3 -B scripts/rtlgraph_dma.py \
  --s0-manifest /tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-dma/local-new

python3 -B scripts/tests/test_rtlgraph_dma.py \
  build/rtlgraph-s0/query/rtlgraph_export
```
