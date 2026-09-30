# A real lowering walkthrough: MXU1 compute

This example follows `VMATMUL.MXU1` through Chisel, recorded FIRRTL/CIRCT, typed control queries, the checked-in profile, and a compiler schedule. The analyzed [Atlas source](https://github.com/ucb-bar/atlas-npu/tree/2ae0bef209df6db78c3de18e8f651bb43855cce9) is pinned to `2ae0bef209df6db78c3de18e8f651bb43855cce9` under `EE290SimConfig`. IR excerpts below are actual recorded lines; local evidence files require the matching artifact and toolchain, rather than a clean-checkout simulator build. Common [evidence assumptions](rtlgraph-model.md#evidence-and-assumptions) apply.

```text
Chisel -> FIRRTL -> CIRCT HW/Comb/Seq -> typed control execution
       -> source-bound profile -> operand footprints -> schedule/check
```

## Accepted event and typed control

The [sequencer](https://github.com/ucb-bar/atlas-npu/blob/2ae0bef209df6db78c3de18e8f651bb43855cce9/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala#L309-L310) accepts compute only when validity, port boundary, FIFO space, accumulator reuse, and weight/push conditions agree. Age zero is this accepted event:

```scala
  val acceptCompute  = io.cmd.valid && isCompute && p0Boundary && !fifoFull &&
                       !accReuseHazard && !computeWslotHazard && !computePushAccHazard
```

Recorded FIRRTL preserves named acceptance nodes; these are two noncontiguous lines, with the intervening nodes combining boundary and negated hazards:

```firrtl
    node _acceptCompute_T = and(io.cmd.valid, isCompute) @[generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala 309:37]
    node acceptCompute = and(_acceptCompute_T_7, _acceptCompute_T_8) @[generators/sp26-atlas-acc/src/main/scala/atlas/mxu/ipt/InnerProductTreesSequencer.scala 310:63]
```

The S0 lowering used `firtool --ir-hw -O=debug --preserve-values=named --mlir-print-debuginfo --disable-annotation-unknown --warn-on-unprocessed-annotations`. CIRCT retains the same guard in `InnerProductTreesSequencer`; `#loc15079` resolves to source line 310:63:

```mlir
    %902 = comb.and bin %io_cmd_valid_0, %isCompute {sv.namehint = "_acceptCompute_T"} : i1 loc(#loc15074)
    %903 = comb.xor bin %fifoFull, %true {sv.namehint = "_acceptCompute_T_2"} : i1 loc(#loc15075)
    %904 = comb.xor bin %accReuseHazard, %true {sv.namehint = "_acceptPopBF16_T_3"} : i1 loc(#loc15076)
    %905 = comb.xor bin %computeWslotHazard, %true {sv.namehint = "_acceptCompute_T_6"} : i1 loc(#loc15077)
    %906 = comb.xor bin %computePushAccHazard, %true {sv.namehint = "_acceptCompute_T_8"} : i1 loc(#loc15078)
    %907 = comb.and bin %902, %p0Boundary, %903, %904, %905, %906 : i1 loc(#loc22041)
    %acceptCompute = hw.wire %907 sym @sym_895  : i1 loc(#loc15079)
```

The [typed exporter](../scripts/rtlgraph/rtlgraph_query.cpp) parses/verifies CIRCT and preserves result types/indices, hierarchy, attributes, and locations. Its module-local IDs differ from textual SSA names. The exported driver is:

```json
{"id":"op1863","kind":"comb.and","operands":["op1858.r0","op1739.r0","op1859.r0","op1860.r0","op1861.r0","op1862.r0"],"results":["op1863.r0"],"result_types":["i1"]}
{"id":"op1864","kind":"hw.wire","operands":["op1863.r0"],"results":["op1864.r0"],"result_types":["i1"]}
```

Acceptance checks cover 128 Boolean assignments. The [connected instruction analysis](../scripts/rtlgraph_instruction_timing.py) executes top/sequencer/core valid state for seven MXU1 commands. Its [canonical report](../profiles/EE290SimConfig/mxu1/profile.json) records overwrite opcode 5 MREG requests at 0–31, reads/core feeds at 1–32, and writes at 3–34; accumulating opcode 6 additionally reads the accumulator at 0–31. A finite recorded RTL transaction accepted at cycle 45,959 corroborates request-to-feed gap 1, feed-to-write gap 2, and retirement age 34 with response/routing checks. Timing does not derive arithmetic semantics.

## Operand footprints and schedule

The [MXU1 profile](../profiles/EE290SimConfig/mxu1/atlas-mxu1.profile) exports `first_write_age=3` and `overwrite_acc_read_hold=0`. [Footprint construction](../src/core/machine.cpp) instantiates operands into row accesses and inclusive holds. The first-write age matches the built-in model; removing an unnecessary overwrite accumulator-read hold changes this [compiler fixture](examples/rtlgraph/mxu1-overwrite.S):

```asm
vmatpop.fp8.acc.mxu1 m8, acc0, e0
vmatmul.mxu1 acc0, m0, w0
ecall
```

Resolved multiply accesses are MREG rows `first=0,count=32,age=0,step=1`, weight rows `first=64,count=32,age=1,step=1`, and accumulator rows `first=64,count=32,age=3,step=1`. Its MXU port holds through 31, accumulator-write port through 3–34, and MXU1 in-flight capacity is 2 through 34. Built-in timing additionally holds accumulator-read ages 0–31; the profile removes it only for overwrite multiply. Accumulating multiply retains reads.

| Checked result | Built-in | MXU1 profile |
| --- | ---: | ---: |
| Pop issue | 0 | 0 |
| WAR pop → overwrite edge | distance 1 | distance 1 |
| Multiply issue | 32 | 1 |
| Intervening delay | `delay 30` | none |
| Modeled fixture cycles | 69 | 38 |

These are compiler-only modeled cycles with preinitialized weights/accumulator. Both schedules pass their selected model's check; this fixture is not a standalone numerical kernel or measured RTL speedup. Resource reservations explain the delay beyond the dependency distance.

## Reproduce

Build the standalone exporter with matching CIRCT/MLIR/LLVM development packages. Set `CIRCT_DIR`, `MLIR_DIR`, and `LLVM_DIR` to their CMake package directories, and `HARDWARE_IR` to the recorded artifact. Its expected SHA-256 is `d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2`.

```sh
cmake -S scripts/rtlgraph -B build/rtlgraph-query \
  -DCIRCT_DIR="$CIRCT_DIR" -DMLIR_DIR="$MLIR_DIR" -DLLVM_DIR="$LLVM_DIR"
cmake --build build/rtlgraph-query --target rtlgraph_export -j2
build/rtlgraph-query/rtlgraph_export "$HARDWARE_IR" \
  InnerProductTreesTop InnerProductTreesSequencer InnerProductTrees \
  > build/rtlgraph-query/mxu1-typed.json
python3 scripts/rtlgraph_instruction_timing.py \
  --query build/rtlgraph-query/rtlgraph_export --hardware-ir "$HARDWARE_IR" \
  --lsu-base profiles/EE290SimConfig/lsu/profile.json \
  --output build/rtlgraph-instruction-profiles
```

The extractor queries its required connected modules. Optional typed caches are re-exported and checked against supplied IR. `--skip-overlap` records zero VPU pair checks and is for development, not canonical evidence. The test driver optionally compares all 841 VPU engine-mask decisions with the native test executable. XLU v2 regeneration requires both source-bound connected facts and a matching passed independent witness manifest supplied as `XLU_WITNESS`. Rebuilding that simulator harness is outside the retained toolchain. To regenerate only conditional XLU engine facts as v1, omit both `--connected-evidence` and `--rtl-witness`:

```sh
python3 scripts/tests/test_rtlgraph_instruction_timing.py \
  --native-probe build/machine-profile-tests --evidence build/rtlgraph-instruction-profiles
python3 scripts/rtlgraph_xlu_connected.py \
  --query build/rtlgraph-query/rtlgraph_export --hardware-ir "$HARDWARE_IR" \
  --output build/xlu/connected.json
python3 scripts/rtlgraph_xlu.py \
  --query build/rtlgraph-query/rtlgraph_export --hardware-ir "$HARDWARE_IR" \
  --connected-evidence build/xlu/connected.json --rtl-witness "$XLU_WITNESS" \
  --output build/xlu/profile
```

For the compiler fixture, build `atlas-opt` using the [README](../README.md), then run:

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
