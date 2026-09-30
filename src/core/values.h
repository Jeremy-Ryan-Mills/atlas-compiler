#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

#include "core/blocks.h"

// Known scalar values and captured DMA.CONFIG high bits; nullopt means unknown.
struct RegValues : std::array<std::optional<uint32_t>, 32> {
    std::optional<uint32_t> dmaBase;
};

RegValues unknownRegs();  // x0 = 0, everything else unknown
RegValues zeroRegs(bool resetDmaBase = false);  // DMA reset requires a supporting profile

// Result of a scalar ALU instruction, or nullopt if an operand is unknown.
std::optional<uint32_t> aluResult(const Instr& in, const RegValues& regs);

// Updates the scalar destination or captures DMA.CONFIG's scalar operand.
void applyScalar(const Instr& in, RegValues& regs);

// Register values known at the start of each block, by forward constant
// propagation over the control-flow graph. The program starts with all registers 0.
std::vector<RegValues> blockEntryValues(const Code& code, bool preciseDma = false);
