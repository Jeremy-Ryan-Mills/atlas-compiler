# Native RTL DMA scheduling

The existing `atlas-opt` profile integration now accepts `--rtl-dma-profile FILE`. This extends the same `MachineModel`, dependency graph, reservation table, list scheduler, and checker already used by the partial MXU profiles. The previous [unary overlap experiment](rtlgraph-dma-overlap.md) remains a historical reference; its handwritten transfer-group rewrite is no longer needed to generate new schedules.

## Hardware facts and compiler rules

The [exporter](../scripts/rtlgraph_dma_profile.py#L51-L94) rechecks the typed CIRCT analysis and artifact identities from the [DMA evidence](rtlgraph-dma-hardware.md), then emits a canonical evidence report and strict `atlas-dma-profile-v1` projection. The [C++ loader](../src/core/machine_profile.cpp#L73-L124) checks fields, configuration, supported geometry, and compatibility with any selected MXU profile's hardware-IR hash. Hash fields identify evidence; the loader itself does not rerun hardware analysis.

The [DMA footprints](../src/core/machine.cpp#L366-L406) and [completion edges](../src/core/depgraph.cpp#L55-L109) change DMA semantics; numerical LSU/VPU timing remains inherited:

- `DMA.LOAD` and `DMA.STORE` capture scalar operands and configured base state at issue. Later scalar overwrites need not wait for transfer completion.
- `DMA.CONFIG` updates scalar base state; it does not consume a transfer slot or channel.
- VMEM word pointers map to line indices with `(pointer >> 3) & 0xffff`. Known operands must select aligned, in-range buffers and positive multiples of 32 bytes, at most 4,096 bytes. That maximum is an imposed supported subset, not a deduction from the 13-bit command field.
- A transfer reserves its memory range until its existing matching `DMA.WAIT`. Reads may overlap reads; any potentially conflicting write is guarded by completion. All external DRAM ranges conservatively alias because configured DRAM bases are not yet tracked precisely.
- Fixed-latency producers must finish their conflicting buffer accesses before DMA launch. Independent LSU/VPU work may overlap DMA because the checked VMEM arbitration gives LSU priority and backpressures DMA.
- Channel reuse and each particular slot in the eight-entry ring require the earlier command's wait. Transfer launch order is preserved, keeping ring assignment predictable.

No wait is inserted. A long `DELAY`, or the cost model's estimated transfer duration, cannot discharge a completion obligation. Every launched transfer must have its explicit wait within the same basic block; pending DMA across block boundaries and DMA in branch delay slots are rejected. Unknown pointers/sizes use conservative aliasing, but runtime operand legality remains an input obligation.

[Scheduling admission](../src/passes/registry.cpp#L95-L119) requires robust DMA mode. The [checker](../src/core/simulator.cpp#L98-L242) keeps transfer lifetimes until explicit waits and checks fixed operations on a minimum dispatch clock that excludes wait stalls. Reservations widen across each wait to cover delayed port uses that could collide after an intermediate stall duration. This checks safety under the inherited local model without claiming a universal RTL proof or DMA latency bound. Reported compiler cycle counts remain cost estimates.

## General assembly adapter

[`rtlgraph_dma_compile.py`](../scripts/rtlgraph_dma_compile.py#L185-L253) translates supported straight-line scalar, DMA, LSU, and VPU assembly into native compiler syntax. Both `native_critical` and `native_input` use ordinary compiler scheduling priorities over the full instruction stream, including every DMA transfer and wait. No unary addresses, register assignments, chain names, transfer-group ordering, or hand-chosen overlap ages are supplied.

By default, CSR instructions retain their barrier semantics. The explicit `--relocate-perf-markers` option checks that the two cycle reads, subtraction, and `DBG1` write form an isolated measurement expression with private scalar registers. It moves only those four measurement instructions so the compiler can schedule across the former timed-window boundary. The counter then measures different work and is diagnostic only. Performance comparisons use first Atlas issue through successful post-DMA `DBG0` instead.

The adapter treats `DBG0` accesses as `atlas.release` publication barriers and preserves existing release annotations through translation.

Every candidate must preserve the complete encoded non-idle instruction multiset and pass native checking again after counter reinsertion. That final check matters: adding scalar instructions can introduce a physical-port collision at a different issue distance. Independent RTL replay still checks actual transfer descriptors, events, and all golden outputs.

## Measured native schedules

The [controlled unary comparison](../build/rtlgraph-dma-native/comparison-128-2.json) uses the same 128-word host, fixture, runtime, and first issue edge as the earlier handwritten and memory-overlap references. The native adapter starts directly from the original handwritten assembly. All five DMA transfers and explicit waits, scalar operands, and non-idle encoded words are preserved.

| Schedule | First Atlas issue → completed-output `DBG0` | Improvement over handwritten |
| --- | ---: | ---: |
| Handwritten `perf_unary` | 10,181 edges | — |
| Previous LSU/VPU overlap | 10,030 edges | 1.48% |
| Native compiler, critical priority | **9,083 edges** | **10.78%** |
| Native compiler, input priority | **9,268 edges** | **8.97%** |

Critical priority saves 1,098 edges against handwritten and 947 against the prior memory-overlap schedule. The two native replays each pass all 1,536 golden words and the independent DMA/LSU/VPU event monitor. Both observe 858 DMA-busy edges with VPU physical-row activity and 384 with LSU VMEM activity. The critical candidate observes 128 denied DMA-read edges and 86 denied DMA-write edges; denied requests remain pending until a grant. These are finite measurements under one host/environment condition, not universal latency bounds or a promise that one scheduling priority always wins.

The compiler keeps the original A→B→C output-transfer order. It completes A's computation and stores while input-B DMA remains pending, then launches A's output DMA while B/C work runs. No extra priority heuristic or unary-specific scheduling transformation was added. Unlike the older manual candidate, the native schedule discovers this overlap through operand ranges, explicit completion dependencies, and resource reservations.

As a separate functional case, the same adapter and profile schedule `perf_vec_layernorm_32x32`: [RTL replay](../build/rtlgraph-dma-native/runs/layernorm_critical_1/manifest.json) passes 512 golden words, and its [independent event check](../build/rtlgraph-dma-native/runs/layernorm_critical_1/dma-events-1.json) validates two DMA, four LSU, and ten VPU commands. No controlled layer-normalization speedup comparison is claimed. Both native priorities also pass compiler checks with unary's original counter barriers retained.

`perf_vec_rmsnorm_softmax` is conservatively rejected: two output stores launch before their waits, and the current DRAM alias model cannot establish disjoint destinations. This is a limitation of the admitted model, not evidence that the handwritten kernel is wrong. More precise configured-base tracking and DRAM ranges are the next way to broaden this coverage; no waits were inserted to force acceptance.

Seven C++ test targets and 73 focused DMA Python tests pass. The 39 new simulator/admission checks include a physical-port collision exposed by an intermediate DMA wait, channel and ring reuse, missing waits after long delays, and block-boundary restrictions. Final adapter review added preservation of `DELAY # keep` and rejection of relocated `atlas.release` markers. Regenerating all six compiler candidates after those fixes produced identical assembly bytes; the replayed programs were unchanged. The [evidence index](../build/rtlgraph-dma-native/results-1.json) binds the final profiles, compiler, regenerated schedules, replay reports, and remaining limits.

## Reproduce

Run from the compiler repository using the existing environment described in the [replay guide](rtlgraph-mxu1.md#replay). Use fresh output paths; no hardware elaboration or simulator rebuild is required.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

cmake -S . -B build/rtlgraph-dma-native/compiler
cmake --build build/rtlgraph-dma-native/compiler -j 4
ctest --test-dir build/rtlgraph-dma-native/compiler --output-on-failure

python3 -B scripts/rtlgraph_dma_profile.py \
  --dma-evidence build/rtlgraph-dma/local-1/dma.json \
  --output build/rtlgraph-dma-native/profile

python3 -B scripts/rtlgraph_dma_compile.py \
  --source build/rtlgraph-corpus/originals-1/perf_unary.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-dma-native/compiler/atlas-opt \
  --profile build/rtlgraph-dma-native/profile/atlas-dma.profile \
  --relocate-perf-markers --output build/rtlgraph-dma-native/candidates

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-dma-native/candidates/native_critical.S \
  --golden-json "$atlas_baremetal/generators/perf_unary.json" \
  --control-manifest build/rtlgraph-memory/control-template-4/manifest.json \
  --capture-dma --run --output build/rtlgraph-dma-native/run
```

The richer canonical machine schema and a Merlin adapter remain future work. These changes extend Atlas compiler integration; they do not add a Merlin dependency or restore automatic DMA-wait insertion.
