"""DMA command/completion and LSU/VPU contract for EE290SimConfig.

Extends the frozen LSU/VPU map. No data payloads are captured; complete output
correctness still comes from the independent host golden comparison.
"""
from rtlgraph_lsu_vcd import SIGNALS as LSU_SIGNALS
from rtlgraph_mxu1_vcd import CORE_SCOPE

DMA_SCOPE = CORE_SCOPE + '.dma'
SIGNALS = dict(LSU_SIGNALS)
_COMMAND = {'op': ('opType', 1), 'channel': ('channelId', 3),
            'line': ('vmemLineAddr', 16), 'dram': ('dramAddress', 64),
            'size': ('transferSize', 13)}
SIGNALS['dma.cmd.valid'] = (DMA_SCOPE + '.io_command_valid', 1)
for _key, (_suffix, _width) in _COMMAND.items():
    SIGNALS['dma.cmd.' + _key] = (DMA_SCOPE + '.io_command_bits_' + _suffix, _width)
for _slot in range(8):
    for _key, _name, _width in (('active', 'slotActive', 1), ('dispatched', 'slotDispatched', 1),
                              ('outstanding', 'slotOutstanding', 9)):
        SIGNALS[f'dma.slot{_slot}.{_key}'] = (f'{DMA_SCOPE}.{_name}_{_slot}', _width)
    for _key, (_suffix, _width) in _COMMAND.items():
        SIGNALS[f'dma.slot{_slot}.{_key}'] = (f'{DMA_SCOPE}.commandQueue_{_slot}_{_suffix}', _width)
    SIGNALS[f'dma.busy{_slot}'] = (f'{DMA_SCOPE}.io_channelBusy_{_slot}', 1)
    SIGNALS[f'scalar.dma_busy{_slot}'] = (f'{CORE_SCOPE}.scalar.io_dma_busy_{_slot}', 1)
for _key, _name, _width in (('enqueue', 'enqueueIdx', 3), ('request_slot', 'requestIdx', 3),
                          ('request_beat', 'requestBeatCount', 9),
                          ('vmem_response', 'io_vmemReadData_valid', 1)):
    SIGNALS['dma.' + _key] = (DMA_SCOPE + '.' + _name, _width)
for _channel, _fields in (('a', (('valid', 1), ('ready', 1), ('bits_source', 6), ('bits_address', 37), ('bits_opcode', 3))),
                         ('d', (('valid', 1), ('ready', 1), ('bits_source', 6)))):
    for _field, _width in _fields:
        SIGNALS[f'dma.tl_{_channel}.' + _field.removeprefix('bits_')] = (f'{DMA_SCOPE}.io_tl_{_channel}_{_field}', _width)
for _key, _name, _width in (('valid', 'pc_ctrl.io_s1_valid', 1), ('stall', 'stall', 1),
                          ('rs1', 'regfile.io_rs1_data', 32), ('rs2', 'regfile.io_rs2_data', 32),
                          ('rd', 'regfile.io_rd_data', 32), ('dma_base', 'dmaBaseReg', 32)):
    SIGNALS['scalar.' + _key] = (CORE_SCOPE + '.scalar.' + _name, _width)
