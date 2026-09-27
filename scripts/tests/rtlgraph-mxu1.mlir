module {
  hw.module @ClockAdapters(in %raw: i1, out clock: !seq.clock, out result: i1) {
    %clock = seq.to_clock %raw
    %result = seq.from_clock %clock
    hw.output %clock, %result : !seq.clock, i1
  }
  // Independent cutpoints expose acceptance polarity and state boundaries.
  hw.module @AcceptanceTest(in %io_cmd_valid: i1, in %isCompute: i1,
      in %p0Boundary: i1, in %fifoFull: i1, in %accReuseHazard: i1,
      in %computeWslotHazard: i1, in %computePushAccHazard: i1,
      in %clock: !seq.clock, out accepted: i1, out fed: i1) {
    %one = hw.constant true
    %space = comb.xor %fifoFull, %one : i1
    %reuse = comb.xor %accReuseHazard, %one : i1
    %weight = comb.xor %computeWslotHazard, %one : i1
    %acc = comb.xor %computePushAccHazard, %one : i1
    %guard = comb.and %io_cmd_valid, %isCompute, %p0Boundary, %space, %reuse, %weight, %acc : i1
    %acceptCompute = hw.wire %guard : i1
    %p0CmdValid = seq.compreg %acceptCompute, %clock : i1
    %feed = hw.wire %p0CmdValid : i1
    hw.output %acceptCompute, %feed : i1, i1
  }
  hw.module @SeqFixture(in %io_coreOut_valid: i1, in %valid: i1,
      out io_compute_valid: i1, out io_accComputeWrite_valid: i1,
      out io_accComputeReadEn: i1) {
    hw.output %valid, %io_coreOut_valid, %valid : i1, i1, i1
  }
  hw.module @CoreFixture(in %io_compute_valid: i1, in %unrelated: i1,
      out unrelated: i1, out io_out_valid: i1) {
    hw.output %unrelated, %io_compute_valid : i1, i1
  }
  hw.module @AccFixture(in %io_computeWriteReq_valid: i1, in %io_computeReadEn: i1) {
    hw.output
  }
  hw.module @WrapperFixture(in %valid: i1, in %unrelated: i1) {
    %compute, %write, %read = hw.instance "seq" @SeqFixture(io_coreOut_valid: %result: i1, valid: %valid: i1) -> (io_compute_valid: i1, io_accComputeWrite_valid: i1, io_accComputeReadEn: i1)
    %other, %result = hw.instance "core" @CoreFixture(io_compute_valid: %compute: i1, unrelated: %unrelated: i1) -> (unrelated: i1, io_out_valid: i1)
    %forward = hw.wire %write : i1
    hw.instance "accBuf" @AccFixture(io_computeWriteReq_valid: %forward: i1, io_computeReadEn: %read: i1) -> ()
    hw.output
  }
}
