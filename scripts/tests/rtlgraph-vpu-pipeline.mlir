hw.module @ResetPipeline(in %clock: !seq.clock, in %reset: i1, in %io_req_valid: i1, out io_resp_valid: i1) {
  %zero = hw.constant 0 : i1
  %first = seq.firreg %io_req_valid clock %clock reset sync %reset, %zero : i1
  %second = seq.firreg %first clock %clock reset sync %reset, %zero : i1
  hw.output %second : i1
}

hw.module @UnresetPipeline(in %clock: !seq.clock, in %io_req_valid: i1, out io_resp_valid: i1) {
  %first = seq.firreg %io_req_valid clock %clock : i1
  hw.output %first : i1
}

hw.module @Combinational(in %io_req_valid: i1, out io_resp_valid: i1) {
  hw.output %io_req_valid : i1
}
