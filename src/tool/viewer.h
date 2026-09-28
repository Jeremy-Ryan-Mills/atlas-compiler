#pragma once

#include <string>
#include <vector>

#include "core/blocks.h"
#include "core/depgraph.h"
#include "core/simulator.h"

// One dependency graph together with the cycle each node issues at.
struct GraphView {
    DepGraph graph;
    std::vector<int> cycles;       // issue cycle of each node, relative to the block start
    std::vector<bool> redundant;   // edges implied by longer paths
    int length = 0;                // cycles until the next block can start
};

struct BlockView {
    std::string name;
    GraphView input, output;       // input: functional assembly (cycles = program order)
    int lowerBound = 0;            // critical path length of the dependency graph
};

struct ProgramView {
    std::string source;
    int inputInstructions = 0;
    std::vector<BlockView> blocks;
    SimResult result;              // simulation of the executable assembly
};

// Builds the input and output graphs of every block. `optimized` must have the
// same blocks as `functional` (the passes keep the block structure).
ProgramView buildProgramView(const std::string& source, const AsmProgram& functional, const Code& optimized,
                             const SimResult& result);

// Self-contained HTML page showing each block's dependency graph in input order
// and as scheduled, with every scheduled instruction placed at the cycle it issues.
std::string renderHtml(const ProgramView& view);
