"""Check DMA wait placement, correctness, and overlap."""

import json

import pytest

from tests.test_dma_wait_insertion import compare_repaired
from tests.test_publication import publication_compiler, publication_dir


@pytest.mark.parametrize('dma_scale', [0.1, 1, 10, 100])
@pytest.mark.parametrize('incoming', [False, True], ids=['local', 'incoming'])
def test_ready_wait_unlocks_vector_work_before_scalar_tail(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, incoming,
):
    source = (
        'addi x7, x0, 32\nlui x1, 1\naddi x2, x0, 1024\n'
        'dma.load.ch0 x1, x0, x7\ndma.wait.ch0\nvload m6, 0(x2)\n'
        'delay 40\nvstore m6, 8(x0)\ndelay 40\n'
        + 'addi x9, x9, 1\n' * 64 + 'ecall\n'
    )
    if incoming:
        source = source.replace('dma.load.ch0', 'dma.config.ch0 x0\nnext:\ndma.wait.ch0\ndma.load.ch0')
    optimized, result = compare_repaired(
        source, source, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale,
    )
    assert result['xrf'][9] == 64
    assert optimized.index('dma.wait.ch0') < optimized.rindex('addi x9')
    if dma_scale == 1:
        cycles = json.loads((publication_dir / 'optimized' / 'result.json').read_text())['cycles']
        assert cycles <= (105 if incoming else 100)  # Previously 141 locally.


@pytest.mark.parametrize('dma_scale', [0.1, 1, 10, 100])
@pytest.mark.parametrize('boundary', ['next:\n', 'jal x0, next\nnop\nnext:\n'], ids=['label', 'jump'])
@pytest.mark.parametrize('explicit', [False, True], ids=['inserted', 'explicit'])
def test_incoming_transfer_overlaps_independent_store(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, boundary, explicit,
):
    setup = 'addi x7, x0, 64\nlui x1, 1\ndma.load.ch0 x1, x0, x7\n' + boundary
    tail = 'vstore m0, 0(x0)\ndelay 40\nlw x2, 0(x1)\ndelay 4\nnop\necall\n'
    source = setup + ('dma.wait.ch0\n' if explicit else '') + tail
    reference = setup + 'dma.wait.ch0\n' + tail
    optimized, result = compare_repaired(
        source, reference, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale,
    )
    assert optimized.count('dma.wait.ch0') == 1
    assert optimized.index('vstore') < optimized.index('dma.wait.ch0') < optimized.index('lw ')
    assert result['vmem'][:1024] == bytes([0x31]) * 1024
    assert result['vmem'][4096:4160] == bytes([0xC3]) * 64
    before = json.loads((publication_dir / 'reference' / 'result.json').read_text())['cycles']
    after = json.loads((publication_dir / 'optimized' / 'result.json').read_text())['cycles']
    if dma_scale >= 1:
        # Slow DMA hides most of the store.
        assert after + 20 <= before


@pytest.mark.parametrize('dma_scale', [1, 100])
@pytest.mark.parametrize('value', [0, 4], ids=['transfer', 'idle'])
@pytest.mark.parametrize('first,second', [('bge', 'bge'), ('bgeu', 'bgeu'), ('beq', 'beq')])
def test_same_guard_needs_no_join_wait(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, value, first, second,
):
    source = (
        f'addi x10, x0, {value}\naddi x11, x0, 4\naddi x7, x0, 32\nlui x1, 1\n'
        f'{first} x10, x11, skip\nnop\ndma.load.ch0 x1, x0, x7\n'
        f'skip:\n{second} x10, x11, done\nnop\ndma.wait.ch0\n'
        'done:\ncsrrwi x0, x1, 0xC10 # atlas.complete\necall\n'
    )
    reference = source.replace('done:\n', 'done:\ndma.wait.ch0\n')
    optimized, result = compare_repaired(
        source, reference, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale,
    )
    assert optimized.count('dma.wait.ch0') == 1
    if value == 0:
        assert result['vmem'][4096:4128] == bytes([0xC3]) * 32


@pytest.mark.parametrize('dma_scale', [1, 100])
@pytest.mark.parametrize('trips', [1, 3, 7])
def test_prefetch_only_reaches_waited_backedge(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, trips,
):
    source = (
        f'addi x10, x0, 0\naddi x11, x0, {trips}\naddi x7, x0, 32\nlui x1, 1\n'
        'dma.load.ch0 x1, x0, x7\nloop:\ndma.wait.ch0\n'
        'addi x10, x10, 1\nbge x10, x11, skip\nnop\ndma.load.ch0 x1, x0, x7\n'
        'skip:\nblt x10, x11, loop\nnop\n'
        'csrrwi x0, x1, 0xC10 # atlas.complete\necall\n'
    )
    reference = source.replace('csrrwi', 'dma.wait.ch0\ncsrrwi')
    optimized, result = compare_repaired(
        source, reference, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale,
    )
    assert optimized.count('dma.wait.ch0') == 1
    assert result['xrf'][10] == trips
    assert result['vmem'][4096:4128] == bytes([0xC3]) * 32


@pytest.mark.parametrize('dma_scale', [1, 100])
@pytest.mark.parametrize('mutation', ['body', 'slot', 'signedness'])
def test_different_guard_still_requires_completion_wait(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, mutation,
):
    slot = 'addi x10, x0, 3' if mutation == 'slot' else 'nop'
    body = 'addi x10, x0, 3\n' if mutation == 'body' else ''
    value = -1 if mutation == 'signedness' else 0
    second = 'bgeu' if mutation == 'signedness' else 'bge'
    source = (
        f'addi x10, x0, {value}\naddi x11, x0, 3\naddi x7, x0, 32\nlui x1, 1\n'
        f'bge x10, x11, skip\n{slot}\ndma.load.ch0 x1, x0, x7\n'
        f'skip:\n{body}{second} x10, x11, done\nnop\ndma.wait.ch0\n'
        'done:\ncsrrwi x0, x1, 0xC10 # atlas.complete\necall\n'
    )
    reference = source.replace('done:\n', 'done:\ndma.wait.ch0\n')
    optimized, result = compare_repaired(
        source, reference, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale,
    )
    assert optimized.count('dma.wait.ch0') == 2
    assert result['vmem'][4096:4128] == bytes([0xC3]) * 32
