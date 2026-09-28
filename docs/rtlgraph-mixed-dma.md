# Full-kernel DMA/MXU scheduling

The existing native scheduler now schedules complete supported scalar/DMA/LSU/VPU/MXU instruction streams through the same machine model. The assembly adapter accepts optional MXU0/MXU1 profiles alongside the [DMA range profile](rtlgraph-dram-ranges.md). Both scheduling and the exact final legality check use the same selected profiles. No C++ scheduler change, numerical latency change, or automatic `DMA.WAIT` insertion was needed.

## Controlled results

The measurement is first Atlas issue through successful `DBG0` after output-DMA completion. It excludes host setup and host golden checking. Each row represents one execution under the shared host/environment for that kernel.

| Fused attention on MXU1 | Completion edges | Saved against handwritten |
|---|---:|---:|
| Handwritten reachable program | 24,932 | — |
| Earlier compute-only profile schedule | 24,000 | 932 (3.74%) |
| Full-kernel DMA profile, critical priority | **22,132** | **2,800 (11.23%)** |
| Full-kernel DMA + MXU1 profiles, critical priority | 22,136 | 2,796 (11.21%) |
| Full-kernel DMA + MXU1 profiles, input priority | **22,132** | **2,800 (11.23%)** |

The best full-kernel schedules save **1,868 edges (7.78%)** against the earlier compute-only schedule under this control. They preserve the same non-idle operations and operands while overlapping transfers with useful engine work. In the combined critical schedule, DMA overlaps 472 MXU physical-row edges, 70 edges of MXU compute activity, 512 LSU VMEM-access edges, and 386 VPU row-active edges. These overlap counts are not additive cycle savings.

The same-priority control is important: adding the MXU1 profile shortens the interval through the ending counter by 32 edges, but final completion is **four edges worse** than the DMA-only critical schedule. Four observed pop/overwrite overlaps occur in the combined critical schedule, versus zero in the DMA-only control. Memory behavior offsets that local timing benefit; this experiment establishes no additional full-completion gain from the MXU1 profile. Counter intervals differ from the handwritten and earlier compute-only programs, so their raw `DBG1` values are diagnostic rather than comparable performance windows.

The DMA-only comparison (`build/rtlgraph-mixed-dma/fused-mxu1/comparison-dma-2.json`), combined-profile comparison (`build/rtlgraph-mixed-dma/fused-mxu1/comparison-profile-2.json`), and earlier-compute comparison (`build/rtlgraph-mixed-dma/fused-mxu1/comparison-compute-only-2.json`) retain exact joins. The comparator's generic `memory_baseline` entry names the DMA-only schedule in the second report and the earlier compute-only schedule in the third. All five executions use one audited 512-word host and issue their first Atlas instruction at edge 145,512.

Two additional VPU comparisons use their own shared 128-word hosts and the unchanged DMA v2 model:

| Kernel | Handwritten edges | Native critical/input edges | Reduction |
|---|---:|---:|---:|
| `perf_vec_layernorm_32x32.S` | 4,608 | 4,593 | 0.33% |
| `perf_softmax.S` | 4,491 | 4,437 | 1.20% |

Both priorities pass for both kernels. Layer normalization overlaps 64 VPU row-active edges with DMA; softmax overlaps 64 LSU VMEM-access edges with DMA and observes 32 denied DMA read-grant edges. The VPU result index (`build/rtlgraph-mixed-dma/vpu/results-1.json`) records the six replays, independent event checks, controls, and comparisons. These small gains do not imply a uniform speedup across memory conditions.

The remaining eight golden-backed programs now have controlled full-kernel comparisons, using the DMA v2 profile and the applicable MXU profiles. Values below are first-issue-to-`DBG0` edges under one fresh shared host per kernel.

| Kernel | Handwritten | Critical | Input | Best reduction |
|---|---:|---:|---:|---:|
| `perf_fused_attention_mxu0.S` | 25,445 | 22,132 | 22,132 | 13.02% |
| `perf_mm_dual_128x128x128.S` | 42,792 | 41,551 | 41,093 | 3.97% |
| `perf_mm_mxu0_64x64x64.S` | 12,189 | 11,941 | 11,941 | 2.03% |
| `perf_mm_mxu0_64x64x128.S` | 19,114 | 18,510 | 18,510 | 3.16% |
| `perf_mm_mxu1_64x64x64.S` | 12,189 | 11,941 | 11,941 | 2.03% |
| `perf_mm_mxu1_64x64x128.S` | 19,114 | 18,510 | 18,510 | 3.16% |
| `perf_vec_rmsnorm_softmax.S` | 8,459 | 7,554 | 7,554 | 10.70% |
| `perf_unary.S` | 10,181 | 9,083 | 9,268 | 10.78% |

The corpus summary (`build/rtlgraph-mixed-corpus/run-2/summary.json`) retains both priorities, exact joins, and the initial monitor rejections discussed below. Together with the earlier three programs, all eleven full-golden `perf_*.S` kernels now have controlled full-kernel results. This sweep does not isolate each MXU profile’s contribution from DMA scheduling.

## What changed and what was validated

The [native adapter](../scripts/rtlgraph_dma_compile.py#L110-L119) translates MXU instructions using the existing compute translator and adds `SELI` scale setup. Its [profile composition](../scripts/rtlgraph_dma_compile.py#L212-L220) adds optional `--mxu0-profile` and `--mxu1-profile` inputs; the comparison [binds their canonical evidence and hardware IR identity](../scripts/rtlgraph_dma_compare.py#L60-L89) to the exact scheduling and checking commands. Translation preserves encoded instructions before scheduling, and final programs preserve the non-idle operation multiset. The expanded corpus exercises both MXUs, dual-engine streams, and VPU kernels.

The [straight-line normalizer](../scripts/rtlgraph_straightline.py#L25-L71) removes label-only lines and an unreachable suffix after the first `ECALL`, after rejecting reachable branches, jumps, and PC-relative instructions. It verifies exact encoded-prefix equality. Fused attention retains 260 reachable words; softmax retains 53. `@PERF_REPORT` was already present in fused attention and is added to softmax. That directive changes host diagnostics, so fair comparisons use freshly controlled hosts made from those normalized handwritten programs. The normalizer does not authorize arbitrary control-flow deletion.

The new `--capture-mixed` mode captures DMA/LSU/VPU and both MXUs together. The [mixed monitor](../scripts/rtlgraph_mixed_trace.py#L34-L108) checks common sampled clock/reset, accepted scalar commands, independent compute/pop row ownership, and physical MREG requests across LSU, VPU, MXU0, and MXU1. It rejects two reads or two writes to one physical bank and rejects same-cycle read/write of the same physical row. DMA completion and request/response accounting still use the existing monitor. MXU write ages are measured, not asserted from the proposed model.

The sweep exposed a false rejection when DMA reused a `VLOAD` source after its 32 VMEM reads while MREG writes still drained. The [launch check](../scripts/rtlgraph_dma_trace.py#L135-L142) now requires all 32 ordered relevant VMEM accesses strictly before reuse. Independent checks still reject missing, reordered, extra, or conflicting accesses. This correction uses observed events; the initial rejections and unchanged captures remain available.

All 24 expanded-corpus executions pass **35,328 golden-word comparisons** and the final native/mixed checks. The eleven earlier captures also pass a regression check with the corrected monitor (`build/rtlgraph-mixed-regression/review-1/results.json`); their original results remain in `build/rtlgraph-mixed-dma/results-1.json`. The focused DMA/mixed suites pass 52 tests, including five new buffer-reuse cases. These measurements reuse the frozen compiler binary.

The [LSU timing analysis](rtlgraph-lsu-timing.md) derives requests at ages 1–32 and, under its one-cycle source-response contract, writes at 3–34 and release at 35. The [memory-logic query](rtlgraph-lsu-timing.md#source-response-contract-from-memory-logic) now establishes that response-valid contract under explicit bank/arbitration exclusions. Payloads, complete row routing, and destination-write acceptance remain outside that proof.

## Limits and reproduction

The combined monitor does not establish push-row ownership, payload/return routing, accumulator read-during-write visibility, or universal timing safety. Physical checks cover the four admitted client families, not arbitrary XLU traffic. Full goldens provide numerical validation for these inputs. The cached simulator's source-to-binary build linkage remains unverified. Exact operation multisets alone do not prove a reorder correct, and one execution per variant is not a universal performance bound.

The adapter remains limited to supported straight-line kernels, with explicit same-block DMA completion and conservative unknown/wrapping DRAM aliases. The [assembly/timing handoff](rtlgraph-contract.md) now packages native assembly, resolved footprints, selected profiles, and provenance for a Merlin emitter. A complete reusable machine description, cross-block DMA scheduling, and a direct Merlin model consumer remain future work in the [plan](../.agents/notes/RTL_GRAPH_PLAN.md).

Use the [documented environment](rtlgraph-mxu1.md#replay) and fresh output directories. This example requires the existing pinned profiles, compiler binary, and 512-word baseline control manifest from this experiment:

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

python3 -B scripts/rtlgraph_straightline.py \
  --source "$atlas_baremetal/assembly/perf_fused_attention_mxu1.S" \
  --assembler "$atlas_baremetal/assembler.py" \
  --output build/rtlgraph-mixed-dma/reproduce-original

python3 -B scripts/rtlgraph_dma_compile.py \
  --source build/rtlgraph-mixed-dma/reproduce-original/perf_fused_attention_mxu1.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-dram-ranges/cmake-1/atlas-opt \
  --profile build/rtlgraph-dram-ranges/profile-2/atlas-dma.profile \
  --mxu1-profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile \
  --relocate-perf-markers --output build/rtlgraph-mixed-dma/reproduce-candidates

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-mixed-dma/reproduce-candidates/native_critical.S \
  --golden-json "$atlas_baremetal/generators/perf_fused_attention_mxu1.json" \
  --control-manifest build/rtlgraph-mixed-dma/fused-mxu1/original-run-1/manifest.json \
  --capture-mixed --run --output build/rtlgraph-mixed-dma/reproduce-run
```

Omit `--mxu1-profile` to construct the same-priority DMA-only control. Use `rtlgraph_dma_compare.py` for the independent final join; it automatically requires mixed observations for MXU programs and rechecks the emitted schedule against every selected profile. The pinned hardware IR and profiles are reused across kernels; scheduling does not require another hardware elaboration.
