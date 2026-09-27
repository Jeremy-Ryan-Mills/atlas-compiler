// Independent local predicates and physical read muxes for mutation checks.
hw.module @BoundaryWeight(in %p0CmdValid: i1, in %p0Row: i6,
    in %p1CmdValid: i1, in %p1Row: i6, in %w0CmdValid: i1, in %w0Row: i6,
    in %p0Cmd_op: i3, in %p1Cmd_op: i3,
    in %p0Cmd_weightSlot: i1, in %p1Cmd_weightSlot: i1,
    out io_weightWriteReq_valid: i1, out io_weightWriteReq_bits_weightSlot: i1) {
  %one = hw.constant true
  %inc = hw.constant 1 : i6
  %zero = hw.constant 0 : i3
  %p0next = comb.add %p0Row, %inc : i6
  %p0last = comb.extract %p0next from 5 : (i6) -> i1
  %p0idle = comb.xor %p0CmdValid, %one : i1
  %p0Boundary = comb.or %p0idle, %p0last {sv.namehint = "p0Boundary"} : i1
  %p1next = comb.add %p1Row, %inc : i6
  %p1last = comb.extract %p1next from 5 : (i6) -> i1
  %p1idle = comb.xor %p1CmdValid, %one : i1
  %p1Boundary = comb.or %p1idle, %p1last {sv.namehint = "p1Boundary"} : i1
  %w0next = comb.add %w0Row, %inc : i6
  %w0last = comb.extract %w0next from 5 : (i6) -> i1
  %w0idle = comb.xor %w0CmdValid, %one : i1
  %w0Boundary = comb.or %w0idle, %w0last {sv.namehint = "w0Boundary"} : i1
  %op0 = comb.icmp eq %p0Cmd_op, %zero : i3
  %op1 = comb.icmp eq %p1Cmd_op, %zero : i3
  %push0 = comb.and %p0CmdValid, %op0 : i1
  %push1 = comb.and %p1CmdValid, %op1 : i1
  %valid = comb.or %push0, %push1 : i1
  %slot = comb.mux %push1, %p1Cmd_weightSlot, %p0Cmd_weightSlot : i1
  hw.output %valid, %slot : i1, i1
}

hw.module @AccumulatorSlice(in %clock: !seq.clock,
    in %io_computeReadEn: i1, in %io_computeReadAddr_accSel: i1,
    in %io_computeReadAddr_rowIdx: i5, in %io_storeReadEn: i1,
    in %io_storeAddr_accSel: i1, in %io_storeAddr_rowIdx: i5,
    in %writeEn: i1, in %writeAddr: i5, in %writeData: i512,
    out data0: i512, out data1: i512) {
  %one = hw.constant true
  %cs0 = comb.xor %io_computeReadAddr_accSel, %one : i1
  %ss0 = comb.xor %io_storeAddr_accSel, %one : i1
  %compute0 = comb.and %io_computeReadEn, %cs0 : i1
  %store0 = comb.and %io_storeReadEn, %ss0 : i1
  %read0En = comb.or %compute0, %store0 {sv.namehint = "read0En"} : i1
  %read0Addr = comb.mux %compute0, %io_computeReadAddr_rowIdx, %io_storeAddr_rowIdx
      {sv.namehint = "read0Addr"} : i5
  %compute1 = comb.and %io_computeReadEn, %io_computeReadAddr_accSel : i1
  %store1 = comb.and %io_storeReadEn, %io_storeAddr_accSel : i1
  %read1En = comb.or %compute1, %store1 {sv.namehint = "read1En"} : i1
  %read1Addr = comb.mux %compute1, %io_computeReadAddr_rowIdx, %io_storeAddr_rowIdx
      {sv.namehint = "read1Addr"} : i5
  %mem0 = "seq.firmem"() {name = "buffer0", readLatency = 1 : i32,
      writeLatency = 1 : i32, ruw = 0 : i32, wuw = 1 : i32}
      : () -> !seq.firmem<32 x 512>
  %data0 = seq.firmem.read_port %mem0[%read0Addr], clock %clock
      enable %read0En : !seq.firmem<32 x 512>
  seq.firmem.write_port %mem0[%writeAddr] = %writeData, clock %clock
      enable %writeEn : !seq.firmem<32 x 512>
  %mem1 = "seq.firmem"() {name = "buffer1", readLatency = 1 : i32,
      writeLatency = 1 : i32, ruw = 0 : i32, wuw = 1 : i32}
      : () -> !seq.firmem<32 x 512>
  %data1 = seq.firmem.read_port %mem1[%read1Addr], clock %clock
      enable %read1En : !seq.firmem<32 x 512>
  seq.firmem.write_port %mem1[%writeAddr] = %writeData, clock %clock
      enable %writeEn : !seq.firmem<32 x 512>
  hw.output %data0, %data1 : i512, i512
}
