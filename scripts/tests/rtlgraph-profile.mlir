hw.module @ValidPipeline(in %clock: !seq.clock, in %reset: i1,
                        in %io_compute_valid: i1, out io_out_valid: i1) {
  %zero = hw.constant false
  %stage0 = seq.firreg %io_compute_valid clock %clock reset sync %reset, %zero : i1
  %stage1 = seq.firreg %stage0 clock %clock reset sync %reset, %zero : i1
  hw.output %stage1 : i1
}

hw.module @ReadGuard(in %p0Boundary: i1, in %acceptCompute: i1, in %p0CmdValid: i1,
                    in %p0Cmd_op: i3, in %io_cmd_bits_op: i3,
                    out io_accComputeReadEn: i1) {
  %one = hw.constant true
  %acc = hw.constant 6 : i3
  %old_acc = comb.icmp eq %p0Cmd_op, %acc : i3
  %new_acc = comb.icmp eq %io_cmd_bits_op, %acc : i3
  %not_boundary = comb.xor %p0Boundary, %one : i1
  %continuing = comb.and %p0CmdValid, %not_boundary, %old_acc : i1
  %accepted = comb.and %p0Boundary, %acceptCompute : i1
  %read = comb.mux %accepted, %new_acc, %continuing : i1
  hw.output %read : i1
}
