# RTL-graph review checkpoint

This checkpoint refreshes kernel validation, exercises automatic DMA waits, and consolidates the Atlas compiler tooling. Merlin implementation, the presentation, and the authorized external simulator rebuild remain deferred.

## Research audit

The supplied Gemini research describes `19e3826`. Its useful topics need these corrections against the current implementation:

| Research claim | Supported interpretation |
| --- | --- |
| Optimal interleaving and maximum parallelism | The [list scheduler](../src/passes/schedule.cpp) applies a heuristic under its model. Only measured whole-kernel comparisons establish observed gains. |
| Pipeline counting establishes exact timing | [Connected analysis](../scripts/rtlgraph_instruction_timing.py) follows state, hierarchy, valid signals, and guards. [XLU evidence](rtlgraph-xlu-connected.md) demonstrates numerically correct overlap prohibited by frontend assertions. |
| DMA completion bounds | [The DMA profile](../profiles/EE290SimConfig/dma/atlas-dma.profile) requires explicit `DMA.WAIT`; external service time has no established finite correctness bound. |
| Opcode encodings/register organization are not extractable | RTL encodes decode and storage facts; [MREG evidence](../profiles/EE290SimConfig/xlu/connected.json) already recovers bank/tracker relationships. Authored specs still define intended semantics, ABI, numerical behavior, and discrepancy criteria. |
| Dynamic loops require branch prediction/host fallback | Branch prediction is unnecessary for loops. [CFG handling](../src/core/blocks.cpp) and [DMA tests](../tests/dma_wait_tests.cpp) cover known joins/backedges; unknown targets and DMA delay slots remain excluded. |
| Timing determines exact PyTorch lowerability | Supported lowering also needs shape/layout/dtype rules, numerical semantics, ABI support, and end-to-end tests. These profiles establish no PyTorch operator set. |
| `atlas-opt` performs register allocation | [The input contract](rtlgraph-contract.md) requires assigned registers. This feature schedules instructions; it does not add generic allocation or tiling. |
| Deployed Merlin integration and a system roofline | [The handoff](rtlgraph-contract.md) is available for a future adapter. A measured optimized reference is not a proven performance ceiling. |

A later presentation should use real pinned CIRCT IR, event traces, and profile output instead of the research's unchecked illustrative MLIR. Distinguish Chisel elaboration, FIRRTL lowering, core-dialect queries, compiler consumption, and independent validation.

## Current validation

The [kernel regression report](rtlgraph-perf-validation.md) records 29 passing executions and 35,328 checked words across eleven original kernels and additional controlled comparisons. All 13 CTest suites pass. [Automatic-wait validation](rtlgraph-dma-integration.md) is recorded separately. Cached RTL remains finite evidence with unverified source-to-executable linkage. Gains depend on the selected heuristic and environment: layernorm is 27 edges slower with a 64-word host control, but 15 faster with the historical 128-word control; input-order priority does not remove the smaller-control regression.

Three other `perf_*.S` sources lacked full goldens. The [fixture generator](../scripts/rtlgraph_perf_fixtures.py) preserves VPU operations and branches while capturing both output registers after each operation. For single-matmul it substitutes exact FP8 constants and appends both BF16 result pops/writeback. Goldens use exact arithmetic, not simulator output. Initial instrumentation incorrectly mixed word and byte addressing; correcting the harness resolved that failed probe without changing RTL.

[Supplemental evidence](../profiles/EE290SimConfig/validation/perf-extra.json) records six passing executions and 13,312 checked words. Final compiler `43de1db` regenerated assembly identical to the executed candidates, with 3/3/4 inserted waits respectively.

| Instrumented probe | Words checked per run | Original edges | Scheduled edges | Timing scope |
| --- | ---: | ---: | ---: | --- |
| `perf_vpu_binary` | 2,560 | 8,353 | 8,184 | Diagnostic only; different hosts |
| `perf_vpu_reduction` | 3,072 | 10,090 | 9,905 | Diagnostic only; different hosts |
| `perf_mm_single` | 1,024 | 7,520 | 7,170 | Same host/launch edge; 350 edges saved (4.65%) |

These are augmented validation workloads, not performance claims for the three unmodified sources. All issued PC/word pairs match assembled programs; each VPU conditional branch is observed once, alongside complete output checks. The matmul comparison includes added observation/writeback work.

## Additional findings and follow-up

The VPU probes exposed a compiler interface gap: the timing simulator cannot resolve branches depending on scalar-loaded numerical data. Explicit `--validation static` now checks block dependencies, resources, and fixed-engine drains, plus DMA lifetimes across reachable CFG joins/backedges. Unknown successors, unsupported delay slots, and unguarded completion remain errors. Numerical execution and dynamic latency are not evaluated; dynamic validation stays the default.

Remaining Atlas limits include broader frontend/interference and payload/visibility coverage, conservative CFG aliasing, and inherited rules. New scheduling relaxations require acceptance and visibility evidence as well as datapath timing.

For future [Merlin](https://github.com/ucb-bar/merlin), retain identities/scopes for Phase 0 discrepancies, consider an optional target scheduler/checker for Phase 1, and use measured references/resource diagnostics in Phase 2. A concrete spec/layout question: current column sum produces `64 × 4 = 256`, while pair-row sum produces `32 × 4 = 128`. Mapping these to PyTorch axes needs an authored layout/numerical contract. Phase 2 must also retain host capacity, environment, and launch identity: changing host programming can alter DRAM phase and the measured benefit of identical Atlas words. Agents can explore layouts, tiling, and fusion while compiler automation handles modeled hazards. Other targets need their own acceptance/stall semantics.

## Draft PR description

Title: `feat: derive Atlas scheduling profiles from RTL`

Add opt-in, source-bound `EE290SimConfig` instruction profiles to the existing Atlas scheduler, preserving row accesses, resource occupancy, and explicit DMA completion. Model-aware waits follow known branches/loops, and static CFG validation admits memory-dependent branches without pretending to simulate their numerical conditions. Connected XLU checks retain frontend reservations even where isolated overlap appears safe.

Validation combines compiler regressions, typed structural/control checks, independent RTL witnesses, and full-output kernel replays. Profiles remain partial; cached simulation is not a verified fresh build, and finite checks are not universal proofs. Measured results and limitations are linked above. Merlin integration is deferred.
