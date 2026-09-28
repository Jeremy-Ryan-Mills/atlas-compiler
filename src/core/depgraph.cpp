#include "core/depgraph.h"

#include <algorithm>
#include <array>
#include <map>
#include <stdexcept>

namespace {

// Adds an edge, or raises the distance of an existing edge between the same nodes.
struct EdgeSet {
    DepGraph& g;
    std::map<std::pair<int, int>, int> index;

    void add(int from, int to, int distance, EdgeKind kind, const std::string& reason) {
        auto it = index.find({from, to});
        if (it == index.end()) {
            index[{from, to}] = (int)g.edges.size();
            g.edges.push_back({from, to, distance, kind, reason});
        } else if (distance > g.edges[it->second].distance) {
            g.edges[it->second] = {from, to, distance, kind, reason};
        }
    }
};

// Does instruction `f` conflict with what DMA instruction `dma` does at completion?
bool conflictsAtCompletion(const Footprint& dma, const Footprint& f, EdgeKind& kind) {
    for (const Access& x : dma.accesses) {
        if (!x.atCompletion) continue;
        for (const Access& y : f.accesses) {
            if (!accessesOverlap(x, y) || (!x.write && !y.write)) continue;
            if (x.res == Res::DmaBase && y.atCompletion) continue;  // the DMA queue keeps these in order
            kind = x.write && y.write ? EdgeKind::WAW : x.write ? EdgeKind::RAW : EdgeKind::WAR;
            return true;
        }
    }
    return false;
}

bool isDmaTransfer(const Instr& in) {
    return in.op->opClass == OpClass::DmaLoad || in.op->opClass == OpClass::DmaStore;
}

std::runtime_error dmaError(const Instr& in, const std::string& reason) {
    return std::runtime_error("line " + std::to_string(in.line) + " (" + formatInstr(in) +
                              "): RTL DMA " + reason);
}

// A transfer's memory lifetime ends at an explicit matching wait, never at the
// estimated dmaCycles. Issue operands have already been captured by hardware.
// The native RTL mode currently admits only transfers completed within a block;
// CFG admission separately requires idle DMA at every block boundary.
void addRtlDmaEdges(DepGraph& g, EdgeSet& edges) {
    const int n = (int)g.nodes.size();
    std::array<int, 8> pending;
    pending.fill(-1);
    std::vector<int> waits(n, -1), launches;
    for (int i = 0; i < n; i++) {
        const Instr& in = g.nodes[i];
        const int ch = in.op->channel;
        if (isDmaTransfer(in)) {
            if (pending[ch] >= 0)
                throw dmaError(in, "channel " + std::to_string(ch) +
                                   " reused without an intervening explicit DMA.WAIT");
            pending[ch] = i;
            launches.push_back(i);
        } else if (in.op->opClass == OpClass::DmaWait && pending[ch] >= 0) {
            waits[pending[ch]] = i;
            edges.add(pending[ch], i, 1, EdgeKind::Order,
                      "RTL DMA transfer must launch before its explicit wait");
            pending[ch] = -1;
        }
    }
    for (int d : pending)
        if (d >= 0)
            throw dmaError(g.nodes[d], "transfer requires an explicit matching DMA.WAIT in the same block");

    // Enqueue has no ready signal. Launch order is preserved, so command i uses
    // the same ring slot as command i-8. A count of outstanding commands alone
    // cannot establish that this particular slot has retired.
    for (size_t i = 8; i < launches.size(); i++) {
        const int previous = launches[i - 8], current = launches[i];
        if (waits[previous] >= current)
            throw dmaError(g.nodes[current], "ring slot reused before the earlier transfer's explicit DMA.WAIT");
        edges.add(waits[previous], current, 1, EdgeKind::Order,
                  "RTL DMA ring slot must retire before its eighth subsequent launch");
    }

    for (int d : launches) {
        const int wait = waits[d];
        for (int k = d + 1; k < n; k++) {
            EdgeKind kind;
            if (k == wait || !conflictsAtCompletion(g.footprints[d], g.footprints[k], kind)) continue;
            if (k < wait)
                throw dmaError(g.nodes[k], "memory access conflicts with pending " + g.nodes[d].op->name +
                                          " before its explicit DMA.WAIT");
            edges.add(wait, k, 1, kind, std::string(edgeKindName(kind)) +
                      " with RTL DMA memory lifetime (released by explicit wait)");
        }
    }
}

}  // namespace

uint32_t dmaOperandRegisters(const std::vector<Instr>& instrs) {
    uint32_t mask = 0;
    for (const Instr& in : instrs) {
        if (in.op->engine != Engine::Dma || in.op->opClass == OpClass::DmaWait) continue;
        if (in.op->opClass != OpClass::DmaConfig) mask |= 1u << in.rd | 1u << in.rs2;
        mask |= 1u << in.rs1;
    }
    return mask & ~1u;
}

DepGraph buildGraph(const std::vector<Instr>& instrs, const RegValues& entry, uint32_t dmaRegs, const MachineModel& model) {
    DepGraph g;
    g.nodes = instrs;
    int n = (int)instrs.size();
    RegValues regs = entry;
    for (const Instr& in : instrs) {
        g.footprints.push_back(footprintOf(in, regs, model));
        applyScalar(in, regs);
    }

    EdgeSet edges{g, {}};
    for (int b = 0; b < n; b++)
        for (int a = 0; a < b; a++) {
            Dependence d = dependence(g.nodes[a], g.footprints[a], g.nodes[b], g.footprints[b], model);
            if (d.distance > 0) edges.add(a, b, d.distance, d.kind, d.reason);
        }

    if (model.rtlDma) addRtlDmaEdges(g, edges);

    // The legacy model reads DMA registers and moves data when it completes, which is only known
    // to have happened once the matching dma.wait issues. Later conflicting accesses
    // therefore wait for that dma.wait.
    for (int d = 0; !model.rtlDma && d < n; d++) {
        const OpInfo& op = *g.nodes[d].op;
        if (op.engine != Engine::Dma || op.opClass == OpClass::DmaWait) continue;
        int wait = -1;
        for (int k = d + 1; k < n && wait < 0; k++)
            if (g.nodes[k].op->opClass == OpClass::DmaWait && g.nodes[k].op->channel == op.channel) wait = k;
        for (int k = d + 1; k < n; k++) {
            EdgeKind kind;
            if (k == wait || !conflictsAtCompletion(g.footprints[d], g.footprints[k], kind)) continue;
            if (wait >= 0 && wait < k)
                edges.add(wait, k, 1, kind, std::string(edgeKindName(kind)) + " with " + op.name + " (done once the wait issues)");
            else
                edges.add(d, k, 1, EdgeKind::Order, op.name + " may still be in flight (no dma.wait in between)");
        }
    }

    // A dma.wait for a transfer started in an earlier block guards data this block can't
    // see, so later VMEM accesses, DMA commands and writes to DMA operand registers stay behind it.
    for (int w = 0; w < n; w++) {
        const OpInfo& op = *g.nodes[w].op;
        if (op.opClass != OpClass::DmaWait) continue;
        bool local = false;
        for (int d = 0; d < w; d++)
            if ((model.rtlDma ? isDmaTransfer(g.nodes[d]) : g.nodes[d].op->engine == Engine::Dma) &&
                g.nodes[d].op->channel == op.channel) local = true;
        if (local) continue;
        for (int k = w + 1; k < n; k++) {
            bool guarded = model.rtlDma ? isDmaTransfer(g.nodes[k]) :
                           g.nodes[k].op->engine == Engine::Dma && g.nodes[k].op->opClass != OpClass::DmaWait;
            for (const Access& a : g.footprints[k].accesses) {
                if (a.res == Res::Vmem) guarded = true;
                if (!model.rtlDma && a.res == Res::XReg && a.write && (dmaRegs >> a.first & 1)) guarded = true;
            }
            if (guarded) edges.add(w, k, 1, EdgeKind::Order, op.name + " guards a transfer started in an earlier block");
        }
    }

    g.in.assign(n, {});
    g.out.assign(n, {});
    for (int e = 0; e < (int)g.edges.size(); e++) {
        g.out[g.edges[e].from].push_back(e);
        g.in[g.edges[e].to].push_back(e);
    }
    return g;
}

std::vector<int> criticalHeights(const DepGraph& g) {
    int n = (int)g.nodes.size();
    std::vector<int> height(n, 0);
    for (int i = n - 1; i >= 0; i--) {
        height[i] = g.footprints[i].doneAge + 1;
        for (int e : g.out[i]) {
            const Edge& ed = g.edges[e];
            int d = ed.distance;
            // A dma.wait holds the frontend until the transfer completes, so count the transfer time.
            const Instr& to = g.nodes[ed.to];
            if (to.op->opClass == OpClass::DmaWait && to.op->channel == g.nodes[i].op->channel)
                d = std::max(d, g.footprints[i].dmaCycles + 2);
            height[i] = std::max(height[i], d + height[ed.to]);
        }
    }
    return height;
}
