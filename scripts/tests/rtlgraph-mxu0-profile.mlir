// Cutpoint fixture: no state/latency behavior is inferred from this circuit.
hw.module @SystolicArraySequencer(in %p0Boundary: i1, in %acceptCompute: i1,
    in %p0CmdValid: i1, in %p0Cmd_op: i3, in %io_cmd_valid: i1,
    in %io_cmd_bits_op: i3, in %addr: i5, in %sel: i1, in %store: i1,
    out io_accComputeReadEn: i1, out io_accComputeReadAddr_accSel: i1,
    out io_accComputeReadAddr_rowIdx: i5, out io_accStoreReadEn: i1,
    out io_accStoreAddr_accSel: i1, out io_accStoreAddr_rowIdx: i5) {
  %one = hw.constant true
  %acc = hw.constant 6 : i3
  %old_acc = comb.icmp eq %p0Cmd_op, %acc : i3
  %new_acc = comb.icmp eq %io_cmd_bits_op, %acc : i3
  %not_boundary = comb.xor %p0Boundary, %one : i1
  %continuing = comb.and %p0CmdValid, %not_boundary, %old_acc : i1
  %accepted = comb.and %p0Boundary, %acceptCompute : i1
  %read = comb.mux %accepted, %new_acc, %continuing : i1
  hw.output %read, %sel, %addr, %store, %sel, %addr : i1, i1, i5, i1, i1, i5
}

// Only wrapper connectivity is exercised here. Physical memory mutation tests
// remain in test_rtlgraph_mxu1_bottleneck.py, whose same helper is used by MXU0.
hw.module @AccumulationBuffers(in %io_computeReadEn: i1,
    in %io_computeReadAddr_accSel: i1, in %io_computeReadAddr_rowIdx: i5,
    in %io_storeReadEn: i1, in %io_storeAddr_accSel: i1, in %io_storeAddr_rowIdx: i5) {
  hw.output
}

hw.module @SystolicArrayTop(in %io_cmd_valid: i1, in %io_cmd_bits_op: i3) {
  %true = hw.constant true
  %false = hw.constant false
  %zero3 = hw.constant 0 : i3
  %zero5 = hw.constant 0 : i5
  %ce, %cs, %cr, %se, %ss, %sr = hw.instance "seq" @SystolicArraySequencer(
      p0Boundary: %true: i1, acceptCompute: %io_cmd_valid: i1,
      p0CmdValid: %false: i1, p0Cmd_op: %zero3: i3,
      io_cmd_valid: %io_cmd_valid: i1, io_cmd_bits_op: %io_cmd_bits_op: i3,
      addr: %zero5: i5, sel: %false: i1, store: %false: i1)
      -> (io_accComputeReadEn: i1, io_accComputeReadAddr_accSel: i1,
          io_accComputeReadAddr_rowIdx: i5, io_accStoreReadEn: i1,
          io_accStoreAddr_accSel: i1, io_accStoreAddr_rowIdx: i5)
  %wire = hw.wire %ce : i1
  hw.instance "accBuf" @AccumulationBuffers(io_computeReadEn: %wire: i1,
      io_computeReadAddr_accSel: %cs: i1, io_computeReadAddr_rowIdx: %cr: i5,
      io_storeReadEn: %se: i1, io_storeAddr_accSel: %ss: i1, io_storeAddr_rowIdx: %sr: i5) -> ()
  hw.output
}

hw.module @AtlasCore(in %valid: i1, in %opcode: i3) {
  hw.instance "mxu0" @SystolicArrayTop(io_cmd_valid: %valid: i1, io_cmd_bits_op: %opcode: i3) -> ()
  hw.output
}
