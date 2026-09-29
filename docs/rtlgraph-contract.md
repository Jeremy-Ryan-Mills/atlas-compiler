# Assembly and timing handoff

[`rtlgraph_contract.py`](../scripts/rtlgraph_contract.py#L122-L163) packages native `before.S`,
selected RTL profiles, their canonical evidence, and the exact compiler identity.
It queries the existing compiler for operand-resolved footprints and can schedule
that snapshot into `after.S`. This is a usable boundary for a Merlin emitter;
it does not implement LLVM lowering or register assignment.

The emitter must supply `atlas-opt` assembly syntax, explicit `DMA.WAIT`
instructions, and `# atlas.release` on the CSR that publishes completed output.
The compiler assumes idle engines, no pending DMA, and zero scalar registers at
program entry; runtime parameters must be established by emitted setup instructions.
DMA base reset is known only with the version-2 DMA profile. Arbitrary incoming
state is unsupported; this handoff defines no dispatch ABI.
This branch uses `atlas.release`; a different compiler annotation is not silently
substituted. Baremetal spellings require the existing
[assembly adapter](rtlgraph-dma-native.md#general-assembly-adapter).

## Contract contents

[`atlas.rtlgraph.contract.v1`](../schemas/rtlgraph-contract.schema.json) contains:

| Field | Meaning |
| --- | --- |
| `config`, `source_ir_sha256` | `EE290SimConfig` and one shared hardware-IR identity |
| `compiler`, `source` | SHA-256 identities of the binary and bundled `before.S` |
| `profiles` | Selected DMA/MXU/LSU projections and their unchanged canonical evidence |
| `footprints` | Native `atlas.footprints.v1` query output for this assembly |
| `semantics`, `limitations` | Required consumer rules and evidence boundaries |
| `merlin` | Pinned comparison reference and unsupported gap-only projection |
| `query` | Exact native query command and log identity |

The [native query](../src/core/model_dump.cpp#L71-L139) exports each block's known scalar/base entry state, instructions,
`Access` row/range ages, inclusive `Hold` intervals and capacities, MREG logical
reservations, VPU occupancy, completion-cost fields, and dependency edges directly
from the [machine model](../src/core/machine.h#L20-L96) and the graph builder. Unknown values
remain unknown. For selected RTL DMA, `at_completion` denotes a memory lifetime
through its explicit wait; `dma_cycles_estimate` supplies no completion guarantee.
Physical-port and VPU compatibility rules still require `ReservationTable`, and
control-flow admission still requires the optimizer. The query marks
`schedule_validated: false`; it is not a standalone scheduler or legality proof.

Only selected override fields are RTL-derived. The optional [LSU profile](rtlgraph-lsu-timing.md)
supplies vector read/write ages and path release; scalar LSU timing, logical reservation
policy, same-cycle visibility, and unselected rules remain inherited. Evidence files retain structural,
finite-trace, and unproved scopes. Export checks projection/report hashes, settings,
and common hardware identity; it does not rerun extraction or certify the evidence.
Bundle files use relative paths; the compiler may be relocated only with matching
bytes. Referenced hardware/build artifacts inside evidence remain external.

## Run

Use a freshly built `atlas-opt` with `--dump-footprints`. Run from this repository
and use fresh output directories. Omit unused profile options.

```sh
python3 -B scripts/rtlgraph_contract.py export \
  --source before.S --atlas-opt build/rtlgraph-lsu-profile/compiler-1/atlas-opt \
  --dma-profile build/rtlgraph-dram-ranges/profile-2/atlas-dma.profile \
  --mxu0-profile build/rtlgraph-mxu0/profile-1/atlas-mxu0.profile \
  --mxu1-profile build/rtlgraph-perf/profile-k64-2/atlas-mxu1.profile \
  --lsu-profile build/rtlgraph-lsu-profile/profile-1/atlas-lsu.profile \
  --output build/rtlgraph-contract/handoff-NEW
python3 -B scripts/rtlgraph_contract.py schedule \
  --contract build/rtlgraph-contract/handoff-NEW/contract.json \
  --priority critical --output build/rtlgraph-contract/schedule-NEW
```

The schedule command first reproduces the saved footprints with the pinned compiler.
It uses `strip-artifacts,schedule`, robust DMA mode, and the
bundled profiles, then checks the exact `after.S`. `handoff.json` records commands,
logs, and artifact identities. Native model checks do not replace RTL replay or
golden numerical checks. The output remains native assembly. Before using the baremetal assembler, translate
the supported straight-line subset with `rtlgraph_dma_compile.translate(...,
to_compiler=False)` and verify its encoding roundtrip, as the existing adapter does.
Loading and dispatching remain separate.

The fused-attention handoff in `build/rtlgraph-lsu-profile/handoff-1` exports
256 instructions with all four profiles. Its scheduled native assembly matches
the existing candidates for both priorities byte-for-byte; this adds no new RTL
performance claim.

## Merlin comparison

The optional Merlin reference was inspected at
[`81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb).
Its [issue-gap entries](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L32-L120)
and [register-gap entries](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L124-L201)
refer to Python execution-unit timing. Those fields alone cannot preserve row-level
accesses, resource capacity/port exclusions, or variable DMA completion. The bundle
reports `no-lossless-gap-projection` and lists these losses; it emits no misleading
Merlin-compatible timing YAML. Merlin can initially invoke this assembly handoff.
A native richer-model consumer remains future work; Merlin is not a dependency.

For LSU, Merlin's emitter supplies `VLOAD`/`VSTORE` operands and buffer allocation;
the bundle's optional `--lsu-profile` supplies timing to `atlas-opt`. Native footprints
then resolve the particular rows, VMEM ranges, and bank/path holds for that assembly.
Merlin can retain those footprints and evidence for diagnostics or a future target
cost model. A single completion latency would discard the distinction between
source accesses, destination writes, and path occupancy. The compiler still checks
the complete dependency and resource rules before emitting a schedule.

The newer [functional assembly contract](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/3ae2b5d76909c529df14c89933f737f9ab19be8d/ASSEMBLY_CONTRACT.md#L38-L68)
is a deferred compiler integration task. When combining branches, automatic wait
insertion must consume the selected model's operand-capture and memory-lifetime
rules, including DRAM ranges and channel/ring constraints. Test register reuse,
configuration changes, and memory conflicts before enabling the combined pipeline;
this feature continues to require explicit waits in its supported subset.
