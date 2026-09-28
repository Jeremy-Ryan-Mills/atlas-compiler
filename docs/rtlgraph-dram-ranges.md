# Configured DRAM ranges for native scheduling

The [native DMA integration](rtlgraph-dma-native.md) can now distinguish disjoint DRAM transfers when their captured configuration, address, and size are known. The `atlas-dma-profile-v2` projection enables this precision through the existing `--rtl-dma-profile` option. Version 1 keeps its conservative all-alias behavior, and the default model remains unchanged.

## Address evidence and its boundary

`DMA.CONFIG` captures a 32-bit scalar value in the global DMA base register. At a subsequent `DMA.LOAD` or `DMA.STORE`, the hardware concatenates that saved value with the instruction's 32-bit DRAM pointer. The saved command supplies later beat requests. Each beat advances by 32 bytes; the TileLink adapter aligns the request and exposes only the low 37 address bits.

For aligned operands, the compiler uses:

```text
captured = (uint64(configured_base) << 32) | low_pointer
physical_start = captured & ((1 << 37) - 1)
physical_range = [physical_start, physical_start + byte_count)
```

A transfer crossing the 37-bit boundary conservatively aliases all DRAM. It is not represented as a misleading contiguous range beyond that boundary. Unknown configuration, pointer, or size also retains the all-alias fallback. Positive aligned transfer-size admission and VMEM range checks remain in force.

The bus projection matters: configuration values `1` and `33` produce the same physical address for the same low pointer. Values `0` and `1` differ by 4 GiB. Carry from the low pointer into the high address portion must also be retained. Distinct raw 64-bit command addresses therefore do not, by themselves, establish disjoint physical memory.

The supplementary typed CIRCT analysis checks [address and size selection](../scripts/rtlgraph_dram.py#L37-L60), [saved-address calculation](../scripts/rtlgraph_dram.py#L62-L83), [request validity and counter progression](../scripts/rtlgraph_dram.py#L85-L162), and [bus address, size, and acceptance](../scripts/rtlgraph_dram.py#L164-L196). The [version-2 exporter](../scripts/rtlgraph_dram_profile.py#L25-L43) freshly rechecks both this evidence and the original DMA facts before emitting the projection. Local structural and control-function checks have explicit scope; they do not prove complete DMA queue reachability, response routing, universal progress, or legality of every address in the system memory map. The existing independent waveform monitor and full-output golden checks provide separate finite execution evidence.

## Compiler behavior

The [scalar-value analysis](../src/core/values.cpp#L55-L101) carries an optional captured DMA base alongside the general registers. `DMA.CONFIG` updates that value from its issue-time operand. Control-flow joins retain it only when every reaching value agrees; unknown paths lose the assumption. Reset initializes the base only when the selected profile supplies the reset fact.

Each [DMA footprint](../src/core/machine.cpp#L393-L419) stores its own physical byte range at launch. A later `DMA.CONFIG` or scalar-register overwrite cannot change the earlier transfer's range. The graph and checker share the same [overflow-safe overlap predicate](../src/core/machine.cpp#L6-L17). Conflicting writes still require completion through an existing matching `DMA.WAIT`; read/read and provably disjoint ranges may coexist.

This admits RMSNorm/softmax's existing concurrent output stores without inserting waits or inventing a transfer order. The scheduler still preserves DMA launch order, enforces channel and ring-slot lifetimes, and widens fixed-engine reservations across variable waits. Same-block completion remains required. Numerical LSU/VPU timing and DMA cost estimates are unchanged.

## Validation method

The handwritten `perf_vec_rmsnorm_softmax.S` and generated critical/input-priority candidates use one audited 128-word host control, the original full-output fixture, and the same simulator/runtime. Only the Atlas program-array bytes change. The performance metric is first Atlas issue through successful `DBG0` after output-DMA completion.

The explicitly enabled benchmark-marker relocation changes the cycle-counter interval, so its value is diagnostic rather than a like-for-like compute-window comparison. All non-idle instruction words, operands, transfers, and explicit waits must be preserved. Each final emitted program is checked by the native compiler and by the independent DMA/LSU/VPU event monitor after RTL execution.

The evidence remains specific to `chipyard.EE290SimConfig` and the pinned hardware IR. The cached simulator's source-to-binary build linkage remains unverified. No universal scheduling-safety proof or Merlin contract export is claimed.

## Measured results

The controlled comparison (`build/rtlgraph-dram-ranges/comparison-rmsnorm-128-1.json`) measures the complete Atlas interval defined above, including input and output DMA waits:

| Schedule | Clock edges | Edges saved against handwritten | Reduction |
|---|---:|---:|---:|
| Handwritten RMSNorm/softmax | 8,459 | — | — |
| Native compiler, critical priority | 7,554 | 905 | 10.70% |
| Native compiler, input priority | 7,554 | 905 | 10.70% |

The generated schedules start RMSNorm work after the first input transfer completes, while the second input transfer remains pending. They launch the first output store before waiting for that second input, then perform softmax while output DMA remains active. Precise DRAM ranges admit these independent accesses. The traces show 770 VPU row-active edges and 256 LSU VMEM-access edges overlapping DMA activity in each candidate, versus zero in the handwritten execution. DMA experiences 54 denied-grant edges under LSU priority; independent request/response checks still pass. These are observed overlaps, not additive cycle savings or a new numerical engine-latency claim.

All three replays pass their 1,024-word golden fixtures. Together, independent monitors check 12 DMA commands, 24 LSU commands, 39 VPU commands, and 768 matched memory request/response pairs. The comparison verifies the same host outside the program array, runtime, fixture, and first issue edge, plus identical non-idle instruction and transfer multisets. No `DMA.WAIT` is inserted. Both scheduling priorities achieve the same measured completion despite slightly different compiler cost estimates.

Eight C++ test targets pass; the new range target contains 117 checks after its final focused rerun. The DMA Python suite passes 76 tests, and the new typed-address suite passes 27. Forty-four additional local Boolean assignments and structural checks validate the extracted address-control functions. High-base aliases, low-pointer carry, unknown configuration, control-flow joins, and wrapping ranges have structural/unit coverage; the RTL performance fixture uses base zero. Each performance variant has one execution under the shared launch condition, so these measurements do not establish a universal memory-latency or performance bound.

The result index (`build/rtlgraph-dram-ranges/results-1.json`) records the final evidence, profile, compiler, tests, and replays. Strengthening the evidence checks produced `profile-2`; regenerating both candidates with it preserved their assembly bytes exactly, and the final comparison rechecked their native legality and artifact linkage. The earlier unary measurements remain a separate experiment.

## Reproduce

Use the environment in the [replay guide](rtlgraph-mxu1.md#replay) and fresh output directories. These commands use the existing typed CIRCT exporter and cached simulator; no hardware elaboration or simulator rebuild is needed.

```sh
atlas_hw=/tools/C/reednicolas/ee194-sp26-chipyard
atlas_baremetal="$atlas_hw/generators/sp26-atlas-acc/baremetal"
export LD_LIBRARY_PATH="$atlas_hw/.conda-env/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

python3 -B scripts/rtlgraph_dram.py \
  --dma-evidence build/rtlgraph-dma/local-1/dma.json \
  --output build/rtlgraph-dram-ranges/local-new
python3 -B scripts/rtlgraph_dram_profile.py \
  --dram-evidence build/rtlgraph-dram-ranges/local-new/dram.json \
  --output build/rtlgraph-dram-ranges/profile-new

python3 -B scripts/rtlgraph_dma_compile.py \
  --source build/rtlgraph-corpus/originals-1/perf_vec_rmsnorm_softmax.S \
  --assembler "$atlas_baremetal/assembler.py" \
  --atlas-opt build/rtlgraph-dram-ranges/cmake-1/atlas-opt \
  --profile build/rtlgraph-dram-ranges/profile-new/atlas-dma.profile \
  --relocate-perf-markers --output build/rtlgraph-dram-ranges/candidates-new

python3 -B scripts/rtlgraph_perf.py \
  --smoke-manifest build/rtlgraph-smoke/run-3/manifest.json \
  --assembly build/rtlgraph-dram-ranges/candidates-new/native_critical.S \
  --golden-json "$atlas_baremetal/generators/perf_vec_rmsnorm_softmax.json" \
  --control-manifest build/rtlgraph-dram-ranges/control-rmsnorm-128-1/manifest.json \
  --capture-dma --run --output build/rtlgraph-dram-ranges/run-new
```

The comparison accepts a native candidate directly against its handwritten baseline; `--memory-baseline` remains available for an additional reference. It rechecks exact emitted instruction words, observed transfer descriptors, runtime and host identity, launch edge, profile provenance, and final native legality.
