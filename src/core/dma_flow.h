#pragma once

#include <array>
#include <set>
#include <vector>

#include "core/blocks.h"
#include "core/machine.h"

// Pending sites per channel, numbered in block/instruction order, excluding waits.
using PendingDma = std::array<std::set<int>, 8>;
unsigned pendingDmaChannels(const PendingDma& pending);

// Wait masks before each blockInstructions() entry and the block exit.
using DmaWaitPlan = std::vector<std::vector<unsigned>>;

struct DmaFlow {
    std::vector<std::vector<PendingDma>> before;  // State before planned waits.
    std::vector<bool> reached;
    std::vector<Footprint> commands;  // Each pending site's footprint, evaluated at its launch.
};

// Throws unless every block reachable from the entry has known, in-range successors;
// `who` names the caller in the message.
void requireKnownSuccessors(const Code& code, const std::string& who);

// Track comparisons until operand writes; widen conservatively at analysis limits.
// Requires idle entry and known CFG edges, including synthetic falloff exits.
DmaFlow analyzeDmaFlow(const Code& code, const DmaWaitPlan& waits = {});
