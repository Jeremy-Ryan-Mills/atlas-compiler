#pragma once

#include <array>
#include <map>
#include <vector>

#include "core/blocks.h"
#include "core/machine.h"

// Each site may still be live on some incoming path. Its age is the maximum
// number of later transfers launched, saturated at the eight-entry RTL ring.
using PendingDma = std::array<std::map<int, unsigned>, 8>;
using DmaWaitPlan = std::vector<std::vector<unsigned>>;

struct DmaFlow {
    std::vector<std::vector<PendingDma>> before;
    std::vector<bool> reached;
    std::vector<Footprint> commands;
};

struct IncomingDma {
    PendingDma pending;
    const std::vector<Footprint>* commands;
};

bool isDmaCommand(const Instr& in, const MachineModel& model);
unsigned pendingDmaChannels(const PendingDma& pending);
void clearDmaChannels(PendingDma& pending, unsigned mask);
Code withDmaExit(const Code& code);
DmaFlow analyzeDmaFlow(const Code& code, const MachineModel& model, const DmaWaitPlan& waits = {});
unsigned requiredDmaWaits(const PendingDma& pending, const Instr& in, const Footprint& footprint,
                          const DmaFlow& flow, const MachineModel& model);
void validateDmaFlow(const Code& code, const MachineModel& model);
