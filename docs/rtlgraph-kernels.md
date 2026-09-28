# Existing kernels and compiler integration

Use `perf_mm_mxu1_64x64x64.S` for the first scheduling comparison. It exercises four weight pushes, four overwriting `VMATMUL.MXU1` operations, four accumulating `VMATMUL.ACC.MXU1` operations, and four FP8 pops, with output checked against an existing DRAM golden fixture. Its [measured body](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L151-L207) is small enough to inspect instruction by instruction, while including back-to-back computation, weight-slot reuse, and overlapping pop/compute activity. The setup loads `m0`–`m7`; results occupy `m8`–`m11`, so this case does not exercise the `mN`/`mN+32` physical-bank aliases.

The simulation target remains `EE290SimConfig`, as specified by the [baremetal Makefile](../../baremetal/Makefile#L1-L28) and [manual baremetal flow](../../baremetal/README.md#L13-L36). Source links below refer to the enclosing Atlas generator checkout. Commands use the original checkout as a read-only input when those source directories are not initialized in the compiler worktree.

## Source audit catalog

Counts below are assembly instructions before `LI` expansion, excluding comments and labels. They describe inspected source, not successful simulation or a timing guarantee.

| Kernel | Instructions | Role and current adapter coverage |
| --- | ---: | --- |
| [`perf_mm_single.S`](../../baremetal/assembly/perf_mm_single.S#L90-L123) | 47 | Smaller busy-counter experiment, but uses both MXUs and does not pop results to DRAM for arithmetic comparison. Its drain comments disagree with its actual `DELAY 34`; do not treat comments as timing evidence. Rejected by the MXU1-only adapter. |
| [`perf_mm_mxu1_64x64x64.S`](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L148-L256) | 120 | Selected functional/performance witness. Adapter roundtrip reproduces all 129 assembled words exactly. |
| [`perf_mm_mxu1_64x64x128.S`](../../baremetal/assembly/perf_mm_mxu1_64x64x128.S#L249-L389) | 196 | Held-out MXU1 accumulation/reuse case. Adapter roundtrip reproduces all 211 original words; four RTL replays and two independent traces pass, as recorded below. |
| [`perf_mm_mxu0_64x64x64.S`](../../baremetal/assembly/perf_mm_mxu0_64x64x64.S#L171-L276) | 124 | Later comparison with the systolic engine. Requires MXU0 evidence and adapter support. |
| [`perf_mm_mxu0_64x64x128.S`](../../baremetal/assembly/perf_mm_mxu0_64x64x128.S#L89-L379) | 204 | Longer systolic-engine accumulation chain; requires MXU0 extraction and validation. |
| [`perf_fused_attention_mxu1.S`](../../baremetal/assembly/perf_fused_attention_mxu1.S#L187-L403) | 260 | Separate mixed-body adapter preserves all 262 assembled words on roundtrip. Handwritten, built-in/critical, and profile/critical replays pass; [measured results](rtlgraph-attention.md) are 2,942 / 2,101 / 2,069 CSR cycles. |
| [`perf_fused_attention_mxu0.S`](../../baremetal/assembly/perf_fused_attention_mxu0.S#L35-L403) | 260 | Corresponding mixed VPU/MXU0 attention pipeline; later target for shared-resource scheduling. |
| [`perf_mm_dual_128x128x128.S`](../../baremetal/assembly/perf_mm_dual_128x128x128.S#L423-L814) | 528 | Later shared-port/bank and simultaneous-engine stress case. Outside the MXU1-only adapter. |
| [`perf_softmax.S`](../../baremetal/assembly/perf_softmax.S#L33-L112) | 51 | Dependent VPU reduction, exponential, reciprocal and multiplication chain. Requires VPU timing and syntax support. |
| [`perf_unary.S`](../../baremetal/assembly/perf_unary.S#L25-L181) | 78 | Independent unary chains intended to share VPU slots. Check actual cycle markers before interpreting its historical utilization comments. |
| [`perf_vec_layernorm_32x32.S`](../../baremetal/assembly/perf_vec_layernorm_32x32.S#L33-L119) | 48 | Vector normalization with dependent reductions and arithmetic. Outside the current adapter. |
| [`perf_vec_rmsnorm_softmax.S`](../../baremetal/assembly/perf_vec_rmsnorm_softmax.S#L37-L155) | 67 | Independent normalization and softmax chains, useful for later VPU resource-overlap analysis. |
| [`perf_vpu_binary.S`](../../baremetal/assembly/perf_vpu_binary.S#L15-L136) | 89 | Multiple accumulated CSR timing windows and scalar verification branches; needs a different measurement/adapter boundary. |
| [`perf_vpu_reduction.S`](../../baremetal/assembly/perf_vpu_reduction.S#L15-L155) | 103 | Reduction checks with multiple timing windows and scalar verification branches; outside the current slice. |

## Preserve operation encodings and the measurement boundary

Baremetal assembly and `atlas-opt` use different dialects. For example, baremetal `VMATPUSH.W.MXU1 0, 4` becomes `vmatpush.weight.mxu1 w0, m4`. Baremetal `VMATPOP.FP8.MXU1 9, 7, 1` becomes `vmatpop.fp8.acc.mxu1 m9, acc1, e7`: **the scale and accumulator operands change position**. The [compiler instruction table](../src/core/asm.cpp#L40-L76) also uses channel suffixes for DMA, typed register prefixes, and parenthesized load/store addresses. Its [pseudo-instruction expansion](../src/core/asm.cpp#L265-L295) handles `LI` and `NOP`, but does not make a raw baremetal file directly compatible.

The [adapter](../scripts/rtlgraph_kernel.py#L46-L128) therefore translates only the body between exactly two `CSRR ..., 0xC00` markers. It admits MXU1 weight pushes, matmuls, accumulating matmuls, FP8 pops, `DELAY`, and no-ops. It rejects unsupported instructions, ambiguous markers, labels, and PC-relative/control-flow code. The rest of the source, including the perf directives and cycle-read instructions, stays byte-for-byte intact.

Every invocation uses the original baremetal assembler to check full-program original→compiler→baremetal encoding equality. Splicing a schedule additionally requires the same multiset of non-idle encoded instructions. These checks catch operand conversion errors and dropped/added operations; they do not prove that a permutation preserves dependencies. The manifest records source, assembler and output hashes, boundary hashes, static issue counts, and that limitation.

The [ending cycle read](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L200-L209) follows the final `VMATPOP.FP8.MXU1` issue, before that pop drains. [Writeback](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L215-L249) begins with `m8`. A reordered last pop could otherwise collide with this unchanged suffix. Generated candidates add an **unmeasured** `DELAY 32` immediately after the ending cycle read, representing 33 idle issue cycles. This is a candidate drain to validate on RTL, not a proof of completion. Preserve that distinction when comparing total runtime: only the CSR dispatch window is directly comparable to the original bracket.

## Generate and replay a candidate

Run from the compiler repository after the [environment setup](rtlgraph-mxu1.md#replay). The examples reuse the passing smoke manifest and its recorded simulator/toolchain inputs. Neither Mill nor a Chipyard Make invocation is needed. Each replay output directory must be new.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
atlas_kernel="$atlas_baremetal/assembly/perf_mm_mxu1_64x64x64.S"
atlas_golden="$atlas_baremetal/generators/perf_mm_mxu1_64x64x64.json"

python3 -B scripts/rtlgraph_kernel.py prepare \
  --source "$atlas_kernel" --assembler "$atlas_baremetal/assembler.py" \
  --output build/rtlgraph-kernel/original.compiler.S

LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  build/rtlgraph-compiler/atlas-opt \
  build/rtlgraph-kernel/original.compiler.S \
  --passes strip-artifacts,schedule \
  -o build/rtlgraph-kernel/baseline_model.compiler.S

python3 -B scripts/rtlgraph_kernel.py splice \
  --source "$atlas_kernel" --assembler "$atlas_baremetal/assembler.py" \
  --scheduled build/rtlgraph-kernel/baseline_model.compiler.S \
  --output build/rtlgraph-kernel/baseline_model.S

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly "$atlas_kernel" --golden-json "$atlas_golden" \
  --output build/rtlgraph-perf/replay_original --run --capture

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-kernel/baseline_model.S \
  --golden-json "$atlas_golden" \
  --output build/rtlgraph-perf/replay_default --run --capture
```

Use only letters, digits and underscores in the replay assembly filename stem, because the baremetal helper embeds that name in generated C. Omit `--run` to prepare without invoking VCS. Replay restores the recorded tool environment and uses the current license environment. The existing fixture is reused; its Python generator is not rerun or independently validated. A successful comparison requires the complete DRAM checks, `DBG0`/halt status, process success, and a nonzero, unambiguous `dbg1_cycles` result. Retain the manifest, generated ELF, functional log, and waveform/checker records.

`serialize --gap 64` is also available in `rtlgraph_kernel.py`; it preserves original operation order and deliberately separates consecutive operations. This can provide a controlled overlap experiment, but is not the performance baseline and must not be presented as an improvement over the hand schedule.

## Static comparison and measured results

The initial default-model run has a static cycle-CSR issue span of **294 cycles**, versus **291** for the original hand schedule. These counts apply `DELAY N` as `N+1` issue cycles and are not observed counter values. The `atlas-opt` summary reports **323→326**, because its model simulation includes the final operation's drain. Those totals should not be compared directly with `dbg1_cycles`. CSR counter-read behavior can also make a measured counter delta differ from waveform edge separation.

Four `EE290SimConfig` replays completed with all **1,024 golden words**, `DBG0=1`, and halted/`ECALL` status passing:

| Assembly/model | Scheduling priority | Measured CSR cycles | Replay |
| --- | --- | ---: | --- |
| Original hand schedule | Original order and delays | 290 | manifest (`build/rtlgraph-perf/baseline-k64-2/manifest.json`) |
| Built-in model | Default critical path | 293 | manifest (`build/rtlgraph-perf/default_model_k64_1/manifest.json`) |
| Built-in model | Input order among ready, legal instructions | 290 | manifest (`build/rtlgraph-perf/default_input_k64_1/manifest.json`) |
| Partial RTL profile | Input order among ready, legal instructions | 290 | manifest (`build/rtlgraph-perf/profile_input_k64_1/manifest.json`) |

The comparison record (`build/rtlgraph-perf/comparison.json`) joins replay assembly hashes to adapter manifests and the four-way compiler comparison (`build/rtlgraph-kernel/priority-comparison/comparison.json`). With critical-path priority, both models produce the same 294-cycle static issue span; with input priority, both produce 291. The partial profile/input-priority combination reproduces the normalized handwritten body exactly; the built-in model/input-priority combination uses a different order. Thus the three-cycle recovery over default scheduling is attributable to priority choice in this experiment. Correcting the unnecessary overwrite accumulator-read hold adds no measured cycle gain here, and the generated schedule does not beat the hand schedule.

The original and profile/input candidate both pass independent row/event checks. Each has four overwriting and four accumulating computes, four weight pushes and four FP8 pops. Scalar issue equals engine acceptance for every command; compute requests occur at ages 0–31, feeds at 1–32, writes at 3–34, and retirement at 34. Seven consecutive compute gaps are 32 cycles; the observed maximum in flight is two. The candidate timing report (`build/rtlgraph-perf/profile_input_k64_1/timing.json`) and measured-window comparison (`build/rtlgraph-perf/profile_input_k64_1/baseline-comparison.json`) confirm matching instruction words, PCs, relative issue cycles, and engine events throughout the window.

Both captured windows span **291 clock edges** but report **290 CSR cycles**. The [cycle-counter update](../../src/main/scala/diplomatic/memory/CSRFile.scala#L46-L60) and [CSR write override](../../src/main/scala/diplomatic/memory/CSRFile.scala#L111-L116) explain the suppressed increment on a counter read. Final pop writeback occurs 322 edges after the first cycle read. This is finite evidence for the marked dispatch window, not a whole-program speedup or proof that the historical utilization “floor” is tight. Generated candidates include the unmeasured drain described above. The cached simulator's saved FIRRTL matches fresh S0, but executable build lineage remains unverified.

Inspect the generated baremetal candidate (`build/rtlgraph-kernel/profile_input.S`) or its dependency-graph viewer (`build/rtlgraph-kernel/profile_input.html`). All build artifacts are local and ignored by Git; the scripts and this guide preserve the reproducible workflow.

## Held-out K128 comparison

`perf_mm_mxu1_64x64x128.S` doubles the accumulation depth without changing the hardware configuration. It has four overwrite computes, twelve accumulating computes, eight weight pushes, and four FP8 pops. The [measured region](../../baremetal/assembly/perf_mm_mxu1_64x64x128.S#L249-L344) retains the K64 convention: the final cycle read follows the last pop issue, excluding its drain and later DRAM writeback.

The compiler uses the **unchanged K64-derived profile** from `profile-k64-2`; K128 did not supply or retune its timing fields. All four distinct replays pass **1,024 golden words**, `DBG0=1`, and halted/`ECALL` status:

| Assembly/model | Scheduling priority | Measured CSR cycles | Replay |
| --- | --- | ---: | --- |
| Original hand schedule | Original order and delays | 546 | manifest (`build/rtlgraph-perf/k128_original_1/manifest.json`) |
| Built-in model | Default critical path | 551 | manifest (`build/rtlgraph-perf/k128_default_critical_1/manifest.json`) |
| Built-in model | Input order among ready, legal instructions | 548 | manifest (`build/rtlgraph-perf/k128_default_input_1/manifest.json`) |
| Partial RTL profile | Input order among ready, legal instructions | 546 | manifest (`build/rtlgraph-perf/k128_profile_input_1/manifest.json`) |

Unlike K64, this shows a **two-cycle benefit from the profile under the same scheduling priority** (548→546). First-write age remains 3 in both models; removing the unnecessary overwrite accumulator-read hold allows the handwritten order at the phase transition. The combined profile/input-priority choice saves five cycles against the default critical-path schedule. It matches, rather than improves on, the hand schedule. The profile/critical variant has an identical assembled instruction stream to default/critical and shares that replay result; it was not counted as another RTL run.

The comparison record (`build/rtlgraph-perf/k128-comparison.json`) joins compiler, adapter, assembler and replay hashes. Its static experiment (`build/rtlgraph-kernel/k128/comparison.json`) records all four model/priority combinations, exact commands and equivalence groups. Inspect the generated assembly (`build/rtlgraph-kernel/k128/profile_input.S`) or dependency viewer (`build/rtlgraph-kernel/k128/profile_input.html`). Reproduce with the K64 commands above, substituting the K128 source/golden and new output paths; retain the frozen profile and vary priority explicitly.

Both captures independently pass all 16 compute, eight push, and four pop obligations. All computes issue and are accepted in the same cycle, with 15 consecutive 32-cycle gaps and a maximum of two in flight. Request/feed/write ages remain 0–31/1–32/3–34. The CSR delta is 546 across 547 physical clock edges; the final pop write occurs 578 edges after the initial cycle read. The held-out profile check (`build/rtlgraph-perf/k128_profile_input_1/heldout-profile-check.json`) tests agreement with the unchanged profile. Four late-transaction mutations (`build/rtlgraph-perf/k128_original_1/negative-checks.json`) are rejected: an altered later compute operand, later weight-push slot and row errors, and truncation during the final compute. These are finite witnesses, not universal safety or a whole-program speedup claim.

The [controlled bank cases](rtlgraph-banks.md) separately test operand-dependent physical contention and compiler repair, without treating intentionally padded validation schedules as performance baselines.

The subsequent [fixed-operation search](rtlgraph-search.md) exhausts smaller issue and completion horizons for K64/K128: their handwritten schedules meet the selected model's lower bounds. Those bounds preserve operands and graph dependencies and are not unrestricted RTL optimality claims. The [mixed-engine attention experiment](rtlgraph-attention.md) then demonstrates an actual improvement over its handwritten kernel, with unchanged inputs/outputs and observed timing through output-writeback completion.

## Experimental profile and future consumers

The [opt-in partial MXU1 profile](rtlgraph-profile.md) controls first accumulator-write age and whether plain overwriting `VMATMUL.MXU1` occupies the accumulator read resource. It does not replace the rest of the machine model. The [profile extractor's evidence checks](../scripts/rtlgraph_mxu1_profile.py#L152-L197) require a passing `EE290SimConfig` performance replay, matching waveform identity, fresh-FIRRTL agreement, supported typed CIRCT wiring, and a checked trace with scalar-issue alignment, overwrite/accumulate cases, and back-to-back computation. It derives the valid-register chain and exhaustively checks the accumulator-read Boolean function. These structural facts and finite witnesses still leave arithmetic equivalence, same-cycle visibility, unseen interference, and cached simulator source-to-binary linkage outside the established scope. Each resulting schedule needs its own functional replay.

Merlin remains an optional future consumer/reference, as described in the [plan](../.agents/notes/RTL_GRAPH_PLAN.md#23-relationship-to-merlin). Its surveyed [Atlas schedule contract](https://github.com/ucb-bar/merlin/blob/refactor/merlin-phase-architecture-clean/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml) is a branch-specific reference to pin and inspect before integration. A reference-only `third_party/merlin` checkout could help compare schemas; it is not required to build, schedule, or replay these kernels. A future adapter must preserve evidence scope and report any constraints lost when projecting row accesses, shared resource occupancy, or completion events into a simpler contract.

## Broader corpus expansion

The [corpus baseline guide](rtlgraph-corpus.md) stages all fourteen originals and records which programs have output goldens. The [scheduling guide](rtlgraph-corpus-scheduling.md) extends the adapter to MXU0, dual-MXU, and normalization kernels while preserving original measurement boundaries and independently tracking profile versus scheduler effects. MXU0 read-resource and VPU issue/resource extraction are documented separately; VPU numerical timings remain inherited.
