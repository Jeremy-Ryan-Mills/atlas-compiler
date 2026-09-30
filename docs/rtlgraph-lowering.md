# A real lowering walkthrough: accepted MXU1 compute

This example follows `VMATMUL.MXU1` from Chisel through recorded FIRRTL and CIRCT HW/Comb/Seq, a typed control query, the checked-in profile, and an operand-resolved compiler schedule. The excerpts below are actual artifact lines; the stage sequence is an explanatory diagram. The target wrapper is publicly described in [`EE290Configs.scala`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26). The analyzed Atlas source is pinned to `2ae0bef209df6db78c3de18e8f651bb43855cce9` in its [canonical repository](https://bwrcrepo.eecs.berkeley.edu/ee194-290c-sp26/sp26-atlas-acc), which requires repository access; no public mirror has been verified.

```text
Chisel → elaborated FIRRTL → CIRCT HW/Comb/Seq → typed SSA/control execution
       → source-bound partial profile → operand footprints → schedule/check
                                                 ↘ independent RTL observations
```

## 1. Preserve the accepted event through lowering

The source is `src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala`, lines 309–310 in the pinned Atlas revision. Its acceptance predicate includes command validity, port boundary, FIFO capacity, accumulator reuse, and weight/push hazards. Age zero means an **accepted engine command**, not merely an asserted command-valid signal.

```scala
  val acceptCompute  = io.cmd.valid && isCompute && p0Boundary && !fifoFull &&
                       !accReuseHazard && !computeWslotHazard && !computePushAccHazard
```

The recorded S0 FIRRTL preserves this predicate as named nodes. The following are two exact, noncontiguous lines, 1059480 and 1059489; the intervening nodes combine `p0Boundary` and the four negated hazards. This is an excerpt, not a standalone FIRRTL module.

```firrtl
    node _acceptCompute_T = and(io.cmd.valid, isCompute) @[generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala 309:37]
    node acceptCompute = and(_acceptCompute_T_7, _acceptCompute_T_8) @[generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala 310:63]
```

S0 ran `firtool --ir-hw -O=debug --preserve-values=named --mlir-print-debuginfo --disable-annotation-unknown --warn-on-unprocessed-annotations`. The actual hardware-IR lines 521384–521390 show the same guard in `InnerProductTreesSequencer`, a `hw.module` beginning at line 519525. SSA operands and location references below are preserved exactly. `#loc15079`, defined at line 623123, maps back to source line 310:63.

```mlir
    %902 = comb.and bin %io_cmd_valid_0, %isCompute {sv.namehint = "_acceptCompute_T"} : i1 loc(#loc15074)
    %903 = comb.xor bin %fifoFull, %true {sv.namehint = "_acceptCompute_T_2"} : i1 loc(#loc15075)
    %904 = comb.xor bin %accReuseHazard, %true {sv.namehint = "_acceptPopBF16_T_3"} : i1 loc(#loc15076)
    %905 = comb.xor bin %computeWslotHazard, %true {sv.namehint = "_acceptCompute_T_6"} : i1 loc(#loc15077)
    %906 = comb.xor bin %computePushAccHazard, %true {sv.namehint = "_acceptCompute_T_8"} : i1 loc(#loc15078)
    %907 = comb.and bin %902, %p0Boundary, %903, %904, %905, %906 : i1 loc(#loc22041)
    %acceptCompute = hw.wire %907 sym @sym_895  : i1 loc(#loc15079)
```

The same module also contains real sequential state, for example hardware-IR line 521307 below. Its location resolves to source line 232:30. Counting these registers cannot establish command acceptance, response alignment, row order, or destination routing; those require following the valid/control connections and executing the state transitions.

```mlir
    %inflightValid_0 = seq.firreg %2365 clock %clock sym @sym_875 reset sync %reset, %false {firrtl.random_init_start = 118 : ui64} : i1 loc(#loc15033)
```

## 2. Query typed identities, then reason about control

The retained [typed exporter](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/dd7342ce6a3c4d051544bf719086edb59cb80765/scripts/rtlgraph_query.cpp#L32-L128) parses and verifies CIRCT IR, preserving types, result indices, hierarchy, attributes, and source locations. This field projection is taken from the freshly rebuilt export of `InnerProductTreesSequencer`; `op1863.r0` is the actual driver of `acceptCompute`. Exporter IDs are local to this module and differ from textual SSA names.

```json
{"id":"op1863","kind":"comb.and","operands":["op1858.r0","op1739.r0","op1859.r0","op1860.r0","op1861.r0","op1862.r0"],"results":["op1863.r0"],"result_types":["i1"]}
{"id":"op1864","kind":"hw.wire","operands":["op1863.r0"],"results":["op1864.r0"],"result_types":["i1"]}
```

The recorded `build/rtlgraph-mxu1/events.json` checks the acceptance function across 128 Boolean assignments and reports `guard_function_matches`. The current [connected timing analysis](../scripts/rtlgraph_instruction_timing.py) executes the top, sequencer, and core valid pipeline for all seven MXU1 commands. Its [canonical report](../profiles/EE290SimConfig/mxu1/profile.json) records, for overwrite opcode 5, MREG requests at ages 0–31, active reads/core feeds at 1–32, and accumulator writes at 3–34. It separately records accumulating opcode 6's accumulator reads at 0–31. Reset/idle entry, one-cycle responses, and no conflicting external port traffic are explicit assumptions; all-state acceptance and numerical arithmetic remain separate obligations.

One actual RTL transaction corroborates the stream: `build/rtlgraph-mxu1-trace/run-1/timing.json` reports accepted cycle **45959**, `Matmul`, MREG 0, weight slot 0, accumulator 0, request-to-feed gap 1 for all 32 rows, feed-to-write gap 2, and retirement age 34. Its settled-before-rising-edge trace checks response alignment and accumulator routing. This finite observation supports the analyzed event timing; it does not establish every overlap or input. The cached simulator's source-to-executable linkage remains unverified; see [validation limits](rtlgraph-model.md#evidence-and-assumptions).

## 3. Resolve the profile into a real schedule

The [MXU1 profile](../profiles/EE290SimConfig/mxu1/atlas-mxu1.profile) exports `first_write_age=3` and `overwrite_acc_read_hold=0`. The compiler's [footprint construction](../src/core/machine.cpp#L283-L297) maps assigned operands into row streams and inclusive holds. First-write age 3 already matches the built-in model; removing the overwrite operation's unnecessary accumulator-read hold is the decisive difference in this [small compiler fixture](examples/rtlgraph/mxu1-overwrite.S).

```asm
vmatpop.fp8.acc.mxu1 m8, acc0, e0
vmatmul.mxu1 acc0, m0, w0
ecall
```

Actual `--dump-footprints` output resolves `VMATMUL.MXU1 acc0, m0, w0` as MREG rows `first=0,count=32,age=0,step=1`, MXU1 weight rows `first=64,count=32,age=1,step=1`, and MXU1 accumulator rows `first=64,count=32,age=3,step=1`. The compiler's `MXU port` index 4 is held through age 31, accumulator-write index 2 through ages 3–34, and MXU1 in-flight capacity is 2 through age 34. The built-in model additionally holds accumulator-read index 2 at ages 0–31; the selected profile removes that hold for overwrite multiplication. `VMATMUL.ACC.MXU1` retains its reads and read hold.

| Checked compiler result | Built-in | MXU1 profile |
| --- | ---: | ---: |
| Pop issue | 0 | 0 |
| WAR edge, pop → overwrite | distance 1 | distance 1 |
| Overwrite multiply issue | 32 | 1 |
| Emitted delay between pop and multiply | `delay 30` | none |
| Entire fixture, modeled cycles | 69 | 38 |

Both generated schedules pass the selected compiler model's check. These are **modeled cycles for a compiler-only probe**, with preinitialized weights/accumulator, not a standalone numerical RTL run or a kernel speedup measurement. Dependency distances alone are insufficient: the resource table explains the additional built-in delay. Frontend reservations still matter; connected XLU evidence permits some datapath overlap that ScalarCore assertions forbid, so [XLU schedules retain logical release ages](rtlgraph-xlu-connected.md#scheduling-consequence).

## 4. Provenance and checked reproduction

Paths are relative to this compiler repository unless stated otherwise. S0 artifacts are local, ignored evidence, not clean-checkout inputs; their full files are intentionally not copied here. `S0` below denotes the recorded directory `/bwrcq/C/reednicolas/ee194-sp26-chipyard/generators/sp26-atlas-acc/atlas-compiler-experiments/build/rtlgraph-s0/EE290SimConfig`. Its `s0-manifest.json` supplies the artifact identities.

| Artifact | SHA-256 |
| --- | --- |
| Pinned `InnerProductTreesSequencer.scala` | `0ded67cccb89f697b008762f6905de99f93da54bbf965dcd9a3470af11ee6193` |
| `S0/elaboration/chipyard.harness.TestHarness.EE290SimConfig.fir` | `197aab9e74d40e98d4291bd8ffb34e018c74e9e5146196384f1f3405c3c9e989` |
| `S0/atlas.hw.mlir` | `d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2` |
| Fresh three-module export, `build/rtlgraph-review/rebuilt-mxu1.json` | `16e25fe52964ea252b62cead41d411b5e99dafa0416aadac7890a5747d7af4c0` |
| `build/rtlgraph-instructions/final/typed.json`, canonical profile input | `3eb7f807419a5af1742801ebd00b3c02edb1b508952cc1ce2cac5a313646633b` |
| Canonical MXU1 `profile.json` | `e43c3db115a6d60c5764b9979c14bf07932675b185cbfe4ad936e27b4cd0bf76` |
| Trace `timing.json` | `0207ec24c57a2d162cc7f19752963b1b30c628f18a5e552e4221255b3d7bd29b` |
| Compiler fixture | `fe4db90e6717634831d4fafb8a212011e51f3c690eeab73cbf8a7516b269c4b9` |
| Checked `build/rtlgraph-cfg-static/atlas-opt` | `057626f940e7f0d70502f2412f0298963697ebbe209948d8dbae26ac3258c067` |

The exporter sources live in retained, reachable [`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765/scripts); the current checkout does not contain them. The following standalone recipe was checked with GCC 11.5 and the recorded CIRCT/LLVM/MLIR package prefix. A fresh export was byte-identical to the cached exporter for all three MXU1 modules. This repairs the source-retrieval gap; a clean checkout still needs matching external CIRCT packages and the recorded S0 IR to reproduce the hardware analysis. No elaboration or full-system build is required for these queries.

```sh
mkdir -p build/rtlgraph-lowering/export-src
git archive dd7342ce6a3c4d051544bf719086edb59cb80765 \
  scripts/rtlgraph_query.cpp scripts/CMakeLists.txt | \
  tar -x -C build/rtlgraph-lowering/export-src --strip-components=1
# Set CIRCT_PREFIX to the matching installed CIRCT/LLVM/MLIR package prefix.
cmake -S build/rtlgraph-lowering/export-src -B build/rtlgraph-lowering/export-build \
  -DCMAKE_C_COMPILER=/usr/bin/gcc -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DCIRCT_DIR="$CIRCT_PREFIX/lib/cmake/circt" \
  -DMLIR_DIR="$CIRCT_PREFIX/lib/cmake/mlir" -DLLVM_DIR="$CIRCT_PREFIX/lib/cmake/llvm"
cmake --build build/rtlgraph-lowering/export-build -j2
# Set S0 to the recorded artifact directory above.
build/rtlgraph-lowering/export-build/rtlgraph_export "$S0/atlas.hw.mlir" \
  InnerProductTreesTop InnerProductTreesSequencer InnerProductTrees \
  > build/rtlgraph-lowering/mxu1-typed.json
```

Build `atlas-opt` using the [repository instructions](../README.md), or set `OPT` to an existing build. The commands below were checked with `OPT=build/rtlgraph-cfg-static/atlas-opt`, identified above, producing `build/rtlgraph-lowering/{builtin,profile}.{json,S}`. On this host the recorded environment's library directory must be added to `LD_LIBRARY_PATH`; that is a local runtime requirement, not a compiler input convention.

```sh
mkdir -p build/rtlgraph-lowering
OPT=build/atlas-opt
INPUT=docs/examples/rtlgraph/mxu1-overwrite.S
PROFILE=profiles/EE290SimConfig/mxu1/atlas-mxu1.profile
"$OPT" "$INPUT" --dump-footprints build/rtlgraph-lowering/builtin.json
"$OPT" "$INPUT" --experimental-mxu1-profile "$PROFILE" \
  --dump-footprints build/rtlgraph-lowering/profile.json
"$OPT" "$INPUT" --passes strip-artifacts,schedule --schedule-priority input \
  -o build/rtlgraph-lowering/builtin.S
"$OPT" "$INPUT" --passes strip-artifacts,schedule --schedule-priority input \
  --experimental-mxu1-profile "$PROFILE" -o build/rtlgraph-lowering/profile.S
"$OPT" build/rtlgraph-lowering/builtin.S --check
"$OPT" build/rtlgraph-lowering/profile.S --experimental-mxu1-profile "$PROFILE" --check
```

Profile extraction can be rerun with `scripts/rtlgraph_instruction_timing.py --query EXPORTER --hardware-ir "$S0/atlas.hw.mlir" --lsu-base profiles/EE290SimConfig/lsu/profile.json --output OUTPUT`; it queries its required connected modules itself. The tracked profile assumptions and [instruction coverage](rtlgraph-instruction-coverage.md) define the analysis scope. A future [Merlin handoff](rtlgraph-contract.md) can preserve these footprints and explicit completion semantics; this walkthrough adds no Merlin implementation.
