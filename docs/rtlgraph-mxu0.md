# Experimental MXU0 accumulator-read profile

The MXU0 extractor independently checks the pinned `EE290SimConfig` systolic sequencer and derives one compiler override: overwrite-only `VMATMUL.MXU0` does not occupy the accumulator-read port. It does not derive systolic timing ages or establish that a rescheduled kernel is safe. The [first extraction report](../build/rtlgraph-mxu0/profile-1/profile.json) and [projection](../build/rtlgraph-mxu0/profile-1/atlas-mxu0.profile) record this structural checkpoint without claiming a simulation result.

## Evidence and interpretation

The [extractor](../scripts/rtlgraph_mxu0_profile.py#L34-L89) exports typed CIRCT SSA for `AtlasCore`, `SystolicArrayTop`, `SystolicArraySequencer`, and `AccumulationBuffers`. It checks all 512 assignments to five independent cutpoints with exact types: port boundary, compute acceptance, current port validity, current opcode, and arriving opcode. The recovered read-enable function is:

```text
if p0Boundary && acceptCompute:
    accumulator_read = arriving_opcode == MatmulAcc
else:
    accumulator_read = p0CmdValid && !p0Boundary && current_opcode == MatmulAcc
```

The [pinned opcode definitions](../../src/main/scala/atlas/mxu/MxuBundles.scala#L27-L34) identify `Matmul` as 5 and `MatmulAcc` as 6. The [SA read helper](../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L378-L382) and its [continuing/accepted-command call sites](../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L444-L459) provide source context; the Boolean check operates on the exported SSA rather than parsing printed MLIR or Scala.

The check includes combinations that may be unreachable, so it establishes the local function for every two-valued cutpoint assignment. It does not derive when acceptance occurs, which state transitions are reachable, how many rows a command reads, or how scalar instructions map to acceptance. The [SA acceptance conditions](../../src/main/scala/atlas/mxu/sa/SystolicArraySequencer.scala#L248-L301) remain separate obligations.

The extractor also binds `AtlasCore.mxu0` to `SystolicArrayTop`, verifies two wrapper command connections and six accumulator read connections, and checks the two physical accumulator memories. Each is a 32×512 `seq.firmem` with one read and one write port and declared read/write latency one. Exhaustive local mux checks cover 16,384 assignments per buffer, confirming that a same-buffer compute/store conflict selects the compute address. This is the actual MXU0 wrapper path, corresponding to [its accumulator connections](../../src/main/scala/atlas/mxu/sa/SystolicArrayTop.scala#L127-L138), even though MXU0 and MXU1 share the `AccumulationBuffers` module definition.

Removing the overwrite instruction's read hold does **not** remove the accumulating instruction's read hold. The [accumulator RTL assertions](../../src/main/scala/atlas/mxu/AccumulationBuffers.scala#L87-L98) prohibit simultaneous compute/store reads of one buffer; the [read mux and physical ports](../../src/main/scala/atlas/mxu/AccumulationBuffers.scala#L147-L168) do not supply two independent reads. A read hold also differs from result dependencies, logical reservations, and instruction-acceptance rules, all of which still apply.

## Partial compiler projection

The projection contains exactly five fields:

```text
schema=atlas-mxu0-profile-v1
config=EE290SimConfig
source_ir_sha256=<SHA-256 of the selected hardware IR>
evidence_sha256=<SHA-256 of the canonical report>
overwrite_acc_read_hold=0
```

Only `overwrite_acc_read_hold` changes the model. MXU0's first accumulator-write age 63, same-accumulator reuse gap 64, and same-weight-slot compute-to-push gap 63 remain inherited model assumptions. The extractor records these under `inherited_not_extracted`; their presence is not evidence that this work derived or validated them. Other instruction profiles, resources, capacities, DMA behavior, and ports remain inherited too.

The evidence hashes identify the CIRCT input and canonical report. The report additionally records the S0/FIRRTL lineage, typed export, exporter executable, extraction driver and analysis helpers, command line, typed operation locations, local checks, and limitations. A projection loader does not independently verify these hardware claims merely by parsing the hashes.

## Reproduce the extraction

From the compiler repository, use a new output directory:

```sh
python3 scripts/rtlgraph_mxu0_profile.py \
  --s0-manifest /bwrcq/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-mxu0/profile-1
```

The command verifies the selected hardware-IR and FIRRTL hashes, exports the four modules, and writes `typed.json`, `profile.json`, and `atlas-mxu0.profile`. Existing output directories are rejected. Missing or ambiguous cutpoints, incorrect widths, unsupported operations, unresolved input/state, feedback, mismatched opcodes, altered wrapper bindings/wires, and unsupported memory topology prevent a successful projection.

The [dedicated tests](../scripts/tests/test_rtlgraph_mxu0_profile.py) pass all 10 cases, including a valid widened-opcode circuit whose additional read condition would escape enumeration of the original three-bit domain. They also mutate opcode semantics, cutpoint identity, Boolean width, constants, comparisons, feedback/state, module bindings, and multi-result instance wiring. The shared physical-memory query has [seven additional focused tests](../scripts/tests/test_rtlgraph_mxu1_bottleneck.py), covering port count, geometry, latency, read wiring, and mux priority.

```sh
python3 scripts/tests/test_rtlgraph_mxu0_profile.py build/rtlgraph-s0/query/rtlgraph_export
python3 scripts/tests/test_rtlgraph_mxu1_bottleneck.py build/rtlgraph-s0/query/rtlgraph_export
```

## Remaining execution and temporal obligations

Start with [`perf_mm_mxu0_64x64x64.S`](../../baremetal/assembly/perf_mm_mxu0_64x64x64.S#L171-L225), then the [K128 accumulation/reuse case](../../baremetal/assembly/perf_mm_mxu0_64x64x128.S#L245-L328). Preserve the setup, actual instruction encodings, measurement markers, output fixture, and completion checks when adapting them. Each generated schedule needs its own functional RTL replay; this static profile is not permission to infer a pass from compiler-model agreement.

The optional [MXU0 observation checker](../scripts/rtlgraph_mxu0_trace.py) now binds scalar issue to command operands and acceptance, assigns its own compute/pop transactions and row counts, and validates operand requests, compute feeds, accumulator read/write ownership, retirement, and FP8/BF16 pop destinations. It reports measured output ages and exact same-accumulator pop/overwrite overlap; it does not assume the MXU1 pipeline or require MXU0 write age 63. Its common completion check independently matches every fired scalar word and PC against assembled source, verifies the recorded functional golden result, and preserves the CSR/DBG0 measurement distinction.

Use `--capture-mxu0` with the existing replay helper for new MXU0 runs. This selects a separate [42-signal map](../scripts/rtlgraph_mxu0_vcd.py), with names and widths checked against saved `SystolicArraySequencer.sv`, plus the existing scalar/CSR fields. Existing MXU1 capture maps and measurement descriptions remain unchanged. MXU0 and MXU1 bank capture modes are mutually exclusive. For a completed capture, write a fresh observation report:

```sh
python3 scripts/rtlgraph_mxu0_trace.py \
  --manifest build/rtlgraph-perf/NEW_MXU0_RUN/manifest.json \
  --output build/rtlgraph-perf/NEW_MXU0_RUN/mxu0-observations.json \
  --require-overlap
```

Omit `--require-overlap` for a baseline that may have no overlap. The flag requires actual same-cycle pop accumulator reads and overwrite operand requests to the same accumulator, rather than treating any successful execution as evidence of the optimization. The checker samples settled values before rising edges. Twelve focused tests cover opcode/operand binding, wrong acceptance, row ownership, result drops, retirement, unknown values, truncated captures, BF16 outputs, and edge sampling. Synthetic positive cases deliberately vary first-write age to prevent accidental dependence on one timing constant.

Remaining obligations include push row/data streams, MREG response routing, accumulator visibility and arithmetic, SA FIFO capacity and readiness recurrences, weight-slot drain, and interference from other engines. The observation checker does not validate these merely because its narrower checks pass. The inherited one-cycle request-to-feed and accumulator-read-to-pop-write interfaces are explicit assumptions. Bounded temporal properties and a verified source-to-simulator build remain further work.

## Observed fused-attention comparison

The three MXU0 fused-attention runs pass all 1,024 golden output comparisons, full scalar word/PC binding, and the scoped MXU0 row-ownership checks. The profile changes only overwrite accumulator-read occupancy; the observation checker measures first-write age 63 and consecutive output rows through age 94 for all eight computes in each run.

| Schedule and observation report | CSR delta | Raw CSR edge span | First issue to final `DBG0` | Same-accumulator pop/overwrite pairs |
| --- | ---: | ---: | ---: | ---: |
| [Original](../build/rtlgraph-corpus/runs/fused_attention_mxu0_original_1/mxu0-observation.json) | 3,430 | 3,431 | 25,570 | 0 |
| [Built-in critical](../build/rtlgraph-corpus/runs/fused_attention_mxu0_builtin_critical_1/mxu0-observation.json) | 2,158 | 2,159 | 23,978 | 0 |
| [Profile critical](../build/rtlgraph-corpus/runs/fused_attention_mxu0_profile_critical_1/mxu0-observation.json) | 2,103 | 2,104 | 24,038 | 3 |

The profile shortens the kernel counter window by 55 cycles relative to the built-in schedule, and by 1,327 cycles relative to the handwritten schedule. The full first-issue-to-`DBG0` observation is 60 cycles longer than the built-in run; these executions do not establish an end-to-end improvement over that baseline. Setup and the suffix's actual DMA waits remain outside the kernel bracket, and their observed costs differ between runs.

All three witnessed pairs are `VMATPOP.BF16.MXU0` followed one cycle later by overwrite `VMATMUL.MXU0` on accumulator 0. The independently decoded scalar PC pairs are 160→161, 166→167, and 210→211. Each pair has 31 overlapping cycles: the pop reads rows 1–31 while the overwrite requests operand rows 0–30, with no accumulator compute-read request. The pop destinations are `m26`, `m28`, and `m26`; following compute sources are `m30`, `m4`, and `m30`. These concrete events corroborate the extracted read-occupancy distinction without turning the finite traces into a universal timing theorem.

The [K128 MXU0 original](../build/rtlgraph-corpus/runs/mm_mxu0_64x64x128_original_1/mxu0-observation.json) also passes, with 16 computes, four pops, and two such gap-one overlaps. The [dual-MXU original](../build/rtlgraph-corpus/runs/mm_dual_128x128x128_original_1/mxu0-observation.json) passes the MXU0 portion with 32 computes, eight pops, and four overlaps. Both reach three observed MXU0 computes in flight, and all observed first writes occur at age 63. The dual report does not independently monitor MXU1 row ownership or every cross-engine bank conflict.

The completed [MXU0 observation aggregate](../build/rtlgraph-corpus/mxu0-observations-1.json) binds all 11 capture reports to their replay manifests and golden outcomes. It records 502 accepted MXU0 commands, 184 computes, 98 pops, and 19 observed overlap pairs; every measured first-write age is 63. The final K128 candidates both pass, with zero overlap pairs under the built-in model and two under the profile. Each run retains its own row observations, command counts, issue gaps, report hashes, and both kernel-window and full-run metrics. These finite observations do not expand the monitor's stated coverage into push-data validation or a complete cross-engine timing proof.
