#include <iostream>
#include <stdexcept>
#include <string>

#include "core/depgraph.h"
#include "passes/pass.h"

static int checks = 0;

static void check(bool condition, const std::string& reason) {
    ++checks;
    if (!condition) throw std::runtime_error(reason);
}

static MachineModel rtlModel() {
    MachineModel model;
    model.rtlDma = true;
    return model;
}

static RegValues entryValues() {
    RegValues entry = zeroRegs();
    entry[1] = 0x90000000;  // DRAM byte pointer
    entry[2] = 32;          // one DMA line
    entry[3] = 0x90001000;
    entry[4] = 0x20000000;  // VMEM word pointer, maps to line zero
    entry[5] = 0x20000400;  // VMEM line 128, disjoint from a 32-row VLOAD
    for (int r = 10; r < 20; ++r) entry[r] = 0x20000800 + (r - 10) * 256;
    return entry;
}

static DepGraph graph(const std::string& source, const RegValues& entry = entryValues()) {
    return buildGraph(parseAsm(source).instrs, entry, 0xFFFFFFFE, rtlModel());
}

static const Edge* edge(const DepGraph& g, int from, int to) {
    for (const Edge& e : g.edges) if (e.from == from && e.to == to) return &e;
    return nullptr;
}

static void rejects(const std::string& source, const std::string& reason,
                    const RegValues& entry = entryValues()) {
    try { graph(source, entry); }
    catch (const std::runtime_error& error) {
        check(std::string(error.what()).find(reason) != std::string::npos,
              "wrong rejection: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("accepted unsafe DMA input: " + source);
}

int main() {
    try {
        // A captured scalar operand may be overwritten immediately after launch.
        // Memory consumers still depend on the actual completion event.
        auto g = graph("dma.load.ch0 x4, x1, x2\naddi x1, x0, 17\ndma.wait.ch0\nvload m0, 0(x4)\n");
        check(edge(g, 0, 1) && edge(g, 0, 1)->distance == 1, "lost launch-time scalar WAR");
        check(edge(g, 0, 2), "wait is not bound to the matching launch");
        check(edge(g, 2, 3) && edge(g, 2, 3)->kind == EdgeKind::RAW, "input read lacks completion guard");
        g = graph("dma.load.ch0 x4, x1, x2\ndma.wait.ch0\naddi x1, x0, 17\n");
        check(!edge(g, 1, 2), "RTL wait unnecessarily guards captured scalar operand");

        // CONFIG is scalar state, neither a transfer nor a channel reservation.
        check(graph("dma.config.ch0 x0\n").edges.empty(), "CONFIG spuriously requires a wait");
        g = graph("dma.config.ch0 x0\ndma.load.ch0 x4, x1, x2\ndma.config.ch0 x0\ndma.wait.ch0\n");
        check(edge(g, 0, 1) && edge(g, 0, 1)->kind == EdgeKind::RAW, "CONFIG base value not captured by launch");
        check(edge(g, 1, 2) && edge(g, 1, 2)->kind == EdgeKind::WAR, "CONFIG can precede earlier base capture");

        // Unknown transfer duration cannot be replaced with a long software gap.
        rejects("dma.load.ch0 x4, x1, x2\ndelay 4095\nvload m0, 0(x4)\ndma.wait.ch0\n", "memory access conflicts");
        rejects("dma.store.ch0 x3, x4, x2\nvstore m0, 0(x4)\ndma.wait.ch0\n", "memory access conflicts");
        rejects("dma.load.ch0 x4, x1, x2\ndelay 4095\n", "matching DMA.WAIT in the same block");
        rejects("dma.load.ch0 x4, x1, x2\ndma.wait.ch1\n", "matching DMA.WAIT in the same block");
        rejects("dma.load.ch0 x4, x1, x2\ndma.load.ch0 x5, x1, x2\ndma.wait.ch0\n", "channel 0 reused");

        // Disjoint VMEM permits useful work before the input wait; unknown alias
        // information instead keeps the lifetime conservative.
        g = graph("dma.load.ch0 x4, x1, x2\nvload m0, 0(x5)\ndma.wait.ch0\n");
        check(!edge(g, 0, 1), "disjoint LSU work unnecessarily depends on DMA");
        RegValues unknown = entryValues();
        unknown[4].reset();
        rejects("dma.load.ch0 x4, x1, x2\nvload m0, 0(x5)\ndma.wait.ch0\n", "memory access conflicts", unknown);
        g = graph("dma.load.ch0 x4, x1, x2\ndma.wait.ch0\nvload m0, 0(x5)\n", unknown);
        check(edge(g, 1, 2), "unknown DMA address lost conservative completion guard");

        // DRAM aliases are not inferred from VMEM disjointness. Read/read DMA
        // traffic can overlap; anything involving a DRAM writer requires a wait.
        g = graph("dma.load.ch0 x4, x1, x2\ndma.load.ch1 x5, x1, x2\ndma.wait.ch0\ndma.wait.ch1\n");
        check(edge(g, 0, 1), "transfer launch order no longer fixes ring assignment");
        rejects("dma.load.ch0 x4, x1, x2\ndma.store.ch1 x3, x5, x2\ndma.wait.ch0\ndma.wait.ch1\n",
                "memory access conflicts");
        g = graph("dma.store.ch0 x3, x4, x2\ndma.wait.ch0\ndma.load.ch1 x5, x1, x2\ndma.wait.ch1\n");
        check(edge(g, 1, 2), "DRAM write/read lacks completion guard");

        // DMA starts only after every fixed-latency producer row is available.
        RegValues fullTile = entryValues();
        fullTile[2] = 1024;
        g = graph("vstore m0, 0(x4)\ndma.store.ch0 x3, x4, x2\ndma.wait.ch0\n", fullTile);
        check(edge(g, 0, 1) && edge(g, 0, 1)->distance == 35,
              "DMA store launch does not wait for the full VSTORE output range");

        // Two outstanding transfers can still overwrite an occupied ring slot:
        // keep command zero live while ch1 repeatedly launches and retires.
        std::string ring = "dma.load.ch0 x10, x1, x2\n";
        for (int i = 1; i < 8; ++i)
            ring += "dma.load.ch1 x" + std::to_string(10 + i) + ", x1, x2\ndma.wait.ch1\n";
        rejects(ring + "dma.load.ch1 x18, x1, x2\ndma.wait.ch1\ndma.wait.ch0\n", "ring slot reused");
        g = graph(ring + "dma.wait.ch0\ndma.load.ch1 x18, x1, x2\ndma.wait.ch1\n");
        check(edge(g, 15, 16), "ring reuse lacks explicit prior-slot completion edge");

        // Scheduling uses the graph's event guards but does not introduce waits.
        auto program = parseAsm("lui x4, 0x20000\nlui x5, 0x20000\naddi x5, x5, 1024\n"
                                "lui x1, 0x90000\naddi x2, x0, 32\ndma.config.ch0 x0\n"
                                "dma.load.ch0 x4, x1, x2\ndma.wait.ch0\nvload m0, 0(x4)\n"
                                "vload m2, 0(x5)\necall\n");
        Code code = buildBlocks(program);
        PassContext ctx;
        ctx.model = rtlModel();
        runPasses(code, {"schedule"}, ctx);
        int waitCount = 0, waitIndex = -1, consumerIndex = -1, independentIndex = -1;
        const auto emitted = flatten(code);
        for (size_t i = 0; i < emitted.instrs.size(); ++i) {
            const Instr& in = emitted.instrs[i];
            if (in.op->opClass == OpClass::DmaWait) ++waitCount, waitIndex = (int)i;
            if (in.op->opClass == OpClass::VLoad && in.rd == 0) consumerIndex = (int)i;
            if (in.op->opClass == OpClass::VLoad && in.rd == 2) independentIndex = (int)i;
        }
        check(waitCount == 1, "native scheduler inserted or deleted an explicit wait");
        check(waitIndex >= 0 && consumerIndex > waitIndex, "native scheduler lost input completion guard");
        check(independentIndex >= 0 && independentIndex < waitIndex, "native scheduler did not overlap independent LSU work");

        // Without the profile the inherited DMA API and acceptance are unchanged.
        auto legacy = buildGraph(parseAsm("dma.config.ch0 x0\n").instrs, zeroRegs());
        check(legacy.footprints[0].dmaCycles == dmaTransferCycles(0), "legacy CONFIG behavior changed");
        buildGraph(parseAsm("dma.load.ch0 x4, x1, x2\nvload m0, 0(x4)\n").instrs, entryValues());
        std::cout << "PASS: " << checks << " RTL DMA graph and scheduling checks\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
