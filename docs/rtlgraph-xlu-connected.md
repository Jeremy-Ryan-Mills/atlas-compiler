# Connected XLU timing evidence

The `EE290SimConfig` XLU profile now covers the connected `AtlasCore` → `XluEngine` → `MregFile` boundary. The hardware artifact is SHA-256 `d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2`.

[Connected facts](../profiles/EE290SimConfig/xlu/connected.json) establish direct command/request/response wiring and execute the actual MREG arbitration and response logic. Each physical bank has one read and one write port, a one-cycle read response, and **undefined same-address read-under-write**. A competing higher-priority read loses the XLU response; hardware does not retry it. The compiler must continue excluding those conflicts.

| Event after accepted `VTRPOSE.XLU` | Ages |
|---|---|
| Source row requests | 1–32 |
| Source responses | 2–33 |
| Destination row writes | 34–65 |
| First possible next XLU launch | 66 |
| Source/destination logical busy signals | 1–33 / 1–65 |

The frontend's XLU launch cone has no dependence on either MREG busy input, and XLU busy does not feed the frontend. **This does not make overlapping launches legal:** separate ScalarCore software-scheduling assertions inspect those busy signals. An XLU command at age 65 is ignored; age 66 is accepted.

## Scheduling consequence

The v2 profile retains register-wide reservations and XLU engine occupancy. Its explicit `read_release_age=33` and `write_release_age=65` fields come from the connected active-register signals, zero-latency tracker mapping, and frontend assertion predicates. They replace unexplained fixed compiler floors with source-bound reservation evidence. The extractor evaluates the actual ScalarCore RAW and WAR/WAW assertion predicates for all 64 architectural register IDs, totaling 384 cases. A `VSTORE` whose source is XLU's destination is prohibited while the destination write-busy bit remains set, through age 65. Its earliest legal issue is age 66. An instruction that overwrites XLU's source generally must respect read-busy through age 33; the decoded instruction controls which checks apply. For example, the `VLOAD` predicate checks pending writes but not pending reads. The compiler keeps its current conservative rule rather than generalizing that instruction-specific exception.

The independent payload experiment deliberately explores below that contract: `VSTORE` issued at gap 34 reads at `35+r`, after XLU writes at `34+r`, and produces correct VMEM rows at ages 37–68. Reusing a source row on the cycle after its read request also preserves XLU's captured bytes. Those results establish datapath behavior, **not legal compiler schedules**. Gap 34 still violates the frontend's RAW assertion. Exploiting it would require an agreed hardware scheduling-contract change and revalidation, which this compiler-only work does not make.

Gap 33 additionally reads and writes the same physical row on the same edge, which is undefined in CIRCT. Neither the logical reservation nor this same-cycle restriction has been relaxed. The connected analysis confirms the existing release ages and explains why they remain necessary.

## Independent checks

[The RTL runner](../scripts/rtlgraph_xlu_rtl.py) copies complete `XluEngine`, `MregFile`, and `LSU` modules from the pinned IR, parses and lowers them with CIRCT, then builds a Verilator executable. The wrapper connects the same request/response ports verified by the structural analysis. It injects commands directly and observes real LSU VMEM writes.

[The numerical harness](../scripts/tests/xlu_connected_rtl_check.cpp) passed 192 cases and 196,608 byte comparisons: all 64 source IDs, in-place destinations, different banks, and high/low registers sharing a bank. Every case overwrites source rows after their read edge and consumes the result at gap 34. These cases exclude ScalarCore and exercise a numerical overlap that its assertions prohibit. A separate conflicting-read case receives only 31 responses and does not finish.

At gap 33 the independent monitor observes 32 prohibited read/write collisions. Verilator happened to produce correct bytes in that run. That numerical result cannot validate source-undefined behavior. The typed evaluator explicitly propagates unknown data for such a collision and rejects the payload claim.

The witness records hashes for the input IR, extracted modules, emitted SV, wrapper, harness, tools, and executable. These remain finite control cases with arbitrary symbolic payload routing and independent numerical executions, not an unbounded proof. Frontend assertion predicates are checked separately from the three-module numerical harness; instruction-fire and decoded fields are explicit cut inputs in those checks. Scalar instruction decoding and full-system VMEM/DMA behavior are outside this witness.

## Reproduce

The [typed exporter](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/dd7342ce6a3c4d051544bf719086edb59cb80765/scripts/rtlgraph_query.cpp) is retained in history. With its binary and the pinned IR available:

```sh
python3 scripts/rtlgraph_xlu_connected.py --query RTLGRAPH_EXPORT \
  --hardware-ir atlas.hw.mlir --output build/xlu/connected.json
python3 scripts/rtlgraph_xlu_rtl.py --query RTLGRAPH_EXPORT \
  --hardware-ir atlas.hw.mlir --circt-opt CIRCT_OPT --verilator VERILATOR_BIN \
  --cxx CXX --ar AR --output build/xlu/rtl
python3 scripts/rtlgraph_xlu.py --query RTLGRAPH_EXPORT \
  --hardware-ir atlas.hw.mlir --connected-evidence build/xlu/connected.json \
  --rtl-witness build/xlu/rtl/manifest.json --output build/xlu/profile
```

Set `VERILATOR_ROOT` to the installed Verilator share directory when using a relocated toolchain. The runner explicitly passes the C++ compiler and archiver to avoid stale paths in generated makefiles. It lowers memories with `--hw-memory-sim` before exporting Verilog.

The same hardware identity, assumptions, physical access rules, and evidence categories can be supplied to a future Merlin adapter. This change implements only the Atlas compiler profile and its validation.
