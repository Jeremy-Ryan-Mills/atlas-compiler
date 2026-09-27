"""Optional MXU0 observation map; existing MXU1 capture contracts stay unchanged.

Names and widths are from the cached EE290 SystolicArraySequencer.sv. Clock
samples use the common adapter's settled left limit before each rising edge.
"""

from rtlgraph_mxu1_vcd import CORE_SCOPE, PERF_SIGNALS

SEQ_SCOPE = CORE_SCOPE + '.mxu0.seq'
_SEQUENCER = {
    'clock': ('clock', 1), 'reset': ('reset', 1),
    'cmd.valid': ('io_cmd_valid', 1), 'cmd.op': ('io_cmd_bits_op', 3),
    'cmd.mreg': ('io_cmd_bits_mregId', 6), 'cmd.accsel': ('io_cmd_bits_accSel', 1),
    'cmd.wslot': ('io_cmd_bits_weightSlot', 1),
    'accept.compute': ('acceptCompute', 1), 'accept.push_p0': ('acceptPushP0', 1),
    'accept.push_p1': ('acceptPushP1', 1), 'accept.push_bf16': ('acceptBF16Push', 1),
    'accept.pop_fp8': ('acceptPopFP8', 1), 'accept.pop_bf16': ('acceptPopBF16', 1),
    'mreg_read.valid': ('io_mregReadReq0_valid', 1),
    'mreg_read.mreg': ('io_mregReadReq0_bits_mregId', 6),
    'mreg_read.row': ('io_mregReadReq0_bits_row', 5),
    'compute.valid': ('io_compute_valid', 1), 'core_out.valid': ('io_coreOut_valid', 1),
    'acc_read.valid': ('io_accComputeReadEn', 1),
    'acc_read.accsel': ('io_accComputeReadAddr_accSel', 1),
    'acc_read.row': ('io_accComputeReadAddr_rowIdx', 5),
    'acc_store.valid': ('io_accStoreReadEn', 1),
    'acc_store.accsel': ('io_accStoreAddr_accSel', 1),
    'acc_store.row': ('io_accStoreAddr_rowIdx', 5),
    'acc_write.valid': ('io_accComputeWrite_valid', 1),
    'acc_write.accsel': ('io_accComputeWrite_bits_accSel', 1),
    'acc_write.row': ('io_accComputeWrite_bits_rowIdx', 5),
    'retire': ('popThisCycle', 1),
}
for _port in (0, 1):
    for _key, _suffix, _width in (('valid', 'valid', 1), ('mreg', 'bits_mregId', 6), ('row', 'bits_row', 5)):
        _SEQUENCER[f'mreg_write{_port}.{_key}'] = (f'io_mregWriteReq{_port}_{_suffix}', _width)

SIGNALS = {key: (SEQ_SCOPE + '.' + name, width) for key, (name, width) in _SEQUENCER.items()}
SIGNALS.update({key: value for key, value in PERF_SIGNALS.items() if key.startswith(('scalar.', 'csr.'))})
