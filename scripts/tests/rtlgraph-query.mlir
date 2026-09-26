// Small structural tests: values and port indices are intentionally distinct.
module {
  hw.module @ScalarCore(in %issue : i1, in %select : i1, out unused : i1, out io_mxu1Cmd_valid : i1) {
    %s1_fire = hw.wire %issue : i1
    %launch = comb.and %s1_fire, %select : i1
    %io_mxu1Cmd_valid = hw.wire %launch : i1
    hw.output %select, %io_mxu1Cmd_valid : i1, i1
  }
  hw.module @InnerProductTreesSequencer(in %io_cmd_valid : i1, out accepted : i1) {
    hw.output %io_cmd_valid : i1
  }
  hw.module @InnerProductTreesTop(in %io_cmd_valid : i1, out accepted : i1) {
    %forward = hw.wire %io_cmd_valid : i1
    %accepted = hw.instance "seq" @InnerProductTreesSequencer(io_cmd_valid: %forward: i1) -> (accepted: i1)
    hw.output %accepted : i1
  }
  hw.module @AtlasCore(in %issue : i1, in %select : i1, out accepted : i1) {
    %unused, %valid = hw.instance "scalar" @ScalarCore(issue: %issue: i1, select: %select: i1) -> (unused: i1, io_mxu1Cmd_valid: i1)
    %accepted = hw.instance "mxu1" @InnerProductTreesTop(io_cmd_valid: %valid: i1) -> (accepted: i1)
    hw.output %accepted : i1
  }
  hw.module @StateCutpoint(in %issue : i1, in %clock : !seq.clock, out result : i1) {
    %s1_fire = hw.wire %issue : i1
    %registered = seq.compreg %s1_fire, %clock : i1
    hw.output %registered : i1
  }
  hw.module.extern @Opaque(in %issue : i1, out unrelated : i1, out result : i1)
  hw.module @InstanceCutpoint(in %issue : i1, out result : i1) {
    %s1_fire = hw.wire %issue : i1
    %unrelated, %result = hw.instance "opaque" @Opaque(issue: %s1_fire: i1) -> (unrelated: i1, result: i1)
    hw.output %result : i1
  }
  hw.module @Ambiguous(in %issue : i1, out result : i1) {
    %a = hw.wire %issue name "s1_fire" : i1
    %b = hw.wire %issue name "s1_fire" : i1
    hw.output %a : i1
  }
}
