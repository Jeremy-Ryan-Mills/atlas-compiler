# VPU issue and resource evidence

The first VPU slice checks the current `EE290SimConfig` issue restrictions, final-write release predicates, and duplicate operand-read control directly in typed CIRCT. The temporal extension below adds exact local functional-unit valid-pipeline laws and a command-attributed row-event monitor. These provide evidence for the inherited compiler model; local unit latency alone does **not** establish full instruction access ages or justify changing a scheduling distance.

The [recorded report](../build/rtlgraph-vpu/run-3/vpu.json) contains the checked functions, opcode overlap matrix, SSA cones, source locations, and input hashes. Its [typed input](../build/rtlgraph-vpu/run-3/typed.json) contains `VectorFSM`, `VectorEngine`, and `VectorEngineTop` from the verified S0 hardware artifact. The [extractor](../scripts/rtlgraph_vpu.py#L228-L264) records its exporter and direct analysis helpers, verifies those inputs remain unchanged during extraction, and records the current read-only Scala references separately. Those source hashes provide context; this is not a new elaboration or source-to-simulator build proof.

## Checked facts

| Fact | Domain checked | Compiler relevance |
| --- | --- | --- |
| Per-operation `issueBusy` mask | All 16,384 assignments of FSM state, two done flags, and two resident opcode fields | Confirms the two-slot issue policy and functional-unit exclusions |
| Each slot's final-write release predicate | All 65,536 assignments of done flag, output pulse, write counter, and limit | Explains the event that permits slot reuse; no issue age inferred |
| Duplicate reads of one logical MREG row | 16,384 register/valid assignments, 2,048 row assignments, 4 request-suppression assignments | Supports sharing one MREG read for identical VPU operands |
| Duplicate-read response-valid selection | All 8 assignments plus a checked reset-to-zero register | Confirms which response-valid signal is forwarded for the duplicated operand |
| Command, issue-mask, and first-read connections | Seven direct SSA identity paths with pinned widths | Identifies launch and resource-control boundaries |

All finite domains here are two-state bit vectors. State and counter cutpoints are treated independently, including combinations that might be unreachable. Exhaustive checking of those local functions is not a reachability or temporal proof.

### Issue policy

The [FSM policy](../../src/main/scala/atlas/vector/VectorFSM.scala#L223-L237) permits a command when idle, when both single-operation slots are done, or when the remaining slot can accommodate a compatible operation. Binary operations and row reductions require both read slots. In the double-operation state, the existing operation must reach its applicable done condition. The [exported mask](../../src/main/scala/atlas/vector/VectorFSM.scala#L484-L485) contains one bit per source opcode plus reserved bit zero.

The [typed check](../scripts/rtlgraph_vpu.py#L136-L153) evaluates the complete 31-bit mask from the `state`, `done1`, `done2`, `inst1`, and `inst2` cutpoints. Inner `VPUOp` values are zero-based; the report identifies each corresponding mask bit as opcode plus one, following the source-correlated [enum order](../../src/main/scala/atlas/vector/VectorIO.scala#L19-L26). The report retains the enum's `fp8` entry without assuming it is an exposed compiler instruction. Invalid resident encodings and invalid FSM-state encoding are included in the finite-domain check.

For one unfinished single-input operation, a different operation can use the other slot only when it does not need both slots or the same functional-unit logic. The checked groups include `vexp.bf16`/`vexp2.bf16`, `vsin.bf16`/`vcos.bf16`, `vsquare.bf16`/`vcube.bf16`, and the immediate-load family. Identical opcodes also conflict. For example, an active `vexp.bf16` can admit `vmov`, but not another exponential operation or `vmul.bf16`; an active row sum occupies the double-operation state until its release condition. The supported-operation relation agrees with the actual linked [compiler overlap functions](../src/core/machine.cpp#L34-L60), as checked below.

This is a legality mask, not an acceptance handshake. The checked wrapper paths forward command valid directly into `VectorEngine` and then `VectorFSM.instFire`, as shown by the [outer connection](../../src/main/scala/atlas/vector/VectorEngineTop.scala#L91-L92) and [engine connection](../../src/main/scala/atlas/vector/VectorEngine.scala#L63-L79). These paths do not gate launch with readiness. Separate source assertions reject [busy-resource issue](../../src/main/scala/atlas/vector/VectorEngineTop.scala#L150-L152) and [an unready sequencer](../../src/main/scala/atlas/vector/VectorEngine.scala#L495-L498). The extractor does not yet establish scalar-issue alignment or recover every assertion's temporal obligation.

### Compiler compatibility

The [joined compatibility report](../build/rtlgraph-vpu/model-1/compatibility.json) checks all **29 supported operations, 29 slot classifications, and 841 ordered overlap pairs** against the built compiler. A [small C++ probe](../scripts/tests/rtlgraph_vpu_model.cpp#L10-L55) calls `findOp`, `vpuUsesBothSlots`, and `vpuCanOverlap` from the existing `libatlas.a`; it does not copy their policy. The [Python join](../scripts/tests/rtlgraph_vpu_model.py#L44-L81) compares those results with the extracted one-unfinished-instruction matrix and checks the compiler's operation classes.

The [explicit source-correlated mapping](../scripts/tests/rtlgraph_vpu_model.py#L22-L41) follows `VPUOp` enum order and the [compiler opcode table](../src/core/asm.cpp#L53-L67). It excludes inner opcode 15, the generic `fp8` enum entry, because the compiler exposes no matching instruction. `vpack.bf16.fp8` and `vunpack.fp8.bf16` map to distinct inner opcodes 16 and 17; neither substitutes for that entry. Undeclared inner values 30 and 31 and reserved scalar issue-mask bit zero are also outside the compiler comparison. This is a documented source correlation, not a newly recovered full ISA decoder.

Before compiling the probe, the [driver](../scripts/tests/rtlgraph_vpu_model.py#L84-L130) verifies the retained typed artifact and re-evaluates its issue guard, rejecting an edited report matrix. It records the build command and hashes of the library, compiler executable, probe, source context, extraction inputs, and helpers; it checks those inputs remain unchanged during the run. The probe is freshly linked to the recorded library, but this check does not rebuild the library or prove its source lineage. The result corroborates inherited resource compatibility; it supplies no new timing values or speedup claim.

### Slot release

The [release predicates](../../src/main/scala/atlas/vector/VectorFSM.scala#L213-L215) are checked independently for both slots:

```text
done = writeDone || (dataOutFire && writeCounter == writeLimit)
```

Consequently, the final result pulse can make a slot done in the same combinational cycle while the stored `writeDone` flag is still false. Merely reaching the counter limit without an output pulse is insufficient. This supports the compiler's distinction between final-write timing and slot occupancy, but does not supply a numerical value for `vpuLive`: result latency, counter advancement, write-limit selection, and complete instruction streams remain unverified by this query.

### Identical operand reads

The [wrapper](../../src/main/scala/atlas/vector/VectorEngineTop.scala#L94-L120) compares both logical register IDs and row IDs. Two valid reads of the exact same row suppress the second physical request. One reset-to-zero `seq.firreg` delays that decision, and its value selects whether port zero or port one supplies the second operand's response-valid signal.

The [typed extraction](../scripts/rtlgraph_vpu.py#L178-L216) checks those Boolean functions, the register's clock/reset/data connections, and the first request's direct address/valid wiring. It checks response **valid control**, not the 256-bit payload or a complete memory transaction. Distinct logical registers such as `m0` and `m32` are not duplicate reads merely because they share a physical bank. This supports the compiler's [same-register, same-row VPU sharing rule](../src/core/reservations.cpp#L49-L73); other bank conflicts still apply.

## Reproduce and extend

From the compiler repository, use a new evidence directory:

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
python3 -B scripts/rtlgraph_vpu.py \
  --s0-manifest "$atlas_hw/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json" \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-vpu/replay
python3 -B scripts/tests/test_rtlgraph_vpu.py build/rtlgraph-s0/query/rtlgraph_export
```

To repeat the compiler corroboration against the existing built library:

```sh
LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib" python3 -B scripts/tests/rtlgraph_vpu_model.py \
  --vpu-report build/rtlgraph-vpu/replay/vpu.json \
  --compiler "$atlas_hw/.conda-env/bin/c++" \
  --library build/rtlgraph-compiler/libatlas.a \
  --output build/rtlgraph-vpu/model-replay
LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib" python3 -B scripts/tests/test_rtlgraph_vpu_model.py \
  build/rtlgraph-s0/query/rtlgraph_export build/rtlgraph-vpu/model-replay/rtlgraph-vpu-model
```

Seven [compiler corroboration tests](../scripts/tests/test_rtlgraph_vpu_model.py#L13-L86) pass with the linked probe and typed fixture. They reject changed overlap decisions, slot/operation classes, missing/reordered/aliased operations, altered source encoding, malformed/incomplete matrices, non-Boolean decisions, and unsupported probe arguments.

Eight [focused tests](../scripts/tests/test_rtlgraph_vpu.py#L1-L98) pass using a [typed fixture](../scripts/tests/rtlgraph-vpu.mlir#L1-L220). They check full local domains and representative overlap/release behavior, and reject changed issue/release functions, wrong widths, aliased cutpoints, feedback, hidden state, unresolved inputs, unsupported comparisons/operations, invalid assignments, and incorrect direct wiring. The evaluator admits only the explicitly implemented bit-vector operations; it does not substitute guessed behavior for unknown CIRCT operations.

## Local valid-pipeline proof

The [pipeline extractor](../scripts/rtlgraph_vpu_pipeline.py#L25-L73) follows each selected functional unit's `io_resp_valid` back to `io_req_valid` in typed CIRCT. Every intermediate operation must be an identity wire or an unconditional, one-bit `seq.firreg` using the module's clock. It rejects muxes, feedback, enables, unknown inputs, different clocks, nonzero reset values, and unsupported register attributes. The [recorded extraction](../build/rtlgraph-vpu/pipelines-2/pipelines.json) derives these lengths for eighteen modules from the pinned hardware IR:

| Functional unit | Clock edges from request-valid to response-valid | Initialization |
| --- | ---: | --- |
| `AddSubSumVec`, `MulRec`, `Rcp`, `Sqrt`, `Exp`, `SinCosVec`, `Log`, `TanhRec`, `Relu`, `RowMax`, `RowMin`, `PairWiseMax`, `PairWiseMin`, `Mov`, `ColAddVec` | 1 | Synchronous reset to zero |
| `SquareCubeVec` | 1 | Unreset valid register; one edge of history required |
| `ReduSumRec` | 6 | Six stages, each synchronously reset to zero |
| `VectorLoadImm` | 0 | Combinational identity |

This is an exact local control recurrence: with reset deasserted throughout the interval, a chain of length *N* reproduces request-valid from *N* clock edges earlier, once that much input history exists. It applies to arbitrary valid sequences and data values because the checked cone contains no data-dependent logic or stalls. It does not prove arithmetic correctness, data/valid alignment, FSM reachability, scalar launch alignment, MREG visibility, or the cached simulator's source-to-binary linkage. In particular, `VectorLoadImm`'s zero local latency is not a zero-age `VLI.ALL` destination write: command registration and wrapper control still matter.

```sh
python3 -B scripts/rtlgraph_vpu_pipeline.py \
  --s0-manifest "$atlas_hw/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json" \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-vpu/pipelines-replay
python3 -B scripts/tests/test_rtlgraph_vpu_pipeline.py build/rtlgraph-s0/query/rtlgraph_export
```

Four [pipeline tests](../scripts/tests/test_rtlgraph_vpu_pipeline.py#L15-L61) use an exported [typed fixture](../scripts/tests/rtlgraph-vpu-pipeline.mlir#L1-L15), covering reset/unreset/combinational chains and rejecting changed clock, reset, width, direction, initialization, enables, attributes, hidden inputs, feedback, and unsupported logic.

## Instruction-attributed temporal capture

The separate `--capture-vpu` mode selects a [56-signal map](../scripts/rtlgraph_vpu_vcd.py#L8-L42) for scalar/CSR events, commands, logical and physical MREG requests, response-valid pulses, physical writes, and FSM state/release cutpoints. The [row-event checker](../scripts/rtlgraph_vpu_trace.py#L79-L216) binds decoded scalar words and operands to VPU commands and independently counts the rows owned by each active slot. It processes a previous instruction's final write before a simultaneous new launch, so same-cycle slot reuse cannot relabel the old result.

For each command, the monitor records logical read ages, response ages, write ages, and final release age. It checks physical duplicate-read suppression, one-cycle physical response alignment, complete source-correlated row/address sequences, and release on the independently counted last write. Row reductions own both read and write streams; two-input arithmetic owns both read streams but one write stream. Binary slot two's FSM `dataInFire` is not treated as the physical operand response: some such operations advance both streams through slot one's fire signal. Exact-row duplicate operands retain two logical uses and one physical request.

The monitor does not assert a proposed LUT's write age and then call that timing validated. It first checks command/row ownership and records actual event ages. Complete golden-backed replay reports separately require the golden result and bind all scalar words/PCs and cycle/completion markers to the assembled program. A separate probe path verifies the original binary/reduction assembly contract, rechecks its numerical comparison success path, and labels the result `numerical_spot_checks`; it does not claim a full-tensor golden. Command-to-row binding checks opcode and register IDs, while immediate and scaling payloads remain outside the capture. These reports establish observed executions, not universal safety for all data, interference, or initial state. Numerical VPU `Access`, release, and `vpuLive` values remain inherited unless a separately justified model change is made.

```sh
# Add --capture-vpu to an otherwise unchanged rtlgraph_perf.py replay.
python3 -B scripts/rtlgraph_vpu_trace.py \
  --manifest build/rtlgraph-vpu/runs/softmax_original_2/manifest.json \
  --output build/rtlgraph-vpu/runs/softmax_original_2/vpu-events.json
python3 -B scripts/tests/test_rtlgraph_vpu_trace.py
```

Twelve [event-monitor tests](../scripts/tests/test_rtlgraph_vpu_trace.py#L62-L165) exercise variable observed output latency, paired row reductions, repeated column-reduction reads, two-input arithmetic, duplicate reads, `VLI.ALL`, two simultaneous unary instructions, and exact-final-write slot reuse. They reject malformed command binding, busy launch, wrong rows, missing responses, dropped result pulses, premature release, incomplete operands, unowned writes, unknown values, reset interruption, and truncated/reordered samples.

### Observed baseline timings

Three handwritten kernels now pass both their original full-tensor goldens and the row-ownership monitor:

| Kernel | Commands observed | Golden words | Original CSR cycles | Row-event report |
| --- | ---: | ---: | ---: | --- |
| `perf_softmax` | 6 | 512 | 337 | [softmax](../build/rtlgraph-vpu/runs/softmax_original_2/vpu-events-final.json) |
| `perf_vec_rmsnorm_softmax` | 13 | 1,024 | 706 | [RMSNorm/softmax](../build/rtlgraph-vpu/runs/rmsnorm_original_2/vpu-events-final.json) |
| `perf_unary` | 16 | 1,536 | 960 | [unary](../build/rtlgraph-vpu/runs/unary_original_2/vpu-events-final.json) |

All observed elementwise `VSUB.BF16`, `VMUL.BF16`, `VSQUARE.BF16`, `VTANH`, `VEXP`, `VSQRT`, `VLOG2`, and `VRECIP.BF16` stream 64 source rows at ages 0–63 and write 64 rows at ages 2–65, with slot release at age 65. `VREDMAX.ROW.BF16` reads both 32-row halves at ages 0–31 and writes both destinations at ages 2–33, releasing at 33. `VREDSUM.ROW.BF16` uses the same paired reads and writes at ages 7–38, releasing at 38. `VLI.ALL` writes at ages 1–64, releasing at 64. Every physical response matches the prior cycle's observed request. The RMSNorm and unary traces exercise two simultaneous independent unary commands with separately attributed slots.

These observations agree with the inherited timing values. The functional-unit shift-chain proofs explain their local latency components, while the captures establish the surrounding launch/read/write/release composition for these executions. This is new timing evidence, **not a newly shortened VPU latency or a VPU timing-profile speedup**. The monitor's physical row accounting does not claim complete accounting of otherwise unconsumed internal FSM fire pulses; for example, column reductions intentionally discard their early result pulses.

The original branching probes now also pass the separate success-path checker and temporal row monitor:

| Probe | Commands, including two initial `VLI.ALL` operations | Numerical checks | Sum of original CSR windows | Row-event report |
| --- | ---: | ---: | ---: | --- |
| `perf_vpu_binary` | 7 | 5 first-element checks | 460 | [binary](../build/rtlgraph-vpu/runs/binary_probe_1/vpu-events-final.json) |
| `perf_vpu_reduction` | 8 | 6 first-element checks | 627 | [reduction](../build/rtlgraph-vpu/runs/reduction_probe_1/vpu-events-final.json) |

The binary probe adds observed `VADD.BF16`, `VMIN.BF16`, and `VMAX.BF16` coverage with reads at ages 0–63, writes at 2–65, and release at 65. Column `VREDSUM.BF16`, `VREDMIN.BF16`, and `VREDMAX.BF16` read the BF16 pair twice at ages 0–127, write the 64 destination rows at 66–129, and release at 129. The row-min probe matches row-max's paired writes at 2–33. These executions corroborate the inherited column/two-input model and supply event ownership evidence, but only one BF16 element per probe operation has numerical validation. Their cumulative singleton timing windows omit stores, loads, comparisons, and intervening work and are not whole-kernel speedup measurements.

The [exact-search unary candidate](rtlgraph-vpu-search.md) also passes all 1,536 golden words and the [temporal monitor](../build/rtlgraph-vpu/runs/unary_exact_1/vpu-events-final.json) for its sixteen VPU commands. Its original timed region improves **960→952 CSR cycles**, eight cycles saved (0.83%), with unchanged observed access/release ages. This is a scheduling improvement using the existing model, now corroborated by these RTL observations; it does not come from reducing a CIRCT-derived latency. First issue through post-DMA `DBG0` is **10,061→10,443 clock edges** in this pair of executions, 382 more edges, so this is **not an observed whole-replay speedup**. The [kernel experiment](rtlgraph-vpu-kernels.md) separates those measurements. Across the three handwritten baselines, this candidate, and two original probes, all **66 captured VPU commands** pass row ownership and release checks; their functional validation remains split between full-tensor goldens and explicitly limited spot checks.
