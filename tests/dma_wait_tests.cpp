#include <iostream>
#include <random>
#include <stdexcept>
#include <string>

#include "core/dma_flow.h"
#include "core/simulator.h"
#include "passes/pass.h"
#include "tool/viewer.h"

namespace {

int checks = 0;
void check(bool condition, const std::string& reason) {
    ++checks;
    if (!condition) throw std::runtime_error(reason);
}

MachineModel rtlModel() {
    MachineModel model;
    model.rtlDma = model.rtlDmaRanges = true;
    return model;
}

const std::string setup = "lui x4, 0x20000\naddi x5, x4, 1024\nlui x1, 0x90000\n"
                          "addi x3, x1, 1024\naddi x2, x0, 32\n";

Code optimize(const std::string& source, const MachineModel& model, bool schedule = true) {
    Code code = buildBlocks(parseAsm(source));
    PassContext ctx;
    ctx.model = model;
    runPasses(code, schedule ? std::vector<std::string>{"strip-artifacts", "insert-dma-waits", "fill-delay-slots", "schedule"} :
                              std::vector<std::string>{"insert-dma-waits"}, ctx);
    validateDmaFlow(code, model);
    return code;
}

int countWaits(const Code& code, int channel = -1) {
    int result = 0;
    for (const Instr& in : flatten(code).instrs)
        result += in.op->opClass == OpClass::DmaWait && (channel < 0 || in.op->channel == channel);
    return result;
}

int find(const Code& code, OpClass op, int rd = -1) {
    const auto program = flatten(code);
    for (size_t i = 0; i < program.instrs.size(); ++i)
        if (program.instrs[i].op->opClass == op && (rd < 0 || program.instrs[i].rd == rd)) return (int)i;
    return -1;
}

void simulateSafe(const Code& code, const MachineModel& model) {
    checkStaticSchedule(flatten(code), model);
    for (double scale : {0.001, 1.0, 100.0}) {
        SimOptions options;
        options.model = model;
        options.dmaLatencyScale = scale;
        const auto result = simulate(flatten(code), options);
        check(result.stopReason.empty(), "simulation stopped: " + result.stopReason);
        check(result.violations.empty(), result.violations.empty() ? "" : result.violations.front());
    }
}

void rejects(const std::string& source, const std::string& reason) {
    try { optimize(source, rtlModel()); }
    catch (const std::runtime_error& e) {
        check(std::string(e.what()).find(reason) != std::string::npos, "unexpected rejection: " + std::string(e.what()));
        return;
    }
    throw std::runtime_error("accepted unsupported input");
}

}  // namespace

int main() {
    try {
        const auto model = rtlModel();
        // RTL operands are captured at launch; the legacy model must retain them.
        const std::string captured = setup + "dma.load.ch0 x4, x1, x2\naddi x1, x0, 17\n"
                                            "dma.config.ch0 x0\nvload m0, 0(x4)\necall\n";
        auto code = optimize(captured, model, false);
        check(countWaits(code) == 1, "RTL capture or CONFIG acquired an unnecessary wait");
        const auto native = flatten(code).instrs;
        int overwrite = -1, wait = -1;
        for (size_t i = 0; i < native.size(); ++i) {
            if (native[i].op->name == "addi" && native[i].rd == 1) overwrite = (int)i;
            if (native[i].op->opClass == OpClass::DmaWait) wait = (int)i;
        }
        check(overwrite >= 0 && overwrite < wait, "RTL captured scalar is guarded until completion");
        code = optimize(captured, model);
        simulateSafe(code, model);
        std::string legacySource = captured;
        legacySource.replace(0, std::string("lui x4, 0x20000").size(), "addi x4, x0, 0");
        code = optimize(legacySource, {}, false);
        const auto legacy = flatten(code).instrs;
        overwrite = wait = -1;
        for (size_t i = 0; i < legacy.size(); ++i) {
            if (legacy[i].op->name == "addi" && legacy[i].rd == 1) overwrite = (int)i;
            if (legacy[i].op->opClass == OpClass::DmaWait && wait < 0) wait = (int)i;
        }
        check(wait >= 0 && wait < overwrite, "legacy completion-time operand was overwritten early");

        // Incoming lifetimes survive labels and both sides of a CFG join.
        const std::string joined = setup + "dma.load.ch0 x4, x1, x2\nbeq x8, x0, left\nnop\n"
            "addi x9, x0, 1\njal x0, join\nnop\nleft:\naddi x9, x0, 2\njoin:\n"
            "vload m2, 0(x5)\nvload m0, 0(x4)\necall\n";
        code = optimize(joined, model);
        check(countWaits(code) == 1, "CFG join unnecessarily drains DMA in every predecessor");
        check(find(code, OpClass::VLoad, 2) < find(code, OpClass::DmaWait), "incoming wait blocks disjoint LSU work");
        simulateSafe(code, model);

        simulateSafe(optimize(setup + "addi x8, x0, 1\n" + joined.substr(setup.size()), model), model);
        const auto view = buildProgramView("DMA join", parseAsm(joined), code, {}, {}, model);
        check(renderHtml(view).find("Dependency edges unavailable") != std::string::npos,
              "viewer represented an invalid pre-insertion graph as validated");

        // Real perf kernels publish success in a successor block after their final store.
        code = optimize(setup + "dma.store.ch0 x1, x4, x2\npass:\naddi x9, x0, 1\n"
            "csrrw x0, x9, 0xC10 # atlas.release\necall\n", model);
        const auto& completion = code.blocks.back().body;
        check(completion.size() >= 3 && completion[0].op->name == "addi" &&
              completion[1].op->opClass == OpClass::DmaWait && completion[2].release,
              "incoming DMA unnecessarily guards the independent publication value");
        simulateSafe(code, model);

        // A loop needs a wait on the backedge reuse as well as at final completion.
        code = optimize(setup + "addi x8, x0, 3\nloop:\ndma.load.ch0 x4, x1, x2\n"
            "addi x8, x8, -1\nbne x8, x0, loop\nnop\necall\n", model);
        check(countWaits(code) == 2, "loop channel reuse or exit lost its completion guard");
        simulateSafe(code, model);

        // Counting outstanding channels is insufficient for an advancing ring.
        std::string ring = setup + "dma.load.ch0 x4, x1, x2\n";
        for (int i = 0; i < 7; ++i) ring += "dma.load.ch1 x5, x1, x2\ndma.wait.ch1\n";
        ring += "ninth:\ndma.load.ch1 x5, x1, x2\ndma.wait.ch1\necall\n";
        code = optimize(ring, model);
        check(countWaits(code, 0) == 1, "ring wrap did not retire its occupied slot");
        check(code.blocks[1].body.front().op->opClass == OpClass::DmaWait &&
              code.blocks[1].body.front().op->channel == 0, "ring reuse crossed a CFG boundary without its wait");
        simulateSafe(code, model);

        // Different high DRAM bits matter even with equal low addresses.
        const std::string separated = setup + "dma.store.ch0 x1, x4, x2\naddi x9, x0, 1\n"
            "dma.config.ch0 x9\ndma.load.ch1 x5, x1, x2\necall\n";
        code = optimize(separated, model, false);
        check(find(code, OpClass::DmaLoad) < find(code, OpClass::DmaWait), "configured disjoint DRAM ranges alias");
        simulateSafe(optimize(separated, model), model);
        const std::string alias = setup + "dma.store.ch0 x1, x4, x2\n"
            "dma.load.ch1 x5, x1, x2\necall\n";
        code = optimize(alias, model, false);
        check(find(code, OpClass::DmaWait) < find(code, OpClass::DmaLoad), "DRAM alias ignored across channels");
        simulateSafe(optimize(alias, model), model);

        // A divergent CONFIG joins to unknown; preserve the conservative wait.
        code = optimize(setup + "dma.store.ch0 x1, x4, x2\nbeq x8, x0, join\nnop\n"
            "addi x9, x0, 1\ndma.config.ch0 x9\njoin:\ndma.load.ch1 x5, x1, x2\necall\n", model);
        check(code.blocks.back().body.front().op->opClass == OpClass::DmaWait,
              "CONFIG join manufactured an exact DRAM range");
        simulateSafe(code, model);

        // Falloff and branches to end labels are real completion boundaries.
        code = optimize(setup + "dma.load.ch0 x4, x1, x2\njal x0, end\nnop\nend:\n", model);
        check(countWaits(code) == 1 && !code.blocks.back().labels.empty(), "end-label exit lacks a wait");
        simulateSafe(code, model);
        code = optimize(setup + "dma.load.ch0 x4, x1, x2\n", model);
        check(countWaits(code) == 1, "falloff transfer was not retired");
        simulateSafe(code, model);

        // Mixed channels and directions repeatedly cross labels and wrap the ring.
        std::mt19937 random(290);
        std::string mixed = setup;
        for (int i = 0; i < 48; ++i) {
            if (i % 5 == 0) mixed += "batch" + std::to_string(i) + ":\n";
            const int channel = random() % 8;
            const bool load = random() % 2, second = random() % 2;
            const std::string operands = load ? (second ? " x5, x3, x2\n" : " x4, x1, x2\n") :
                                                (second ? " x3, x5, x2\n" : " x1, x4, x2\n");
            mixed += std::string(load ? "dma.load.ch" : "dma.store.ch") + std::to_string(channel) + operands;
        }
        simulateSafe(optimize(mixed + "ecall\n", model), model);

        // Publication remains a completion event; insertion is idempotent.
        code = optimize(setup + "dma.store.ch0 x1, x4, x2\ncsrrwi x0, x1, 0xC10 # atlas.release\necall\n", model);
        check(countWaits(code) == 1, "publication did not wait for DMA output");
        simulateSafe(code, model);
        PassContext ctx;
        ctx.model = model;
        insertDmaWaits(code, ctx);
        check(ctx.log.back() == "insert-dma-waits: inserted 0 waits", "wait insertion is not idempotent");

        rejects(setup + "jal x0, end\ndma.load.ch0 x4, x1, x2\nend:\necall\n", "delay slots");
        rejects(setup + "jalr x0, 0(x1)\nnop\n", "jalr");

        // Numerical load values are unknown, but both branch paths have timing obligations.
        const std::string dataBranch = setup + "lhu x10, 0(x4)\nbne x10, x0, other\nnop\n"
            "vadd.bf16 m8, m0, m2\njal x0, end\nnop\nother:\nvsub.bf16 m8, m0, m2\nend:\necall\n";
        const auto dataSchedule = flatten(optimize(dataBranch, model));
        check(!simulate(dataSchedule, SimOptions{.model = model}).stopReason.empty(), "dynamic checker unexpectedly knows payload");
        checkStaticSchedule(dataSchedule, model);
        auto rejectsStatic = [&](const std::string& source, const std::string& reason) {
            try { checkStaticSchedule(parseAsm(source), model); }
            catch (const std::runtime_error& error) {
                check(std::string(error.what()).find(reason) != std::string::npos,
                      "unexpected static rejection: " + std::string(error.what()));
                return;
            }
            throw std::runtime_error("static checker accepted " + reason);
        };
        rejectsStatic("beq x0, x0, end\nnop\nvadd.bf16 m8, m0, m2\n"
                      "vsub.bf16 m10, m8, m2\ndelay 100\nnop\nend:\necall\n", "insufficient issue distance");
        rejectsStatic("vadd.bf16 m8, m0, m2\nnext:\necall\n", "block boundary");
        rejectsStatic("vadd.bf16 m8, m0, m2\ndelay 100\necall\n", "halt bypasses");
        rejectsStatic("jalr x0, x1, 0\nnop\n", "known CFG targets");
        rejectsStatic("jal x0, end\nend:\n", "explicit branch delay slot");
        rejectsStatic("jal x0, end\ncsrrw x0, x0, 0xC10 # atlas.release\nend:\necall\n", "delay slot");
        rejectsStatic(setup + "dma.load.ch0 x4, x1, x2\nbeq x0, x0, end\nnop\n"
                      "dma.wait.ch0\nend:\necall\n", "DMA");
        std::cout << "PASS: " << checks << " selected-model DMA wait and CFG checks\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
