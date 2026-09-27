# VPU performance kernel boundaries and validation

The VPU follow-up preserves the handwritten kernels' operations, operands, input fixtures, and measured boundaries. The [previous corpus results](rtlgraph-corpus-scheduling.md) already include `perf_softmax`, `perf_vec_layernorm_32x32`, and `perf_vec_rmsnorm_softmax`. The remaining VPU programs need two different adapters: `perf_unary` has a full-output golden but timed memory instructions; `perf_vpu_binary` and `perf_vpu_reduction` have numerical spot checks and multiple measured regions.

## Unary chains with timed memory setup and writeback

`perf_unary` executes sixteen unary operations across three chains: eight operations on A, five on B, and three on another copy of B. The actual source does not contain the extra B/C priming described in some historical comments; the existing generator's executed operation lists match the assembly. Its two cycle reads enclose six `VLOAD`s, the compute chains, scalar address setup, and six `VSTORE`s. The “not timed” writeback comment is therefore inaccurate; the measured instruction boundary governs the comparison.

The [optional memory-wrapper adapter](../scripts/rtlgraph_schedule.py#L36-L85) selects only the contiguous register compute block. It preserves the original timed load/store regions byte-for-byte and requires every `VLOAD` and `VSTORE` to retain an immediately following `DELAY` of at least 33. Interleaved memory/scalar instructions and control flow remain rejected. This preserves an inherited transfer-ready assumption; it is not a newly extracted or universally proven memory timing contract.

The compiler's completion sentinel drains compute before the unchanged stores. The original CSR boundaries stay in place, so memory instructions remain inside the measured window. The [static issue summary](../scripts/rtlgraph_schedule.py#L88-L103) counts actual `LI` expansion, but excludes memory stalls and is not an RTL measurement. Generated candidates still require all 1,536 existing golden words and exact scalar/CSR trace binding.

The comparison helper independently reselects the timed memory wrapper from every replay's assembly. Its encoded setup and writeback, including their `DELAY`s, must match both the original replay and the candidate manifest. This check supplements the existing full-body non-idle instruction multiset comparison, which alone would not detect changed memory waits.

```sh
atlas_baremetal=/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/baremetal
python3 -B scripts/rtlgraph_schedule.py \
  --source build/rtlgraph-corpus/originals-1/perf_unary.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-compiler/atlas-opt \
  --preserve-memory-wrapper \
  --output build/rtlgraph-vpu-kernels/unary-candidates-replay
```

The first [candidate manifest](../build/rtlgraph-vpu-kernels/unary-candidates-1/manifest.json) records identical built-in critical/input streams and a static full-window count of 1,079 versus the original 961. The previous original RTL replay measured 960 CSR cycles; static issue counts and observed CSR differences are distinct metrics. This is a regression prediction, not a measured improvement. The greedy schedule starts chain C as soon as a slot becomes available, interfering with later work on the longest chain. Deliberately leaving a slot idle can be a useful scheduling choice; priority among currently ready operations alone does not provide that look-ahead.

## Measured unary search candidate

The [VPU search](rtlgraph-vpu-search.md) finds a schedule that keeps the longest chain progressing and fits the other two chains around it. The [candidate assembly](../build/rtlgraph-vpu-schedules/unary-exact-2/exact.S) preserves all sixteen operations and their operands. Its encoding matches the replayed `unary-exact-1/exact.S`; the second manifest improves provenance wording without changing the schedule.

Both original and candidate pass all **1,536 golden words** and all sixteen command/row monitors. The [paired comparison](../build/rtlgraph-vpu/unary-comparison-1.json) independently verifies unchanged outer setup/suffix, the timed memory wrapper, non-idle instruction words, goldens, simulator runtime, and runtime options.

| Measured interval | Handwritten | Search candidate | Cycles/edges saved |
| --- | ---: | ---: | ---: |
| Original CSR window, including timed loads/stores | 960 | 952 | 8 (0.83%) |
| First Atlas issue through post-DMA `DBG0` | 10,061 | 10,443 | −382 (regression) |
| Starting cycle read through `DBG0` | 5,274 | 5,461 | −187 (regression) |

This is a measured counter-window improvement, **not an observed whole-kernel speedup**. Setup before the first counter and the suffix after the second counter each increase by 195 edges in these executions. Their instruction encodings are unchanged; runtime/memory phase effects remain part of the wider measurement. Do not attribute those differences to a changed compute instruction count, or infer a universal completion bound from one run per encoding.

A later [fixed-host comparison and memory-overlap experiment](rtlgraph-memory-overlap.md#measured-result) controls host code/layout and IMEM programming length. Under that control, handwritten and this compute-only candidate both take 10,181 edges through `DBG0`; the eight saved counter cycles are offset by eight suffix edges. The separate memory-overlap candidate improves both intervals. These are new controlled executions; the older regression above remains part of the historical evidence.

The eight-cycle gain comes from fixed-model scheduling search. The new [CIRCT valid-chain proofs and VPU row observations](rtlgraph-vpu.md#local-valid-pipeline-proof) corroborate existing timing; they introduce no shorter numerical latency. Softmax, RMSNorm/softmax, and layer normalization search results are encoding-identical to their already validated `builtin_critical` candidates, so this follow-up finds no additional scheduling gain for those fixed-operation cases.

The [combined evidence index](../build/rtlgraph-vpu/results-1.json) records six new successful RTL replays, **66 monitored VPU commands**, 4,608 full-golden word comparisons across four runs, eleven numerical spot checks across two probes, and eighteen local functional-unit valid-chain proofs. Scope and performance regressions are retained separately.

To replay a new unary candidate with the [licensed environment](rtlgraph-mxu1.md#replay), choose a fresh output directory:

```sh
python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-vpu-schedules/unary-exact-2/exact.S \
  --golden-json "$atlas_baremetal/generators/perf_unary.json" \
  --output build/rtlgraph-vpu/runs/unary-exact-replay --run --capture-vpu
python3 -B scripts/rtlgraph_vpu_trace.py \
  --manifest build/rtlgraph-vpu/runs/unary-exact-replay/manifest.json \
  --output build/rtlgraph-vpu/runs/unary-exact-replay/vpu-events.json
```

## Binary and reduction probes

Each original measured region contains exactly one VPU instruction followed by `DELAY`. Neither program offers operation-reordering freedom within its original regions. Shortening a delay requires a validated completion boundary and must not move work outside the measured region. The [explicit probe contract](../scripts/rtlgraph_vpu_probes.py#L14-L85) accepts only the original encoded programs; it does not generate optimized variants.

| Probe | Timed operations, in order | Original numerical checks |
| --- | --- | --- |
| `perf_vpu_binary` | Two `VLI.ALL`s; `VADD.BF16`, `VSUB.BF16`, `VMUL.BF16`, `VMIN.BF16`, `VMAX.BF16` | First BF16 element is 6, 2, 8, 2, 4 respectively for input constants 4 and 2 |
| `perf_vpu_reduction` | Two `VLI.ALL`s; `VREDSUM.BF16`, `VREDMIN.BF16`, `VREDMAX.BF16`; corresponding three `.ROW.BF16` operations | First BF16 element is 256, 4, 4, 128, 4, 4 respectively for a constant input of 4 |

After each operation the program performs `VSTORE`, loads the first BF16 element with `LHU`, and uses `BNE` against the expected constant. The cumulative `DBG1` value sums the individual CSR windows; it excludes stores, loads, comparison branches, and other instructions between them. These are numerical spot checks, not full-tensor goldens or merely utilization thresholds.

`DBG0=1` alone is ambiguous: the binary probe's first failure and the reduction probe's second failure can also write one. The [trace checker](../scripts/rtlgraph_vpu_probes.py#L88-L145) instead binds every fired instruction word and PC through the exact success-path `DBG0`. Every comparison branch must fall through; every timing window must match its original PC pair; and `DBG1` must equal the sum of observed CSR differences. The runner must additionally check successful process termination and eventual halted/`ECALL` status. Atlas branch displacements are twice the word offset in the encoded B immediate, which the contract checks explicitly.

Passing these checks establishes five or six observed numerical spot checks, respectively. It does not validate the remaining result elements, all possible operand values, or universal VPU timing. Keep these reports distinct from the eleven full-output golden-backed corpus entries.

Both originals now have passing, independently rechecked captures. The [binary observation](../build/rtlgraph-vpu/runs/binary_probe_1/probe-observation.json) records seven CSR windows of 65, 65, 66, 66, 66, 66, and 66 cycles, totaling **460**, with all five numerical checks reaching the correct success PC. The [reduction observation](../build/rtlgraph-vpu/runs/reduction_probe_1/probe-observation.json) records eight windows of 65, 65, 130, 130, 130, 39, 34, and 34 cycles, totaling **627**, with all six checks passing. Host `DBG1` matches the waveform sum in both cases. First issue through successful `DBG0` spans 709 and 926 clock edges, respectively. These are original-baseline measurements; no binary/reduction optimization has been performed.

The adapter tests exercise wrapper preservation, interleaved-transfer rejection, immediate wait requirements, and scalar pseudo-instruction expansion. Probe tests reject a failure whose `DBG0` equals one, skipped branches, changed instructions, incomplete execution, altered CSR reads, and incorrect cumulative timing.
