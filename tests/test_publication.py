"""Catch early publication in the ordinary model with nonzero data."""

import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import re
import shlex

import pytest

from tests import harness


PUBLISH = 'csrrwi x0, x1, 0xC10 # atlas.complete\n'


@pytest.fixture
def publication_dir(request, tmp_path):
    root = request.config.getoption('artifacts_dir')
    path = tmp_path if root is None else Path(root) / re.sub(r'[^A-Za-z0-9_.-]', '_', request.node.name)
    path.mkdir(parents=True, exist_ok=True)
    (path / 'executable.S').unlink(missing_ok=True)
    return path


def compiler_input(source):
    """Turn a test program written for npu_model (with `delay`s, and completions as
    `csrrw(i) ... # atlas.complete`) into functional assembly for atlas-opt."""
    source = re.sub(
        r'(?m)^([ \t]*)csrrw(i?)[ \t]+x0,[ \t]*x(\d+),[ \t]*([^\s#]+)'
        r'[ \t]*#[ \t]*atlas\.complete[ \t]*$',
        lambda m: f'{m[1]}atlas.complete {m[3] if m[2] else "x" + m[3]}, {m[4]}',
        source,
    )
    return harness.strip_delays(source)


@pytest.fixture
def publication_compiler(optimizer):
    if optimizer in harness.BUILTIN_OPTIMIZERS.values():
        pytest.skip('publication contract requires an external atlas-opt')
    def compile_source(source, directory):
        return optimizer(compiler_input(source), directory)
    return compile_source


@pytest.fixture
def publication_without_dma_insertion(publication_compiler, pytestconfig):
    """Test manual-wait validation without automatic repair."""
    command = shlex.split(pytestconfig.getoption('atlas_opt') or
                          str(harness.REPO_ROOT / 'build' / 'atlas-opt'))
    command += shlex.split(pytestconfig.getoption('atlas_opt_args'))
    command += ['--passes', 'remove-nops,fill-delay-slots,schedule']
    compile_source = harness.external_optimizer(command)
    return lambda source, directory: compile_source(compiler_input(source), directory)


def observe(source, hardware_config_cls, directory, *, dma_scale=1, expected_dbg0=1):
    from npu_model.logging import LoggerConfig
    from npu_model.simulation import Simulation

    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'program.S').write_text(source)
    config = hardware_config_cls()
    config.arch_state_config = replace(config.arch_state_config, dram_size=8192)
    config.offchip_link_core_cycles_per_beat *= dma_scale
    program = harness.program_from_asm(source, [])
    sim = Simulation(
        hardware_config=config,
        logger_config=LoggerConfig(filename=str(directory / 'trace.json')),
        program=program,
        verbose=False,
    )
    try:
        core = sim.core
        core.reset()
        state = core.arch_state
        state.mrf[0].fill_(0x31)
        state.mrf[2].fill_(0x7B)
        state.mrf[4].fill_(0x2A)
        state.dram[:1024].fill_(0xC3)
        publication = None
        write_csrf = state.write_csrf

        def observe_csr_write(address, value):
            nonlocal publication
            write_csrf(address, value)
            # Observe before later engine ticks, which can hide same-cycle writes.
            if publication is None and address == 0xC10 and state.read_csrf(0xC10) == expected_dbg0:
                publication = {
                    'cycle': core.cycle_count,
                    'active_engines': tuple(unit.name for unit in core.exus if unit.has_in_flight),
                    'vmem': state.vmem.numpy().tobytes(),
                    'dram': state.dram.numpy().tobytes(),
                    'mreg6': state.mrf[6].numpy().tobytes(),
                    'mreg8': state.mrf[8].numpy().tobytes(),
                }

        state.write_csrf = observe_csr_write
        with contextlib.redirect_stdout(io.StringIO()):
            while not core.is_finished() and core.cycle_count < 100000:
                core.tick()
        assert core.is_finished(), 'publication test timed out'
        assert publication is not None, f'never observed dbg0 == {expected_dbg0}'
        summary = {k: hashlib.sha256(v).hexdigest() if isinstance(v, bytes) else v
                   for k, v in publication.items()}
        summary.update(final_cycle=core.cycle_count, halt_reason=state.halt_reason,
                       dma_scale=dma_scale, expected_dbg0=expected_dbg0,
                       observation='first matching CSR write, before later engine ticks',
                       vstore_bytes_at_publication=publication['vmem'][:1024].count(0x31),
                       dma_bytes_at_publication=publication['vmem'][4096:4128].count(0xC3))
        (directory / 'first-publication.json').write_text(json.dumps(summary, indent=2) + '\n')
        return publication
    finally:
        sim.close()


def assert_safe_publication(before, after):
    # Only DRAM is observable at a completion; atlas-opt also finishes all earlier work.
    assert before['active_engines'] == (), 'safe reference published with work in flight'
    assert after['active_engines'] == (), 'optimized release published with work in flight'
    assert after['dram'] == before['dram'], 'DRAM differed at FIRST publication'


def compare(source, compiler, hardware_config_cls, directory, *, dma_scale=1):
    optimized = compiler(harness.strip_delays(source), directory)
    before = observe(source, hardware_config_cls, directory / 'reference', dma_scale=dma_scale)
    after = observe(optimized, hardware_config_cls, directory / 'optimized', dma_scale=dma_scale)
    assert_safe_publication(before, after)
    return optimized, before, after


@pytest.mark.parametrize('halt', ['', 'ecall\n', 'ebreak\n'])
def test_release_waits_for_vstore_at_first_publication(
    publication_compiler, hardware_config_cls, publication_dir, halt
):
    source = 'vstore m0, 0(x0)\ndelay 34\n' + PUBLISH + halt
    compare(source, publication_compiler, hardware_config_cls, publication_dir)


def test_release_waits_for_multiple_engines(
    publication_compiler, hardware_config_cls, publication_dir
):
    source = ('vstore m0, 0(x0)\nvmov m8, m4\nvtrpose.xlu m6, m2\n'
              'delay 100\n' + PUBLISH)
    compare(source, publication_compiler, hardware_config_cls, publication_dir)


@pytest.mark.parametrize('dma_scale', [1, 10, 100])
@pytest.mark.parametrize('label', ['', 'publish:\n'])
def test_release_waits_for_dma_and_independent_vstore(
    publication_compiler, hardware_config_cls, publication_dir, dma_scale, label
):
    source = ('addi x7, x0, 32\nlui x1, 1\ndma.load.ch0 x1, x0, x7\n'
              'vstore m0, 0(x0)\ndma.wait.ch0\ndelay 40\n' + label + PUBLISH)
    compare(source, publication_compiler, hardware_config_cls, publication_dir, dma_scale=dma_scale)


@pytest.mark.parametrize('wait', ['', 'dma.wait.ch1\n'])
@pytest.mark.parametrize('label', ['', 'publish:\n'])
def test_release_rejects_missing_or_wrong_dma_wait(
    publication_without_dma_insertion, publication_dir, wait, label
):
    source = 'addi x7, x0, 64\ndma.load.ch0 x0, x0, x7\n' + wait + label + PUBLISH
    with pytest.raises(harness.OptimizerError, match=r'(?i)(complete|dma|wait)'):
        publication_without_dma_insertion(source, publication_dir)
    assert not (publication_dir / 'executable.S').exists()


def test_release_marker_survives_reoptimization(
    publication_compiler, hardware_config_cls, publication_dir
):
    source = 'vstore m0, 0(x0)\ndelay 34\n' + PUBLISH
    first, _, _ = compare(source, publication_compiler, hardware_config_cls, publication_dir)
    assert first.count('atlas.complete') == 1
    repeat = publication_dir / 'repeat'
    repeat.mkdir(exist_ok=True)
    # Converting the output back to functional assembly and optimizing again gives it back.
    second = publication_compiler(harness.to_functional(first), repeat)
    assert second == first
    observed = observe(second, hardware_config_cls, repeat / 'model')
    assert observed['active_engines'] == ()


def test_unmarked_progress_marker_remains_immediate(
    publication_compiler, hardware_config_cls, publication_dir
):
    source = 'vstore m0, 0(x0)\ncsrrwi x0, x1, 0xC10 # progress only\n'
    optimized = publication_compiler(source, publication_dir)
    observed = observe(optimized, hardware_config_cls, publication_dir / 'model')
    assert 'atlas.complete' not in optimized
    assert observed['cycle'] == 3
    assert observed['active_engines'] == ('LSU',)


@pytest.mark.parametrize('delay,cycle,written,active', [(32, 36, 992, ('LSU',)), (33, 37, 1024, ())])
def test_observer_detects_lsu_completion_later_in_publication_tick(
    hardware_config_cls, publication_dir, delay, cycle, written, active
):
    # At cycle 36, the CSR precedes VSTORE's last row; post-tick state hides it.
    # This model-only probe ignores the annotation.
    source = f'vstore m0, 0(x0)\ndelay {delay}\n' + PUBLISH
    observed = observe(source, hardware_config_cls, publication_dir / 'model')
    assert observed['cycle'] == cycle
    assert observed['active_engines'] == active
    assert observed['vmem'][:written] == bytes([0x31]) * written
    assert observed['vmem'][written:1024] == bytes(1024 - written)
