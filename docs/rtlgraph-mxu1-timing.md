# MXU1 timing traces

This extends the [event extraction and functional checkpoint](rtlgraph-mxu1.md) with passive simulator tracing, a clock-edge adapter, and an independent transaction/row checker. The evidence applies to the captured execution of the existing `mxu1_single_input_tile_bf16.S` witness on the recorded cached `EE290SimConfig` simulator. It does not establish universally safe issue distances or change compiler scheduling profiles.

## Observed timing

The 2026-09-26 [capture manifest](../build/rtlgraph-mxu1-trace/run-1/manifest.json) and [timing report](../build/rtlgraph-mxu1-trace/run-1/timing.json) record 91,412 pre-edge samples on a 2,000 ps sequencer clock. One `VMATMUL.MXU1` was accepted at sample 45,959, reading `m0` with weight slot 0 and writing accumulator 0. The independent checker passes, and the same traced simulation passes all 512 golden output-word comparisons with `DBG0 = 1`, halt/ECALL status `0x00000005`, and process exit zero.

| Observed event | Age relative to acceptance |
| --- | --- |
| Read request for row `r` | `r`, covering 0–31 |
| Returned operand / compute feed for row `r` | `r + 1`, covering 1–32 |
| Core result / accumulator write request for row `r` | `r + 3`, covering 3–34 |
| Final feed and feed-port reuse | 32 |
| Final write and in-flight retirement | 34 |
| Compute state idle again | 35 |

All 32 observed feed-to-write gaps are two cycles. All three row streams advance one row per cycle. These observations agree with the source-derived hypothesis below. Write request timing does not establish same-edge read visibility, and the witness retains its original conservative `DELAY` instructions rather than testing minimum legal instruction spacing.

## Capture and sampling

The [capture helper](../scripts/rtlgraph_mxu1_capture.py#L218-L338) takes a passing smoke manifest, verifies the recorded MXU1 completion, executable, ELF, and staged runtime inputs, and copies them into a separate output directory. It restores the smoke run's `PATH` and `LD_LIBRARY_PATH` while using the current EE194 license environment. UCLI records 94 selected signals to VPD; the matching `vpd2vcd` converts it to VCD. The manifest records both tools, exact commands, signal selection, input hashes, conversion, functional completion, and trace closure. The cached simulator supports this through `-debug_pp`; its testbench's `+vcdfile` route is disabled because the build lacks `+define+DEBUG`.

The [VCD adapter](../scripts/rtlgraph_mxu1_vcd.py#L101-L171) groups all changes at each timestamp and samples the previous values when the sequencer's clock rises. Thus every sample is the settled left limit of that edge, before sequential state updates. Age zero is the sampled `acceptCompute` event. This is an engine-acceptance age; scalar issue and `DELAY` encoding have not been measured by this capture. The adapter requires the selected signals and their widths, checks a uniform clock period, preserves unknown values, and rejects unsupported dump changes rather than assigning them invented values.

Two signals removed from the optimized simulator interface are reconstructed explicitly from observed hardware state:

| Logical observation | Actual captured state | Source contract |
| --- | --- | --- |
| MXU1 P0 MREG response-valid and bank | `bankReadValid_d_0..31` and `bankReadPort_d_0..31`; a valid bank tagged with read-port index 2 returns to MXU1 P0 | [port order](../../src/main/scala/atlas/mreg/MregFile.scala#L75-L94), [registered routing](../../src/main/scala/atlas/mreg/MregFile.scala#L264-L304) |
| Compute busy | `p0IsCompute || inflightValid_0 || inflightValid_1` | [busy logic](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L685-L689) |

The trace header labels these reconstructions. Response-valid is not synthesized by delaying the expected request. Invalid command payloads and inactive bank-port tags may remain unknown; the checker requires values only when their control signals make them meaningful.

## Independent obligations

The [trace checker](../scripts/rtlgraph_mxu1_trace.py#L89-L284) assigns a new monitor transaction ID on each accepted compute and stores its operands. It attributes rows using its own request/feed/write counters and in-order queue. RTL FIFO row counters and destination tags are not used as the attribution oracle.

The checks require exactly 32 ordered requests, feeds, and writes per accepted compute; matching MREG and accumulator addresses; one-hot response routing to the preceding request's physical bank (`mreg & 31`); the one-cycle MREG response/feed contract; accumulator reads only for `MatmulAcc`; and retirement on the final write. Feed-port reuse and compute busy are checked separately. Missing cycles, unknown relevant controls, reset-interrupted work, and traces that end before compute drains are rejected. A final feed and the next command's row-zero request may share a sample.

Arithmetic pipeline latency is measured from feed/write pairs rather than imposed by a proposed timing table. Synthetic tests include a different arithmetic latency to ensure the checker can report a changed value. The checker validates event ordering and addresses; arithmetic data remains covered by the existing golden-output test, and same-cycle storage visibility is still outside this trace contract.

All 40 focused tests pass: ten capture/recovery checks, nine waveform-adapter checks, and 21 transaction-checker checks. Three [mutations of the actual captured trace](../build/rtlgraph-mxu1-trace/run-1/negative-checks-canonical.json) are also rejected for their intended reasons: returning the first operand from the wrong physical bank, changing the first write's row, and truncating the capture during computation. The [replay script](../build/rtlgraph-mxu1-trace/run-1/negative-checks.py) mutates samples only in memory and records the unchanged input hash.

The source-derived hypothesis for this configuration is MREG request row `r` at age `r`, feed at `r + 1`, and accumulator write request at `r + 3`. The [sequencer request/feed logic](../../src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L468-L497), [core valid pipeline](../../src/main/scala/atlas/mxu/ipt/InnerProductTrees.scala#L43-L52), and [parameter defaults](../../src/main/scala/atlas/common/InnerProductTreeParams.scala#L9-L42) explain that hypothesis. Comparing an observed trace to it is finite validation, not a temporal proof across operands, interference, and reachable states.

## Replaying the trace pipeline

Run from the compiler worktree. The [previous replay guide](rtlgraph-mxu1.md#replay) describes the hardware checkout and environment recipe; the commands below use the saved passing smoke run so that its exact ELF and runtime inputs are reused. Choose a fresh capture directory on every attempt.

```sh
source /tools/C/ee194-sp26/bwrc-env.sh
source "$HOME/miniforge3/bin/activate"
source /tools/C/reednicolas/ee194-sp26-chipyard/env.sh
python3 scripts/rtlgraph_mxu1_capture.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --output build/rtlgraph-mxu1-trace/replay-1 --run --timeout-seconds 600
python3 scripts/rtlgraph_mxu1_vcd.py \
  build/rtlgraph-mxu1-trace/replay-1/mxu1.vcd \
  --output build/rtlgraph-mxu1-trace/replay-1/samples.jsonl
python3 scripts/rtlgraph_mxu1_trace.py \
  build/rtlgraph-mxu1-trace/replay-1/samples.jsonl \
  --output build/rtlgraph-mxu1-trace/replay-1/timing.json
```

Omit `--run` to prepare the capture without a simulator invocation. The three helpers have separate success conditions: `captured` requires functional completion plus a closed waveform; conversion requires supported signals and sampling; `trace_obligations_passed` requires a complete accepted computation satisfying the monitor's obligations. Preserve all three records. The source-to-executable build linkage remains unverified even though the simulator's saved FIRRTL matches the fresh S0 FIRRTL byte-for-byte.

The recorded simulation closed normally at VCS `$finish`, which exits before Tcl resumes after `run`; the helper records `NORMAL_VCS_FINISH` and checks successful waveform parsing in addition to functional/process completion. The first converter invocation used an unsupported architecture override. The [finalizer](../scripts/rtlgraph_mxu1_capture.py#L147-L215) recovered the same recorded VPD with `VCS_ARCH_OVERRIDE=linux` and `VCS_MODE_FLAG=64`, preserving the original failed conversion and manifest in the run directory. To recover a successful simulation after a conversion failure, use `python3 scripts/rtlgraph_mxu1_capture.py --finalize-existing PATH/manifest.json`; this does not rerun VCS. The latest `finalizations` entry records the successful conversion and parser checks.

Focused helper checks are `python3 scripts/tests/test_rtlgraph_mxu1_capture.py`, `python3 scripts/tests/test_rtlgraph_mxu1_vcd.py`, and `python3 scripts/tests/test_rtlgraph_mxu1_trace.py`. They exercise simulator-input provenance, argument ordering, capture completion, pre-edge sampling, unknown values, response-bank attribution, row integrity, back-to-back acceptance, resets, and truncated captures.

## Remaining scope

The next witnesses should exercise `VMATMUL.ACC.MXU1`, back-to-back accepted computes, accumulator reuse, and shared-bank/port interference, with independently initialized data. The monitor supports several of those event shapes, but synthetic checker tests are not RTL measurements. Add scalar issue/PC attribution, weight lifetime checks, and storage visibility properties before projecting observed event ages into scheduling constraints. Keep environment contracts and a recorded source-to-simulator build as separate open S0 obligations, and add an appropriately scoped temporal property before treating a profile as established beyond the observed runs.
