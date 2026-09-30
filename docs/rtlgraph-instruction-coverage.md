# Connected instruction timing coverage

The instruction extractor executes selected control cones from the pinned `EE290SimConfig` CIRCT hardware IR. It follows `hw.instance` connections through wrappers, sequencers, and functional-unit valid pipelines. It does not replace those pipelines with latencies copied from Chisel or the compiler.

`ControlCircuit` rejects timing dependencies on payload inputs, unsupported operations, changed clocks, and asynchronous register semantics. It preserves finite integer widths and synchronous updates. Unreset state starts unknown; reset and idle flushing must establish known timing outputs before measurement. Operand metadata may remain unknown while its valid bit is clear.

## Newly covered facts

| Component | Extracted behavior | Compiler projection |
| --- | --- | --- |
| VPU, 29 implemented commands | Physical MREG row streams, active register reservations, per-operation issue-busy masks, and two-command overlap | `atlas-vpu-profile-v1`; read age 0, ordinary write age 2, row-sum 7, column-reduction 66, pack/unpack 3, immediate 1 |
| Scalar LSU | Accepted scalar command → command register → LSU request/response → scalar response register/writeback | LSU v2 adds memory age 1, scalar/scale-register load write age 3, and next-load age 3 |
| MXU0 | All seven commands through `SystolicArrayTop`, sequencer, and systolic valid pipeline | MXU0 v2 adds first accumulator-write age 63; overwrite still has no accumulator-read hold |
| MXU1 | All seven commands through `InnerProductTreesTop`, sequencer, and core valid pipeline | Existing first-write age 3 and overwrite-read projection gain connected control evidence |

All streams contain 32 rows per MREG. Pair operations select the second register as required by the actual FSM. `VPACK.BF16.FP8` writes every other cycle; `VUNPACK.FP8.BF16` writes 64 consecutive rows. `VREDSUM.ROW.BF16` writes both destination registers together. Column reductions read the source pair twice. The reserved VPU `fp8` command is excluded.

The new values agree with the built-in model. Their immediate contribution is traceable coverage and checking of future RTL changes, not a new speedup claim.

## Temporal checks and scope

The extractor checks all 841 ordered VPU command pairs at the earliest second launch permitted by the engine's per-operation issue-busy mask. With distinct operands, both physical row streams must equal their isolated executions. This checks the two-slot control behavior; it does not remove compiler MREG reservations or establish every frontend assertion for aliased operands.

For each MXU, nine finite cases combine compute, weight/accumulator push, and pop at selected boundaries. A negative MXU1 case records that accumulator push followed by `VMATMUL.ACC.MXU1` at distance 32 produces no second compute writes; the checked positive boundary is 33. Stream readiness alone is insufficient for command acceptance.

Hardware mutations remove a VPU valid stage, introduce payload-dependent control, and add an MXU1 valid stage. The extractor must respectively change its timing result, reject the cone, and delay writeback. Unit tests additionally exercise hierarchy wiring, register feedback, unknown initialization, signed comparisons, and malformed clock/reset structures.

These are bounded control executions with arbitrary payload excluded from the control cone. Numerical correctness, SRAM response behavior under contention, and whole-frontend legality remain separate checks. In particular, `ScalarCore` asserts logical MREG hazards even though it does not stall those commands. SV assertion regions are outside this extractor's output cones. Existing logical reservation floors remain in force.

Scalar extraction has explicit `issueScalarLoad`, `issueScalarStore`, decoded `mem_cmd`, and `hostStart` cutpoints. It assumes the accepted-command cutpoints and a one-cycle VMEM response. Its report preserves the earlier vector LSU report rather than relabeling those facts as newly rederived.

## Reproduce

```sh
python3 scripts/rtlgraph_instruction_timing.py \
  --query build/rtlgraph-s0/query/rtlgraph_export \
  --hardware-ir /path/to/atlas.hw.mlir \
  --lsu-base profiles/EE290SimConfig/lsu/profile.json \
  --output build/rtlgraph-instruction-profiles
python3 scripts/tests/test_rtlgraph_instruction_timing.py
```

Compare all 841 extracted engine-mask decisions with the native compiler using the existing test executable:

```sh
python3 scripts/tests/test_rtlgraph_instruction_timing.py \
  --native-probe build/rtlgraph-integrated/machine-profile-tests \
  --evidence build/rtlgraph-instructions/final-independent
```

The exporter source remains available at [`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/dd7342ce6a3c4d051544bf719086edb59cb80765/scripts/rtlgraph_query.cpp). The optional `--typed` cache is re-exported and compared against the supplied hardware IR before use. `--skip-overlap` records zero VPU pair checks and is intended for quick development iterations, not the checked-in evidence.

Reports retain the hardware, exporter, extractor, and typed output hashes. This distinction between hardware identity, facts, assumptions, and evidence is also suitable for a later Merlin adapter; no Merlin integration is implemented here.
