# Scheduling the performance corpus

This milestone extends hardware-model extraction to MXU0 and VPU and uses the existing fourteen `perf_*.S` programs to exercise the result. The [baseline guide](rtlgraph-corpus.md) distinguishes the eleven programs with output goldens from three probes needing dedicated validation. The [MXU0 projection](rtlgraph-mxu0.md) contributes a new resource correction; [VPU extraction](rtlgraph-vpu.md) currently corroborates existing resource rules without changing numerical timing.

The CIRCT artifact represents the selected hardware configuration, `chipyard.EE290SimConfig`. It is extracted once for that pinned hardware, then the compiler instantiates its rules for each kernel's instructions and operands. Changing a kernel does not itself require elaborating the hardware again. Changing relevant hardware requires regenerating and validating the evidence.

## Candidate construction and attribution

The [adapter](../scripts/rtlgraph_schedule.py#L28-L143) accepts the register-only MXU0, MXU1, dual-MXU, and selected VPU bodies between two cycle-counter reads. It handles both `CSRR` and read-only `CSRRS`, both MXU operand conventions, `VLI.ALL`, and the unary operations needed by normalization kernels. It verifies the complete original assembler roundtrip, preserves the non-idle encoded operation/operand multiset, and keeps setup, counter reads, and output-writeback instructions unchanged. Control flow and address-relative instructions are rejected. Timed scalar or memory operations, including `perf_unary`'s `VLOAD`/`VSTORE` and address setup, require a separate entry-state contract and are currently rejected.

The adapter prepares critical-priority and input-priority schedules, with built-in and applicable partial profiles. It records identical encoded candidates so they need not be replayed twice. Every distinct replay uses the original golden fixture. Profiles can compose across MXUs only with identical hardware-IR hashes; MXU0's schema cannot inject an MXU1 first-write age. Other timing, acceptance, logical reservations, ports, and capacities remain inherited unless separately documented as extracted.

A compiler-only `ECALL` sentinel forces modeled completion before the ending counter. The sentinel is removed, while its preceding drain stays **inside** the measured window. The original matrix-only kernels end their counters immediately after final pop issue, so their handwritten windows omit some completion work that candidates include. Report that difference alongside first-instruction-to-`DBG0` and first-counter-to-`DBG0` intervals; do not call a smaller modeled window a hardware speedup. Whole-kernel timing also includes memory-environment variation.

The [comparison helper](../scripts/rtlgraph_compare.py#L22-L111) rechecks golden results and full scalar execution/CSR binding, then requires identical setup/suffix encodings, non-idle operations, golden fixture, simulator/runtime hashes, and normalized simulator settings including the random seed. It attributes a profile contribution only against a built-in run with the same priority. Captures bind every fired scalar word and PC through successful `DBG0`, after the final `DMA.WAIT`. They do not capture the exact `ECALL` halt edge.

## Measured results

The [final original-baseline report](../build/rtlgraph-corpus/baseline-results-2.json) verifies all eleven golden-backed originals and their complete scalar/CSR/`DBG0` traces. Three special probes remain unvalidated; `perf_unary` has a passing baseline but no scheduling candidate. The original source headers are preserved in generated assembly, so historical header timing comments are superseded by the measured reports.

The [consolidated results](../build/rtlgraph-corpus/scheduling-results-1.json) record **19 new successful RTL replays**, including eleven generated candidates across seven additional kernels. Replays ran with at most three VCS jobs at once. All 241 Python tests and six C++ test targets pass. The joined comparisons include [MXU0 fused attention](../build/rtlgraph-corpus/comparisons/fused_mxu0-1.json), [RMSNorm/softmax](../build/rtlgraph-corpus/comparisons/rmsnorm_softmax-1.json), [softmax](../build/rtlgraph-corpus/comparisons/softmax-1.json), and [layer normalization](../build/rtlgraph-corpus/comparisons/layernorm-1.json). Each checks unchanged operations/setup/suffix, identical fixtures/runtime settings, functional goldens, and exact scalar execution.

| Kernel | Handwritten CSR cycles | Built-in scheduler | Partial RTL profile | Interpretation |
| --- | ---: | ---: | ---: | --- |
| MXU0 fused attention | 3,430 | 2,158 | 2,103 | Critical priority held fixed; the new MXU0 field contributes 55 cycles |
| Dual-MXU 128×128×128 | 1,099 | 1,189 | 1,095 | Input priority held fixed; combined MXU0/MXU1 profiles contribute 94 cycles |
| MXU0 64×64×64 | 291 | 355 | 323 | Profile saves 32 against built-in; candidate window includes additional completion work |
| MXU0 64×64×128 | 547 | 611 | 579 | Same 32-cycle profile correction and differing completion boundary |
| RMSNorm/softmax | 706 | 631 | — | 75 cycles from existing scheduling; VPU guards corroborated, numerical timing inherited |
| Softmax | 337 | 337 | — | No measured window improvement |
| Layer normalization | 605 | 604 | — | One-cycle window improvement; wider completion regresses in this execution |

The remaining paired reports cover [dual-MXU](../build/rtlgraph-corpus/comparisons/dual-1.json), [MXU0 K64](../build/rtlgraph-corpus/comparisons/mxu0_k64-1.json), and [MXU0 K128](../build/rtlgraph-corpus/comparisons/mxu0_k128-1.json). Dual-MXU passes 4,096 golden words in every variant. Its four-cycle improvement over handwritten includes candidate completion draining inside the counter window; the 94-cycle controlled gain is from the combined profiles, without isolating each engine's individual contribution. MXU0 K64/K128 do not demonstrate a faster handwritten schedule: the extra 32 candidate CSR cycles reflect the completion boundary, and the profile only recovers the built-in model's unnecessary read-port delay.

The profiled MXU0 fused schedule saves **1,327 cycles (38.69%)** against handwritten in the original measured compute region. Its [event report](../build/rtlgraph-corpus/runs/fused_attention_mxu0_profile_critical_1/mxu0-observation.json) observes three BF16-pop→overwrite pairs on accumulator zero, each with issue gap one and 31 cycles of overlapping pop reads and overwrite operand requests. All eight computes write rows at observed ages 63–94. The original and built-in control have no such overlap. This is a finite witnessed use of the independently extracted read-port correction, not a universal proof.

Full completion must be read separately. First Atlas issue→`DBG0` spans are **25,570 / 23,978 / 24,038** edges for handwritten / built-in / profile MXU0 fused attention. Thus the profile is 1,532 edges (5.99%) faster than handwritten in this execution but **60 edges slower than the built-in control**, despite its 55-cycle compute advantage. RMSNorm saves 75 CSR cycles but only seven first-issue→`DBG0` edges; layer normalization saves one CSR cycle while that wider interval grows by 66. Dual-MXU first-issue→`DBG0` spans are 43,120 / 42,988 / 43,151 edges, so the profiled run is 31 edges slower than handwritten despite its four-cycle CSR gain. MXU0 K64 spans are 12,086 / 12,172 / 12,106; K128 spans are 19,428 / 19,219 / 19,402. The reports retain these regressions. Memory wait and launch-phase differences prevent interpreting each fixed-window gain as an equal whole-kernel gain.

## Reproduce a paired experiment

Use a new directory for each output. The [existing environment and replay setup](rtlgraph-mxu1.md#replay) supplies the toolchain and licensed cached simulator.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/riscv-tools/lib:$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3 scripts/rtlgraph_schedule.py \
  --source build/rtlgraph-corpus/originals-1/perf_fused_attention_mxu0.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-compiler/atlas-opt \
  --mxu0-profile build/rtlgraph-mxu0/profile-1/atlas-mxu0.profile \
  --output build/rtlgraph-corpus/candidates-replay/perf_fused_attention_mxu0

python3 scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-corpus/candidates-replay/perf_fused_attention_mxu0/profile_critical.S \
  --golden-json "$atlas_baremetal/generators/perf_fused_attention_mxu0.json" \
  --output build/rtlgraph-corpus/runs/fused_mxu0_profile_replay \
  --run --capture-mxu0
```

Repeat for the original and built-in control. For dual-MXU kernels, supply `--mxu1-profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile` to candidate preparation as well. VPU-only kernels use the adapter without either profile flag. Keep at most three VCS jobs active.

The optional `--capture-mxu0` path records a separate 42-signal map. Its [checker](../scripts/rtlgraph_mxu0_trace.py#L44-L226) binds scalar instructions to accepted MXU0 commands, attributes compute/pop row streams, checks accumulator-read ownership, and reports observed same-accumulator pop/overwrite overlap. It measures write ages rather than assuming the MXU1 pipeline. Push data, MREG response routing, arithmetic/visibility, and all cross-engine conflicts remain outside this narrow monitor.

```sh
python3 scripts/rtlgraph_mxu0_trace.py \
  --manifest build/rtlgraph-corpus/runs/fused_mxu0_profile_replay/manifest.json \
  --output build/rtlgraph-corpus/runs/fused_mxu0_profile_replay/mxu0-observation.json \
  --require-overlap
```

Use `--require-overlap` for a case intentionally exercising that behavior; a baseline without overlap can still be a valid program. Functional and timing failures are retained as failed evidence rather than counted as performance results.

The [completed MXU0 audit aggregate](../build/rtlgraph-corpus/mxu0-observations-1.json) binds eleven captures, 502 accepted commands, 184 computes, 98 pops, and nineteen pop/overwrite overlap pairs to their original replay/golden artifacts. All checks passed without weakening the monitor. Every observed compute first writes at age 63; this corroborates the inherited value only for these executions.

## Evidence limits and next extraction work

These experiments keep operations and operands fixed: no register renaming, tiling, algorithm changes, or kernel-specific hardware re-elaboration. Single successful executions do not prove correctness for all data or interference. VPU resource guards are exhaustively checked as local Boolean functions, but row ages and state reachability are not proven. The cached simulator's saved FIRRTL matches fresh elaboration, while its source-to-binary build linkage remains unverified.

The next model work is a full VPU launch/read/write/release timing slice and broader MXU0 temporal properties. A richer canonical machine description, RTG/IIG views, and the optional Merlin adapter remain open. Merlin is an optional reference and future consumer, not a new build dependency; [the plan](../.agents/notes/RTL_GRAPH_PLAN.md) retains Radiance/Muon and Vortex as later portability experiments.
