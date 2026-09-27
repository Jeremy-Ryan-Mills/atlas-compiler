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
        std::filesystem::remove(path);
        std::cout << "PASS: partial profile parsing, default isolation, resource use, graph, scheduler and simulator\n";
    } catch (const std::exception& error) {
        std::filesystem::remove(path);
        std::cerr << error.what() << '\n';
        return 1;
    }
}
