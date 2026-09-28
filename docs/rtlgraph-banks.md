# MREG bank evidence and controlled conflict witnesses

This extends the MXU1 slice from instruction timing to physical MREG contention. The compiler already maps `mN` and `mN+32` to the same bank and checks per-cycle port reservations. The new work checks that mapping against typed CIRCT and exercises its scheduling consequence on the recorded `EE290SimConfig` simulator. It does not introduce a new machine-profile field.

## Structural evidence

The [address functions](../../src/main/scala/atlas/common/MregParams.scala#L72-L78) map a register to `mreg_id % 32` and a physical row to `(mreg_id // 32) * 32 + logical_row`. Thus `m0` row 31 and `m32` row 0 occupy distinct rows 31 and 32 in physical bank 0. The [MREG implementation](../../src/main/scala/atlas/mreg/MregFile.scala#L108-L127) predecodes each request independently. Its [conflict checks](../../src/main/scala/atlas/mreg/MregFile.scala#L182-L217) assert when multiple readers or multiple writers target one bank, and its [memory accesses](../../src/main/scala/atlas/mreg/MregFile.scala#L248-L257) expose one read and one write port per bank.

The [MREG extractor](../scripts/rtlgraph_mreg.py#L81-L104) uses the existing typed CIRCT exporter, then checks all eight read and eight write predecode functions. For each request port it enumerates 128 valid/register assignments for bank selection and 2,048 register/row assignments for physical row addressing. Exact input widths and permitted input dependencies are checked before enumeration; unsupported operations, state, feedback, and altered mappings are rejected. This is an exhaustive combinational check of the selected cones, not just sampled addresses.

The structural report (`build/rtlgraph-mreg/structure_1/mreg.json`) also records 32 `seq.firmem` banks, each 64 rows by 256 bits, with exactly one read port and one write port. It checks their named address, enable, clock, and write-data connections and records one-cycle memory-port latency attributes. Source locations, typed operations, exporter/source hashes, and the command are retained. These checks do not prove the entire arbitration network, response routing, memory contents, same-cycle visibility, or instruction timing.

Reproduce from the compiler repository, with the [environment](rtlgraph-mxu1.md#replay) set up and a new output directory:

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_compiler_hw="$atlas_hw/generators/sp26-atlas-acc/atlas-compiler-experiments"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/riscv-tools/lib:$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python3 scripts/rtlgraph_mreg.py \
  --s0-manifest "$atlas_compiler_hw/build/rtlgraph-s0/EE290SimConfig/s0-manifest.json" \
  --exporter build/rtlgraph-s0/query/rtlgraph_export \
  --output build/rtlgraph-mreg/structure_replay
python3 scripts/tests/test_rtlgraph_mreg.py build/rtlgraph-s0/query/rtlgraph_export
```

## Isolating a read-port conflict

The fixtures derive from the existing [K64 kernel](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L151-L207) and reuse its 1,024-word functional golden. Only the second weight tile's `VLOAD` destination and matching `VMATPUSH.W.MXU1` source are renamed, with one added delay before that push. Other tile values, MXU assignments, compute/accumulation order, and output destinations are preserved.

| Fixture | Renamed weight register | Compute-to-push issue distance | Intended distinction |
| --- | --- | ---: | --- |
| Alias boundary | `m32` | 32 | Compute finishes requesting `m0` before the push requests the other half of bank 0 |
| Deliberately invalid alias | `m32` | 31 | One simultaneous read: compute `m0` row 31 and push `m32` row 0, in bank 0 |
| Different-bank control | `m33` | 31 | Same spacing and values, but the push uses bank 1 |
| Compiler repair | `m32` | Determined by scheduling | Reschedule the invalid input while enforcing the physical bank rule |

The weight push uses the other engine read port and a different weight slot from the active compute. The logical registers also differ. This isolates physical read-port contention from register data dependencies and weight-slot exclusion. The compiler's [physical port checks](../src/core/reservations.cpp#L20-L78) should reject the invalid input and allow a corrected schedule. The intentionally slower controlled fixtures are validation cases, not performance baselines.

The [fixture generator](../scripts/rtlgraph_conflicts.py#L57-L131) checks the original assembler's instruction words before and after renaming, reverses the rename, and verifies that only the two intended MREG bitfields and one added `DELAY` change. It also runs compiler preflight checks and schedules a repair. Reproduce with the already generated K64 profile:

```sh
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
python3 scripts/rtlgraph_conflicts.py \
  --source "$atlas_baremetal/assembly/perf_mm_mxu1_64x64x64.S" \
  --assembler "$atlas_baremetal/assembler.py" \
  --golden-json "$atlas_baremetal/generators/perf_mm_mxu1_64x64x64.json" \
  --atlas-opt build/rtlgraph-compiler/atlas-opt \
  --profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile \
  --output build/rtlgraph-conflicts/fixtures_replay
```

The recorded fixtures and preflight (`build/rtlgraph-conflicts/fixtures-2/manifest.json`) show the invalid case rejected only for `MREG bank 0 port busy (m32)`, while safe/control/repaired bodies pass the selected model. The compiler repair restores a 32-cycle first-compute-to-push gap and overlaps later independent work. Model agreement alone is insufficient; the RTL outcomes and observed requests must be checked separately.

The original setup loads [A00 into `m0`](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L76-L82) from DRAM `0x90000000` and [B10 into `m6`](../../baremetal/assembly/perf_mm_mxu1_64x64x64.S#L130-L136), renamed by the fixture, from `0x90001800`. All 32 corresponding preload beats differ, as do 1,006 of 1,024 bytes; A00 row 31 also differs from B10 row 0. These are inspected K64 setup addresses, not a generalized load-address analysis. Distinct data makes a wrong-half read observable through the functional reference.

## Independent observations and evidence limits

Bank capture extends the existing perf signal selection with actual MregFile P0/P1 request valid, register, and row signals. `scripts/rtlgraph_mxu1_banks.py` checks their observed simultaneous bank use. In successful runs it also matches the P1 row stream to scalar-issued weight pushes and checks actual registered bank-return tags against the following weight writes. It does not merely assert a hypothesized issue gap.

For a positive fixture, the replay/conversion/check sequence is:

```sh
python3 scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-conflicts/fixtures_replay/alias_safe.S \
  --golden-json "$atlas_baremetal/generators/perf_mm_mxu1_64x64x64.json" \
  --output build/rtlgraph-perf/bank_safe_replay --run --capture-banks
python3 scripts/rtlgraph_mxu1_vcd.py \
  build/rtlgraph-perf/bank_safe_replay/mxu1.vcd --banks \
  --output build/rtlgraph-perf/bank_safe_replay/samples.jsonl
python3 scripts/rtlgraph_mxu1_banks.py \
  build/rtlgraph-perf/bank_safe_replay/samples.jsonl \
  --output build/rtlgraph-perf/bank_safe_replay/banks.json
```

Use distinct output directories for `bank_control`, `alias_repaired`, and `alias_invalid`. The invalid replay is expected to return failure; inspect its retained log and capture before running the bank checker with `--expect-conflict`. That flag requests an observed-collision diagnostic and does not convert a failed program into a functional pass.

An intentionally invalid run is reported separately as `bank_read_conflict_observed`, allowing a capture truncated by an RTL assertion. The expected collision must also match the fixture's register/row identities and the simulator's specific bank-conflict diagnostic. Such a result is a negative witness; it is never a passing functional or timing result. The normal checker still requires complete command/row streams and functional replays still require all golden outputs.

## Measured results

All four distinct fixture programs were replayed on `EE290SimConfig`. The combined assessment (`build/rtlgraph-conflicts/results-1.json`) verifies the assembly, golden fixture, waveform, checker-input, and simulator-log hash links before accepting each outcome:

| Fixture | Target issue gap | CSR cycles | Functional and waveform outcome |
| --- | ---: | ---: | --- |
| Alias boundary, `m32` | 32 observed | 321 | 1,024 golden words and complete temporal/bank checks pass |
| Deliberately invalid alias, `m32` | 31 scheduled | — | Exact bank-0 assertion and intended simultaneous read observed; functional replay fails |
| Different-bank control, `m33` | 31 observed | 320 | 1,024 golden words and complete temporal/bank checks pass |
| Compiler repair, `m32` | 32 observed | 291 | 1,024 golden words and complete temporal/bank checks pass |

The invalid capture directly records P0 requesting `m0` row 31 and P1 requesting `m32` row 0 at captured pre-edge cycle 82,216. Both select bank 0, at physical rows 31 and 32 respectively. The simulator reports `MregFile bank conflict: multiple read ports targeting physical bank 0 (m0 or m32)`. The control passes with the same 31-cycle spacing and `m33`, while the alias boundary passes at 32 cycles. Together these isolate the intended physical contention in these executions; they do not prove a spacing rule for all operands or resource users.

Every positive trace checks eight computes, four weight pushes, and four FP8 pops. P1 weight reads occur at ages 0–31, with responses and weight writes at 1–32. The safe, control, and repaired runs allow 65, 66, and 95 cycles of P0/P1 overlap respectively when the banks differ. The repaired schedule enforces the target 32-cycle gap while overlapping later independent work. Its 291-cycle measured window is one cycle longer than the original nonalias K64 window of 290; the deliberately padded safe/control fixtures are not performance baselines. As with the kernel comparisons, the CSR window excludes final pop drain and later output stores.

The [assessment script](../scripts/rtlgraph_conflict_report.py#L45-L101) requires complete positive command streams and output checks, and independently matches the negative assertion and observed register/row tuple. Reproduce the combined check against the recorded local runs with a new output filename:

```sh
python3 scripts/rtlgraph_conflict_report.py \
  --fixtures build/rtlgraph-conflicts/fixtures-2/manifest.json \
  --case alias_safe build/rtlgraph-perf/bank_alias_safe_1/manifest.json build/rtlgraph-perf/bank_alias_safe_1/banks.json \
  --case alias_invalid build/rtlgraph-perf/bank_alias_invalid_1/manifest.json build/rtlgraph-perf/bank_alias_invalid_1/banks.json \
  --case bank_control build/rtlgraph-perf/bank_control_1/manifest.json build/rtlgraph-perf/bank_control_1/banks.json \
  --case alias_repaired build/rtlgraph-perf/bank_alias_repaired_1/manifest.json build/rtlgraph-perf/bank_alias_repaired_1/banks.json \
  --output build/rtlgraph-conflicts/results_recheck.json
```

All generated evidence is local under ignored `build/` directories. The scripts and this guide preserve the workflow.

The monitor covers the MXU1 P0/P1 read pair. Other engines, write/write contention, simultaneous read/write behavior, all operand aliases, and universal scheduling safety remain outside this slice. The cached simulator's saved FIRRTL matches fresh S0 FIRRTL; source-to-executable build linkage remains unverified. A future rich model or optional Merlin adapter can retain this structural and finite-execution evidence without treating it as a whole-machine proof.
