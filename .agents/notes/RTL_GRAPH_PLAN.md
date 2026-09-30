# RTL-derived instruction scheduling

RTL-graph derives operand accesses, occupancy, launch conditions, and completion events from a pinned hardware configuration. The compiler consumes instruction scheduling facts rather than FPGA cell counts. See the [model guide](../../docs/rtlgraph-model.md), [contract reference](../../docs/rtlgraph-contract.md), [lowering example](../../docs/rtlgraph-lowering.md), and [measured results](../../docs/rtlgraph-perf-validation.md).

## Implemented boundary

The profiles target `chipyard.EE290SimConfig`, publicly described in [bringup-chipyard](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26), with Atlas sources in [ucb-bar/atlas-npu](https://github.com/ucb-bar/atlas-npu). Recorded revisions and the shared CIRCT hash identify the analyzed hardware. The historical [AtlasShuttleVectorConfig](https://github.com/ucb-bar/atlas-npu/blob/main/chipyard/config/AtlasConfigs.scala#L40-L48) is a separate configuration.

```text
pinned Chisel configuration and elaboration inputs
  -> FIRRTL -> CIRCT HW/Comb/Seq with remaining SV constructs
  -> typed structural queries and bounded control execution
  -> source-bound component profiles and evidence
  -> operand-resolved footprints -> dependency graph/reservations
  -> scheduled assembly and compiler checks -> independent RTL/numerical checks
```

All six component profiles are optional partial projections. Extraction follows hierarchy, def-use, enables, counters, and state transitions. Counting pipeline registers cannot establish acceptance or variable-latency completion. The retained implementation uses targeted typed queries; a complete extracted machine remains future work. [CIRCT dialect documentation](https://circt.llvm.org/docs/Dialects/) describes the input IR.

The compatibility target is `Access` row streams and alias ranges, inclusive `Hold` occupancy, logical reservations/release ages, and explicit completion edges. Physical contention differs from logical dependencies: `m0` and `m32` have different logical identities but share a bank. Read/read creates no data edge while simultaneous reads can conflict on a port. Mnemonic minimum gaps cannot preserve every stream, capacity, or forbidden issue distance.

## Next work

Extend frontend acceptance, visibility, and interference coverage before relaxing retained rules. Extend the component/footprint interfaces into a complete machine schema only when evidence supports it. Decode and storage organization can be extracted; intended semantics, numerical contracts, layout, and dispatch ABI still need authored definitions. The model guide holds the common evidence limits, including cached-simulator provenance.

A future [Merlin](https://github.com/ucb-bar/merlin) adapter can retain hardware/evidence identity for discrepancy review, invoke an optional Atlas scheduler/checker, and compare measured references. The existing [handoff](../../docs/rtlgraph-contract.md) preserves rich footprints; no adapter is implemented. Scheduling profiles do not establish register allocation, tiling, a PyTorch operator set, or a performance ceiling.

[Muon/Radiance](https://github.com/ucb-bar/radiance/blob/main/docs/muon.md) is a potential target for warp context, register mapping, occupancy, operand collection, and hardware hazards. [Vortex integration in Merlin](https://github.com/ucb-bar/merlin/pull/16) offers a SystemVerilog direction; first test a small slice through [CIRCT's SystemVerilog frontend](https://circt.llvm.org/docs/Tools/circt-verilog/). Each target needs its own instruction identity, acceptance/stall, completion, resource, and warp semantics: an Atlas software issue restriction may correspond to a hardware stall elsewhere.
