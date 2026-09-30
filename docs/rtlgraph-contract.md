# Assembly and timing handoff

`rtlgraph_contract.py` bundles native `before.S`, selected profiles/evidence, resolved footprints, and the exact `atlas-opt` identity. Verification reproduces footprints before scheduling `after.S`. This supports external consumers such as a future [Merlin](https://github.com/ucb-bar/merlin) adapter; lowering, register assignment, translation, loading, and dispatch are outside the interface.

## Input and schema

The emitter must resolve operations, registers, addresses, and runtime scalar setup into native `atlas-opt` syntax. Entry assumes idle engines, no pending DMA, and zero scalar registers. Matching DMA waits must already be present for this read-only export; prepare them manually or with [wait insertion](rtlgraph-model.md#dma-and-control-flow). Preserve `# atlas.release` on the publishing CSR as specified in the [README](../README.md).

The bundle schema is `atlas.rtlgraph.contract.v1`; its resolved query is `atlas.footprints.v1`. Component format identifiers are `atlas-mxu0-profile-v2`, `atlas-mxu1-profile-v1`, `atlas-dma-profile-v2`, `atlas-lsu-profile-v2`, `atlas-xlu-profile-v2`, and `atlas-vpu-profile-v1`.

| Field | Meaning |
| --- | --- |
| `config`, `source_ir_sha256` | `EE290SimConfig` and common hardware-IR identity |
| `compiler`, `source` | SHA-256 identities of `atlas-opt` and bundled `before.S` |
| `profiles` | Selected component projections and evidence |
| `footprints` | Resolved accesses, inclusive holds, logical reservations, VPU occupancy, completion estimates, and dependencies |
| `semantics`, `limitations` | Assembly, age/completion conventions, and evidence scope |
| `merlin` | Pinned comparison reference and precision-loss report |

Footprints include known scalar/base entry state and, with RTL DMA, launch-site accesses and pending incoming lifetimes per block. Unknown values remain unknown. The query is not a scheduler or legality proof: physical capacities and control-flow admission still require the compiler. Export checks profile/report hashes, settings, and common hardware identity; [the model guide](rtlgraph-model.md#evidence-and-assumptions) defines the evidence boundary.

## Export, verify, and schedule

Build `atlas-opt` with `--dump-footprints` support. Omit unused profiles:

```sh
python3 -B scripts/rtlgraph_contract.py export \
  --source before.S --atlas-opt build/atlas-opt \
  --mxu0-profile profiles/EE290SimConfig/mxu0/atlas-mxu0.profile \
  --mxu1-profile profiles/EE290SimConfig/mxu1/atlas-mxu1.profile \
  --dma-profile profiles/EE290SimConfig/dma/atlas-dma.profile \
  --lsu-profile profiles/EE290SimConfig/lsu/atlas-lsu.profile \
  --xlu-profile profiles/EE290SimConfig/xlu/atlas-xlu.profile \
  --vpu-profile profiles/EE290SimConfig/vpu/atlas-vpu.profile \
  --output build/rtlgraph-contract/handoff
python3 -B scripts/rtlgraph_contract.py verify \
  --contract build/rtlgraph-contract/handoff/contract.json
build/atlas-opt build/rtlgraph-contract/handoff/before.S \
  -o build/rtlgraph-contract/handoff/after.S --dma-timing robust \
  --experimental-mxu0-profile build/rtlgraph-contract/handoff/mxu0/atlas-mxu0.profile \
  --experimental-mxu1-profile build/rtlgraph-contract/handoff/mxu1/atlas-mxu1.profile \
  --rtl-dma-profile build/rtlgraph-contract/handoff/dma/atlas-dma.profile \
  --rtl-lsu-profile build/rtlgraph-contract/handoff/lsu/atlas-lsu.profile \
  --rtl-xlu-profile build/rtlgraph-contract/handoff/xlu/atlas-xlu.profile \
  --rtl-vpu-profile build/rtlgraph-contract/handoff/vpu/atlas-vpu.profile
```

Verification reruns the native query and requires exact equality; scheduling then performs final compiler checks. Bundle paths are relative except for the compiler. `verify --atlas-opt` permits relocation only when binary bytes match. `semantics.dma_completion` is `explicit-wait` with RTL DMA and `modeled-completion` otherwise; verification rejects inconsistent conventions. Static CFG validation is a separate optimization/check mode and cannot be combined with footprint export.

## Consumer precision

The comparison reference is [Merlin at `81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb). Its Atlas contract's `minimum_issue_gap` and `register_dependency_gap` cannot losslessly express row streams, bank/port selection, capacities, or variable DMA completion. The bundle records `no-lossless-gap-projection` rather than emitting timing YAML. A target adapter can emit `before.S`, invoke Atlas scheduling/checking, assemble/execute `after.S`, and retain profile identity with numerical validation and measurements. Complete machine extraction would extend these interfaces.
