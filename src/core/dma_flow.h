#pragma once

#include <array>
#include <set>
#include <vector>

#include "core/blocks.h"

// Pending sites per channel, numbered in block/instruction order, excluding waits.
using PendingDma = std::array<std::set<int>, 8>;
unsigned pendingDmaChannels(const PendingDma& pending);

// Wait masks before each blockInstructions() entry and the block exit.
using DmaWaitPlan = std::vector<std::vector<unsigned>>;

struct DmaFlow {
    std::vector<std::vector<PendingDma>> before;  // State before planned waits.
    std::vector<bool> reached;
};

// Track comparisons until operand writes; widen conservatively at analysis limits.
// Requires idle entry and known CFG edges, including synthetic falloff exits.
DmaFlow analyzeDmaFlow(const Code& code, const DmaWaitPlan& waits = {});
