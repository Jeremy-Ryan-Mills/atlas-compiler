# MXU1 event extraction and functional witnesses

The 2026-09-26 checkpoint extends the typed CIRCT query into MXU1 acceptance, operand requests, compute feed, writeback requests, and release-related events. The query passes on the fresh `chipyard.EE290SimConfig` IR. The original two smoke kernels pass on the recorded cached simulator. Subsequent [passive traces and independent checks](rtlgraph-mxu1-timing.md) cover the single-matmul witness and a K64 baseline with eight computes, four weight pushes, and four FP8 pops. An [experimental partial profile](rtlgraph-profile.md) now feeds two evidence-backed fields into opt-in scheduling; the [kernel guide](rtlgraph-kernels.md) records the comparison workflow. The default compiler model and Atlas RTL remain unchanged.

## Hardware and worktree boundary

The initialized hardware checkout and recorded S0 build remain at `/bwrcq/C/reednicolas/ee194-sp26-chipyard`. The compiler worktree is `/users/reednicolas/.codex/worktrees/37e0/ee194-sp26-chipyard/generators/sp26-atlas-acc/atlas-compiler-experiments`, on `feat/rtl-graph`. Its enclosing Atlas checkout is not initialized, so the new helpers take explicit paths to the original hardware inputs. New query, compilation, simulator, and run outputs stay in this compiler worktree's ignored `build/` directory. Source links below use the normal initialized Chipyard layout.

The IR input is the recorded current-source S0 artifact at `generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/atlas.hw.mlir` in the original checkout, SHA-256 `d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2`. The [S0 guide](rtlgraph-s0.md#current-ee290simconfig-checkpoint) records its build and lowering provenance. The new worktree builds its own typed exporter and reruns the structural query against that input.

## Extracted events and their scope

The [event extractor](../scripts/rtlgraph_mxu1.py#L21-L48) selects named values in `InnerProductTreesSequencer` and follows their typed SSA definitions. The [report](../build/rtlgraph-mxu1/events.json) contains 11 event cones, 19 operand/state context cones, six structural paths, four exact wrapper connections, and register-operand uses reached from acceptance and retirement. Its status is `event_checks_passed`.

| Event | Selected signal | Meaning and limit |
| --- | --- | --- |
| Command present | `io_cmd_valid` | A command is presented; acceptance is checked separately |
| Compute accepted | `acceptCompute` | Local command-class, resource, and hazard guards all allow acceptance |
| MREG access | `io_mregReadReq0_valid`, `io_mregReadResp0_valid` | Request and response signals are located separately; correspondence and data availability still need temporal checks |
| Accumulator read | `io_accComputeReadEn` | Read request; operation and destination context are retained |
| Compute feed | `io_compute_valid` | Data presented to the inner-product core |
| Core result and writeback | `io_coreOut_valid`, `io_accComputeWrite_valid` | Result and accumulator write request; this is not yet a proof of storage commit or same-cycle visibility |
| Feed-port reuse | `p0Boundary` | The feed port can accept a new command subject to the other guards |
| In-flight retirement | `popThisCycle` | Final-row result retires the in-flight head |
| Compute busy | `io_compBusy` | Feed activity or an in-flight compute remains |

The [acceptance check](../scripts/rtlgraph_mxu1.py#L205-L222) reconstructs this Boolean function from CIRCT operations and checks all 128 assignments to its seven named cutpoints:

```text
acceptCompute = io_cmd_valid && isCompute && p0Boundary && !fifoFull
                && !accReuseHazard && !computeWslotHazard
                && !computePushAccHazard
```

This establishes local two-valued Boolean equivalence, with no external force overrides. The hazard cutpoints' own temporal meanings, reachable states, and software issue safety are not proved. The [source acceptance and assertion logic](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L301-L337) provides a separate review reference. The command interface has no ready handshake; a rejected command must not be treated as a transparently stalled instruction.

The [row-zero request](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L478-L497), [compute feed](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L468-L475), and [writeback/FIFO retirement](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L646-L689) explain why acceptance, feed-port reuse, and final retirement need distinct events. In particular, compute feed consumes MREG response bits without checking their valid signal in that branch; an independent monitor must check the response obligation rather than assuming the request alone proves it.

The query preserves exact instance result and port indices through the sequencer/core/accumulator wrapper. Cones stop at supported registers; module instances, clock adapters, and unsupported operations remain unresolved. Register operand indices are recorded without assigning data/enable/clock timing semantics. Twelve focused tests cover guard polarity, missing and wide cutpoints, unsupported operations, state boundaries, clock adapters, and incorrect multi-result wiring. This event query emits no cycle ages, resource capacities, transaction attribution, or scheduling distances; those are separate concerns handled in part by the later trace checker and partial-profile extractor.

## Existing functional witnesses

The [smoke helper](../scripts/rtlgraph_smoke.py) assembles and compiles the selected existing programs, stages the cached VCS executable and runtime inputs in a new directory, and optionally runs both. It derives the compiler and simulator invocation from the [baremetal Makefile](../../baremetal/Makefile#L96-L123), [baremetal README](../../baremetal/README.md#L13-L36), and Chipyard's simulator rules without invoking Make. The output manifest hashes the driver, assembly, fixture, generated C, ELF files, compiler dependencies, selected link inputs, simulator, runtime libraries, coverage design inputs, and DRAM configuration; it preserves commands and logs.

| Witness | Coverage |
| --- | --- |
| [MXU1 single input tile](../../baremetal/assembly/mxu1_single_input_tile_bf16.S#L1-L57) | One 32×32 FP8 matrix multiply through `VMATPUSH.W.MXU1`, `VMATMUL.MXU1`, and `VMATPOP.BF16.MXU1`, followed by DMA output and 512 expected-word comparisons |
| [Saturn/Atlas VMEM smoke](../../../../tests/saturn_atlas_vmem_smoke.c#L9-L114) | 16 scalar words and 64 RVV words round-tripped through Atlas VMEM |

The MXU1 program is regenerated from the current assembly and existing offset-based JSON fixture. Its Python golden-data generator was not rerun or independently validated. This avoids silently using the older generated C's different address placement: `DMA.CONFIG` supplies the upper address word, rather than selecting a relative/absolute address mode. The current assembly uses upper word zero and DRAM addresses beginning at `0x90000000`. Its existing `DELAY` instructions are retained; this is a functional witness, not a minimum-spacing experiment.

A successful MXU1 run requires process success, the expected data-check PASS marker, `DBG0 == 1`, and `(status & 7) == 5`, matching the [halt/ECALL definitions](../../../../tests/atlas_mmio.h#L32-L45). The VMEM run requires process success and its final PASS marker. Both reject recognized failure diagnostics and timeouts. Nine helper tests cover failure classification, completion checks, compiler flags, runtime argument ordering, staging, and process cleanup.

The cached simulator's saved `EE290SimConfig` FIRRTL is byte-for-byte identical to fresh S0 FIRRTL: SHA-256 `197aab9e74d40e98d4291bd8ffb34e018c74e9e5146196384f1f3405c3c9e989`. The manifest still marks the executable's source-to-build linkage `UNVERIFIED`; adjacent matching FIRRTL does not prove that the executable was built from it. A successful replay therefore supplies a finite observation for the recorded cached executable, not a functional equivalence proof for the analyzed IR.

The completed [run-3 manifest](../build/rtlgraph-smoke/run-3/manifest.json) records `passed`: both processes returned zero without timing out. The [MXU1 log](../build/rtlgraph-smoke/run-3/atlas_mxu1_single_input_tile_bf16/simulation.log) reports all 512 DRAM comparisons passed, `DBG0 = 1`, and `status = 0x00000005`; the [VMEM log](../build/rtlgraph-smoke/run-3/saturn_atlas_vmem_smoke/simulation.log) reports both scalar and vector transfers passed. The logs retain 584 and 167 address-alignment warnings respectively; these finite functional results do not validate memory-model timing. Earlier attempt directories are retained: `run-1` was interrupted during license acquisition, and `run-2` exposed a runner argument-ordering error before kernel execution. The corrected runner places `-no_save` inside the `+permissive` envelope, with a regression check for that ordering.

The later [K64 performance replay](../build/rtlgraph-perf/baseline-k64-2/manifest.json) uses [`perf_mm_mxu1_64x64x64.S`](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L151-L256), retaining its setup, hand schedule, and output fixture. All 1,024 DRAM output-word comparisons pass with `DBG0 = 1` and halt status `5`. Its [timing report](../build/rtlgraph-perf/baseline-k64-2/timing.json) independently attributes eight computes, four pushes and four FP8 pops to scalar issue. The reported CSR delta is 290; the two cycle-read issue edges are 291 cycles apart. These are distinct measured quantities. See the [kernel guide](rtlgraph-kernels.md) for compiler-generated variants and their own replay records.

## Replay

Run from this compiler repository. Set `RTLGRAPH_CHIPYARD` to the initialized hardware checkout and `RTLGRAPH_ARTIFACTS` to its saved compiler build directory. These environment steps correspond to the BWRC, Miniforge, Chipyard, and generator-PATH setup in `~/bin/ee194_env.sh`; shell integration and `mill --version` are unnecessary here. The recorded retry used the complete environment setup and license-server access after the sandboxed launch could not contact that server.

```sh
export RTLGRAPH_CHIPYARD=/bwrcq/C/reednicolas/ee194-sp26-chipyard
export RTLGRAPH_ARTIFACTS="$RTLGRAPH_CHIPYARD/generators/sp26-atlas-acc/atlas-compiler-experiments/build"
source /tools/C/ee194-sp26/bwrc-env.sh
source "$HOME/miniforge3/bin/activate"
source "$RTLGRAPH_CHIPYARD/env.sh"
export PATH="$PATH:$RTLGRAPH_CHIPYARD/generators/sp26-atlas-acc"

mkdir -p build/rtlgraph-s0/EE290SimConfig
python3 scripts/rtlgraph_query.py "$RTLGRAPH_ARTIFACTS/rtlgraph-s0/EE290SimConfig/atlas.hw.mlir" \
  --toolchain "$RTLGRAPH_CHIPYARD/.conda-env/riscv-tools" \
  --build-dir build/rtlgraph-s0/query \
  --export-json build/rtlgraph-s0/EE290SimConfig/atlas-typed.json \
  > build/rtlgraph-s0/EE290SimConfig/atlas-query.json
python3 scripts/rtlgraph_mxu1.py "$RTLGRAPH_ARTIFACTS/rtlgraph-s0/EE290SimConfig/atlas.hw.mlir" \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-mxu1/events.json
python3 scripts/tests/test_rtlgraph_mxu1.py build/rtlgraph-s0/query/rtlgraph_export
python3 scripts/tests/test_rtlgraph_smoke.py

# Choose a new output directory for every attempt.
python3 scripts/rtlgraph_smoke.py \
  --chipyard-root "$RTLGRAPH_CHIPYARD" \
  --fresh-fir "$RTLGRAPH_ARTIFACTS/rtlgraph-s0/EE290SimConfig/elaboration/chipyard.harness.TestHarness.EE290SimConfig.fir" \
  --output build/rtlgraph-smoke/replay-1 --run --timeout-seconds 300
```

Omit `--run` to stage and compile without invoking VCS. The helper uses seed 1, a 70-million-cycle simulator ceiling, and a per-command wall-clock timeout. It keeps coverage and DRAM outputs inside the new run directory and excludes historical coverage test results from the staged design database.

## Temporal follow-up

The [original trace slice](rtlgraph-mxu1-timing.md#observed-timing) records acceptance, MREG requests and registered bank-return routing, compute feed, accumulator write row/destination, and final retirement for one compute. Its checker assigns independent transaction identities and validates rows against its own queue. The [K64 extension](rtlgraph-mxu1-timing.md#k64-baseline) now observes both `VMATMUL.MXU1` and `VMATMUL.ACC.MXU1`, seven consecutive 32-cycle compute-acceptance gaps, and at most two computes in flight. All eight computes have zero scalar-issue-to-acceptance gap, read-request ages 0–31, feed ages 1–32, and write-request ages 3–34 in that capture. These finite observations do not establish every weight/accumulator lifetime constraint, shared-port rule, or same-cycle visibility obligation.

The [Saturn/Atlas FP8 quantization bridge](../../../../tests/saturn_atlas_fp8_quantize_bridge.c#L1-L46) is a useful later integrated witness. Full S0 still needs explicit simulation/environment contracts and resolution of the cached executable's build linkage. S1 has this acceptance/event slice; S2 has the single-compute and multi-operation finite witnesses. The [partial profile](rtlgraph-profile.md) begins consumer integration by projecting first-write age and overwrite accumulator-read occupancy into existing `Access`/`Hold`/`Footprint` construction. It inherits the rest of the model and requires separate replay of each emitted schedule. Broader timing coverage, bounded temporal properties, and complete model extraction remain open.
