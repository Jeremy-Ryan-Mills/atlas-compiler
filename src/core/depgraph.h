#pragma once

#include <string>
#include <vector>

#include "core/machine.h"

// b must issue at least `distance` cycles after a (from = a, to = b).
struct Edge {
    int from, to;
    int distance;
    EdgeKind kind;
    std::string reason;
};

// Dependency graph of a straight-line list of instructions (one basic block).
// Nodes are the instructions in program order, so every edge points forward.
struct DepGraph {
    std::vector<Instr> nodes;
    std::vector<Footprint> footprints;
    std::vector<Edge> edges;
    std::vector<std::vector<int>> in, out;  // edge indices entering / leaving each node
};

// `dmaRegs` is a bit mask of x registers read by DMA commands anywhere in the
// program. The legacy model guards their completion-time reads across blocks;
// the RTL model captures them at launch and does not extend those reads.
// RTL DMA scheduling requires each transfer's explicit matching wait within its
// block, and rejects conflicting memory uses or ring/channel reuse before it.
DepGraph buildGraph(const std::vector<Instr>& instrs, const RegValues& entry, uint32_t dmaRegs = 0xFFFFFFFE,
                    const MachineModel& model = {});

uint32_t dmaOperandRegisters(const std::vector<Instr>& instrs);

// Longest path (in cycles) from each node until everything after it has finished.
// The scheduler issues the instructions with the largest height first.
std::vector<int> criticalHeights(const DepGraph& g);
