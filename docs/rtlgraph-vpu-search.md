# Fixed-operation VPU search

The VPU search finds a shorter schedule for `perf_unary` while retaining its sixteen instructions and operands. It also establishes that the existing built-in schedules for softmax, RMSNorm/softmax, and layer normalization are optimal under the current compiler model for their fixed operations and dependency graphs. Numerical VPU timing remains inherited from the compiler; these results do not establish a temporal RTL lower bound or a new CIRCT-derived latency.

| Kernel | Operations | Built-in last modeled use | Search last modeled use | Lower-bound justification |
| --- | ---: | ---: | ---: | --- |
| `perf_unary` | 16 | 653 | 527 | Search reaches the dependency-path bound after fourteen branch nodes |
| `perf_softmax` | 6 | 336 | 336 | Built-in schedule already meets the dependency-path bound |
| `perf_vec_rmsnorm_softmax` | 13 | 630 | 630 | Horizon 629 exhausted in 195 branch nodes |
| `perf_vec_layernorm_32x32` | 10 | 603 | 603 | Horizon 602 exhausted in one branch node |

Ages start at the first compute instruction, age zero, and include the final instruction's modeled resource use. They exclude the memory wrapper. For example, last-use age 527 is an inclusive span of 528 cycles. These are model results, not measured CSR deltas. The reports for unary (`build/rtlgraph-vpu-schedules/unary-exact-2/search.json`), softmax (`build/rtlgraph-vpu-schedules/perf_softmax-exact-2/search.json`), RMSNorm/softmax (`build/rtlgraph-vpu-schedules/perf_vec_rmsnorm_softmax-exact-2/search.json`), and layer normalization (`build/rtlgraph-vpu-schedules/perf_vec_layernorm_32x32-exact-2/search.json`) retain instruction identities, issue ages, dependencies, search status, and any exhausted horizon. A horizon value of `-1` means no smaller horizon was exhausted; unary and softmax instead meet the direct dependency bound.

The three non-unary outputs are encoding-identical to their existing `builtin_critical` candidates. Unary changes operation order while maintaining the original dependency edges. Its eight-operation chain issues every 66 model cycles, reaching the bound `7 * 66 + 65 = 527`; the other five- and three-operation chains fit around it. This keeps the longest chain progressing while respecting two VPU slots, operation compatibility, and MREG reservations.

The unary experiment manifest (`build/rtlgraph-vpu-schedules/unary-exact-2/manifest.json`) retains the original and built-in cases and adds an `exact` case for `rtlgraph_compare`. Its static encoded counter window is 953 issue cycles, compared with 961 handwritten and 1,079 built-in, before hardware stalls. Thus it predicts eight cycles saved against handwritten. The immutable `VLOAD`/`DELAY` prefix and `LI`/`VSTORE`/`DELAY` suffix stay inside the original counters. Modeled compute completion is drained before writeback. Static counts are not measured speedups; the completed RTL comparison below retains the wider completion interval.

The [completed RTL comparison](rtlgraph-vpu-kernels.md#measured-unary-search-candidate) measures unary at **960→952 CSR cycles**, with all 1,536 golden words and row events passing. First issue through `DBG0` regresses **10,061→10,443 edges**; there is no observed whole-kernel speedup. The measured gain inside the original counter window is from scheduling, with numerical VPU timing unchanged.

The subsequent [fixed-host control](rtlgraph-memory-overlap.md#measured-result) keeps both versions at 10,181 edges through completion: the same eight counter cycles are offset by eight suffix edges. A separate adapter then improves completion by admitting independent timed memory operations into the schedule. That broader memory/compute experiment does not change the fixed-operation VPU search results or their model-only optimality scope.

## Search contract

The [solver admission checks](../scripts/rtlgraph_vpu_search.cpp#L116-L143) accept one unlabeled body of at most forty VPU instructions, plus an optional `ECALL` sentinel. Delays and no-op fillers are stripped. Accesses must be known MREG accesses; scalar operations, memory operations, external-register accesses, alternate ports, and extra resources with capacity greater than one are rejected. Every instruction must be legal alone, and every ordered pair must have monotone legal spacing. A nonmonotone conflict is rejected rather than replaced with an unsafe minimum distance.

The [resource constraints](../scripts/rtlgraph_vpu_search.cpp#L144-L159) include ordered-pair spacing from the real `ReservationTable`, single issue, and explicit capacity-two clauses. Any three half-open VPU slot intervals must contain a disjoint pair. Each triple therefore contributes six possible orientations of a non-overlap constraint. Pair compatibility alone would miss three individually compatible operations competing for two slots; the first exploratory RMSNorm search exposed exactly this problem.

The [search](../scripts/rtlgraph_vpu_search.cpp#L33-L114) propagates longest-path bounds and branches on unresolved resource clauses. It begins with the better built-in critical/input-priority schedule, searches below that completion horizon, and stops when it reaches a dependency bound, exhausts the smaller horizon, or times out. Timeout remains `search_incomplete`, with a validated feasible schedule and a separate lower bound. Every exported candidate is [checked against all original dependency edges, the complete reservation table, and the compiler simulator](../scripts/rtlgraph_vpu_search.cpp#L179-L192).

The [runner](../scripts/rtlgraph_vpu_search.py#L24-L91) reconstructs the prepared body from the frozen original, invokes the solver, and uses the scheduling adapter to check assembler roundtrip and the unchanged encoded non-idle instruction multiset. It records hashes for the original inputs, solver source and executable, supplied library, current compiler sources, adapter, reports, and candidate. Those hashes do not by themselves prove executable-to-source/library build provenance; retain the build invocation separately. Neither the search nor the runner modifies the default compiler scheduler or timing model.

## Reproduce and test

Use the already built compiler library and the original checkout's C++ toolchain. This does not elaborate Chipyard or rebuild the simulator.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_cxx="$atlas_hw/.conda-env/bin/c++"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
mkdir -p build/rtlgraph-vpu-schedules
"$atlas_cxx" -std=c++20 -O2 -Wall -Wextra -Wpedantic -Isrc \
  scripts/rtlgraph_vpu_search.cpp build/rtlgraph-compiler/libatlas.a \
  -o build/rtlgraph-vpu-schedules/vpu-search
"$atlas_cxx" -std=c++20 -O2 -Wall -Wextra -Wpedantic -Isrc \
  scripts/tests/rtlgraph_vpu_search_tests.cpp build/rtlgraph-compiler/libatlas.a \
  -o build/rtlgraph-vpu-schedules/vpu-search-tests
build/rtlgraph-vpu-schedules/vpu-search-tests
python3 scripts/tests/test_rtlgraph_vpu_search.py \
  build/rtlgraph-vpu-schedules/vpu-search
```

The [solver tests](../scripts/tests/rtlgraph_vpu_search_tests.cpp#L1-L123) passed 1,800 comparisons against exhaustive integer placements and 3,993 comparisons against the complete reservation table. They also exercise three-way overload, unsupported capacity, nonmonotone conflicts, and timeout. Five [CLI tests](../scripts/tests/test_rtlgraph_vpu_search.py#L1-L92) check invalid budgets/options/instructions, output preservation, and timeout exporting a feasible seed without an optimality claim.

After preparing unary with `rtlgraph_schedule.py --preserve-memory-wrapper`, use a fresh search output directory:

```sh
python3 scripts/rtlgraph_vpu_search.py \
  --candidate-manifest build/rtlgraph-vpu-kernels/unary-candidates-1/manifest.json \
  --solver build/rtlgraph-vpu-schedules/vpu-search \
  --library build/rtlgraph-compiler/libatlas.a \
  --output build/rtlgraph-vpu-schedules/unary-replay \
  --seconds 60
```

For the three register-only kernels, use their manifests under `build/rtlgraph-corpus/candidates-1/` and separate output directories. The runner detects a preserved memory wrapper from its input manifest. Any new encoding needs a replay using the original golden fixture; identical encodings need not be replayed solely because the search found the same schedule again.
