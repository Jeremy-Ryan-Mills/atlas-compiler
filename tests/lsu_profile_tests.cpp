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

static const Access& access(const Footprint& f, Res res) {
    for (const auto& a : f.accesses) if (a.res == res) return a;
    throw std::runtime_error("missing access");
}

static const Hold& hold(const Footprint& f, Unit unit) {
    for (const auto& h : f.holds) if (h.unit == unit) return h;
    throw std::runtime_error("missing hold");
}

static Code scheduled(const AsmProgram& program, const MachineModel& model) {
    Code code = buildBlocks(program);
    PassContext ctx;
    ctx.model = model;
    ctx.schedulePriority = SchedulePriority::Input;
    runPasses(code, {"strip-artifacts", "schedule"}, ctx);
    SimOptions options;
    options.model = model;
    const auto result = simulate(flatten(code), options);
    check(result.violations.empty() && result.stopReason.empty(), "selected-model schedule did not validate");
    return code;
}

int main() {
    const auto path = std::filesystem::temp_directory_path() /
        ("atlas-lsu-profile-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    try {
        const std::string base = "schema=atlas-lsu-profile-v1\nconfig=EE290SimConfig\nsource_ir_sha256=" +
            std::string(64, 'a') + "\nevidence_sha256=" + std::string(64, 'b') +
            "\nrows=32\nrow_step=1\noperand_capture=issue\n";
        const std::string original = base +
            "vload_read_age=1\nvload_write_age=3\nvload_first_free_age=35\n"
            "vstore_read_age=1\nvstore_write_age=3\nvstore_first_free_age=35\n";
        auto load = [&](const std::string& text, const MachineModel& prior = MachineModel{}) {
            { std::ofstream file(path); file << text; }
            return readExperimentalLsuProfile(path.string(), prior);
        };
        auto rejected = [&](const std::string& text) {
            bool caught = false;
            try { load(text); } catch (const std::runtime_error&) { caught = true; }
            check(caught, "malformed LSU profile accepted");
        };
        auto replace = [&](const std::string& before, const std::string& after) {
            auto text = original;
            text.replace(text.find(before), before.size(), after);
            return text;
        };
        for (size_t begin = 0; begin < original.size();) {
            size_t end = original.find('\n', begin) + 1;
            const auto line = original.substr(begin, end - begin);
            rejected(original.substr(0, begin) + original.substr(end));
            rejected(original + line);
            rejected(original.substr(0, begin) + line.substr(0, line.find('=') + 1) + "unsupported\n" + original.substr(end));
            begin = end;
        }
        for (const char* value : {"0", "65", "-1", "2junk", "999999999999"})
            rejected(replace("vload_read_age=1", std::string("vload_read_age=") + value));
        rejected(replace("vstore_first_free_age=35", "vstore_first_free_age=129"));
        rejected(replace("vload_write_age=3", "vload_write_age=1"));
        rejected(replace("vstore_first_free_age=35", "vstore_first_free_age=34"));
        rejected(original + "unknown=1\n");
        rejected(original + "not key value\n");
        MachineModel prior;
        prior.rtlDma = prior.rtlDmaRanges = true;
        prior.mxu1FirstWriteAge = 5;
        prior.mxu0OverwriteAccReadHold = false;
        prior.sourceIrSha256 = std::string(64, 'a');
        auto composed = load(original, prior);
        check(composed.rtlLsu && composed.rtlDmaRanges && composed.rtlDma &&
              composed.mxu1FirstWriteAge == 5 && !composed.mxu0OverwriteAccReadHold,
              "LSU loading discarded selected model fields");
        prior.sourceIrSha256 = std::string(64, 'c');
        bool mismatch = false;
        try { load(original, prior); } catch (const std::runtime_error&) { mismatch = true; }
        check(mismatch, "LSU combined evidence from different hardware IR");
        const auto model = load(original);
        check(model.rtlLsu && !MachineModel{}.rtlLsu, "profile selection did not remain optional");
        auto program = parseAsm("vload m0, 0(x0)\nvstore m0, 8(x0)\necall\n");
        check(printAsm(flatten(scheduled(program, model))) == printAsm(flatten(scheduled(program, {}))),
              "current extracted timing changed existing schedule");
        for (int i = 0; i < 2; ++i) {
            const auto& in = program.instrs[i];
            const auto f = footprintOf(in, zeroRegs(), model);
            check(f.error.empty() && f.doneAge == 34 && hold(f, i ? Unit::VstorePath : Unit::VloadPath).to == 34,
                  "first free age was mistaken for final occupied age");
            check(access(f, i ? Res::MReg : Res::Vmem).age == 1 &&
                  access(f, i ? Res::Vmem : Res::MReg).age == 3,
                  "current read/write timing changed");
            auto unknown = footprintOf(parseAsm(i ? "vstore m0, 0(x1)\n" : "vload m0, 0(x1)\n").instrs[0],
                                       unknownRegs(), model);
            int banks = 0;
            for (const auto& h : unknown.holds) if (h.unit == Unit::VmemBank) ++banks;
            check(unknown.error.empty() && access(unknown, Res::Vmem).anywhere && banks == kVmemBanks,
                  "unknown address lost conservative all-bank exclusion");
            for (uint32_t pointer : {8u, 0x60000u}) {
                auto regs = zeroRegs();
                regs[1] = pointer;
                auto bad = parseAsm(i ? "vstore m0, 0(x1)\n" : "vload m0, 0(x1)\n").instrs[0];
                check(!footprintOf(bad, regs, model).error.empty(), "known illegal address accepted");
            }
        }
        // Synthetic timing exercises the consumer; it is not new hardware evidence.
        const auto changed = load(base +
            "vload_read_age=2\nvload_write_age=5\nvload_first_free_age=38\n"
            "vstore_read_age=2\nvstore_write_age=5\nvstore_first_free_age=38\n");
        const auto loadIn = program.instrs[0], storeIn = program.instrs[1];
        const auto lf = footprintOf(loadIn, zeroRegs(), changed);
        const auto sf = footprintOf(storeIn, zeroRegs(), changed);
        check(access(lf, Res::Vmem).age == 2 && access(lf, Res::MReg).lastAge() == 36 &&
              hold(lf, Unit::VmemBank).from == 2 && hold(lf, Unit::VmemBank).to == 33 &&
              hold(lf, Unit::VloadPath).to == 37 && lf.writeRelease == 37 && lf.doneAge == 37,
              "VLOAD selected access/hold timing not propagated");
        check(access(sf, Res::MReg).age == 2 && access(sf, Res::Vmem).lastAge() == 36 &&
              hold(sf, Unit::VmemBank).from == 5 && hold(sf, Unit::VmemBank).to == 36 &&
              hold(sf, Unit::VstorePath).to == 37 && sf.readRelease == 37,
              "VSTORE selected access/hold timing not propagated");
        check(lf.mregWrites == std::vector<int>{0} && lf.writeDuringRead && sf.mregReads == std::vector<int>{0},
              "inherited logical reservation policy changed");
        const auto graph = buildGraph(program.instrs, zeroRegs(), 0, changed);
        bool edgeFound = false;
        for (const auto& edge : graph.edges) if (edge.from == 0 && edge.to == 1 && edge.distance == 38) edgeFound = true;
        check(edgeFound, "LSU selected lifetime missing from dependency graph");
        auto oldSchedule = scheduled(program, model), newSchedule = scheduled(program, changed);
        check(oldSchedule.blocks[0].issue[1] == 35 && newSchedule.blocks[0].issue[1] == 38,
              "LSU selected lifetime missing from scheduled issue distance");
        SimOptions selected;
        selected.model = changed;
        check(!simulate(flatten(oldSchedule), selected).violations.empty(),
              "selected-model check accepted old insufficient spacing");
        ReservationTable before, after;
        before.reserve(loadIn, footprintOf(loadIn, zeroRegs(), model), 0);
        after.reserve(loadIn, lf, 0);
        auto secondLoad = parseAsm("vload m1, 8(x0)\n").instrs[0];
        check(before.conflict(secondLoad, footprintOf(secondLoad, zeroRegs(), model), 35).empty() &&
              !after.conflict(secondLoad, footprintOf(secondLoad, zeroRegs(), changed), 35).empty() &&
              after.conflict(secondLoad, footprintOf(secondLoad, zeroRegs(), changed), 38).empty(),
              "LSU path hold did not follow selected first-free age");
        auto scalarStore = parseAsm("sw x2, 0(x0)\n").instrs[0];
        auto scalarFootprint = footprintOf(scalarStore, zeroRegs(), changed);
        check(!before.conflict(scalarStore, scalarFootprint, 0).empty() &&
              after.conflict(scalarStore, scalarFootprint, 0).empty() &&
              before.conflict(scalarStore, scalarFootprint, 32).empty() &&
              !after.conflict(scalarStore, scalarFootprint, 32).empty(),
              "VMEM bank reservation did not move with read requests");
        auto pop = parseAsm("vmatpop.fp8.acc.mxu1 m32, acc0, e0\n").instrs[0];
        check(before.conflict(pop, footprintOf(pop, zeroRegs(), model), 34).empty() &&
              !after.conflict(pop, footprintOf(pop, zeroRegs(), changed), 34).empty(),
              "physical MREG write port did not move with destination writes");
        check(footprintOf(scalarStore, zeroRegs(), changed).doneAge == footprintOf(scalarStore, zeroRegs()).doneAge,
              "LSU vector profile changed scalar LSU behavior");
        std::filesystem::remove(path);
        std::cout << "PASS: LSU profile parsing, composition, access timing, reservations, dependencies and scheduling\n";
    } catch (const std::exception& error) {
        std::filesystem::remove(path);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
