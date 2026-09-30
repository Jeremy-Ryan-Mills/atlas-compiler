# Kernel regression

The regression compares the complete Atlas instruction stream against the handwritten kernel and a previously optimized stream, using all six checked-in `EE290SimConfig` profiles. The replay metric is the number of Atlas clock edges from the first accepted instruction to successful `DBG0` publication after all DMA channels become idle. Host programming, host output verification, simulator wall time, and raw host `mcycles` are excluded. `dbg1_cycles` remains a diagnostic because counter instructions can move between schedules.

The canonical [baremetal corpus](https://bwrcrepo.eecs.berkeley.edu/ee194-290c-sp26/sp26-atlas-acc) has 14 `perf_*.S` files. Eleven have existing nonempty external fixtures covering their output tensors. `perf_mm_single`, `perf_vpu_binary`, and `perf_vpu_reduction` need separate output instrumentation; results for instrumented probes must be distinguished from the original kernels.

[`rtlgraph_perf.py`](../scripts/rtlgraph_perf.py) uses the recorded cached simulator and checks its binary, runtime, toolchain, and DRAM configuration hashes. For a controlled comparison, it compiles one host program with a fixed-capacity Atlas instruction array. Candidate replays patch only that array in the same ELF. Padding follows the terminal `ECALL` and is not executed. The host code, golden data, ELF layout, and instruction-memory programming work remain identical, and the comparison also checks the Atlas launch edge. Variable DRAM behavior still prevents interpreting a finite execution as a universal latency bound.

For a native compiler candidate from a straight-line baremetal kernel, normalize only its unreachable suffix and unused labels, then schedule and check the final emitted stream. This adapter also checks encoded instruction preservation. Set `insert_waits=True` to remove handwritten waits and regenerate them with the selected model.

```python
from pathlib import Path
from rtlgraph_assembly import load_assembler, prepare, straight_line_prefix

baremetal = Path("/path/to/sp26-atlas-acc/baremetal")
assembler = baremetal / "assembler.py"
source = baremetal / "assembly/perf_unary.S"
Path("before.S").write_text(straight_line_prefix(source.read_text(), load_assembler(assembler)))
profiles = {role: Path(f"profiles/EE290SimConfig/{role}/atlas-{role}.profile")
            for role in ("dma", "lsu", "vpu", "xlu", "mxu0", "mxu1")}
prepare("before.S", assembler, "build/atlas-opt", "build/candidate",
        profiles=profiles, insert_waits=False)
```

Run the Python snippet with `PYTHONPATH=scripts`; its output is `build/candidate/candidate.S`. The following commands replay `before.S` and that candidate (named `after.S` below) against the same fixture:

```sh
source /tools/C/ee194-sp26/bwrc-env.sh
python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly before.S --golden-json fixture.json \
  --output build/replay-before --capacity 512
python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly after.S --golden-json fixture.json \
  --output build/replay-after \
  --control-manifest build/replay-before/manifest.json
```

The capacity must fit both programs and cannot exceed 1024 words. `--capacity 0` supports branch/loop witnesses without a fixed-host performance claim. `--witness-manifest` additionally checks the compiler, selected profile, generator, native input/output, baremetal input, and golden identities recorded by the DMA witness generator.

## Results

The [recorded regression](../profiles/EE290SimConfig/validation/perf-corpus.json) contains 29 actual RTL executions and 35,328 checked 32-bit output words. The current compiler (`43de1db`) emits exactly the previous combined-model instruction streams for all eleven default schedules; matching encoded streams share replay evidence rather than being counted as extra executions. A separate post-execution audit matched all 5,055 accepted instruction PCs and words to the assembled programs.

The table uses the historical fixed-host capacities. Every row compares one shared host and launch condition, with all six profiles selected and the default critical-path scheduling priority.

| Kernel | Handwritten edges | Current default edges | Saved |
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

These defaults are not always the fastest known schedules. Restored `--schedule-priority input` reproduces the dual-MXU stream that takes 41,093 edges, saving another 458. Selecting only the DMA profile reproduces the fused-MXU1 stream that takes 22,132 edges, four fewer than the combined model. Both alternative instruction streams were freshly replayed and reproduced by the final compiler. Profile coverage and scheduling priority are separate choices; neither guarantees an optimum.

**Layernorm regresses in a second controlled environment.** Changing the fixed host capacity from 128 words to 64 changes host programming/layout and memory-system conditions. The Atlas instruction words remain identical, and both sides still share their host within each pair:

| Kernel at 64-word capacity | Handwritten edges | Current default edges | Saved |
| --- | ---: | ---: | ---: |
| `perf_softmax.S` | 4,643 | 4,639 | 4 |
| `perf_vec_layernorm_32x32.S` | 4,803 | 4,830 | -27 |

Input priority also takes 4,830 edges for that layernorm case. At 128 words, the corresponding improvements are 54 and 15 edges. This is measured sensitivity to the launch environment, not a change between compiler versions: the previous and current combined-model streams are identical. Performance gains are conditional on the recorded execution; static resource timing does not establish a DRAM completion bound.

The [three supplemental probes](../profiles/EE290SimConfig/validation/perf-extra.json) cover the remaining corpus files with explicit full-output instrumentation. Their altered programs and fixtures are reported separately from the original eleven kernels.

These are finite numerical and execution checks. The cached simulator's source-to-binary build linkage remains unverified. A fresh simulator build is deferred. The checked-in profile identities preserve the selected hardware model for a future Merlin handoff, without implementing that integration.
