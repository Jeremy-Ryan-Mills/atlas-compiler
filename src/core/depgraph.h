#pragma once

#include <string>
#include <vector>

#include "core/machine.h"
#include "core/dma_flow.h"

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

// dmaRegs guards legacy completion-time scalar reads; RTL captures them at issue.
// Without incoming CFG state, RTL transfers must complete within this block.
DepGraph buildGraph(const std::vector<Instr>& instrs, const RegValues& entry, uint32_t dmaRegs = 0xFFFFFFFE,
                    const MachineModel& model = {}, const IncomingDma* incoming = nullptr);

uint32_t dmaOperandRegisters(const std::vector<Instr>& instrs);

// Longest path (in cycles) from each node until everything after it has finished.
// The scheduler issues the instructions with the largest height first.
std::vector<int> criticalHeights(const DepGraph& g);

// Selected-model completion conflicts; RTL operands have already been captured.
bool conflictsAtCompletion(const Footprint& dma, const Footprint& next, EdgeKind& kind,
                           const MachineModel& model = {});
