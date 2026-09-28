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

The [DMA-only comparison](../build/rtlgraph-mixed-dma/fused-mxu1/comparison-dma-2.json), [combined-profile comparison](../build/rtlgraph-mixed-dma/fused-mxu1/comparison-profile-2.json), and [earlier-compute comparison](../build/rtlgraph-mixed-dma/fused-mxu1/comparison-compute-only-2.json) retain exact joins. The comparator's generic `memory_baseline` entry names the DMA-only schedule in the second report and the earlier compute-only schedule in the third. All five executions use one audited 512-word host and issue their first Atlas instruction at edge 145,512.

Two additional VPU comparisons use their own shared 128-word hosts and the unchanged DMA v2 model:

| Kernel | Handwritten edges | Native critical/input edges | Reduction |
|---|---:|---:|---:|
| `perf_vec_layernorm_32x32.S` | 4,608 | 4,593 | 0.33% |
| `perf_softmax.S` | 4,491 | 4,437 | 1.20% |

Both priorities pass for both kernels. Layer normalization overlaps 64 VPU row-active edges with DMA; softmax overlaps 64 LSU VMEM-access edges with DMA and observes 32 denied DMA read-grant edges. The [VPU result index](../build/rtlgraph-mixed-dma/vpu/results-1.json) records the six replays, independent event checks, controls, and comparisons. These small gains do not imply a uniform speedup across memory conditions.

## What changed and what was validated

The [native adapter](../scripts/rtlgraph_dma_compile.py#L110-L119) translates MXU instructions using the existing compute translator and adds `SELI` scale setup. Its [profile composition](../scripts/rtlgraph_dma_compile.py#L212-L220) adds optional `--mxu0-profile` and `--mxu1-profile` inputs; the comparison [binds their canonical evidence and hardware IR identity](../scripts/rtlgraph_dma_compare.py#L60-L89) to the exact scheduling and checking commands. Translation preserves encoded instructions before scheduling, and final programs preserve the non-idle operation multiset. MXU0 and dual-MXU assembly roundtrips are tested; the new mixed RTL performance result here is MXU1 fused attention.

The [straight-line normalizer](../scripts/rtlgraph_straightline.py#L25-L71) removes label-only lines and an unreachable suffix after the first `ECALL`, after rejecting reachable branches, jumps, and PC-relative instructions. It verifies exact encoded-prefix equality. Fused attention retains 260 reachable words; softmax retains 53. `@PERF_REPORT` was already present in fused attention and is added to softmax. That directive changes host diagnostics, so fair comparisons use freshly controlled hosts made from those normalized handwritten programs. The normalizer does not authorize arbitrary control-flow deletion.

The new `--capture-mixed` mode captures DMA/LSU/VPU and both MXUs together. The [mixed monitor](../scripts/rtlgraph_mixed_trace.py#L33-L107) checks common sampled clock/reset, accepted scalar commands, independent compute/pop row ownership, and physical MREG requests across LSU, VPU, MXU0, and MXU1. It rejects two reads or two writes to one physical bank and rejects same-cycle read/write of the same physical row. DMA completion and request/response accounting still use the existing monitor. MXU write ages are measured, not asserted from the proposed model.

Across eleven RTL replays, all **8,192 golden-word comparisons** pass. Independent monitors account for **48 DMA, 144 LSU, 238 VPU, and 230 MXU1 commands**, plus **4,608 accepted DMA requests and responses**. The five fused runs each check all 1,024 golden words; each VPU run checks 512. The [combined result index](../build/rtlgraph-mixed-dma/results-1.json) records the final artifacts and 100 passing focused Python tests. Existing C++ sources and the frozen compiler binary are unchanged.

The parallel [LSU timing analysis](rtlgraph-lsu-timing.md) derives request ages 1–32 from typed control recurrences. Assuming one-cycle source responses, bounded composition derives writes at 3–34 and release at 35. It checks 1,024 local transition cases and 64 initial-counter trajectories, without importing the compiler timing table. This strengthens the evidence for inherited LSU timing; it is not a new shorter latency, a `circt-bmc` result, or a proof of the external response assumption.

## Limits and reproduction

The combined monitor does not establish push-row ownership, payload/return routing, accumulator read-during-write visibility, or universal timing safety. Physical checks cover the four admitted client families, not arbitrary XLU traffic. Full goldens provide numerical validation for these inputs. The cached simulator's source-to-binary build linkage remains unverified. Exact operation multisets alone do not prove a reorder correct, and one execution per variant is not a universal performance bound.

The adapter remains limited to supported straight-line kernels, with explicit same-block DMA completion and conservative unknown/wrapping DRAM aliases. Rich machine-schema completion, cross-block DMA scheduling, additional temporal proofs, and optional Merlin export remain separate work in the [plan](../.agents/notes/RTL_GRAPH_PLAN.md).

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
