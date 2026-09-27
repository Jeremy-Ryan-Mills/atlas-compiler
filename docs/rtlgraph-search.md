# Fixed-operation scheduling search

The handwritten K64 and K128 schedules are optimal under the selected compiler model for their existing instruction identities, operands, and dependency graphs. An exhaustive search found no earlier last issue or last modeled resource use. This is a model result, not a proof that all RTL programs implementing these computations have the same lower bound.

The [recorded search manifest](../build/rtlgraph-search/run-1/manifest.json) binds the source bodies, frozen partial profile, search source/executable, linked compiler library, model sources, commands, and reports by SHA-256. The profile is the same `profile-k64-2/atlas-mxu1.profile` used by the [kernel experiments](rtlgraph-kernels.md) and [bank witnesses](rtlgraph-banks.md).

| Body | Fixed operations | Earliest last issue | Earliest last modeled resource use | Exhausted smaller horizons | Search nodes per objective |
| --- | ---: | ---: | ---: | --- | ---: |
| K64 | 8 matmuls, 4 pushes, 4 pops | 289 | 321 | 288 / 320 | 47 |
| K128 | 16 matmuls, 8 pushes, 4 pops | 545 | 577 | 544 / 576 | 2,999 |

All ages in this table start at the first measured-body instruction, age zero. Last resource-use ages 321 and 577 correspond to inclusive intervals of 322 and 578 cycles. The existing cycle-read brackets have raw issue-edge spans 291 and 547, with observed CSR deltas 290 and 546. The benchmark ends after the final `VMATPOP.FP8.MXU1` issue; the completion objective separately includes its remaining modeled activity. Neither objective includes setup or DRAM writeback.

The search retained the already legal schedules at these bounds, so it produced no faster MXU-only candidate requiring another RTL replay. The prior functional replays validate those particular upper-bound schedules; they do not establish the lower bounds. Reports for [K64 issue](../build/rtlgraph-search/run-1/k64_issue.json), [K64 completion](../build/rtlgraph-search/run-1/k64_completion.json), [K128 issue](../build/rtlgraph-search/run-1/k128_issue.json), and [K128 completion](../build/rtlgraph-search/run-1/k128_completion.json) contain every instruction, dependency, pairwise spacing constraint, and resulting issue age.

## Search method and scope

The standalone [search helper](../scripts/rtlgraph_schedule_search.cpp#L172-L244) links the existing compiler library. It calls `buildGraph` and obtains the actual `Footprint` for each instruction; it does not reproduce the timing model in another language. `DELAY` and no-op fillers are removed before searching. The admissible input is one unlabeled body of at most 40 instructions containing only `VMATPUSH.W.MXU1`, `VMATMUL.MXU1`, `VMATMUL.ACC.MXU1`, and `VMATPOP.FP8.MXU1` in compiler syntax. It preserves every instruction's operands and all original dependency edges, while considering different legal operation orders.

For each ordered pair, [reservation-table queries](../scripts/rtlgraph_schedule_search.cpp#L130-L140) find its forbidden issue distances. A pair is represented by a disjunction: either the first instruction precedes the second by its required distance, or the reverse order satisfies its own distance. Every pair also enforces single instruction issue. The helper rejects a nonmonotone spacing pattern instead of replacing it with a potentially incorrect minimum distance.

One alternative-port reduction needs an explicit restriction. In this instruction subset, only weight pushes can use P1. Each push reserves the one-capacity weight stream at ages 1 through 32, which separates pushes enough that their preferred P1 reservations at ages 0 through 31 cannot overlap. Thus allowing the push's alternate P0 cannot invalidate the pairwise lower bound. The [admission checks](../scripts/rtlgraph_schedule_search.cpp#L99-L129) verify that reservation shape and reject other P1 users or altered alternatives. These restrictions are not a claim about all instruction families.

The [search](../scripts/rtlgraph_schedule_search.cpp#L30-L97) maintains longest-path constraints, derives earliest/latest ages within a proposed horizon, forces impossible ordering choices, and branches on the remaining disjunctions. It tests one cycle below the best known schedule. Exhausting every branch proves that horizon infeasible for this pairwise relaxation, and therefore for the complete model. Omitted multi-instruction capacity constraints can only weaken that lower bound. Each proposed solution is independently checked against every graph edge, the full `ReservationTable`, and the compiler simulator before export. A feasible relaxed solution that fails those checks causes an error; it is not exported as a legal schedule. A timeout reports `search_incomplete`, never infeasibility or optimality.

The initial upper bound is the better of the existing critical-height and input-priority schedulers. For these two bodies it already meets the exhausted lower bound. The tool leaves the compiler's normal scheduler and default model unchanged.

## What the hardware audit adds

The model's 32-cycle accumulator-read exclusion is material to these bounds. The [accumulator implementation](../../src/main/scala/atlas/mxu/AccumulationBuffers.scala#L79-L98) asserts against simultaneous compute and store reads of the same accumulator buffer. Its [read selection and memory access](../../src/main/scala/atlas/mxu/AccumulationBuffers.scala#L147-L168) merge those requests onto one read port per buffer. Thus a `VMATMUL.ACC.MXU1` read stream and `VMATPOP.FP8.MXU1` cannot obtain extra bandwidth merely because they are distinct operations. The partial profile removes the erroneous read hold for overwrite `VMATMUL.MXU1`, which performs no accumulator read; it retains the accumulating operation's hold.

The separate [typed audit](../build/rtlgraph-bottleneck/audit-2/bottleneck.json), produced by the [resource extractor](../scripts/rtlgraph_mxu1_bottleneck.py#L85-L183), checks two 32-row by 512-bit accumulator memories with one read and one write port each, 16,384 read-enable/address assignments per bank, and six exact wrapper connections. It also checks 128 assignments for each of three sequencer boundary predicates and 1,024 assignments for the shared weight-write output. Seven focused tests reject altered cutpoint widths, boundaries, state/feedback, memory geometry/latency/ports, wiring, and mux priorities. Those facts support particular modeled bottlenecks. They do not turn this fixed-model search into a temporal RTL proof: the complete sequencer transition behavior, all scheduler assumptions, and source-to-cached-executable lineage remain separate evidence obligations. Register renaming, changed tile order or algorithm, different instruction forms, and other configurations are outside this search.

## Reproduce and test

Build from the compiler repository after building its current `libatlas.a`:

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_cxx="$atlas_hw/.conda-env/bin/c++"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$atlas_cxx" -std=c++20 -O2 -Wall -Wextra -Wpedantic -Isrc \
  scripts/rtlgraph_schedule_search.cpp build/rtlgraph-compiler/libatlas.a \
  -o build/rtlgraph-compiler/rtlgraph-schedule-search
"$atlas_cxx" -std=c++20 -O2 -Wall -Wextra -Wpedantic -Isrc \
  scripts/tests/rtlgraph_schedule_search_tests.cpp build/rtlgraph-compiler/libatlas.a \
  -o build/rtlgraph-compiler/rtlgraph-schedule-search-tests
build/rtlgraph-compiler/rtlgraph-schedule-search-tests
```

The [focused tests](../scripts/tests/rtlgraph_schedule_search_tests.cpp#L1-L91) compare 3,600 solver outcomes with exhaustive integer-time enumeration and 6,591 three-operation placements with the real reservation table. They also reject an unsupported alternative-port contract and a nonmonotone conflict, and distinguish timeout from infeasibility. These tests passed for the recorded run.

Use a new output directory and the original prepared bodies from the [kernel adapter](rtlgraph-kernels.md):

```sh
mkdir -p build/rtlgraph-search/replay
build/rtlgraph-compiler/rtlgraph-schedule-search \
  --source build/rtlgraph-kernel/original.compiler.S \
  --profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile \
  --objective issue --seconds 60 \
  --output build/rtlgraph-search/replay/k64_issue.compiler.S \
  --report build/rtlgraph-search/replay/k64_issue.json
```

For K128, use `build/rtlgraph-kernel/k128/original.compiler.S`. For completion, select `--objective completion` and distinct output filenames. The helper requires new output files and records its tested horizon and whether the search finished. Any future faster candidate must be spliced into the original setup/writeback, checked against the original assembler and golden data, and replayed on RTL before claiming a hardware improvement.

Reproduce the separate hardware resource audit with the existing S0 artifact and typed exporter:

```sh
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/riscv-tools/lib:$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3 scripts/rtlgraph_mxu1_bottleneck.py \
  --s0-manifest "$atlas_hw/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json" \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-bottleneck/audit_replay
python3 scripts/tests/test_rtlgraph_mxu1_bottleneck.py build/rtlgraph-s0/query/rtlgraph_export
```
