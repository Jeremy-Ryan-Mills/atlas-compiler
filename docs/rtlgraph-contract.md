# Assembly and timing handoff

The RTL-graph handoff gives a compiler emitter a reproducible boundary to the
existing Atlas scheduler. `rtlgraph_contract.py` packages native `before.S`, the
selected RTL profiles and evidence, operand-resolved footprints, and the exact
`atlas-opt` binary. Verification reproduces those footprints before the bundle
is used to schedule `after.S`.

This is usable by a Merlin emitter today. It does not implement linalg/Atlas/LLVM
lowering, register assignment, assembly translation, loading, or dispatch.

## Input contract

The emitter supplies `atlas-opt` assembly syntax with:

- operations, registers, addresses, and runtime scalar setup resolved;
- explicit matching `DMA.WAIT` instructions;
- `# atlas.release` on the CSR that publishes completed output; and
- idle engines, no pending DMA, and zero scalar registers at entry.

The current subset does not define arbitrary incoming machine state or a dispatch
ABI. Emitters must produce the native syntax accepted by `atlas-opt`. The
historical baremetal adapter remains available with the research tooling at
`dd7342c`.
This feature does not insert DMA waits.

`atlas.rtlgraph.contract.v1` records:

| Field | Meaning |
| --- | --- |
| `config`, `source_ir_sha256` | `EE290SimConfig` and the common hardware-IR identity |
| `compiler`, `source` | SHA-256 identities of `atlas-opt` and bundled `before.S` |
| `profiles` | Selected MXU0, MXU1, DMA, and LSU projections plus canonical evidence |
| `footprints` | Native operand-resolved `Access`, `Hold`, reservation, and dependency data |
| `semantics`, `limitations` | Assembly, age, completion rules, and evidence boundaries |
| `merlin` | Pinned comparison reference and precision-loss report |

The native footprint query includes known scalar/base entry state, instructions,
row and byte-range accesses, inclusive resource holds, logical MREG reservations,
VPU occupancy, completion-cost fields, and dependency edges. Unknown values stay
unknown. Physical port/capacity checks and control-flow admission still require
the compiler; a footprint file alone is not a scheduler or legality proof.

Only selected fields are RTL-derived. Other rules remain inherited as described
in the [model guide](rtlgraph-model.md). Export verifies profile/report hashes,
settings, and common hardware identity, but it does not rerun extraction or
prove the evidence. A scheduled result still requires numerical and RTL
validation appropriate to the kernel.

## Create and schedule a bundle

Use a freshly built `atlas-opt` that supports `--dump-footprints`:

```sh
python3 -B scripts/rtlgraph_contract.py export \
  --source before.S --atlas-opt build/atlas-opt \
  --mxu0-profile profiles/EE290SimConfig/mxu0/atlas-mxu0.profile \
  --mxu1-profile profiles/EE290SimConfig/mxu1/atlas-mxu1.profile \
  --dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --lsu-profile profiles/EE290SimConfig/lsu/atlas-lsu.profile \
  --output build/rtlgraph-contract/handoff

python3 -B scripts/rtlgraph_contract.py verify \
  --contract build/rtlgraph-contract/handoff/contract.json

build/atlas-opt build/rtlgraph-contract/handoff/before.S \
  -o build/rtlgraph-contract/handoff/after.S \
  --dma-timing robust \
  --experimental-mxu0-profile build/rtlgraph-contract/handoff/mxu0/atlas-mxu0.profile \
  --experimental-mxu1-profile build/rtlgraph-contract/handoff/mxu1/atlas-mxu1.profile \
  --rtl-dma-profile build/rtlgraph-contract/handoff/dma/atlas-dma.profile \
  --rtl-lsu-profile build/rtlgraph-contract/handoff/lsu/atlas-lsu.profile
```

Omit unused profiles. The verify step reruns the native footprint query with the
pinned compiler and requires exact equality. The normal optimizer invocation then
runs its scheduling and final checks. Bundle artifact paths are relative except
for the compiler, which may move only when its bytes still match; `verify`
accepts `--atlas-opt` for that relocated but byte-identical binary.

## Merlin relationship

The comparison point is
[`ucb-bar/merlin@81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb).
Its Atlas contract expresses
[`minimum_issue_gap`](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L32-L120)
and
[`register_dependency_gap`](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L124-L201)
entries. Those scalar gaps cannot losslessly represent row streams, bank/port
selection, resource capacity, or variable DMA completion. The bundle therefore
reports `no-lossless-gap-projection` instead of emitting misleading timing YAML.

The immediate integration is:

```text
Merlin lowering and buffer assignment
    -> native before.S
    -> RTL profiles + resolved footprints
    -> atlas-opt scheduling and checking
    -> after.S
    -> assembler, execution, and numerical validation
```

Merlin can retain the profiles, footprints, and evidence for diagnostics and a
future target cost model. A richer native consumer should preserve row access
ages, logical reservations, physical bank/path holds, capacities, and explicit
completion events.

The newer compiler
[`ASSEMBLY_CONTRACT.md`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/3ae2b5d76909c529df14c89933f737f9ab19be8d/ASSEMBLY_CONTRACT.md#L38-L68)
is a deferred branch-integration concern. Automatic wait insertion must use the
selected model's issue-time capture, DRAM ranges, channel/ring reuse, and memory
conflict rules. It should be tested for register reuse and configuration changes
when the branches meet; it does not require redesigning this handoff.
