#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

#include "core/depgraph.h"
#include "core/simulator.h"
#include "passes/pass.h"
#include "tool/viewer.h"

static int checks = 0;

static void check(bool condition, const std::string& reason) {
    ++checks;
    if (!condition) throw std::runtime_error(reason);
}

static MachineModel rangedModel() {
    MachineModel model;
    model.rtlDma = model.rtlDmaRanges = true;
    return model;
}

static RegValues entry() {
    auto regs = zeroRegs(true);
    regs[1] = 0x90000000;
    regs[2] = 32;
    regs[3] = 0x90000020;
    regs[4] = 0x20000000;
    regs[5] = 0x20000400;
    return regs;
}

static Access dram(const Footprint& f) {
    for (const auto& a : f.accesses) if (a.res == Res::Dram) return a;
    throw std::runtime_error("missing DRAM footprint");
}

static DepGraph graph(const std::string& source, const RegValues& regs = entry(),
                      const MachineModel& model = rangedModel()) {
    return buildGraph(parseAsm(source).instrs, regs, 0xFFFFFFFE, model);
}

static void rejects(const std::string& source, const RegValues& regs = entry(),
                    const MachineModel& model = rangedModel()) {
    try { graph(source, regs, model); }
    catch (const std::runtime_error& error) {
        check(std::string(error.what()).find("memory access conflicts") != std::string::npos,
              "unexpected rejection: " + std::string(error.what()));
        return;
    }
    throw std::runtime_error("unsafe DRAM overlap accepted");
}

static bool hasEdge(const DepGraph& g, int from, int to) {
    for (const auto& e : g.edges) if (e.from == from && e.to == to) return true;
    return false;
}

static const std::string pair =
    "dma.store.ch0 x1, x4, x2\ndma.store.ch1 x3, x5, x2\ndma.wait.ch0\ndma.wait.ch1\n";

static void profileTests() {
    const std::string common = "config=EE290SimConfig\nsource_ir_sha256=" + std::string(64, 'a') +
        "\nevidence_sha256=" + std::string(64, 'e') + "\noperand_capture=issue\nconfig_update=issue\n"
        "vmem_word_address_low_bit=3\nvmem_line_address_bits=16\ntransfer_size_bits=13\n"
        "vmem_line_bytes=32\nvmem_lines=49152\nchannels=8\ncommand_slots=8\ncompletion=explicit-wait\n"
        "lsu_priority_over_dma=1\nsupported_max_transfer_bytes=4096\n";
    const std::string capability = "dram_address=base32-concat-low32\ndram_address_bits=37\n"
        "dram_alignment_bytes=32\ndram_base_reset=0\ndram_wrap=conservative-alias\n";
    const std::string v1 = "schema=atlas-dma-profile-v1\n" + common;
    const std::string v2 = "schema=atlas-dma-profile-v2\n" + common + capability;
    auto path = std::filesystem::temp_directory_path() /
        ("atlas-dma-range-profile-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    struct Cleanup { std::filesystem::path path; ~Cleanup() { std::filesystem::remove(path); } } cleanup{path};
    auto load = [&](const std::string& text, const MachineModel& prior = MachineModel{}) {
        { std::ofstream stream(path); stream << text; }
        return readExperimentalDmaProfile(path.string(), prior);
    };
    auto model = load(v2);
    check(model.rtlDma && model.rtlDmaRanges, "v2 did not enable precise DRAM ranges");
    check(!load(v1, model).rtlDmaRanges, "v1 inherited a capability without its evidence");
    auto bad = [&](const std::string& text, const MachineModel& prior = MachineModel{}) {
        try { load(text, prior); } catch (const std::runtime_error&) { ++checks; return; }
        throw std::runtime_error("malformed v2 profile accepted");
    };
    for (size_t begin = 0; begin < v2.size();) {
        const size_t end = v2.find('\n', begin) + 1;
        const auto line = v2.substr(begin, end - begin);
        const auto equals = line.find('=');
        bad(v2.substr(0, begin) + v2.substr(end));
        bad(v2 + line);
        bad(v2.substr(0, begin) + line.substr(0, equals + 1) + "unsupported\n" + v2.substr(end));
        begin = end;
    }
    bad(v1 + capability);
    bad(v2 + "unrecognized=0\n");
    auto other = model;
    other.sourceIrSha256 = std::string(64, 'f');
    bad(v2, other);
    model.mxu1FirstWriteAge = 4;
    check(load(v2, model).mxu1FirstWriteAge == 4, "v2 discarded existing MXU capability");
}

static void footprintTests() {
    check(!zeroRegs().dmaBase && !unknownRegs().dmaBase, "unproven initial base was assumed zero");
    check(zeroRegs(true).dmaBase == 0, "profile reset fact was lost");
    auto regs = entry();
    const auto store = parseAsm("dma.store.ch0 x1, x4, x2\n").instrs[0];
    auto access = [&](const RegValues& r) { return dram(footprintOf(store, r, rangedModel())); };
    auto a = access(regs);
    check(!a.anywhere && a.dramFirstByte == 0x90000000 && a.dramBytes == 32,
          "known address was not represented as a byte interval");
    auto v1 = rangedModel();
    v1.rtlDmaRanges = false;
    check(dram(footprintOf(store, regs, v1)).anywhere, "v1 became precise without new evidence");
    for (int unknown : {-1, 1, 2}) {
        auto r = regs;
        if (unknown == -1) r.dmaBase.reset(); else r[unknown].reset();
        check(access(r).anywhere, "unknown CONFIG/pointer/size lost conservative aliasing");
    }
    regs.dmaBase = 1;
    a = access(regs);
    check(a.dramFirstByte == 0x190000000ULL, "address above 32 bits was truncated");
    regs.dmaBase = 33;
    check(access(regs).dramFirstByte == a.dramFirstByte, "TileLink 37-bit projection was omitted");
    regs.dmaBase = 0;
    regs[1] = 0xffffffe0;
    regs[2] = 64;
    a = access(regs);
    check(!a.anywhere && a.dramFirstByte == 0xffffffe0 && a.dramBytes == 64,
          "low32 carry was mistaken for a 32-bit wrap");
    auto second = regs;
    second.dmaBase = 1;
    second[1] = 0;
    second[2] = 32;
    check(accessesOverlap(a, access(second)), "low32 carry did not alias the next base window");
    second[1] = 32;
    check(!accessesOverlap(a, access(second)), "adjacent wide ranges spuriously alias");
    regs.dmaBase = 31;
    check(access(regs).anywhere, "37-bit bus wrap was unsafely represented as a single range");
    regs[2] = 32;
    a = access(regs);
    check(!a.anywhere && a.dramFirstByte == (uint64_t{1} << 37) - 32,
          "legal final bus line was rejected or overflowed");
    regs.dmaBase = 0xffffffff;
    check(access(regs).dramFirstByte == a.dramFirstByte, "high CONFIG bits did not truncate");
    regs[2] = 64;
    check(access(regs).anywhere, "64-bit command wrap lost conservative aliasing");
    regs = entry();
    for (const auto& in : parseAsm("addi x9, x0, 1\ndma.config.ch0 x9\naddi x9, x0, 7\n").instrs)
        applyScalar(in, regs);
    check(regs[9] == 7 && regs.dmaBase == 1 && access(regs).dramFirstByte == 0x190000000ULL,
          "CONFIG retained a live scalar-register reference instead of its captured value");
}

static void dependencyTests() {
    auto g = graph(pair);
    check(hasEdge(g, 0, 1) && !hasEdge(g, 2, 1), "disjoint transfers lost launch order or gained a wait");
    auto regs = entry();
    regs[3] = *regs[1];
    rejects(pair, regs);
    regs[2] = 64;
    regs[3] = *regs[1] + 32;
    rejects(pair, regs);
    auto legacy = rangedModel();
    legacy.rtlDmaRanges = false;
    rejects(pair, entry(), legacy);
    auto unknown = entry();
    unknown.dmaBase.reset();
    rejects(pair, unknown);
    g = graph("dma.store.ch0 x1, x4, x2\ndma.wait.ch0\ndma.load.ch1 x5, x1, x2\ndma.wait.ch1\n");
    check(hasEdge(g, 1, 2), "overlapping store/load lost explicit completion guard");
    graph("dma.load.ch0 x4, x1, x2\ndma.store.ch1 x3, x5, x2\ndma.wait.ch0\ndma.wait.ch1\n");
    graph("dma.load.ch0 x4, x1, x2\ndma.load.ch1 x5, x1, x2\ndma.wait.ch0\ndma.wait.ch1\n");

    const std::string configPair = "dma.store.ch0 x1, x4, x2\naddi x9, x0, 1\ndma.config.ch0 x9\n"
        "dma.store.ch1 x1, x5, x2\ndma.wait.ch0\ndma.wait.ch1\n";
    g = graph(configPair);
    check(dram(g.footprints[0]).dramFirstByte == 0x90000000 &&
          dram(g.footprints[3]).dramFirstByte == 0x190000000ULL,
          "CONFIG retroactively changed an issued transfer");
    check(hasEdge(g, 0, 2) && hasEdge(g, 2, 3), "base capture WAR/RAW ordering was lost");
    auto sameBus = configPair;
    sameBus.replace(sameBus.find("x0, 1"), 5, "x0, 32");
    rejects(sameBus);
    auto unknownBase = configPair;
    unknownBase.replace(unknownBase.find("addi x9, x0, 1"), 14, "csrrs x9, x0, 0xc00");
    rejects(unknownBase);
}

static void cfgTests() {
    auto cfg = [](const std::string& s) { return buildBlocks(parseAsm(s)); };
    auto code = cfg("addi x9, x0, 1\ndma.config.ch0 x9\nnext:\naddi x9, x0, 2\necall\n");
    auto values = blockEntryValues(code, true);
    check(values[1].dmaBase == 1, "CONFIG value did not propagate across an idle block boundary");
    code = cfg("beq x0, x0, join\nnop\naddi x9, x0, 1\ndma.config.ch0 x9\njoin:\necall\n");
    values = blockEntryValues(code, true);
    check(!values.back().dmaBase, "differing predecessor CONFIG values were not joined to unknown");
    code = cfg("dma.config.ch0 x0\nbeq x0, x0, join\nnop\ndma.config.ch0 x0\njoin:\necall\n");
    check(blockEntryValues(code, true).back().dmaBase == 0, "equal CONFIG joins lost precision");
    code = cfg("entry:\naddi x9, x9, 1\ndma.config.ch0 x9\nbne x9, x0, entry\nnop\necall\n");
    values = blockEntryValues(code, true);
    check(!values[0].dmaBase && !values[0][9], "entry backedge retained reset-only assumptions");
    code = cfg("dma.config.ch0 x0\njalr x0, x1, 0\nnop\n");
    values = blockEntryValues(code, true);
    check(!values[0].dmaBase && !values[0][1], "unknown indirect successor retained entry assumptions");
    code = cfg("jal x0, done\nnop\nunreachable:\necall\ndone:\necall\n");
    values = blockEntryValues(code, true);
    check(!values[1].dmaBase && !values[1][1], "unreachable block received reset assumptions");
}

static void nativeTests() {
    const std::string setup = "lui x1, 0x90000\naddi x3, x1, 32\naddi x2, x0, 32\n"
        "lui x4, 0x20000\naddi x5, x4, 1024\n";
    const auto original = parseAsm(setup + pair + "ecall\n");
    SimOptions options;
    options.model = rangedModel();
    for (double scale : {0.001, 1.0, 100.0}) {
        options.dmaLatencyScale = scale;
        const auto before = simulate(original, options);
        check(before.violations.empty() && before.stopReason.empty(), "checker rejects safe disjoint transfers");
        Code code = buildBlocks(original);
        PassContext ctx;
        ctx.model = options.model;
        runPasses(code, {}, ctx);
        const auto after = simulate(flatten(code), options);
        check(after.violations.empty() && after.stopReason.empty(), "scheduler/checker disagree on precise ranges");
        int transfers = 0, firstWait = -1;
        for (const auto& in : flatten(code).instrs) {
            if (in.op->opClass == OpClass::DmaWait && firstWait == -1) firstWait = transfers;
            if (in.op->opClass == OpClass::DmaStore) ++transfers;
        }
        check(transfers == 2 && firstWait == 2, "independent stores were not launched before their waits");
        const auto view = buildProgramView("ranged.S", original, code, before, after, options.model);
        check(!view.blocks.empty(), "viewer did not use ranged entry state");
    }
    const auto unsafe = parseAsm(setup + "dma.store.ch0 x1, x4, x2\n"
        "addi x9, x0, 32\ndma.config.ch0 x9\ndma.store.ch1 x1, x5, x2\ndma.wait.ch0\ndma.wait.ch1\necall\n");
    auto bad = simulate(unsafe, options);
    check(!bad.violations.empty(), "checker missed high-base bus-address alias");
    const auto captured = parseAsm(setup + "dma.store.ch0 x1, x4, x2\n"
        "addi x9, x0, 1\ndma.config.ch0 x9\ndma.store.ch1 x1, x5, x2\ndma.wait.ch0\ndma.wait.ch1\necall\n");
    auto good = simulate(captured, options);
    check(good.violations.empty() && good.stopReason.empty(), "checker did not retain captured prior CONFIG");
    Code configuredCode = buildBlocks(captured);
    PassContext configuredContext;
    configuredContext.model = options.model;
    runPasses(configuredCode, {}, configuredContext);
    good = simulate(flatten(configuredCode), options);
    check(good.violations.empty() && good.stopReason.empty(), "scheduling changed captured CONFIG values");
}

int main() {
    try {
        profileTests();
        footprintTests();
        dependencyTests();
        cfgTests();
        nativeTests();
        std::cout << "PASS: " << checks << " RTL DMA DRAM range checks\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
