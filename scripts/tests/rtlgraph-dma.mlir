// Local cutpoint fixture: no full DMA trajectory or queue correctness is implied.
hw.module @DmaSlot(in %clock : !seq.clock, in %reset : i1,
  in %io_command_valid : i1, in %enqueueIdx : i3,
  in %slotAFireOH_0 : i1, in %slotDFireOH_0 : i1,
  in %slotOutstanding_0 : i9, in %slotDispatched_0 : i1,
  in %io_command_bits_opType : i1, in %io_command_bits_channelId : i3,
  in %io_command_bits_vmemLineAddr : i16, in %io_command_bits_dramAddress : i64,
  in %io_command_bits_transferSize : i13, out active : i1) {
  %zero = hw.constant false
  %one = hw.constant true
  %zero3 = hw.constant 0 : i3
  %zero9 = hw.constant 0 : i9
  %one9 = hw.constant 1 : i9
  %notinc = comb.xor %slotAFireOH_0, %one : i1
  %notdec = comb.xor %slotDFireOH_0, %one : i1
  %inconly = comb.and %slotAFireOH_0, %notdec : i1
  %deconly = comb.and %slotDFireOH_0, %notinc : i1
  %isone = comb.icmp eq %slotOutstanding_0, %one9 : i9
  %iszero = comb.icmp eq %slotOutstanding_0, %zero9 : i9
  %decresult = comb.mux %deconly, %isone, %iszero : i1
  %nextzero = comb.mux %inconly, %zero, %decresult : i1
  %willBeZero = hw.wire %nextzero : i1
  %selected = comb.icmp eq %enqueueIdx, %zero3 : i3
  %capture = comb.and %io_command_valid, %selected : i1
  %commandQueue_0_opType = seq.firreg %next_opType clock %clock : i1
  %next_opType = comb.mux %capture, %io_command_bits_opType, %commandQueue_0_opType : i1
  %commandQueue_0_channelId = seq.firreg %next_channelId clock %clock : i3
  %next_channelId = comb.mux %capture, %io_command_bits_channelId, %commandQueue_0_channelId : i3
  %commandQueue_0_vmemLineAddr = seq.firreg %next_vmemLineAddr clock %clock : i16
  %next_vmemLineAddr = comb.mux %capture, %io_command_bits_vmemLineAddr, %commandQueue_0_vmemLineAddr : i16
  %commandQueue_0_dramAddress = seq.firreg %next_dramAddress clock %clock : i64
  %next_dramAddress = comb.mux %capture, %io_command_bits_dramAddress, %commandQueue_0_dramAddress : i64
  %commandQueue_0_transferSize = seq.firreg %next_transferSize clock %clock : i13
  %next_transferSize = comb.mux %capture, %io_command_bits_transferSize, %commandQueue_0_transferSize : i13
  %slotActive_0 = seq.firreg %nextactive clock %clock reset sync %reset, %zero : i1
  %retire = comb.and %slotActive_0, %slotDispatched_0, %willBeZero : i1
  %activeorenqueue = comb.or %slotActive_0, %capture : i1
  %nextactive = comb.mux %retire, %zero, %activeorenqueue : i1
  hw.output %slotActive_0 : i1
}

hw.module @VmemBank(in %clock : !seq.clock, in %reset : i1,
  in %lsuScalarWriteBankOH : i6,
  in %lsuVecWriteBankOH : i6,
  in %lsuScalarReadBankOH : i6,
  in %lsuVecReadBankOH : i6,
  in %dmaWriteBankOH : i6,
  in %dmaReadBankOH : i6,
  in %tlWriteBankOH : i6,
  in %tlReadBankOH : i6,
  out readGrant : i1, out writeGrant : i1) {
  %zero = hw.constant false
  %one = hw.constant true
  %c0 = hw.constant 0 : i3
  %c1 = hw.constant 1 : i3
  %c2 = hw.constant 2 : i3
  %c3 = hw.constant 3 : i3
  %c4 = hw.constant 4 : i3
  %bit0 = comb.extract %lsuScalarWriteBankOH from 0 : (i6) -> i1
  %accessSel_leaf_valid = hw.wire %bit0 : i1
  %bit1 = comb.extract %lsuVecWriteBankOH from 0 : (i6) -> i1
  %accessSel_leaf_1_valid = hw.wire %bit1 : i1
  %bit2 = comb.extract %lsuScalarReadBankOH from 0 : (i6) -> i1
  %accessSel_leaf_2_valid = hw.wire %bit2 : i1
  %bit3 = comb.extract %lsuVecReadBankOH from 0 : (i6) -> i1
  %accessSel_leaf_3_valid = hw.wire %bit3 : i1
  %bit4 = comb.extract %dmaWriteBankOH from 0 : (i6) -> i1
  %accessSel_leaf_4_valid = hw.wire %bit4 : i1
  %bit5 = comb.extract %dmaReadBankOH from 0 : (i6) -> i1
  %accessSel_leaf_5_valid = hw.wire %bit5 : i1
  %bit6 = comb.extract %tlWriteBankOH from 0 : (i6) -> i1
  %accessSel_leaf_6_valid = hw.wire %bit6 : i1
  %bit7 = comb.extract %tlReadBankOH from 0 : (i6) -> i1
  %accessSel_leaf_7_valid = hw.wire %bit7 : i1
  %win0 = hw.wire %accessSel_leaf_valid : i1
  %prior0 = hw.wire %accessSel_leaf_valid : i1
  %notprior1 = comb.xor %prior0, %one : i1
  %win1 = comb.and %accessSel_leaf_1_valid, %notprior1 : i1
  %prior1 = comb.or %prior0, %accessSel_leaf_1_valid : i1
  %notprior2 = comb.xor %prior1, %one : i1
  %win2 = comb.and %accessSel_leaf_2_valid, %notprior2 : i1
  %prior2 = comb.or %prior1, %accessSel_leaf_2_valid : i1
  %notprior3 = comb.xor %prior2, %one : i1
  %win3 = comb.and %accessSel_leaf_3_valid, %notprior3 : i1
  %prior3 = comb.or %prior2, %accessSel_leaf_3_valid : i1
  %notprior4 = comb.xor %prior3, %one : i1
  %win4 = comb.and %accessSel_leaf_4_valid, %notprior4 : i1
  %prior4 = comb.or %prior3, %accessSel_leaf_4_valid : i1
  %notprior5 = comb.xor %prior4, %one : i1
  %win5 = comb.and %accessSel_leaf_5_valid, %notprior5 : i1
  %prior5 = comb.or %prior4, %accessSel_leaf_5_valid : i1
  %notprior6 = comb.xor %prior5, %one : i1
  %win6 = comb.and %accessSel_leaf_6_valid, %notprior6 : i1
  %prior6 = comb.or %prior5, %accessSel_leaf_6_valid : i1
  %notprior7 = comb.xor %prior6, %one : i1
  %win7 = comb.and %accessSel_leaf_7_valid, %notprior7 : i1
  %prior7 = comb.or %prior6, %accessSel_leaf_7_valid : i1
  %bankDmaReadGrant_0 = hw.wire %win5 : i1
  %bankDmaWriteGrant_0 = hw.wire %win4 : i1
  %client7 = comb.mux %win7, %c4, %c0 : i3
  %client5 = comb.mux %win5, %c3, %client7 : i3
  %client3 = comb.mux %win3, %c2, %client5 : i3
  %client2 = comb.mux %win2, %c1, %client3 : i3
  %valid = comb.or %win2, %win3, %win5, %win7 : i1
  %r1_bankReadClient_0 = seq.firreg %client2 clock %clock : i3
  %r1_bankReadValid_0 = seq.firreg %valid clock %clock reset sync %reset, %zero : i1
  hw.output %bankDmaReadGrant_0, %bankDmaWriteGrant_0 : i1, i1
}
