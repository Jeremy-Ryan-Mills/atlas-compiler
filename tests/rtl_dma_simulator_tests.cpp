#include <iostream>
#include <stdexcept>
#include <string>

#include "core/simulator.h"
#include "passes/pass.h"

static int checks = 0;

static void check(bool condition, const std::string& reason) {
    ++checks;
    if (!condition) throw std::runtime_error(reason);
}

static SimOptions options(double scale = 1.0) {
    SimOptions opt;
    opt.model.rtlDma = true;
    opt.dmaLatencyScale = scale;
    return opt;
}

static const std::string setup =
    "lui x4, 0x20000\naddi x5, x4, 1024\nlui x1, 0x90000\naddi x2, x0, 32\n";

static SimResult sim(const std::string& source, double scale = 1.0) {
    return simulate(parseAsm(source), options(scale));
}

static bool contains(const SimResult& result, const std::string& reason) {
    for (const std::string& violation : result.violations)
        if (violation.find(reason) != std::string::npos) return true;
    return false;
}

static void accepts(const std::string& source, double scale = 1.0) {
    SimResult result = sim(source, scale);
    if (!result.violations.empty())
        throw std::runtime_error("unexpected violation: " + result.violations.front());
    check(result.stopReason.empty(), "unexpected stop: " + result.stopReason);
}

static void rejects(const std::string& source, const std::string& reason, double scale = 1.0) {
    const SimResult result = sim(source, scale);
    check(contains(result, reason), "expected violation containing '" + reason + "'");
}

static void rejectsAdmission(const std::string& source, const std::string& reason, bool robust = true) {
    Code code = buildBlocks(parseAsm(source));
    PassContext ctx;
    ctx.model = options().model;
    ctx.robustDma = robust;
    try { runPasses(code, {"schedule"}, ctx); }
    catch (const std::runtime_error& error) {
        check(std::string(error.what()).find(reason) != std::string::npos,
              "unexpected admission rejection: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("unsafe program admitted: " + source);
}

int main() {
    try {
        // Scalar configuration does not occupy a channel or enqueue ring slot.
        accepts("dma.config.ch0 x0\necall\n");
        accepts(setup + "dma.load.ch0 x4, x1, x2\naddi x4, x0, 128\naddi x1, x0, 64\n"
                        "addi x2, x0, 96\ndma.config.ch0 x1\ndma.wait.ch0\necall\n");

        // Guessed completion cannot retire a memory/channel reservation, even
        // with a long explicit delay or a tiny estimated DMA latency.
        const std::string load = setup + "dma.load.ch0 x4, x1, x2\n";
        for (double scale : {0.001, 1.0, 100.0}) {
            rejects(load, "program ends without explicit DMA.WAIT", scale);
            rejects(load + "delay 4095\nnop\necall\n", "missing explicit DMA.WAIT", scale);
            rejects(load + "delay 4095\nvload m0, 0(x4)\ndma.wait.ch0\n", "until its explicit DMA.WAIT", scale);
            rejects(load + "delay 4095\ndma.load.ch0 x5, x1, x2\ndma.wait.ch0\n", "channel 0 is still busy", scale);
            rejects(load + "delay 4095\ncsrrwi x0, x1, 0xC10 # atlas.release\ndma.wait.ch0\n",
                    "atlas.release publishes", scale);
            accepts(load + "dma.wait.ch0\nvload m0, 0(x4)\ndelay 34\nnop\necall\n", scale);
        }
        rejects(load + "dma.wait.ch1\nvload m0, 0(x4)\ndma.wait.ch0\n", "until its explicit DMA.WAIT");

        // VMEM buffers are live throughout transfers, and DRAM stores cannot be
        // assumed disjoint just because their VMEM source is a different tile.
        accepts(load + "vload m0, 0(x5)\ndma.wait.ch0\ndelay 34\nnop\necall\n");
        rejects(setup + "dma.store.ch0 x1, x4, x2\nvstore m0, 0(x4)\ndma.wait.ch0\n",
                "until its explicit DMA.WAIT");
        rejects(load + "dma.store.ch1 x1, x5, x2\ndma.wait.ch0\ndma.wait.ch1\n",
                "until its explicit DMA.WAIT");
        accepts(load + "dma.load.ch1 x5, x1, x2\ndma.wait.ch0\ndma.wait.ch1\necall\n");

        // A DMA may access memory immediately; its average transfer duration
        // does not create time for an older LSU producer to finish.
        rejects(setup + "vstore m0, 0(x4)\ndma.store.ch0 x1, x4, x2\ndma.wait.ch0\n",
                "is still accessing", 100.0);
        accepts(setup + "vstore m0, 0(x4)\ndelay 34\ndma.store.ch0 x1, x4, x2\ndma.wait.ch0\necall\n");

        // A physical ring slot can be busy with only two outstanding commands.
        std::string ring = setup + "dma.load.ch0 x4, x1, x2\n";
        for (int i = 1; i < 8; ++i)
            ring += "dma.load.ch1 x5, x1, x2\ndma.wait.ch1\n";
        rejects(ring + "dma.load.ch1 x5, x1, x2\ndma.wait.ch1\ndma.wait.ch0\n", "ring slot 0 is still occupied");
        accepts(ring + "dma.wait.ch0\ndma.load.ch1 x5, x1, x2\ndma.wait.ch1\necall\n");

        // A long estimated wait cannot hide a fixed-latency RAW dependency: the
        // hardware can complete the DMA before the wait reaches the frontend.
        rejects(load + "vsquare.bf16 m2, m0\ndma.wait.ch0\nvsquare.bf16 m4, m2\n",
                "reserved for writing", 100.0);

        // The store fits a hole between two reduction reads at zero stall. An
        // intermediate wait length shifts it into the second read burst. This
        // is a physical bank alias (m32/m0), not a logical register dependency.
        const std::string burst = setup + "dma.load.ch0 x5, x1, x2\nvredsum.bf16 m4, m0\ndelay 27\n";
        accepts(burst + "nop\nnop\nvstore m32, 0(x4)\ndelay 130\ndma.wait.ch0\nnop\necall\n");
        rejects(burst + "dma.wait.ch0\nnop\nvstore m32, 0(x4)\ndelay 130\nnop\necall\n",
                "MREG bank 0 port busy", 100.0);

        // Native admission rejects unsupported CFG/lifetime assumptions before
        // passes mutate the program. CONFIG needs no completion annotation.
        rejectsAdmission(setup + "dma.load.ch0 x4, x1, x2\ndma.wait.ch0\n", "robust variable-wait", false);
        rejectsAdmission(setup + "dma.load.ch0 x4, x1, x2\nlater:\ndma.wait.ch0\n", "block boundaries");
        rejectsAdmission(setup + "jal x0, done\ndma.load.ch0 x4, x1, x2\ndone:\ndma.wait.ch0\n",
                         "delay slots");
        rejectsAdmission(setup + "dma.load.ch0 x4, x1, x2\ncsrrwi x0, x1, 0xC10 # atlas.release\ndma.wait.ch0\n",
                         "pending RTL DMA");
        for (const std::string& source : {
                 std::string("dma.config.ch0 x0\ncsrrwi x0, x1, 0xC10 # atlas.release\necall\n"),
                 setup + "addi x20, x0, 2\nloop:\ndma.load.ch0 x4, x1, x2\ndma.wait.ch0\n"
                         "addi x20, x20, -1\nbne x20, x0, loop\nnop\necall\n"}) {
            Code code = buildBlocks(parseAsm(source));
            PassContext ctx;
            ctx.model = options().model;
            runPasses(code, {"schedule"}, ctx);
            auto result = simulate(flatten(code), options());
            check(result.violations.empty() && result.stopReason.empty(), "safe idle-boundary schedule rejected");
        }

        // Legacy mode still retires DMA according to its established estimate.
        auto legacy = simulate(parseAsm("addi x2, x0, 32\ndma.load.ch0 x0, x0, x2\ndelay 100\nnop\necall\n"));
        check(legacy.violations.empty(), "legacy DMA completion semantics changed");
        std::cout << "PASS: " << checks << " RTL DMA simulator and admission checks\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
