#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

#include "core/depgraph.h"
#include "core/reservations.h"
#include "core/simulator.h"
#include "passes/pass.h"

static void check(bool value, const char* why) {
    if (!value) throw std::runtime_error(why);
}

static void checkDmaProfile(const std::filesystem::path& path, const MachineModel& mxus) {
    const std::string text =
        "schema=atlas-dma-profile-v1\nconfig=EE290SimConfig\nsource_ir_sha256=" + std::string(64, 'a') +
        "\nevidence_sha256=" + std::string(64, 'e') + "\noperand_capture=issue\nconfig_update=issue\n"
        "vmem_word_address_low_bit=3\nvmem_line_address_bits=16\ntransfer_size_bits=13\n"
        "vmem_line_bytes=32\nvmem_lines=49152\nchannels=8\ncommand_slots=8\ncompletion=explicit-wait\n"
        "lsu_priority_over_dma=1\nsupported_max_transfer_bytes=4096\n";
    auto load = [&](const std::string& input, const MachineModel& prior = MachineModel{}) {
        { std::ofstream f(path); f << input; }
        return readExperimentalDmaProfile(path.string(), prior);
    };
    auto model = load(text, mxus);
    check(model.rtlDma && model.mxu1FirstWriteAge == mxus.mxu1FirstWriteAge &&
          model.mxu0OverwriteAccReadHold == mxus.mxu0OverwriteAccReadHold,
          "DMA profile discarded prior MXU settings");
    check(!MachineModel{}.rtlDma, "default model enabled RTL DMA");
    // Every field is mandatory and its accepted values are deliberately narrow.
    for (size_t begin = 0; begin < text.size();) {
        size_t end = text.find('\n', begin) + 1;
        const auto line = text.substr(begin, end - begin);
        const auto equal = line.find('=');
        for (const auto& malformed : {
                 text.substr(0, begin) + text.substr(end), text + line,
                 text.substr(0, begin) + line.substr(0, equal + 1) + "unsupported\n" + text.substr(end)}) {
            bool rejected = false;
            try { load(malformed); } catch (const std::runtime_error&) { rejected = true; }
            check(rejected, "missing, duplicated, or unsupported DMA field accepted");
        }
        begin = end;
    }
    bool rejected = false;
    try { load(text + "unknown=0\n"); } catch (const std::runtime_error&) { rejected = true; }
    check(rejected, "unknown DMA field accepted");
    auto otherIr = mxus;
    otherIr.sourceIrSha256 = std::string(64, 'f');
    rejected = false;
    try { load(text, otherIr); } catch (const std::runtime_error&) { rejected = true; }
    check(rejected, "DMA profile accepted another hardware IR");

    auto commands = parseAsm("dma.config.ch0 x5\ndma.load.ch0 x6, x1, x12\n"
                             "dma.store.ch1 x1, x6, x12\naddi x1, x0, 32\ndma.wait.ch0\n").instrs;
    auto regs = zeroRegs();
    regs[1] = 0x90000000;
    regs[6] = 0x20000400;
    regs[12] = 2048;
    auto config = footprintOf(commands[0], regs, model);
    check(config.error.empty() && config.dmaCycles == 0 && config.doneAge == 0,
          "RTL DMA.CONFIG was queued");
    for (const auto& access : config.accesses)
        check(!access.atCompletion, "RTL DMA.CONFIG did not capture at issue");
    check(footprintOf(commands[0], regs).dmaCycles == dmaTransferCycles(0),
          "default DMA.CONFIG behavior changed");
    auto loadFootprint = footprintOf(commands[1], regs, model);
    auto storeFootprint = footprintOf(commands[2], regs, model);
    check(loadFootprint.error.empty() && storeFootprint.error.empty(), "legal RTL DMA rejected");
    for (const auto& footprint : {loadFootprint, storeFootprint}) {
        int capture = 0, vmem = 0, dram = 0;
        for (const auto& access : footprint.accesses) {
            if (access.res == Res::XReg || access.res == Res::DmaBase) {
                check(!access.atCompletion && access.age == 0 && !access.write,
                      "DMA operand not captured at issue");
                ++capture;
            } else if (access.res == Res::Vmem) {
                check(access.first == 128 && access.count == 64 && access.atCompletion && !access.anywhere,
                      "DMA word address/range was not converted to local VMEM lines");
                ++vmem;
            } else if (access.res == Res::Dram) {
                check(access.atCompletion && access.anywhere, "DRAM alias lifetime missing");
                ++dram;
            }
        }
        check(capture == 4 && vmem == 1 && dram == 1, "DMA footprint lost captured fields or memory");
    }
    for (const auto& access : loadFootprint.accesses) {
        if (access.res == Res::Vmem) check(access.write, "DMA load did not write VMEM");
        if (access.res == Res::Dram) check(!access.write, "DMA load did not read DRAM");
    }
    for (const auto& access : storeFootprint.accesses) {
        if (access.res == Res::Vmem) check(!access.write, "DMA store did not read VMEM");
        if (access.res == Res::Dram) check(access.write, "DMA store did not write DRAM");
    }
    check(!footprintOf(commands[1], regs).error.empty(), "default byte-address interpretation changed");
    auto overwrite = footprintOf(commands[3], regs, model);
    check(dependence(commands[1], loadFootprint, commands[3], overwrite, model).distance == 1,
          "launch-captured scalar cannot be overwritten after launch");
    auto wait = footprintOf(commands[4], regs, model);
    check(dependence(commands[0], config, commands[4], wait, model).distance == 0,
          "scalar DMA.CONFIG was treated as a queued channel command");
    check(dependence(commands[0], config, commands[1], loadFootprint, model).distance == 1,
          "DMA base configuration dependence missing");

    for (uint32_t pointer : {0x20000001u, 0x20060000u, 0x2005fff8u}) {
        auto bad = regs;
        bad[6] = pointer;
        check(!footprintOf(commands[1], bad, model).error.empty(), "unaligned or out-of-range DMA VMEM accepted");
    }
    for (uint32_t size : {0u, 1u, 31u, 33u, 4097u, 8192u, 0xffffffffu}) {
        auto bad = regs;
        bad[12] = size;
        check(!footprintOf(commands[1], bad, model).error.empty(), "unsupported DMA size accepted");
    }
    auto bad = regs;
    bad[1] = 0x90000001;
    check(!footprintOf(commands[1], bad, model).error.empty(), "unaligned DMA DRAM pointer accepted");
    auto end = regs;
    end[6] = 0x2005fff8;
    end[12] = 32;
    check(footprintOf(commands[1], end, model).error.empty(), "last physical DMA VMEM line rejected");
    auto largest = regs;
    largest[12] = 4096;
    check(footprintOf(commands[1], largest, model).error.empty(), "supported maximum DMA size rejected");
    auto unknown = footprintOf(commands[1], unknownRegs(), model);
    for (const auto& access : unknown.accesses)
        if (access.res == Res::Vmem)
            check(access.anywhere && access.atCompletion, "unknown DMA address was not conservative");
}

int main() {
    auto path = std::filesystem::temp_directory_path() /
        ("atlas-profile-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    try {
        std::string base = "schema=atlas-mxu1-profile-v1\nconfig=EE290SimConfig\nsource_ir_sha256=" +
            std::string(64, 'a') + "\nevidence_sha256=" + std::string(64, 'b') + "\n";
        auto load = [&](const std::string& text) {
            { std::ofstream f(path); f << text; }
            return readExperimentalMxu1Profile(path.string());
        };
        MachineModel model = load(base + "first_write_age=3\noverwrite_acc_read_hold=0\n");
        for (const std::string& bad : {
                 base + "first_write_age=0\noverwrite_acc_read_hold=0\n",
                 base + "first_write_age=33\noverwrite_acc_read_hold=0\n",
                 base + "first_write_age=3junk\noverwrite_acc_read_hold=0\n",
                 base + "first_write_age=3\noverwrite_acc_read_hold=2\n",
                 base + "first_write_age=3\nfirst_write_age=4\noverwrite_acc_read_hold=0\n",
                 base + "first_write_age=3\noverwrite_acc_read_hold=0\nunknown=1\n",
                 std::string("first_write_age=3\noverwrite_acc_read_hold=0\n")}) {
            bool rejected = false;
            try { load(bad); } catch (const std::runtime_error&) { rejected = true; }
            check(rejected, "malformed profile accepted");
        }
        auto program = parseAsm("vmatpop.fp8.acc.mxu1 m8, acc0, e0\nvmatmul.mxu1 acc0, m0, w0\n");
        const auto& pop = program.instrs[0];
        const auto& multiply = program.instrs[1];
        ReservationTable table;
        table.reserve(pop, footprintOf(pop, zeroRegs()), 0);
        check(!table.conflict(multiply, footprintOf(multiply, zeroRegs()), 1).empty(), "default model changed");
        check(table.conflict(multiply, footprintOf(multiply, zeroRegs(), model), 1).empty(), "overwrite still occupies accumulator read");
        auto accumulate = parseAsm("vmatmul.acc.mxu1 acc0, m0, w0\n").instrs[0];
        check(!table.conflict(accumulate, footprintOf(accumulate, zeroRegs(), model), 1).empty(), "accumulation read hold lost");
        auto mxu0 = parseAsm("vmatmul.mxu0 acc0, m0, w0\n").instrs[0];
        check(footprintOf(mxu0, zeroRegs()).doneAge == footprintOf(mxu0, zeroRegs(), model).doneAge, "MXU0 changed");

        PassContext ctx;
        ctx.model = model;
        Code code = buildBlocks(program);
        runPasses(code, {}, ctx);
        SimOptions options;
        options.model = model;
        auto optimized = flatten(code);
        check(simulate(optimized, options).violations.empty(), "profile schedule rejected by profile simulator");
        check(!simulate(optimized).violations.empty(), "profile was not used by scheduler");

        // A changed extracted pipeline depth must affect accesses, occupancy,
        // dependency edges, emitted spacing, and the validation simulator.
        model.mxu1FirstWriteAge = 5;
        auto consumer = parseAsm("vmatmul.mxu1 acc0, m0, w0\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n");
        auto graph = buildGraph(consumer.instrs, zeroRegs(), 0, model);
        check(graph.edges.size() == 1 && graph.edges[0].distance == 6, "write age missing from dependence");
        check(graph.footprints[0].doneAge == 36, "write age missing from footprint");
        code = buildBlocks(consumer);
        ctx.model = model;
        runPasses(code, {}, ctx);
        check(code.blocks[0].issue[1] == 6, "write age missing from emitted schedule");
        options.model = model;
        check(simulate(flatten(code), options).violations.empty(), "changed latency schedule invalid");

        std::string mxu0Text = "schema=atlas-mxu0-profile-v1\nconfig=EE290SimConfig\nsource_ir_sha256=" +
            std::string(64, 'a') + "\nevidence_sha256=" + std::string(64, 'c') + "\noverwrite_acc_read_hold=0\n";
        auto load0 = [&](const std::string& text, const MachineModel& prior = MachineModel{}) {
            { std::ofstream f(path); f << text; }
            return readExperimentalMxu0Profile(path.string(), prior);
        };
        auto combined = load0(mxu0Text, model);
        check(combined.mxu1FirstWriteAge == 5 && !combined.mxu1OverwriteAccReadHold &&
              !combined.mxu0OverwriteAccReadHold, "profile composition discarded an earlier projection");
        auto pop0 = parseAsm("vmatpop.fp8.acc.mxu0 m8, acc0, e0\n").instrs[0];
        auto accumulate0 = parseAsm("vmatmul.acc.mxu0 acc0, m0, w0\n").instrs[0];
        ReservationTable mxu0Table;
        mxu0Table.reserve(pop0, footprintOf(pop0, zeroRegs()), 0);
        check(!mxu0Table.conflict(mxu0, footprintOf(mxu0, zeroRegs()), 1).empty(), "default MXU0 model changed");
        check(mxu0Table.conflict(mxu0, footprintOf(mxu0, zeroRegs(), combined), 1).empty(), "MXU0 overwrite still holds accumulator read");
        check(!mxu0Table.conflict(accumulate0, footprintOf(accumulate0, zeroRegs(), combined), 1).empty(), "MXU0 accumulation lost read reservation");
        check(footprintOf(mxu0, zeroRegs(), combined).doneAge == 94, "MXU0 write timing changed");
        check(load0(mxu0Text).mxu1OverwriteAccReadHold, "MXU0 projection changed MXU1 default");
        for (const auto& bad : {mxu0Text + "first_write_age=3\n", mxu0Text + "unknown=0\n",
                               mxu0Text + "overwrite_acc_read_hold=1\n", base + "overwrite_acc_read_hold=0\n"}) {
            bool rejected = false;
            try { load0(bad); } catch (const std::runtime_error&) { rejected = true; }
            check(rejected, "malformed MXU0 projection accepted");
        }
        model.sourceIrSha256 = std::string(64, 'd');
        bool mismatchedIrRejected = false;
        try { load0(mxu0Text, model); } catch (const std::runtime_error&) { mismatchedIrRejected = true; }
        check(mismatchedIrRejected, "profiles from different IR were combined");
        auto program0 = parseAsm("vmatpop.fp8.acc.mxu0 m8, acc0, e0\nvmatmul.mxu0 acc0, m0, w0\n");
        code = buildBlocks(program0);
        ctx.model = combined;
        runPasses(code, {}, ctx);
        check(code.blocks[0].issue[1] == 1, "MXU0 projection missing from emitted schedule");
        options.model = combined;
        check(simulate(flatten(code), options).violations.empty(), "MXU0 profile schedule invalid");
        check(!simulate(flatten(code)).violations.empty(), "MXU0 profile was not used by scheduler");
        checkDmaProfile(path, combined);
        std::filesystem::remove(path);
        std::cout << "PASS: partial profile parsing, default isolation, resource use, graph, scheduler and simulator\n";
    } catch (const std::exception& error) {
        std::filesystem::remove(path);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
