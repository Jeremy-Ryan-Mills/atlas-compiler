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

static const Access& access(const Footprint& footprint, Res resource) {
    for (const auto& candidate : footprint.accesses)
        if (candidate.res == resource) return candidate;
    throw std::runtime_error("missing access");
}

static const Hold& hold(const Footprint& footprint, Unit unit) {
    for (const auto& candidate : footprint.holds)
        if (candidate.unit == unit) return candidate;
    throw std::runtime_error("missing hold");
}

static Code schedule(const AsmProgram& program, const MachineModel& model) {
    Code code = buildBlocks(program);
    PassContext context;
    context.model = model;
    runPasses(code, {"strip-artifacts", "schedule"}, context);
    SimOptions options;
    options.model = model;
    const auto result = simulate(flatten(code), options);
    check(result.violations.empty() && result.stopReason.empty(), "selected-model schedule did not validate");
    return code;
}

int main() {
    const auto temporary = std::filesystem::temp_directory_path() /
        ("atlas-profile-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    try {
        const std::filesystem::path root = ATLAS_SOURCE_DIR "/profiles/EE290SimConfig";
        MachineModel model = readExperimentalMxu1Profile((root / "mxu1/atlas-mxu1.profile").string());
        model = readExperimentalMxu0Profile((root / "mxu0/atlas-mxu0.profile").string(), model);
        model = readExperimentalDmaProfile((root / "dma/atlas-dma.profile").string(), model);
        model = readExperimentalLsuProfile((root / "lsu/atlas-lsu.profile").string(), model);
        check(model.sourceIrSha256 == "d2fd900eadda35788ca85a4c0f3ad8058d7ca7c1856af4351b6bd6be6cf1fbe2",
              "checked-in profiles did not compose over one hardware IR");
        check(model.rtlDma && model.rtlDmaRanges && model.rtlLsu &&
              model.mxu1FirstWriteAge == 3 && !model.mxu0OverwriteAccReadHold &&
              !model.mxu1OverwriteAccReadHold && model.vloadFirstFreeAge == 35,
              "checked-in profile settings changed");

        auto mxu = parseAsm("vmatpop.fp8.acc.mxu1 m8, acc0, e0\nvmatmul.mxu1 acc0, m0, w0\n");
        ReservationTable table;
        table.reserve(mxu.instrs[0], footprintOf(mxu.instrs[0], zeroRegs()), 0);
        check(!table.conflict(mxu.instrs[1], footprintOf(mxu.instrs[1], zeroRegs()), 1).empty(),
              "default MXU model changed");
        check(table.conflict(mxu.instrs[1], footprintOf(mxu.instrs[1], zeroRegs(), model), 1).empty(),
              "selected MXU overwrite still reserves an accumulator read");
        auto accumulate = parseAsm("vmatmul.acc.mxu1 acc0, m0, w0\n").instrs[0];
        check(!table.conflict(accumulate, footprintOf(accumulate, zeroRegs(), model), 1).empty(),
              "accumulating multiply lost its accumulator read");

        auto lsu = parseAsm("vload m0, 0(x0)\nvstore m0, 8(x0)\necall\n");
        for (int i = 0; i < 2; ++i) {
            const auto footprint = footprintOf(lsu.instrs[i], zeroRegs(), model);
            check(footprint.doneAge == 34 &&
                  hold(footprint, i ? Unit::VstorePath : Unit::VloadPath).to == 34,
                  "checked-in LSU first-free age was not exclusive");
            check(access(footprint, i ? Res::MReg : Res::Vmem).age == 1 &&
                  access(footprint, i ? Res::Vmem : Res::MReg).age == 3,
                  "checked-in LSU access timing changed");
        }
        auto originalSchedule = schedule(lsu, model);
        check(originalSchedule.blocks[0].issue[1] == 35, "checked-in LSU lifetime missing from schedule");

        const std::string common = "config=EE290SimConfig\nsource_ir_sha256=" + std::string(64, 'a') +
            "\nevidence_sha256=" + std::string(64, 'b') + "\n";
        const std::string lsuBase = "schema=atlas-lsu-profile-v1\n" + common +
            "rows=32\nrow_step=1\noperand_capture=issue\n";
        const std::string changedLsu = lsuBase +
            "vload_read_age=2\nvload_write_age=5\nvload_first_free_age=38\n"
            "vstore_read_age=2\nvstore_write_age=5\nvstore_first_free_age=38\n";
        auto loadLsu = [&](const std::string& text) {
            { std::ofstream file(temporary); file << text; }
            return readExperimentalLsuProfile(temporary.string());
        };
        auto changed = loadLsu(changedLsu);
        const auto loadFootprint = footprintOf(lsu.instrs[0], zeroRegs(), changed);
        check(access(loadFootprint, Res::Vmem).age == 2 && access(loadFootprint, Res::MReg).age == 5 &&
              hold(loadFootprint, Unit::VloadPath).to == 37 && loadFootprint.writeRelease == 37,
              "selected LSU timing did not reach footprints and reservations");
        check(schedule(lsu, changed).blocks[0].issue[1] == 38,
              "selected LSU timing did not reach dependencies and scheduling");
        SimOptions selected;
        selected.model = changed;
        check(!simulate(flatten(originalSchedule), selected).violations.empty(),
              "selected LSU checker accepted old spacing");

        const std::string changedMxu = "schema=atlas-mxu1-profile-v1\n" + common +
            "first_write_age=5\noverwrite_acc_read_hold=0\n";
        { std::ofstream file(temporary); file << changedMxu; }
        auto mxuModel = readExperimentalMxu1Profile(temporary.string());
        auto consumer = parseAsm("vmatmul.mxu1 acc0, m0, w0\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n");
        const auto graph = buildGraph(consumer.instrs, zeroRegs(), 0, mxuModel);
        check(graph.edges.size() == 1 && graph.edges[0].distance == 6 &&
              graph.footprints[0].doneAge == 36,
              "selected MXU latency did not reach footprints and dependencies");
        check(schedule(consumer, mxuModel).blocks[0].issue[1] == 6,
              "selected MXU latency did not reach scheduling");

        auto rejects = [&](const std::string& text, auto loader) {
            { std::ofstream file(temporary); file << text; }
            bool caught = false;
            try { loader(); } catch (const std::runtime_error&) { caught = true; }
            check(caught, "malformed profile accepted");
        };
        rejects(changedMxu + "first_write_age=5\n", [&] { readExperimentalMxu1Profile(temporary.string()); });
        rejects(changedMxu + "unknown=1\n", [&] { readExperimentalMxu1Profile(temporary.string()); });
        rejects(changedLsu.substr(changedLsu.find('\n') + 1), [&] { readExperimentalLsuProfile(temporary.string()); });
        rejects(changedLsu + "vload_read_age=2\n", [&] { readExperimentalLsuProfile(temporary.string()); });
        std::string badAges = changedLsu;
        badAges.replace(badAges.find("vload_write_age=5"), 17, "vload_write_age=2");
        rejects(badAges, [&] { readExperimentalLsuProfile(temporary.string()); });

        const std::string dmaV1 = "schema=atlas-dma-profile-v1\n" + common +
            "operand_capture=issue\nconfig_update=issue\nvmem_word_address_low_bit=3\n"
            "vmem_line_address_bits=16\ntransfer_size_bits=13\nvmem_line_bytes=32\n"
            "vmem_lines=49152\nchannels=8\ncommand_slots=8\ncompletion=explicit-wait\n"
            "lsu_priority_over_dma=1\nsupported_max_transfer_bytes=4096\n";
        { std::ofstream file(temporary); file << dmaV1; }
        auto conservativeDma = readExperimentalDmaProfile(temporary.string());
        check(conservativeDma.rtlDma && !conservativeDma.rtlDmaRanges,
              "DMA v1 gained precise ranges without supporting evidence");
        rejects(dmaV1 + "unknown=1\n", [&] { readExperimentalDmaProfile(temporary.string()); });

        MachineModel other;
        other.sourceIrSha256 = std::string(64, 'f');
        { std::ofstream file(temporary); file << changedMxu; }
        bool mismatch = false;
        try { readExperimentalMxu1Profile(temporary.string(), other); }
        catch (const std::runtime_error&) { mismatch = true; }
        check(mismatch, "profiles from different hardware IR were combined");

        std::filesystem::remove(temporary);
        std::cout << "PASS: checked-in profile composition, parsing, footprints, scheduling and checking\n";
    } catch (const std::exception& error) {
        std::filesystem::remove(temporary);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
