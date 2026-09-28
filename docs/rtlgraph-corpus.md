# Performance corpus baselines

The [corpus helper](../scripts/rtlgraph_corpus.py) stages the existing `perf_*.S` programs without changing their encoded Atlas instructions. It adds only a missing `@PERF_REPORT` comment, records original/staged/assembler/golden/generator hashes, and reuses existing fixtures without running their generators. The original baremetal checkout stays read-only.

There are 14 programs: 11 have nonempty DRAM checks, `perf_mm_single` has no output checks, and the VPU binary/reduction microbenchmarks have no JSON fixture and multiple timed regions with branches. The latter three require separate validation. The subsequent [VPU follow-up](rtlgraph-vpu-kernels.md#binary-and-reduction-probes) validates the two VPU probes through their original numerical spot checks and exact success paths; they remain distinct from full-output goldens. A utilization threshold or `DBG0` value alone is not an arithmetic-correctness result; some microbenchmark failure codes can overlap the success value.

```sh
python3 -B scripts/rtlgraph_corpus.py prepare \
  --baremetal-root /tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/baremetal \
  --output build/rtlgraph-corpus/originals-1 \
  --reuse perf_mm_mxu1_64x64x64=build/rtlgraph-perf/baseline-k64-2/manifest.json \
  --reuse perf_mm_mxu1_64x64x128=build/rtlgraph-perf/k128_original_1/manifest.json \
  --reuse perf_fused_attention_mxu1=build/rtlgraph-perf/fused_original_1/manifest.json

python3 -B scripts/rtlgraph_corpus.py run \
  --inventory build/rtlgraph-corpus/originals-1/inventory.json \
  --case perf_mm_mxu0_64x64x64 \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --output build/rtlgraph-corpus/runs/mxu0_k64_original_1
```

Use the established BWRC/Conda environment for licensed VCS execution. Each `run` dispatches one isolated replay and always captures the scalar/CSR waveform. Coordinate **at most three VCS processes across all workers**; the helper does not impose a machine-wide job limit. Start with smaller kernels, and allow additional wall time for the dual-engine kernel's 4,096 output checks. Do not invoke Chipyard Make or regenerate existing goldens.

`report --inventory ... --run NAME=MANIFEST --output NEWFILE` joins new runs with the verified prior baselines, rechecking their artifacts when each report is generated. A passing entry requires a completed successful process, the exact nonempty golden-check count, `DBG0=1`, halted/`ECALL` status, a fully parsed capture, and assembled instruction-word identity with the original. Failed/incomplete records remain visible; special cases are not reported as passing. Kernels without an assigned or reused replay remain `not_run`, including work that is still queued.

For each baseline that passes, compare paired candidates with the same fixtures, setup and output suffix. Deduplicate identical instruction streams. Use the [completion helper](../scripts/rtlgraph_completion.py) when its straight-line, two-marker requirements hold, and retain distinct CSR-window, first-instruction-to-`DBG0`, and compute-start-to-`DBG0` measurements. An MXU1 signal capture does not independently validate MXU0 or VPU row timing. Host raw cycle counts, simulator wall time, and utilization thresholds are not substitutes for kernel completion measurements.

## Completed original baselines

The staged inventory (`build/rtlgraph-corpus/originals-1/inventory.json`) records all 14 instruction-preserving copies. The final baseline report (`build/rtlgraph-corpus/baseline-results-2.json`) independently validates all **11 golden-backed originals**, including every fired scalar instruction and consecutive PC through `DBG0`, the two cycle markers, final `DMA.WAIT`, and eventual host halted/`ECALL` status. Each entry links its replay and completion artifacts. The audit driver (`build/rtlgraph-corpus/finalize-baselines.py`) preserves existing completion reports and checks them against freshly derived waveform results.

| Original kernel | Golden words checked | CSR cycle delta | First issue → `DBG0` clock edges |
| --- | ---: | ---: | ---: |
| `perf_fused_attention_mxu0` | 1,024 | 3,430 | 25,570 |
| `perf_fused_attention_mxu1` | 1,024 | 2,942 | 24,931 |
| `perf_mm_dual_128x128x128` | 4,096 | 1,099 | 43,120 |
| `perf_mm_mxu0_64x64x128` | 1,024 | 547 | 19,428 |
| `perf_mm_mxu0_64x64x64` | 1,024 | 291 | 12,086 |
| `perf_mm_mxu1_64x64x128` | 1,024 | 546 | 19,745 |
| `perf_mm_mxu1_64x64x64` | 1,024 | 290 | 12,096 |
| `perf_softmax` | 512 | 337 | 4,668 |
| `perf_unary` | 1,536 | 960 | 10,061 |
| `perf_vec_layernorm_32x32` | 512 | 605 | 4,707 |
| `perf_vec_rmsnorm_softmax` | 1,024 | 706 | 8,576 |

`perf_mm_single`, `perf_vpu_binary`, and `perf_vpu_reduction` were **unvalidated special cases** at this baseline checkpoint, not passing entries in its golden-backed report. The later [VPU follow-up](rtlgraph-vpu-kernels.md#binary-and-reduction-probes) checks the two VPU probes with branch-aware numerical spot-check validation. `perf_mm_single` still lacks output goldens. No fixtures or original sources were regenerated to fill those gaps.

The CSR column measures each program's existing window, not necessarily full engine completion. Some original matrix kernels end that window at the last pop issue. The completion column includes setup and actual output/DMA waits; it reflects one memory-environment execution rather than a universal latency bound. Candidate comparisons must also include the observed first instruction and first counter through `DBG0`, after the final `DMA.WAIT`; the host halt status confirms eventual `ECALL` without locating its waveform edge. These checks do not establish full MXU0/MXU1/VPU temporal correctness, and the cached simulator's source-to-binary build lineage remains unverified. See [corpus scheduling and comparisons](rtlgraph-corpus-scheduling.md) for candidate-specific results and timing scope.
