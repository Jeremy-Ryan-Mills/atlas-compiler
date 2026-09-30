# Assembly and timing handoff

[`rtlgraph_contract.py`](../scripts/rtlgraph_contract.py) packages native `before.S`, selected RTL profiles/evidence, operand-resolved footprints, and the exact `atlas-opt` binary. Verification reproduces the footprints before scheduling `after.S`. This is an external-consumer interface, including a possible [Merlin](https://github.com/ucb-bar/merlin) adapter; it does not implement Merlin lowering, register assignment, assembly translation, loading, or dispatch.

The profile target is `chipyard.EE290SimConfig`. Its current public source is [`EE290Configs.scala`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26) in [bringup-chipyard](https://github.com/ucb-ee194-tapeout/bringup-chipyard). The bundle's IR hash and recorded source revisions identify the analyzed configuration; the moving `main` link is a reference, not its provenance.

## Input contract

The emitter supplies native `atlas-opt` syntax with:

- operations, registers, addresses, and runtime scalar setup resolved;
- matching `DMA.WAIT` instructions, authored or prepared by the optional [wait-insertion pass](rtlgraph-dma-integration.md);
- `# atlas.release` on the CSR that publishes completed output; and
- idle engines, no pending DMA, and zero scalar registers at entry.

Export is a read-only query and requires the waits already present. Arbitrary incoming state and a dispatch ABI are outside this subset. The historical baremetal adapter is retained at [`dd7342c`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/tree/dd7342ce6a3c4d051544bf719086edb59cb80765).

The component `.profile` formats are already versioned (currently MXU0/DMA/LSU/XLU v2 and MXU1/VPU v1). The JSON bundle `atlas.rtlgraph.contract.v1` and resolved query `atlas.footprints.v1` preserve their richer access/resource/completion semantics; a complete extracted-machine schema would extend these interfaces. The bundle records:

| Field | Meaning |
| --- | --- |
| `config`, `source_ir_sha256` | `EE290SimConfig` and common hardware-IR identity |
| `compiler`, `source` | SHA-256 identities of `atlas-opt` and bundled `before.S` |
| `profiles` | Selected MXU0, MXU1, DMA, LSU, XLU, and VPU projections with evidence |
| `footprints` | Resolved accesses, inclusive holds, logical reservations, VPU occupancy, completion estimates, and dependency edges |
| `semantics`, `limitations` | Assembly, age/completion conventions, and evidence boundaries |
| `merlin` | Pinned comparison reference and precision-loss report |

The footprint query includes known scalar/base entry state and, for RTL DMA, launch-site footprints and pending incoming lifetimes per block. Unknown values stay unknown. Physical port/capacity checks and control-flow admission still require the compiler. The query alone is neither a scheduler nor a legality proof.

Only selected fields are RTL-derived; [the model guide](rtlgraph-model.md) identifies inherited rules. Export verifies profile/report hashes, settings, and common hardware identity. It does not rerun extraction or prove the evidence. Scheduled programs still need appropriate numerical and RTL validation.

## Create and schedule a bundle

Use a freshly built `atlas-opt` supporting `--dump-footprints`. Omit unused profiles.

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

Verification reruns the native footprint query and requires exact equality. The optimizer then performs scheduling and final checks. Bundle paths are relative except for the compiler; `verify --atlas-opt` permits relocation only when the binary bytes match. Consumers with memory-dependent branches can use the separate optimization/checking mode `--validation static` described in [the model guide](rtlgraph-model.md); it does not evaluate numerical behavior and cannot be combined with footprint export.

`semantics.dma_completion` is `explicit-wait` with an RTL DMA profile and `modeled-completion` otherwise. Verification rejects missing/inconsistent timing conventions, including older bundles labeled unconditionally `explicit-wait`; re-export those bundles.

## Future Merlin relationship

The comparison is pinned to [`ucb-bar/merlin@81a585b`](https://github.com/ucb-bar/merlin/tree/81a585b857838baeba35bc55eab7db10525db7cb), whose Atlas contract expresses [`minimum_issue_gap`](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L32-L120) and [`register_dependency_gap`](https://github.com/ucb-bar/merlin/blob/81a585b857838baeba35bc55eab7db10525db7cb/examples/atlas/phase1/contracts/hwbringup_atlas_v0/schedule_contract.yaml#L124-L201). Scalar gaps cannot losslessly represent row streams, bank/port selection, resource capacities, or variable DMA completion. The bundle reports `no-lossless-gap-projection` instead of emitting misleading timing YAML.

A future target-specific integration could emit native `before.S`, invoke `atlas-opt` with selected profiles, and assemble/execute `after.S` with numerical validation. Retaining the evidence and profile identity would support Phase 0 discrepancy review and Phase 2 comparisons. An optimized measured kernel would be a reference candidate, not a proven performance roofline. No Merlin adapter is implemented in this branch.

The separate [`ASSEMBLY_CONTRACT.md`](https://github.com/Jeremy-Ryan-Mills/atlas-compiler/blob/3ae2b5d76909c529df14c89933f737f9ab19be8d/ASSEMBLY_CONTRACT.md#L38-L68) changes the functional/executable input interface. Future convergence should preserve the selected-model capture, memory-range, channel/ring, and control-flow rules already implemented here; it need not redesign this handoff.
