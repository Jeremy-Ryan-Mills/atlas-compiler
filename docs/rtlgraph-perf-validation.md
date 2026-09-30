# Kernel validation results

The measured interval runs from first accepted Atlas instruction to successful `DBG0` publication after output DMA completion. It excludes host setup/checking and simulator wall time; movable private CSR counters are diagnostic. Controlled pairs patch only a fixed-capacity Atlas instruction array in one host ELF, retaining golden data, host layout, programming work, and launch edge. Capacity must fit both programs and remain at most 1,024 words. DRAM service can still depend on launch conditions.

The corpus is in [Atlas NPU baremetal](https://github.com/ucb-bar/atlas-npu/tree/main/baremetal). Eleven original `perf_*.S` kernels have full-output goldens; three others require additional instrumentation. The [archived evidence](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/644992119b9527d8dad019200f80a80d9e0ffee4/profiles/EE290SimConfig/validation) records inputs, hashes, controls, and individual runs. These are recorded results, not new executions during documentation cleanup. Common [evidence limits](rtlgraph-model.md#evidence-and-assumptions) apply.

## Original kernels

The regression records 29 RTL executions and 35,328 checked 32-bit words; an audit matched all 5,055 accepted instruction PCs/words to assembled programs. All six profiles and default critical-path scheduling produce the following results at historical fixed-host capacities:

| Kernel | Handwritten edges | Scheduled edges | Saved |
| --- | ---: | ---: | ---: |
| `perf_fused_attention_mxu0.S` | 25,445 | 22,132 | 3,313 |
| `perf_fused_attention_mxu1.S` | 24,932 | 22,136 | 2,796 |
| `perf_mm_dual_128x128x128.S` | 42,792 | 41,551 | 1,241 |
| `perf_mm_mxu0_64x64x128.S` | 19,114 | 18,510 | 604 |
| `perf_mm_mxu0_64x64x64.S` | 12,189 | 11,941 | 248 |
| `perf_mm_mxu1_64x64x128.S` | 19,114 | 18,510 | 604 |
| `perf_mm_mxu1_64x64x64.S` | 12,189 | 11,941 | 248 |
| `perf_softmax.S` | 4,491 | 4,437 | 54 |
| `perf_unary.S` | 10,181 | 9,083 | 1,098 |
| `perf_vec_layernorm_32x32.S` | 4,608 | 4,593 | 15 |
| `perf_vec_rmsnorm_softmax.S` | 8,459 | 7,554 | 905 |

Input-order priority improves dual-MXU to 41,093 edges, saving another 458. DMA-only profiling gives fused-MXU1 22,132 edges, four fewer than the combined model despite a longer modeled compute window. Both alternatives were replayed. Local resource gains and list-scheduler priorities therefore need whole-kernel measurement.

Layernorm changes from 4,803 to 4,830 edges with a 64-word host capacity, while the 128-word control above saves 15. Softmax changes from 4,643 to 4,639 at 64 words. Within each pair, host and launch conditions match; Atlas words are unchanged across capacities. The phase audit finds output DMA launches 74 edges earlier in both layernorm controls, then takes 102 extra edges at 64 words or 60 extra at 128, with one fewer publication edge. These sum to the 27-edge regression and 15-edge improvement. Input-order priority also takes 4,830 at 64 words. Traces identify the DMA phase responsible but contain insufficient memory request/response state to establish its cause.

## Additional correctness coverage

Automatic wait insertion regenerated all 139 removed handwritten waits across the eleven golden-backed kernels and passed 13,824 output-word checks. Eight emitted streams match separately replayed explicit-wait candidates; fused-attention variants and softmax received additional direct runs. Matched automatic/explicit schedules have equal completion times. This validates synchronization generation without claiming another throughput gain.

A three-block DMA witness takes both branch outcomes, reuses captured pointer registers, and checks all 96 DRAM words for 384 bytes copied. Its first-issue-to-publication interval is 1,017 edges; its internal CSR bracket is 1,005 cycles. The [archived replay](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/644992119b9527d8dad019200f80a80d9e0ffee4/profiles/EE290SimConfig/dma/cfg-replay.json) records the finite correctness run.

Three instrumented probes add full-output observations to original sources lacking goldens. VPU probes preserve operations/branches and capture both outputs; single-matmul substitutes exact FP8 constants and adds BF16 pops/writeback. Six runs check 13,312 words:

| Instrumented probe | Words/run | Original edges | Scheduled edges | Comparison scope |
| --- | ---: | ---: | ---: | --- |
| `perf_vpu_binary` | 2,560 | 8,353 | 8,184 | Different hosts; diagnostic |
| `perf_vpu_reduction` | 3,072 | 10,090 | 9,905 | Different hosts; diagnostic |
| `perf_mm_single` | 1,024 | 7,520 | 7,170 | Shared host/launch; 350 edges saved (4.65%) |

These altered workloads establish output checks separately from the original eleven. The connected XLU/MREG/LSU witness checks 192 cases and 196,608 bytes with source overwrites and early consumers; its gap-34 overlap excludes ScalarCore and violates frontend reservations, so it is datapath evidence only. Gap 33 collides on all 32 physical rows and is rejected despite a simulator's numerical output. Higher-priority read interference loses a response. The model retains the restrictions explained in [profile semantics](rtlgraph-model.md#profile-semantics).
