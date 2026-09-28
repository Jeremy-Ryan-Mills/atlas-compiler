#pragma once

#include <array>
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

// Pending transfers per channel, evaluated at their launch sites.
using IncomingDma = std::array<std::vector<Footprint>, 8>;
// dmaRegs marks registers read at DMA completion; null incomingDma keeps broad barriers.
DepGraph buildGraph(const std::vector<Instr>& instrs, const RegValues& entry, uint32_t dmaRegs = 0xFFFFFFFE, const IncomingDma* incomingDma = nullptr);

uint32_t dmaOperandRegisters(const std::vector<Instr>& instrs);

// DMA completion conflicts, excluding FIFO-ordered base updates.
bool conflictsAtCompletion(const Footprint& dma, const Footprint& other, EdgeKind& kind);

// Longest path (in cycles) from each node until everything after it has finished.
// The scheduler issues the instructions with the largest height first.
std::vector<int> criticalHeights(const DepGraph& g);
