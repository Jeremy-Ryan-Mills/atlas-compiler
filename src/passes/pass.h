#pragma once

#include <string>
#include <vector>

#include "core/blocks.h"
#include "core/machine.h"

// Settings and results shared by the passes of one run.
struct PassContext {
    bool robustDma = true;         // never assume when a dma.wait releases (.agents/OPEN_QUESTIONS.md #1)
    std::vector<std::string> log;  // each pass adds a line describing what it did
    MachineModel model;
};

// A pass rewrites the blocks of a program in place. See src/passes/README.md.
struct Pass {
    const char* name;         // used with --passes on the command line
    const char* description;
    void (*run)(Code& code, PassContext& ctx);
};

// Every pass, in the order they run.
const std::vector<Pass>& allPasses();

// Runs named passes in registry order. Empty names select the standard passes;
// insert-dma-waits is opt-in.
void runPasses(Code& code, const std::vector<std::string>& names, PassContext& ctx);

// The passes (one .cpp file each).
void stripArtifacts(Code& code, PassContext& ctx);
void insertDmaWaits(Code& code, PassContext& ctx);
void fillDelaySlots(Code& code, PassContext& ctx);
void schedule(Code& code, PassContext& ctx);
