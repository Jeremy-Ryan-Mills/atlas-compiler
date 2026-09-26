"""Compiler regressions checked by the NPU model."""

import contextlib
from dataclasses import replace
import io

import pytest

from tests import harness


@pytest.fixture
def compiler(optimizer):
    if optimizer in harness.BUILTIN_OPTIMIZERS.values():
        pytest.skip("compiler regressions require an external atlas-opt")
    return optimizer


def run_asm(source, hardware_config_cls, workdir, *, dma_scale=1):
    from npu_model.logging import LoggerConfig
    from npu_model.simulation import Simulation

    config = hardware_config_cls()
    # Only DRAM's first line is used; keep real VMEM geometry.
    config.arch_state_config = replace(config.arch_state_config, dram_size=4096)
    config.offchip_link_core_cycles_per_beat *= dma_scale
    program = harness.program_from_asm(source, [])
    sim = Simulation(
        hardware_config=config,
        logger_config=LoggerConfig(filename=str(workdir / "trace.json")),
        program=program,
        verbose=False,
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            sim.run(max_cycles=10000)
        assert sim.core.is_finished(), "regression program timed out"
        state = sim.core.arch_state
        return {
            "xrf": tuple(state.xrf),
            "dram": state.dram.numpy().tobytes(),
            "vmem": state.vmem.numpy().tobytes(),
            "halt": state.halt_reason,
        }
    finally:
        sim.close()


@pytest.mark.parametrize(
    "source, instruction",
    [
        ("jal x1, target\naddi x1, x0, 17\ntarget:\nsw x1, 0(x0)\n", "jal"),
        ("jal x1, target\naddi x2, x1, 7\ntarget:\nsw x2, 0(x0)\n", "jal"),
        (
            "addi x1, x0, 5\njalr x0, x1, 0\nnop\nnop\n"
            "addi x2, x0, 17\nsw x2, 0(x0)\n",
            "jalr",
        ),
        ("beq x0, x0, target\nauipc x1, 0\ntarget:\nsw x1, 0(x0)\n", "auipc"),
    ],
)
def test_unrelocatable_control_flow_is_rejected(compiler, tmp_path, source, instruction):
    with pytest.raises(harness.OptimizerError, match=instruction):
        compiler(source, tmp_path)
    assert not (tmp_path / "executable.S").exists()


@pytest.mark.parametrize(
    "jump, branch_value, x2_result, x4_result",
    [("jal x0, target", 0, 3, 0), ("beq x3, x0, target", 0, 3, 0), ("beq x3, x0, target", 1, 17, 9)],
    ids=["jump", "taken", "fallthrough"],
)
def test_branches_take_effect_immediately(
    compiler, hardware_config_cls, tmp_path, jump, branch_value, x2_result, x4_result
):
    # In functional assembly the instructions after a branch run only on the fall-through path.
    functional = (
        f"addi x2, x0, 3\naddi x3, x0, {branch_value}\naddi x4, x0, 0\n"
        f"{jump}\naddi x2, x2, 14\naddi x4, x0, 9\ntarget:\n"
        "sw x2, 0(x0)\nsw x4, 4(x0)\n"
        "addi x7, x0, 32\ndma.store.ch0 x0, x0, x7\ndma.wait.ch0\n"
    )
    # The hand-written executable reference gives the branch an empty delay slot.
    reference = functional.replace(f"{jump}\n", f"{jump}\nnop\n")
    optimized = compiler(functional, tmp_path)
    before = run_asm(reference, hardware_config_cls, tmp_path)
    after = run_asm(optimized, hardware_config_cls, tmp_path)
    assert before == after
    assert int.from_bytes(after["dram"][:4], "little") == x2_result
    assert int.from_bytes(after["dram"][4:8], "little") == x4_result


@pytest.mark.parametrize("halt", ["ecall", "ebreak"])
@pytest.mark.parametrize("boundary", ["", "halt_label:\n"])
def test_halt_waits_for_load_writeback(compiler, hardware_config_cls, tmp_path, halt, boundary):
    source = (
        "addi x1, x0, 17\nsw x1, 0(x0)\nlw x2, 0(x0)\n"
        f"delay 2\nnop\n{boundary}{halt}\n"
    )
    optimized = compiler(harness.strip_delays(source), tmp_path)
    before = run_asm(source, hardware_config_cls, tmp_path)
    after = run_asm(optimized, hardware_config_cls, tmp_path)
    assert before == after
    assert after["xrf"][2] == 17
    assert after["halt"] == halt
    # Optimizing the output again, without its delays, gives the same program.
    assert compiler(harness.strip_delays(optimized), tmp_path) == optimized


@pytest.mark.parametrize("dma_scale", [1, 2, 10, 100])
def test_dma_wait_preserves_gapped_port_reservations(compiler, hardware_config_cls, tmp_path, dma_scale):
    source = (
        "vredsum.bf16 m4, m0\ndma.config.ch0 x5\ndelay 26\n"
        "dma.wait.ch0\ndelay 140\naddi x5, x0, 0\nvstore m32, 0(x5)\n"
    )
    optimized = compiler(harness.strip_delays(source), tmp_path)
    before = run_asm(source, hardware_config_cls, tmp_path, dma_scale=dma_scale)
    after = run_asm(optimized, hardware_config_cls, tmp_path, dma_scale=dma_scale)
    assert before == after


# TODO: Add loop-unrolling, block-merging, and DMA-prefetch/channel-reuse cases.
# Cover branch paths, odd trip counts, and DMA latency; compare live outputs.
# Return cycles separately from state and assert speedups on targeted fixtures.
