// Small structural fixture: state registers are arbitrary cutpoints. No FSM
// trajectory is asserted by the local-function extractor.
hw.module @LSU(in %clock : !seq.clock, in %reset : i1,
  in %io_cmd_valid : i1, in %io_cmd_bits_op : i2,
  in %io_vmemVecReadData_valid : i1, in %io_mregReadResp_valid : i1,
  out io_vmemVecRead_valid : i1, out io_mregReadReq_valid : i1,
  out io_mregWriteReq_valid : i1, out io_vmemVecWrite_valid : i1,
  out io_vloadBusy : i1, out io_vstoreBusy : i1,
  out io_activeMregWrite_valid : i1, out io_activeMregRead_valid : i1) {
  %zero = hw.constant false
  %zero2 = hw.constant 0 : i2
  %one = hw.constant 1 : i2
  %two = hw.constant 2 : i2
  %vloadState = seq.firreg %zero2 clock %clock reset sync %reset, %zero2 : i2
  %vstoreState = seq.firreg %zero2 clock %clock reset sync %reset, %zero2 : i2
  %isload = comb.icmp eq %io_cmd_bits_op, %one : i2
  %isstore = comb.icmp eq %io_cmd_bits_op, %two : i2
  %loadcmd = comb.and %io_cmd_valid, %isload : i1
  %storecmd = comb.and %io_cmd_valid, %isstore : i1
  %issueVloadCmd = hw.wire %loadcmd : i1
  %issueVstoreCmd = hw.wire %storecmd : i1
  %loadread = comb.icmp eq %vloadState, %one : i2
  %storeread = comb.icmp eq %vstoreState, %one : i2
  %vloadIssueRead = hw.wire %loadread : i1
  %vstoreIssueRead = hw.wire %storeread : i1
  %vloadRespPending = seq.firreg %vloadIssueRead clock %clock reset sync %reset, %zero : i1
  %loadwrite = comb.and %io_vmemVecReadData_valid, %vloadRespPending : i1
  %vloadWritePending = seq.firreg %loadwrite clock %clock reset sync %reset, %zero : i1
  %vstoreRespPending_d = seq.firreg %vstoreIssueRead clock %clock reset sync %reset, %zero : i1
  %vstoreRespPending_q = seq.firreg %vstoreRespPending_d clock %clock reset sync %reset, %zero : i1
  %mregReadRespValid_q = seq.firreg %io_mregReadResp_valid clock %clock reset sync %reset, %zero : i1
  %storewrite = comb.and %mregReadRespValid_q, %vstoreRespPending_q : i1
  %loadstatebusy = comb.icmp ne %vloadState, %zero2 : i2
  %storestatebusy = comb.icmp ne %vstoreState, %zero2 : i2
  %loadbusy = comb.or %loadstatebusy, %vloadRespPending, %vloadWritePending : i1
  %storebusy = comb.or %storestatebusy, %vstoreRespPending_d, %vstoreRespPending_q : i1
  %vloadBusy = hw.wire %loadbusy : i1
  %vstoreBusy = hw.wire %storebusy : i1
  hw.output %vloadIssueRead, %vstoreIssueRead, %vloadWritePending, %storewrite,
    %vloadBusy, %vstoreBusy, %vloadBusy, %vstoreBusy : i1, i1, i1, i1, i1, i1, i1, i1
}
