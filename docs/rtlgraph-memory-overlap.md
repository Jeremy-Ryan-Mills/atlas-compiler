# Unary memory/compute overlap and controlled replay

This experiment extends scheduling across the timed `VLOAD`/compute/`VSTORE` boundaries in handwritten `perf_unary`. It retains all sixteen VPU operations, six loads, six stores, scalar address setup, operand identities, input/output fixtures, outer DMA commands and waits, and the original two cycle-counter locations. The original hardware and baremetal sources remain unchanged. New compiler invocations use `build/rtlgraph-no-dma-waits/atlas-opt`, rebuilt after removing automatic DMA-wait insertion from this branch's history.

## Measured result

The memory-overlap comparison (`build/rtlgraph-memory/overlap-comparison-1.json`) and previous-search control (`build/rtlgraph-memory/exact-comparison-1.json`) use the same fixed 128-word host. All three runs pass all 1,536 golden words and the independent LSU/VPU row monitors.

| Atlas interval | Handwritten | Previous compute-only search | Memory-overlap schedule |
| --- | ---: | ---: | ---: |
| Original CSR counter window | 960 | 952 | **797** |
| First issue through post-DMA `DBG0` | 10,181 | 10,181 | **10,030** |
| Before the starting counter | 4,673 | 4,673 | 4,673 |
| After the ending counter through `DBG0` | 4,547 | 4,555 | 4,559 |

The overlap candidate saves **163 counter cycles (16.98%)** and **151 edges through final DMA completion (1.48%)**. All three begin at global edge 57,931, and their complete setup issue gaps match. Its 12 extra suffix edges partly offset the timed-window gain. The previous search's eight saved counter cycles are exactly offset by eight additional suffix edges; it has no completion-time gain under this control. The phase reports (`build/rtlgraph-memory/overlap_1-phases.json`) retain each changed `DMA.WAIT` interval.

The handwritten (`build/rtlgraph-memory/runs/original_1/lsu-events-1.json`), search (`build/rtlgraph-memory/runs/exact_1/lsu-events-1.json`), and overlap (`build/rtlgraph-memory/runs/overlap_1/lsu-events-1.json`) row reports account for 36 LSU and 48 VPU commands. Every LSU transfer reads at ages 1–32, receives responses at 2–33, writes at 3–34, and is first not busy at age 35, matching the inherited model. Handwritten and compute-only schedules have no LSU/VPU overlap. The new schedule has 277 edges with outstanding LSU work and a VPU row access, including **255 edges with simultaneous LSU and VPU physical MREG accesses**, without an observed logical-register, physical-port, or VMEM-bank collision.

A second paired comparison (`build/rtlgraph-memory/overlap-83-comparison-1.json`) uses an unpadded 83-word host buffer: both the handwritten and overlap programs contain exactly 83 words. It changes host layout/programming work relative to the 128-word experiment while holding those properties fixed within the pair. The same candidate again improves **960→797 counter cycles**, and completion improves **9,975→9,807 edges**, saving **168 edges (1.68%)**. Both first issue at global edge 56,097; setup takes 4,498 edges in each, and the suffix changes 4,516→4,511. Both full goldens and row monitors pass, again observing 255 simultaneous LSU/VPU MREG-access edges in the candidate. These two controlled launch conditions support a repeatable measured gain; they are not a sweep of all memory-system states or a universal performance guarantee.

The combined evidence index (`build/rtlgraph-memory/results-1.json`) records five successful RTL replays, **7,680 golden-word comparisons, 60 LSU commands, 80 VPU commands, and 98 passing focused tests**. The earlier uncontrolled comparison also passes revalidation with the updated completion/comparison tools, retaining its original regression.

## Why the replay harness changed

The [earlier unary comparison](rtlgraph-vpu-kernels.md#measured-unary-search-candidate) saved eight counter cycles but added 382 edges through final DMA completion. The phase attribution (`build/rtlgraph-vpu/unary-phase-attribution-1.json`) independently binds all executed instruction words and matches the encoded setup/suffix. Every extra outer-window edge occurs in a `DMA.WAIT` issue interval: setup changes by 84 + 111 = 195 edges; writeback changes by 111 + 75 + 9 = 195. Other outer instruction gaps match. The optimized run's first Atlas issue also starts 3,527 global edges later. Those old captures do not reveal the underlying bus-level cause.

The [fixed-host control](../scripts/rtlgraph_replay_control.py#L26-L63) compiles one host with a 128-word `volatile const` Atlas program array and patches only that array in each candidate ELF. The host instruction bytes, golden data, code/data addresses, program upload/readback length, simulator, DRAM configuration, and seed are held fixed. Volatile loads prevent the C compiler from embedding instruction values in host code. Zero padding follows the sole terminal `ECALL` and is never executed by Atlas. It is part of the host buffer, not an added kernel delay.

The [independent audit](../scripts/rtlgraph_replay_control.py#L163-L212) reassembles the original source, checks generated C against those words, reconstructs the controlled C, validates the ELF object/section/load-segment bounds, and verifies every byte outside the program array against the template. Completion verification and paired comparisons require the same audited control descriptor. This controls host layout and programming work; it does not force identical DRAM state after differently timed Atlas execution.

## Scheduling change and hardware evidence

The [memory-window adapter](../scripts/rtlgraph_memory_schedule.py#L75-L191) admits the existing narrow load/compute/store source shape, reconstructs the `LI`-defined entry addresses, requires setup DMA waits, and passes the entire timed body to the existing scheduler. A separate compiler block seeds entry scalar values; those seed instructions never enter the real kernel. The adapter verifies the encoded non-idle instruction multiset and retains the outer source verbatim. The compiler's completion sentinel drains all scheduled LSU/VPU work before the original ending counter.

For unary, the prepared critical-priority candidate (`build/rtlgraph-memory/candidates-2/memory_critical.S`) begins A's `VSQUARE.BF16` at body age 70 instead of 210, while the independent B/C registers are still loading. It starts storing completed C at age 527 while A/B continue computing. The static dispatch window is 798 edges, versus 961 handwritten and 953 for the previous compute-only search. These counts include scalar pseudo-instruction expansion; the standalone compiler log also includes discarded entry-seeding instructions and is not the measured window.

The [LSU analysis](rtlgraph-lsu.md) extracts independent local load/store request and busy functions, registered response controls, active reservations, and AtlasCore connections from typed CIRCT. Logical register reservations still prohibit using partially loaded operands or storing a still-written destination. The candidate overlaps work on independent registers, retaining the existing model's numerical timing and physical-bank constraints. There is no new shortened LSU/VPU latency or general machine-schema extension in this experiment.

The 100-signal capture includes LSU and VPU commands, physical row requests, responses, writes, reservations, scalar/CSR events, deterministic LSU VMEM requests, and DMA grants. The monitor reconstructs transactions from accepted commands and ordered row addresses, measures their ages, and checks logical/physical conflicts independently of the proposed schedule. Full-output golden checks validate payloads. The typed checks are local control proofs; the measured row streams are finite observations, not a universal instruction-scheduling safety proof.

These facts are additional evidence toward the canonical access/resource model in [RTL_GRAPH_PLAN.md](../.agents/notes/RTL_GRAPH_PLAN.md). A general model loader and the optional Merlin adapter remain future work; the new LSU report is not yet a Merlin contract export.

## Reproduce

Use the existing environment setup and licensed simulator described in the [replay guide](rtlgraph-mxu1.md#replay). No Chipyard Make, Mill, elaboration, or simulator rebuild is required. The underlying configuration remains `chipyard.EE290SimConfig`, as selected in the original baremetal Makefile/README.

```sh
atlas_baremetal=/tools/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/baremetal
export LD_LIBRARY_PATH=/tools/C/reednicolas/ee194-sp26-chipyard/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
python3 -B scripts/rtlgraph_memory_schedule.py \
  --source build/rtlgraph-corpus/originals-1/perf_unary.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-no-dma-waits/atlas-opt \
  --output build/rtlgraph-memory/new-candidates

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-corpus/originals-1/perf_unary.S \
  --golden-json "$atlas_baremetal/generators/perf_unary.json" \
  --control-program-words 128 --capture-lsu \
  --output build/rtlgraph-memory/new-template
```

For each variant, select its unchanged source assembly and use `--control-manifest build/rtlgraph-memory/new-template/manifest.json --capture-lsu --run` with a fresh output directory. Keep at most three licensed simulator processes running concurrently. The replay validates all 1,536 existing golden words; then run `rtlgraph_lsu_trace.py --manifest ... --output ...` for row/overlap evidence, `rtlgraph_phases.py --baseline ... --candidate ... --output ...` for outer-phase attribution, and `rtlgraph_compare.py` with the corresponding prepared candidate manifest for the complete comparison.

To reproduce the second control, prepare another template with `--control-program-words 83` and replay only handwritten and memory-overlap assemblies against that template. The earlier compute-only search has 85 words and is intentionally rejected by this smaller control. Compare within each template; the two templates themselves are different host executables.

The reported completion interval starts at the first Atlas instruction and ends at `DBG0` after the final `DMA.WAIT`. It excludes host preload, IMEM programming, and golden comparison time. The capture does not identify the exact `ECALL` halt edge; the host log independently checks eventual halted status. Cached simulator source-to-binary linkage remains unverified, even though the recorded saved FIRRTL matches fresh elaboration.
