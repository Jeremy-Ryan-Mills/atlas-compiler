"""Narrow VPU temporal capture for cached EE290SimConfig hardware.

Names/widths follow the cached VectorEngineTop/VectorEngine/VectorFSM SV.
Samples are settled values immediately before each rising clock edge.
"""

from rtlgraph_mxu1_vcd import CORE_SCOPE, PERF_SIGNALS

TOP_SCOPE = CORE_SCOPE + '.vpu'
ENGINE_SCOPE = TOP_SCOPE + '.core'
FSM_SCOPE = ENGINE_SCOPE + '.fsm'
SIGNALS = {key: value for key, value in PERF_SIGNALS.items()
           if key.startswith(('scalar.', 'csr.'))}

_TOP = {
    'clock': ('clock', 1), 'reset': ('reset', 1),
    'cmd.valid': ('io_cmd_valid', 1), 'cmd.op': ('io_cmd_bits_op', 5),
    'cmd.vd': ('io_cmd_bits_vd', 6), 'cmd.vs1': ('io_cmd_bits_vs1', 6),
    'cmd.vs2': ('io_cmd_bits_vs2', 6), 'issue_busy': ('io_issueBusy', 31),
    'mirrored': ('mirroredReadReq', 1), 'mirrored_d': ('mirroredReadReq_d', 1),
}
for _port in (0, 1):
    for _kind, _rtl in (('read', 'Read'), ('write', 'Write')):
        for _key, _suffix, _width in (('valid', 'valid', 1), ('mreg', 'bits_mregId', 6), ('row', 'bits_row', 5)):
            _TOP[f'physical.{_kind}{_port}.{_key}'] = (f'io_mreg{_rtl}Req{_port}_{_suffix}', _width)
    _TOP[f'physical.response{_port}'] = (f'io_mregReadResp{_port}_valid', 1)
SIGNALS.update({key: (TOP_SCOPE + '.' + name, width) for key, (name, width) in _TOP.items()})

_FSM = {
    'inst_fire': ('io_in_instFire', 1), 'inst_type': ('io_in_instType', 5),
    'state': ('state', 2), 'ready': ('io_out_VEReady', 1),
}
for _slot in (1, 2):
    for _key, _name, _width in (('done', 'done', 1), ('write_done', 'writeDone', 1),
                               ('write_count', 'writeCounter', 7),
                               ('read_done', 'readDone', 1), ('resident_op', 'inst', 5)):
        _FSM[f'slot{_slot}.{_key}'] = (_name + str(_slot), _width)
    _FSM[f'slot{_slot}.response'] = (f'io_in_dataInFire{_slot}', 1)
    _FSM[f'slot{_slot}.result'] = (f'io_in_dataOutFire{_slot}', 1)
    for _key, _name, _width in (('valid', 'Valid', 1), ('mreg', 'Bank', 6), ('row', 'Row', 5)):
        _FSM[f'logical.read{_slot}.{_key}'] = (f'io_out_read{_name}{_slot}', _width)
SIGNALS.update({key: (FSM_SCOPE + '.' + name, width) for key, (name, width) in _FSM.items()})
