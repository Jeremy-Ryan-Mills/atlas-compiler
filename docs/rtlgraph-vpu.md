# VPU issue and resource evidence

The first VPU slice checks the current `EE290SimConfig` issue restrictions, final-write release predicates, and duplicate operand-read control directly in typed CIRCT. It confirms useful parts of the inherited compiler model. It does **not** derive numerical instruction latencies or justify changing a scheduling distance yet.

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

The next useful temporal slice is a VPU instruction family used by softmax or RMSNorm: associate scalar launch with actual source-row requests, response-valid pulses, destination writes, and final slot release. A row reduction also needs paired-read/write attribution. Successful kernel replay supplies functional evidence, while these local structural checks identify the control signals to monitor. Until that work validates the access ages and occupancy intervals, the VPU timing portions of `Access`, `Hold`, and `Footprint` remain inherited from the built-in model.
