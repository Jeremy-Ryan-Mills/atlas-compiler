# Experimental MXU1 scheduling profile

The first consumer integration is a partial profile for `EE290SimConfig`. It changes two MXU1 properties in `atlas-opt`: the first accumulator-write age, and whether overwrite-only `VMATMUL.MXU1` occupies an accumulator-read resource. Other instruction profiles, MREG reservations, shared-port rules, DMA behavior, and resource capacities remain inherited from the built-in model. The default invocation retains the existing model.

## Evidence and projection

The [extractor](../scripts/rtlgraph_mxu1_profile.py#L40-L139) follows the typed CIRCT data inputs of the core's unconditional, synchronous-reset `seq.firreg` valid chain. Clock changes, asynchronous reset, unsupported operations, and feedback/enable muxes are rejected. In the pinned hardware it finds two registers, yielding the recurrence `out_valid(t) = compute_valid(t-2)` while reset stays deasserted. This derives result-valid timing, not arithmetic correctness.

The same extractor checks the sequencer's accumulator-read enable over all 512 assignments to five named cutpoints: port boundary, acceptance, current port validity, current opcode, and arriving opcode. It requires distinct SSA identities with exact `i1/i1/i1/i3/i3` types before enumerating those domains. The function enables reads for `VMATMUL.ACC.MXU1` and not overwrite-only `VMATMUL.MXU1`. This matches [the RTL read helper](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L408-L411). The check establishes the combinational function over those cutpoints; it is not a reachable-state or temporal proof.

Before emitting a compiler projection, the extractor requires a passing functional replay and a matching checked waveform with both compute modes, back-to-back computation, scalar issue/acceptance alignment, and complete row streams. The [K64 baseline](../build/rtlgraph-perf/baseline-k64-2/timing.json) observes reads at scalar-relative ages 0–31, feeds at 1–32, and writes at 3–34, agreeing with the structurally derived two-cycle valid pipeline. The first-write age is therefore a composition of a structural pipeline fact and finite request/feed observations. It is not entirely recovered by static analysis.

The [canonical report](../build/rtlgraph-perf/profile-k64-2/profile.json) preserves source/IR/tool hashes, typed operation locations, guard evidence, trace identity, scope, and inherited-model limitations. The adjacent [compiler projection](../build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile) uses a small strict `key=value` format:

```text
schema=atlas-mxu1-profile-v1
config=EE290SimConfig
source_ir_sha256=<SHA-256 of the hardware IR>
evidence_sha256=<SHA-256 of the canonical report>
first_write_age=3
overwrite_acc_read_hold=0
```

The [loader](../src/core/machine_profile.cpp#L8-L50) rejects missing, duplicate, unknown, or malformed fields and unsupported geometry/latency ranges. Hash fields identify accompanying evidence; the C++ loader does not independently validate that evidence. The extraction command performs artifact checks and produces the projection. Neither format is a complete target description.

`first_write_age` drives accumulator writes, write-resource occupancy, compute retirement occupancy, and same-accumulator precedence constraints. The overwrite-read setting removes only the plain MXU1 matmul's accumulator-read hold. Accumulating matmuls retain their read hold, and MXU0 remains unchanged. The selected model flows through dependency construction, reservation scheduling, both timing-simulator runs, and the graph viewer, without mutable global model state.

## Reproduce extraction and scheduling

The [kernel guide](rtlgraph-kernels.md) covers the original assembly, adapter, environment, and functional replay. Run from the compiler worktree. The existing fresh S0 build remains in the original checkout; subsequent evidence is local to this worktree. Choose new extraction and replay directories.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_compiler_hw="$atlas_hw/generators/sp26-atlas-acc/atlas-compiler-experiments"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/riscv-tools/lib:$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

python3 scripts/rtlgraph_mxu1_vcd.py \
  build/rtlgraph-perf/baseline-k64-2/mxu1.vcd --perf \
  --output build/rtlgraph-perf/baseline-k64-2/samples_replay.jsonl
python3 scripts/rtlgraph_mxu1_trace.py \
  build/rtlgraph-perf/baseline-k64-2/samples_replay.jsonl \
  --output build/rtlgraph-perf/baseline-k64-2/timing_replay.json
python3 scripts/rtlgraph_mxu1_profile.py \
  --s0-manifest "$atlas_compiler_hw/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json" \
  --perf-manifest build/rtlgraph-perf/baseline-k64-2/manifest.json \
  --timing-report build/rtlgraph-perf/baseline-k64-2/timing_replay.json \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-perf/profile_replay

build/rtlgraph-compiler/atlas-opt build/rtlgraph-kernel/original.compiler.S \
  --passes strip-artifacts,schedule --schedule-priority input \
  --experimental-mxu1-profile build/rtlgraph-perf/profile_replay/atlas-mxu1.profile \
  -o build/rtlgraph-kernel/profile_replay.compiler.S \
  --viz build/rtlgraph-kernel/profile_replay.html
```

Splice the resulting body into the original kernel and replay with the same golden fixture using the kernel guide. The optional `--schedule-priority input` picks the first ready, resource-legal instruction in original order; the default `critical` priority uses critical-path height. Both use identical dependency and resource checks. Keeping the choice explicit lets the performance experiment distinguish model changes from heuristic changes. Input priority is not expected to be best for every kernel.

## Validation and remaining scope

The synthetic [pipeline-sensitivity test](../scripts/tests/test_rtlgraph_profile_integration.py) adds a real `seq.firreg` to a small CIRCT fixture, reruns the typed exporter and latency extraction, loads the resulting test projection into `atlas-opt`, and checks that the emitted dependent pop moves from cycle 4 to cycle 5. The Atlas RTL is unchanged. Focused extractor tests reject altered guards, wider opcode domains, aliased cutpoints, unsupported clocks/resets/enables, missing overlap, and inconsistent timing; C++ tests exercise loader rejection, unchanged defaults, resource conflicts, dependency timing, scheduling, and simulator agreement.

The [K64 replay results](rtlgraph-kernels.md#static-comparison-and-measured-results) distinguish the priority effect from the model correction: input priority recovers the handwritten 290-cycle CSR window with either model, while the partial profile also reproduces its instruction order. The profile candidate passes all 1,024 output checks and independent event obligations. A review subsequently added cutpoint-width/identity rejection; regeneration in `profile-k64-2` preserves both numerical overrides and emits byte-identical scheduled assembly. The simulation and compiler-comparison manifests retain their original `profile-k64-1` evidence identity rather than relabeling it.

The subsequent [held-out K128 comparison](rtlgraph-kernels.md#held-out-k128-comparison) uses `profile-k64-2` unchanged. With input priority held constant, the profile schedule takes 546 CSR cycles versus 548 for the built-in model, while both pass all 1,024 output checks. The default critical-path schedule takes 551; the hand schedule takes 546. Independent checking covers 16 compute transactions and repeated weight-slot reuse. This is a measured two-cycle benefit from the overwrite-read correction on a second kernel, not a claim of improvement over its handwritten schedule.

The [fused-attention comparison](rtlgraph-attention.md) also keeps that profile unchanged. With critical priority held constant, the built-in model takes 2,101 CSR cycles and the profile takes 2,069, versus 2,942 for handwritten. All three pass 1,024 output checks. Removing the overwrite accumulator-read hold permits overlap with a previous pop, contributing 32 cycles beyond the built-in scheduler's improvement. Modeled completion is kept before the ending CSR; exact scalar execution and post-DMA `DBG0` timing are independently checked. The VPU and BF16-transfer rules used by this mixed kernel remain inherited.

```sh
python3 scripts/tests/test_rtlgraph_mxu1_profile.py build/rtlgraph-s0/query/rtlgraph_export
python3 scripts/tests/test_rtlgraph_profile_integration.py \
  build/rtlgraph-s0/query/rtlgraph_export build/rtlgraph-compiler/atlas-opt
ctest --test-dir build/rtlgraph-compiler --output-on-failure
```

This is finite validation for one configuration and three original kernels, with structural support for two model fields. The [bank experiments](rtlgraph-banks.md) add controlled operand-alias cases and corroborate an inherited physical-resource rule. Universal safety, same-cycle storage visibility, other interference patterns, parameter generalization, and a recorded source-to-simulator build remain open. The saved simulator FIRRTL matches fresh S0 FIRRTL, but that does not establish binary build lineage. Merlin remains a future adapter to the richer evidence/model representation; this partial projection is not yet a Merlin contract.

## Composing the independently extracted MXU0 projection

The [MXU0 profile](rtlgraph-mxu0.md) adds one separately derived override via `--experimental-mxu0-profile FILE`. It can be combined with `--experimental-mxu1-profile FILE`; the loader preserves each engine's fields and rejects different hardware-IR identities. The MXU0 schema accepts no first-write age, so it cannot accidentally copy MXU1 pipeline timing into the systolic array. Default behavior remains the built-in model, and accumulating matmuls retain their read reservations in both profiles.

The [corpus adapter](../scripts/rtlgraph_schedule.py) prepares both MXUs and selected VPU instructions, validates assembler roundtrips and fixed operation/operand multisets, and compares matching priority policies. [VPU extraction](rtlgraph-vpu.md) currently corroborates existing issue/resource rules without adding a latency override. The richer canonical machine description and optional Merlin adapter remain future work.
