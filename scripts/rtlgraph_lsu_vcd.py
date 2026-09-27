"""LSU/VPU capture contract for the cached EE290SimConfig simulation.

The map includes the existing VPU contract verbatim. LSU fields expose actual
requests, returned valids, busy reservations and VMEM arbitration observations.
It does not capture row payloads or establish the simulator's build lineage.
"""

from rtlgraph_mxu1_vcd import CORE_SCOPE
from rtlgraph_vpu_vcd import SIGNALS as VPU_SIGNALS

LSU_SCOPE = CORE_SCOPE + '.lsu'
SIGNALS = dict(VPU_SIGNALS)
_LSU = {
    'cmd.valid': ('io_cmd_valid', 1), 'cmd.op': ('io_cmd_bits_op', 2),
    'cmd.mreg': ('io_cmd_bits_mregBank', 6),
    'cmd.line': ('io_cmd_bits_vmemLineAddr', 16),
    'load.busy': ('io_vloadBusy', 1), 'store.busy': ('io_vstoreBusy', 1),
    'load.state': ('vloadState', 2), 'store.state': ('vstoreState', 2),
    'load.counter': ('vloadCounter', 6), 'store.counter': ('vstoreCounter', 6),
    'mreg.response': ('io_mregReadResp_valid', 1),
    'vmem.response': ('io_vmemVecReadData_valid', 1),
    'active.read.valid': ('io_activeMregRead_valid', 1),
    'active.read.mreg': ('io_activeMregRead_bits', 6),
    'active.write.valid': ('io_activeMregWrite_valid', 1),
    'active.write.mreg': ('io_activeMregWrite_bits', 6),
}
for _kind, _rtl in (('read', 'Read'), ('write', 'Write')):
    for _key, _suffix, _width in (('valid', 'valid', 1), ('mreg', 'bits_mregId', 6), ('row', 'bits_row', 5)):
        _LSU[f'mreg.{_kind}.{_key}'] = (f'io_mreg{_rtl}Req_{_suffix}', _width)
for _key, _rtl in (('load', 'VecRead'), ('store', 'VecWrite'),
                    ('scalar_read', 'ScalarRead'), ('scalar_write', 'ScalarWrite')):
    for _field, _suffix, _width in (('valid', 'valid', 1), ('bank', 'bits_bankIdx', 3), ('row', 'bits_bankAddr', 13)):
        _LSU[f'vmem.{_key}.{_field}'] = (f'io_vmem{_rtl}_{_suffix}', _width)
SIGNALS.update({'lsu.' + key: (LSU_SCOPE + '.' + name, width)
                for key, (name, width) in _LSU.items()})
for _kind in ('Read', 'Write'):
    SIGNALS['scalar.mreg_' + _kind.lower() + '_busy'] = (
        CORE_SCOPE + '.scalar.io_mreg' + _kind + 'Busy', 64)

# DMA requests are arbitrated, unlike deterministic LSU requests. A request
# without a grant is not a completed memory access and is counted separately.
for _kind in ('Read', 'Write'):
    for _key, _suffix, _width in (('valid', 'valid', 1), ('bank', 'bits_bankIdx', 3), ('row', 'bits_bankAddr', 13)):
        SIGNALS[f'vmem.dma_{_kind.lower()}.{_key}'] = (
            f'{CORE_SCOPE}.vmem.io_dma{_kind}_{_suffix}', _width)
    SIGNALS[f'vmem.dma_{_kind.lower()}.grant'] = (
        f'{CORE_SCOPE}.vmem.io_dma{_kind}Grant', 1)
