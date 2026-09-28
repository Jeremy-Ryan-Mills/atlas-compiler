#pragma once

#include <string>
#include <vector>

#include "core/asm.h"
#include "core/machine.h"

struct SimOptions {
    long long maxCycles = 50000000;
    double dmaLatencyScale = 1.0;  // cost estimate; RTL safety never relies on this latency
    int maxViolations = 20;
    MachineModel model;
};

struct SimResult {
    long long cycles = 0;       // inherited model cycle count; RTL DMA remains a cost estimate
    long long issued = 0;       // issued instructions, including delays but not halt
    long long delays = 0;       // dynamic `delay` instructions
    std::vector<std::string> violations;          // broken dependences or hardware rules
    std::string stopReason;                       // empty when the program ran to its end
};

// Timing simulation with scalar execution and in-flight hazard checks. With the
// optional RTL DMA model, explicit waits alone retire transfers; fixed-engine
// checks use minimum issue spacing and reservations widened over arbitrary waits.
// Halt reports unfinished work. Legacy falloff drains; RTL DMA falloff requires waits.
SimResult simulate(const AsmProgram& prog, const SimOptions& opt = {});
