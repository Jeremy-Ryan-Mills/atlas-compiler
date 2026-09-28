"""Combined DMA/LSU/VPU and both MXU row maps for one shared execution.

The old maps remain intact. MXU names are prefixed to avoid VPU collisions;
each engine keeps its own sampled reset and command signals.
"""
from rtlgraph_dma_vcd import SIGNALS as DMA_SIGNALS
from rtlgraph_mxu0_vcd import SIGNALS as MXU0_SIGNALS
from rtlgraph_mxu1_vcd import CORE_SCOPE

SIGNALS = dict(DMA_SIGNALS)
MXU_MAPS = {}
for engine in (0, 1):
    mapping = {key: (path.replace('.mxu0.seq.', f'.mxu{engine}.seq.'), width)
               for key, (path, width) in MXU0_SIGNALS.items()}
    for port in (0, 1):
        for direction, rtl in (('read', 'Read'), ('write', 'Write')):
            for field, suffix, width in (('valid', 'valid', 1), ('mreg', 'bits_mregId', 6), ('row', 'bits_row', 5)):
                mapping[f'port.{direction}{port}.{field}'] = (
                    f'{CORE_SCOPE}.mxu{engine}.seq.io_mreg{rtl}Req{port}_{suffix}', width)
    MXU_MAPS[engine] = mapping
    for key, signal in mapping.items():
        if key.startswith(('scalar.', 'csr.')):
            if SIGNALS[key] != signal:
                raise ValueError('Mixed capture scalar/CSR contract mismatch')
        else:
            SIGNALS[f'mxu{engine}.' + key] = signal


def engine_sample(sample, engine):
    return {key: sample[key if key.startswith(('scalar.', 'csr.')) else f'mxu{engine}.' + key]
            for key in MXU_MAPS[engine]}
