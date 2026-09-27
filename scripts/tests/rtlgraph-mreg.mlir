hw.module @MregSlice(in %clock: !seq.clock,
    in %io_mxu1ReadReq0_valid: i1, in %io_mxu1ReadReq0_bits_mregId: i6,
    in %io_mxu1ReadReq0_bits_row: i5,
    in %bankReadRow_0: i6, in %bankReadValid_0: i1,
    in %bankWriteRow_0: i6, in %bankWriteValid_0: i1,
    in %bankWriteData_0: i256, out data: i256) {
  %one = hw.constant 1 : i32
  %zero = hw.constant 0 : i27
  %low = comb.extract %io_mxu1ReadReq0_bits_mregId from 0 : (i6) -> i5
  %shift = comb.concat %zero, %low : i27, i5
  %bank = comb.shl %one, %shift : i32
  %mask = comb.replicate %io_mxu1ReadReq0_valid : (i1) -> i32
  %readBankOHs_2 = comb.and %bank, %mask {sv.namehint = "readBankOHs_2"} : i32
  %high = comb.extract %io_mxu1ReadReq0_bits_mregId from 5 : (i6) -> i1
  %readPhysRows_2 = comb.concat %high, %io_mxu1ReadReq0_bits_row
      {sv.namehint = "readPhysRows_2"} : i1, i5
  %mem = "seq.firmem"() {name = "banks_0", readLatency = 1 : i32,
      writeLatency = 1 : i32, ruw = 0 : i32, wuw = 1 : i32}
      : () -> !seq.firmem<64 x 256>
  %data = seq.firmem.read_port %mem[%bankReadRow_0], clock %clock
      enable %bankReadValid_0 : !seq.firmem<64 x 256>
  seq.firmem.write_port %mem[%bankWriteRow_0] = %bankWriteData_0, clock %clock
      enable %bankWriteValid_0 : !seq.firmem<64 x 256>
  hw.output %data : i256
}
