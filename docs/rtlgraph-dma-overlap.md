# Unary DMA/compute overlap with explicit waits

This experiment extends the [completed LSU/VPU overlap](rtlgraph-memory-overlap.md) across the original DMA boundaries in `perf_unary`. It preserves every encoded non-idle instruction, both input transfers, all three output transfers, every `DMA.WAIT`, and the existing full-output golden fixture. It uses `chipyard.EE290SimConfig` and the existing simulator selected by the baremetal Makefile/README. The original hardware and handwritten assembly remain unchanged.

The subsequent [native DMA integration](rtlgraph-dma-native.md) replaces this experiment’s unary-specific schedule transformation with compiler dependency and resource rules. The measurements below retain their original adapter and evidence identities.

## Measured completion

The 128-word controlled comparison (`build/rtlgraph-dma/comparison-128-1.json`) and 83-word comparison (`build/rtlgraph-dma/comparison-83-1.json`) each hold the host ELF outside its Atlas program array, runtime, fixture, seed, and first Atlas issue edge fixed. The smaller buffer changes host layout and launch conditions relative to the larger control; compare schedules within each column. Each replay passes all 1,536 output words and the independent DMA/LSU/VPU monitor.

| Schedule | 128-word host: first issue → `DBG0` | 83-word host: first issue → `DBG0` |
| --- | ---: | ---: |
| Handwritten `perf_unary` | 10,181 edges | 9,975 edges |
| Previous LSU/VPU overlap | 10,030 edges | 9,807 edges |
| Output DMA overlap | **9,583 edges** | 9,549 edges |
| Input and output DMA overlap | **9,584 edges** | **9,381 edges** |

In the 128-word control, output-only overlap saves **598 edges (5.87%)** against the handwritten kernel and **447 edges (4.46%)** against the previous memory-overlap winner. The combined candidate saves **597 edges (5.86%)** and **446 edges (4.45%)**, respectively. The one-edge difference does not establish a meaningful benefit from moving the input wait in this condition. All four begin at global edge 57,931. The combined candidate observes 193 DMA-busy edges with VPU physical row activity and 192 with LSU VMEM activity. DMA reads are denied a grant for 32 edges and writes for 48 edges; these counts describe waiting edges, not distinct transactions. The monitor counts only granted requests as accesses. All five transfers complete and each existing wait matches its completed command. Output-only overlap has 127 DMA-busy edges with VPU row activity, 128 with LSU VMEM activity, and 32 edges of denied DMA reads; it has no denied DMA writes.

The same second input transfer completes at edge 62,603 in all four runs, despite the candidate's concurrent A work and grant denials. The combined candidate launches C's output transfer at edge 63,130 while independent local work remains; its final output DMA completes at 67,513, followed by `DBG0` two edges later. Individual DMA durations change with memory-system timing and output order. The total saving is a measured effect of this schedule, not a sum of universally fixed DMA-latency reductions.

The output-only trace (`build/rtlgraph-dma/runs/output_1/dma-events-comparison.json`) places C's final `VSTORE` write at edge 63,201 and DMA launch at 63,202, the first idle LSU edge. The combined trace (`build/rtlgraph-dma/runs/input_output_1/dma-events-comparison.json`) similarly observes final write at 63,129 and launch at 63,130. Both therefore start the transfer strictly after its complete source data is written.

In both output-overlap variants, C's 64 DMA source-line reads occur at ages 1–62, then 95 and 96. The 32-cycle gap is denied DMA service while deterministic LSU traffic occupies bank zero. The monitor checks that the remaining line identities resume correctly and all 64 acknowledgements complete. This is a concrete execution witness for the [priority/backpressure contract](rtlgraph-dma-hardware.md#completion-and-shared-memory-obligations), not a fixed completion-latency rule.

In the 83-word control, all four schedules first issue at edge 56,097. Combined input/output overlap saves **594 edges (5.95%)** against handwritten and **426 edges (4.34%)** against the previous memory-overlap schedule. Output-only overlap saves **426 edges (4.27%)** and **258 edges (2.63%)**, respectively. The combined candidate has the same 193 DMA/VPU and 192 DMA/LSU overlap edges as in the larger control, with 32 denied read edges and 26 denied write edges. Output-only again has 127/128 overlap edges and 32 denied read edges. Thus both candidates improve completion in both tested conditions, while the extra input-wait move helps substantially only in the second. These observations do not establish a universal ranking or memory-latency bound.

The evidence index (`build/rtlgraph-dma/results-1.json`) joins **eight successful replays, 12,288 golden-word comparisons, 40 DMA commands with 2,560 matched request/response pairs, 96 LSU commands, and 128 VPU commands**. It also records 24,296 separately scoped local CIRCT cutpoint checks, 67 DMA-elided compiler projections, 63 new focused tests, and 14 existing replay tests.

## Scheduling and hardware contract

The [narrow adapter](../scripts/rtlgraph_dma_schedule.py#L190-L235) starts from the validated memory-overlap schedule. `output_overlap` moves the C output transfer ahead of B and A and launches it after C's final `VSTORE` write, while independent A/B computation and stores continue. The existing channel-one wait still precedes each subsequent output transfer. `input_output_overlap` additionally moves the channel-one input wait past A's two loads and first `VSQUARE.BF16`, retaining it before the first B-buffer `VLOAD`.

The adapter evaluates concrete scalar addresses, including signed `ADDI`, and uses the hardware's word-address-to-line mapping. It rejects unfinished input consumption, output launch before all output rows are written, conflicting live buffers, channel reuse before its wait, and missing completion waits. The five transfer descriptors and the full encoded non-idle multiset must match the handwritten source. Preserving instruction words alone would not preserve transfer semantics if scalar setup moved incorrectly; both checks are required.

The [typed hardware checks](rtlgraph-dma-hardware.md) establish selected launch/capture, wait, completion, address-slice, and arbitration facts. `DMA.CONFIG` updates a scalar base register. `DMA.LOAD` and `DMA.STORE` capture resolved command fields at launch, with no command backpressure that could rescue an overwritten slot. `DMA.WAIT` tests the selected channel's busy state. LSU accesses take priority over DMA on a shared VMEM bank; a denied DMA request must wait for a grant. All unary buffers use bank zero, so their logical independence does not eliminate bank contention.

These facts differ from parts of the inherited compiler DMA model, including completion-time scalar reads, queued configuration, and VMEM pointer units. The experiment therefore uses a separate explicit-wait/range checker and checks only the local LSU/VPU projection with the existing compiler. It does not claim that the native compiler DMA model has been corrected. The removed automatic wait-insertion commit remains absent.

Numerical local timing remains inherited. For the combined candidate, the moved input wait can stall with A's first unary operation active. The adapter checks every additional integer stall from zero through 65 against the compiler's local resource/dependence model. By the end of that interval, all earlier local work is complete; a longer wait translates the remaining local schedule without changing its relative timing. Output waits occur after local work drains. This argument covers this admitted schedule under the local model, not arbitrary kernels or a bound on DMA completion.

## Independent validation and comparison

The [204-signal capture](../scripts/rtlgraph_dma_vcd.py#L7-L36) extends the previous LSU/VPU map with accepted DMA operands, saved command slots, per-channel busy state, TileLink requests/responses and source IDs, and DMA VMEM responses. The [event monitor](../scripts/rtlgraph_dma_trace.py#L58-L264) reconstructs ownership from scalar launches and accepted bus transactions. It checks saved fields, ordered request addresses, response tags, actual VMEM grants, live-buffer conflicts, explicit waits, and complete retirement before the successful `DBG0`. It measures completion ages without imposing a guessed DMA latency. Full goldens independently check payload correctness.

The [comparison](../scripts/rtlgraph_dma_compare.py#L30-L103) requires the same audited host ELF outside its Atlas program array, capacity, runtime, fixture, and first-issue edge. It rechecks the complete instruction stream and observed DMA transfer descriptors. Its performance metric is first Atlas issue through `DBG0` after all output DMA completes. This excludes host setup and golden comparison time; the exact `ECALL` halt edge is not captured.

The original cycle-counter instructions remain present, but DMA work now crosses their boundaries. Their values are diagnostic and are not reported as a like-for-like compute-window speedup. The handwritten and previous memory-overlap schedules remain separate references so that any additional DMA benefit can be distinguished from the earlier memory/compute gain.

## Reproduce

Use the environment and licensed simulator setup in the [replay guide](rtlgraph-mxu1.md#replay); no Chipyard Make, Mill, elaboration, or simulator rebuild is required. Keep outputs in fresh directories and at most three licensed simulator processes active.

```sh
atlas_baremetal=/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/baremetal
export LD_LIBRARY_PATH=/tools/C/reednicolas/ee194-sp26-chipyard/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
python3 -B scripts/rtlgraph_dma_schedule.py \
  --source build/rtlgraph-corpus/originals-1/perf_unary.S \
  --memory-baseline build/rtlgraph-memory/candidates-2/memory_critical.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-no-dma-waits/atlas-opt \
  --output build/rtlgraph-dma/new-candidates

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-dma/new-candidates/input_output_overlap.S \
  --golden-json "$atlas_baremetal/generators/perf_unary.json" \
  --control-manifest build/rtlgraph-memory/control-template-4/manifest.json \
  --capture-dma --run --output build/rtlgraph-dma/new-run

python3 -B scripts/rtlgraph_dma_trace.py \
  --manifest build/rtlgraph-dma/new-run/manifest.json \
  --output build/rtlgraph-dma/new-run/dma-events.json
```

Replay the handwritten and memory-overlap references against the same control manifest with `--capture-dma`, then use `rtlgraph_dma_compare.py --original ... --memory-baseline ... --candidate NAME=MANIFEST --candidate-manifest ... --output ...`. These scripts are experiment adapters, not a general machine-description loader or Merlin export. The event-dependent completion and resource facts can inform that future canonical schema without converting variable DMA latency into a fixed issue gap.
