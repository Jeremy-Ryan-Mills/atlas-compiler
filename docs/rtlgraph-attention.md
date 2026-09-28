# Fused-attention scheduling experiment

This page records the earlier compute-only experiment. The subsequent [full-kernel DMA/MXU comparison](rtlgraph-mixed-dma.md) replays that schedule under a shared host control and improves its completion **24,000→22,132 edges** while preserving the reachable operations and outputs.

This extends the fixed-operation scheduling experiment to [`perf_fused_attention_mxu1.S`](../../baremetal/assembly/perf_fused_attention_mxu1.S#L187-L403). Its two K-tile stages combine MXU1 matmuls, BF16 accumulator transfers, and VPU normalization operations. The existing golden fixture checks 1,024 output words. VPU and BF16-transfer timings still come from the built-in model; the unchanged K64-derived CIRCT profile supplies only its two documented MXU1 properties.

The generated profile/critical schedule beats the handwritten kernel: **2,942→2,069 measured CSR cycles**, a reduction of **873 cycles (29.67%)**. All three compared programs pass all 1,024 golden output words, `DBG0=1`, and halted/`ECALL` status. This is the first measured improvement over a handwritten kernel in this extraction experiment.

The [K64/K128 search](rtlgraph-search.md) establishes that the handwritten schedules reach the current model's optimum for fixed operands and instructions. This mixed kernel provides a different opportunity: overlapping independent engine work and reusing the accumulator while a previous result streams out.

## Measured results and attribution

| Schedule | CSR window cycles | First Atlas issue → `DBG0` clock edges | First cycle read → `DBG0` clock edges |
| --- | ---: | ---: | ---: |
| Handwritten | 2,942 | 24,931 | 6,066 |
| Built-in model, critical priority | 2,101 | 24,073 | 5,152 |
| Partial RTL profile, critical priority | **2,069** | **23,978** | **5,068** |

The scheduler using the built-in model recovers 841 cycles of overlap. Holding critical-path priority constant, the CIRCT-derived correction recovers another **32 cycles**. First-write age is unchanged at 3; the relevant profile difference removes the unnecessary accumulator-read hold for overwrite-only `VMATMUL.MXU1`. This permits `VMATPOP.BF16.MXU1` to keep reading old accumulator rows while a newly issued overwrite compute produces replacements later. The source/typed overlap audit (`build/rtlgraph-bottleneck/audit-1/fused-overlap.json`) also examines the candidate's two-cycle `VMATPUSH.ACC.BF16.MXU1`→`VMATPOP.FP8.MXU1` streams; that overlap was already admitted by the built-in model and is not counted as a new profile fact.

The generated kernel (`build/rtlgraph-kernel/fused/candidates-1/profile_critical.S`) preserves the original operations, setup, and output stores. Its completion report (`build/rtlgraph-perf/fused_profile_critical_1/completion.json`) binds every observed scalar instruction word and consecutive PC from the first issue through `DBG0` to the actual assembled program. Equivalent reports cover the handwritten baseline (`build/rtlgraph-perf/fused_original_1/completion.json`) and built-in control (`build/rtlgraph-perf/fused_builtin_critical_1/completion.json`). The checks match both cycle-counter reads, the reported `DBG1` delta, the final `DMA.WAIT`, and the successful completion marker.

The observed whole-kernel interval improves by **953 edges (3.82%)**, including setup and output writeback. From the first measured cycle read through completion, it improves by **998 edges (16.45%)**. These are single executions in the recorded memory environment, not universal DMA latency or averaged benchmark estimates: unchanged setup and suffix instructions experience different memory wait durations. The same-priority profile contribution is the 32-cycle CSR-window difference; the larger completion difference against the built-in control must not all be attributed to that field.

The joined comparison (`build/rtlgraph-perf/fused-comparison.json`) verifies identical assembled setup and suffix words, the shared golden fixture, and the individual completion reports. Its replay script (`build/rtlgraph-kernel/fused/join_completion.py`) records those checks. The observed setup spans are 18,865 / 18,921 / 18,910 edges and suffix spans are 3,123 / 3,050 / 2,998 for handwritten / built-in / profile, respectively.

`ECALL` suppresses scalar issue-valid and is not separately captured. The waveform establishes the preceding `DBG0` event after the final DMA wait; the host log establishes eventual halted/`ECALL` status. It does not supply an exact `ECALL` clock edge. Host `raw_mcycles` includes host-side work and is not used for these comparisons. VPU row-level behavior and universal arithmetic equivalence are not proven by these finite functional checks.

## Preserve the computation and measure completion

The original uses two `CSRRS` cycle-counter snapshots and reports their difference through `CSR_DBG1`. Its final VPU operation is followed by a delay before the ending snapshot. The [post-store suffix](../../baremetal/assembly/perf_fused_attention_mxu1.S#L381-L399) writes the outputs to VMEM, waits for DMA writeback, then signals `DBG0` and executes `ECALL`.

The local baseline prepends only `# @PERF_REPORT`, enabling host reporting without changing any of the original 262 instruction words. It reuses the existing golden JSON rather than regenerating it. The baseline preparation record (`build/rtlgraph-kernel/fused/baseline-preparation.json`) records the exact inputs and encoding check. The original source's `@PYTHON_GEN` filename uses different capitalization from the existing generator; the replay passes the golden path explicitly.

The [attention adapter](../scripts/rtlgraph_attention.py#L19-L113) handles this kernel's extra instructions and `CSRRS` markers. It verifies full-program roundtrip encoding with the original assembler, preserves every non-idle encoded operation and operand, and verifies that setup, cycle markers, and output-writeback instructions are unchanged. Control-flow and PC-relative instructions are rejected; the original unreachable suffix labels are preserved. Encoding checks do not prove that a permutation is semantically valid.

For scheduling, the adapter appends a compiler-only `ECALL` sentinel to the extracted body. The scheduler drains modeled work before that sentinel. Splicing replaces it with the original ending CSR while retaining the drain and any halt-guard no-op **inside** the measured window. There is no extra unmeasured drain. This is a model-based boundary obligation to validate on RTL, not a proof of all VPU timings.

Do not use the compiler log's original-body cycle count as the benchmark baseline: the artificial original `ECALL` follows a delay, and the [simulator's halt handling](../src/core/simulator.cpp#L246-L248) skips that delay. The real original ending CSR does not have this behavior. Static assembly counting and observed CSR/waveform timings preserve the actual boundary.

## Reproduce the candidates

From the compiler repository, after the [replay environment setup](rtlgraph-mxu1.md#replay), use a new output directory:

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/riscv-tools/lib:$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3 scripts/rtlgraph_attention.py \
  --source build/rtlgraph-kernel/fused/fused_original.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-compiler/atlas-opt \
  --profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile \
  --output build/rtlgraph-kernel/fused/candidates_replay
```

The four-way candidate manifest (`build/rtlgraph-kernel/fused/candidates-1/manifest.json`) records built-in/profile models crossed with critical/input scheduling priority. All pass the compiler's model checks. Static cycle-snapshot issue spans are 2,943 for handwritten, 2,102 for built-in/critical, 2,101 for built-in/input, 2,070 for profile/critical, and 2,101 for profile/input. These are clock-edge predictions; observed CSR deltas differ because reading the counter suppresses an increment.

Replay the original and selected candidates separately with the same golden and simulator:

```sh
python3 scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-kernel/fused/candidates_replay/profile_critical.S \
  --golden-json "$atlas_baremetal/generators/perf_fused_attention_mxu1.json" \
  --output build/rtlgraph-perf/fused_profile_replay --run --capture
python3 scripts/rtlgraph_completion.py \
  --manifest build/rtlgraph-perf/fused_profile_replay/manifest.json \
  --output build/rtlgraph-perf/fused_profile_replay/completion.json
```

The hardware target remains `EE290SimConfig`. No hardware source is modified. The existing VCS executable's saved FIRRTL matches fresh S0 IR inputs, but its source-to-binary build lineage remains unverified. All generated evidence resides in ignored local `build/` directories.
