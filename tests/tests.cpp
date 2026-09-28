// Small self-contained test runner: each TEST registers a function; CHECK records failures.
// Add tests for a new pass at the bottom, next to the other pass tests.
#include <cstdio>
#include <filesystem>
#include <functional>
#include <string>
#include <vector>

#include "core/depgraph.h"
#include "core/reservations.h"
#include "core/simulator.h"
#include "passes/pass.h"

static std::vector<std::pair<std::string, std::function<void()>>> tests;
static int failures = 0;

#define TEST(name)                                                         \
    static void name();                                                    \
    static bool reg_##name = (tests.push_back({#name, name}), true);       \
    static void name()
#define CHECK(cond)                                                                       \
    do {                                                                                  \
        if (!(cond)) {                                                                    \
            std::printf("  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);                 \
            failures++;                                                                   \
        }                                                                                 \
    } while (0)
#define CHECK_EQ(a, b)                                                                                     \
    do {                                                                                                   \
        auto va = (a);                                                                                     \
        auto vb = (b);                                                                                     \
        if (!(va == vb)) {                                                                                 \
            std::printf("  FAIL %s:%d: %s == %s (%lld vs %lld)\n", __FILE__, __LINE__, #a, #b, (long long)va, \
                        (long long)vb);                                                                    \
            failures++;                                                                                    \
        }                                                                                                  \
    } while (0)

// Issue distance the dependence model requires between the first and second instruction.
static int distanceBetween(const std::string& first, const std::string& second) {
    AsmProgram p = parseAsm(first + "\n" + second + "\n");
    RegValues regs = zeroRegs();
    Footprint fa = footprintOf(p.instrs[0], regs);
    Footprint fb = footprintOf(p.instrs[1], regs);
    return dependence(p.instrs[0], fa, p.instrs[1], fb).distance;
}

static long long simulatedCycles(const std::string& text) { return simulate(parseAsm(text)).cycles; }

// Runs the full pass pipeline (or just `passes`) and returns the flattened result.
static AsmProgram optimize(const AsmProgram& in, const std::vector<std::string>& passes = {}) {
    Code code = buildBlocks(in);
    PassContext ctx;
    runPasses(code, passes, ctx);
    return flatten(code);
}

TEST(parse_and_print_round_trip) {
    std::string text =
        "lui x3, 0x1\n"
        "loop_1:\n"
        "vload m0, 8(x31)   # comment\n"
        "vmatmul.mxu1 acc0, m0, w1\n"
        "vpack.bf16.fp8 m4, m0, e1\n"
        "vmatpop.fp8.acc.mxu0 m2, acc1, e3\n"
        "dma.load.ch2 x4, x1, x7\n"
        "blt x8, x9, loop_1\n"
        "addi x0, x0, 0\n";
    AsmProgram a = parseAsm(text);
    AsmProgram b = parseAsm(printAsm(a));
    CHECK_EQ(a.instrs.size(), b.instrs.size());
    for (size_t i = 0; i < a.instrs.size(); i++) CHECK(formatInstr(a.instrs[i]) == formatInstr(b.instrs[i]));
    CHECK(a.instrs[1].comment == "comment");
    CHECK(a.labels[1].size() == 1 && a.labels[1][0] == "loop_1");
}

TEST(li_expands_like_npu_model) {
    AsmProgram p = parseAsm("li x1, 0x2800\nli x2, 7\n");
    CHECK_EQ(p.instrs.size(), 3u);
    CHECK(formatInstr(p.instrs[0]) == "lui x1, 3");
    CHECK(formatInstr(p.instrs[1]) == "addi x1, x1, 2048");
    RegValues regs = zeroRegs();
    for (const Instr& in : p.instrs) applyScalar(in, regs);
    CHECK_EQ(*regs[1], 0x2800u);
    CHECK_EQ(*regs[2], 7u);
}

TEST(numeric_branch_offsets_become_labels) {
    AsmProgram p = parseAsm("addi x1, x1, 1\nblt x1, x2, -1\naddi x0, x0, 0\n");
    CHECK(!p.instrs[1].target.empty());
    CHECK(!p.labels[0].empty() && p.labels[0][0] == p.instrs[1].target);
}

TEST(dependence_distances_match_rtl_timing) {
    CHECK_EQ(distanceBetween("addi x1, x0, 5", "addi x2, x1, 1"), 1);
    CHECK_EQ(distanceBetween("vload m0, 0(x4)", "vadd.bf16 m4, m0, m2"), 35);      // write reservation until age 34
    CHECK_EQ(distanceBetween("vstore m0, 0(x4)", "vload m0, 8(x4)"), 1);           // vload may write during a read
    CHECK_EQ(distanceBetween("vadd.bf16 m4, m0, m2", "vload m1, 8(x5)"), 30);      // m1 rows are read at ages 32..63
    CHECK_EQ(distanceBetween("vadd.bf16 m4, m0, m2", "vmul.bf16 m6, m4, m2"), 66);
    CHECK_EQ(distanceBetween("vadd.bf16 m4, m0, m2", "vmov m0, m8"), 64);          // sources released at age 63
    CHECK_EQ(distanceBetween("vmatmul.mxu0 acc0, m0, w0", "vmatpop.bf16.acc.mxu0 m2, acc0"), 64);
    CHECK_EQ(distanceBetween("vmatmul.mxu1 acc0, m0, w0", "vmatpop.bf16.acc.mxu1 m2, acc0"), 4);
    CHECK_EQ(distanceBetween("vmatmul.mxu0 acc0, m0, w0", "vmatpush.weight.mxu0 w0, m1"), 63);
    CHECK_EQ(distanceBetween("vmatpush.weight.mxu0 w0, m1", "vmatmul.mxu0 acc0, m0, w0"), 1);
    CHECK_EQ(distanceBetween("vmatpush.weight.mxu1 w0, m1", "vmatmul.mxu1 acc0, m0, w0"), 32);
    CHECK_EQ(distanceBetween("vmatpop.bf16.acc.mxu1 m2, acc0", "vadd.bf16 m4, m2, m6"), 33);
    CHECK_EQ(distanceBetween("vtrpose.xlu m1, m1", "vmatpush.weight.mxu1 w0, m1"), 66);
    CHECK_EQ(distanceBetween("srli x31, x4, 2", "vload m0, 0(x31)"), 1);
    CHECK_EQ(distanceBetween("vload m0, 0(x31)", "srli x31, x5, 2"), 1);          // the address is latched at issue
    CHECK_EQ(distanceBetween("vmov m2, m0", "vmov m6, m4"), 0);                    // independent (the VPU slot rule is separate)
}

TEST(vpu_slots_and_mreg_ports) {
    AsmProgram p = parseAsm("vmov m2, m0\nvmov m6, m4\nvsquare.bf16 m10, m8\nvstore m32, 0(x4)\n");
    RegValues regs = zeroRegs();
    std::vector<Footprint> f;
    for (const Instr& in : p.instrs) f.push_back(footprintOf(in, regs));
    ReservationTable t;
    t.reserve(p.instrs[0], f[0], 0);
    CHECK(!t.conflict(p.instrs[1], f[1], 64).empty());  // same lane logic until the last write
    CHECK(t.conflict(p.instrs[1], f[1], 65).empty());
    CHECK(t.conflict(p.instrs[2], f[2], 1).empty());    // different logic: the second VPU slot
    CHECK(!t.conflict(p.instrs[3], f[3], 2).empty());   // m32 shares m0's read port, which vmov uses until cycle 31
    CHECK(t.conflict(p.instrs[3], f[3], 31).empty());   // vstore reads start at age 1
}

TEST(simulator_matches_npu_model_cycle_counts) {
    // Expected values are npu_model rtl-match's cycle counts for the same programs.
    CHECK_EQ(simulatedCycles("addi x1, x0, 1\n"), 2);
    CHECK_EQ(simulatedCycles("addi x1, x0, 1\naddi x2, x0, 2\n"), 3);
    CHECK_EQ(simulatedCycles("addi x1, x0, 1\ndelay 5\naddi x2, x0, 2\n"), 9);
    CHECK_EQ(simulatedCycles("dma.config.ch0 x0\ndma.wait.ch0\naddi x1, x0, 1\n"), 8);
    CHECK_EQ(simulatedCycles("addi x7, x0, 1024\nlui x4, 0x2\ndma.load.ch0 x4, x0, x7\ndma.wait.ch0\naddi x1, x0, 1\n"), 522);
    CHECK_EQ(simulatedCycles("addi x7, x0, 1024\nlui x4, 0x2\ndma.load.ch0 x4, x0, x7\n"), 519);
    CHECK_EQ(simulatedCycles("addi x7, x0, 1024\nlui x4, 0x2\nlui x5, 0x3\ndma.load.ch0 x4, x0, x7\n"
                             "dma.load.ch1 x5, x0, x7\ndma.wait.ch1\naddi x1, x0, 1\n"), 1039);
    CHECK_EQ(simulatedCycles("lui x4, 0x1\nvload m0, 0(x4)\n"), 37);
    CHECK_EQ(simulatedCycles("vmatmul.mxu0 acc0, m0, w0\n"), 96);
    CHECK_EQ(simulatedCycles("vadd.bf16 m4, m0, m2\n"), 67);
}

TEST(simulator_reports_violations) {
    SimResult r = simulate(parseAsm("vload m0, 0(x4)\nvadd.bf16 m4, m0, m2\n"));
    CHECK(!r.violations.empty());
    r = simulate(parseAsm("vload m0, 0(x4)\ndelay 33\nvadd.bf16 m4, m0, m2\n"));
    CHECK(r.violations.empty());
}

TEST(scheduler_overlaps_independent_engines) {
    std::string text =
        "lui x4, 0x1\n"
        "srli x31, x4, 2\n"
        "vload m0, 0(x31)\n"
        "delay 34\n"
        "vmatmul.mxu0 acc0, m8, w0\n"
        "delay 95\n"
        "vadd.bf16 m4, m0, m2\n"
        "delay 66\n";
    AsmProgram in = parseAsm(text);
    AsmProgram out = optimize(in);
    SimResult before = simulate(in), after = simulate(out);
    CHECK(after.violations.empty());
    CHECK(after.cycles < before.cycles);
}

TEST(schedules_stay_valid_when_dma_is_slower) {
    // A matmul started before a dma.wait still writes acc0 after the wait; the push
    // behind the wait must not be scheduled into the gap before those writes.
    std::string text =
        "addi x7, x0, 64\n"
        "lui x4, 0x2\n"
        "dma.load.ch0 x4, x0, x7\n"
        "vmatmul.mxu0 acc1, m8, w0\n"
        "dma.wait.ch0\n"
        "vmatpush.acc.bf16.mxu0 acc1, m2\n";
    AsmProgram out = optimize(parseAsm(text));
    for (double scale : {1.0, 1.5, 3.0, 10.0}) {
        SimOptions o;
        o.dmaLatencyScale = scale;
        CHECK(simulate(out, o).violations.empty());
    }
}

TEST(delay_slot_motion_preserves_link_dependencies) {
    // Test directly; the full pipeline rejects link addresses.
    for (const std::string slot : {"addi x2, x1, 7", "addi x1, x0, 17"}) {
        Code code = buildBlocks(parseAsm("jal x1, target\n" + slot + "\ntarget:\nsw x1, 0(x0)\n"));
        PassContext ctx;
        stripArtifacts(code, ctx);
        CHECK(code.blocks[0].slot.has_value());
        CHECK(code.blocks[0].body.empty());
    }
    Code independent = buildBlocks(parseAsm("jal x0, target\naddi x2, x0, 17\ntarget:\nsw x2, 0(x0)\n"));
    PassContext ctx;
    stripArtifacts(independent, ctx);
    CHECK(!independent.blocks[0].slot.has_value());
    CHECK_EQ(independent.blocks[0].body.size(), 1u);
}

TEST(unrelocatable_control_flow_is_rejected_before_mutation) {
    for (const std::string source : {
             "delay 10\njalr x0, x1, 0\nnop\n",
             "delay 10\njal x1, target\nnop\ntarget:\naddi x2, x0, 17\n",
             "delay 10\nauipc x1, 0\n",
             "delay 10\nbeq x0, x0, target\nauipc x1, 0\ntarget:\nsw x1, 0(x0)\n",
             "delay 10\nbeq x0, x0, target\ndelay 1 # keep\ntarget:\nsw x1, 0(x0)\n",
             "delay 10\nbeq x0, x0, target\necall\ntarget:\nsw x1, 0(x0)\n",
             "delay 10\nbeq x0, x0, target\nebreak\ntarget:\nsw x1, 0(x0)\n"}) {
        Code code = buildBlocks(parseAsm(source));
        std::string before = printAsm(flatten(code));
        PassContext ctx;
        bool rejected = false;
        try { runPasses(code, {}, ctx); }
        catch (const std::runtime_error&) { rejected = true; }
        CHECK(rejected);
        CHECK(printAsm(flatten(code)) == before);
        CHECK(ctx.log.empty());
    }
}

TEST(artifact_delay_slots_require_the_stripping_pass) {
    const std::string source = "beq x0, x0, target\ndelay 8\ntarget:\naddi x1, x0, 17\n";
    AsmProgram out = optimize(parseAsm(source));
    CHECK(simulate(out).violations.empty());
    Code code = buildBlocks(parseAsm(source));
    PassContext ctx;
    bool rejected = false;
    try { runPasses(code, {"schedule"}, ctx); }
    catch (const std::runtime_error&) { rejected = true; }
    CHECK(rejected);
    CHECK(ctx.log.empty());
}

TEST(dma_wait_reserves_gaps_in_live_port_windows) {
    Instr reduction = makeInstr("vredsum.bf16", 4, 0);
    Instr store = makeInstr("vstore", 32);
    ReservationTable table;
    table.reserve(reduction, footprintOf(reduction, zeroRegs()), 0);
    // Reads occupy ages 0..31 and 64..95; the store fits the gap.
    CHECK(table.conflict(store, footprintOf(store, zeroRegs()), 31).empty());
    table.extendForWait(29);
    CHECK(!table.conflict(store, footprintOf(store, zeroRegs()), 31).empty());
    table.extendForWait(30);
    CHECK(!table.conflict(store, footprintOf(store, zeroRegs()), 31).empty());
    CHECK(table.conflict(store, footprintOf(store, zeroRegs()), 96).empty());
}

TEST(dma_wait_invalidates_exact_row_and_read_sharing_assumptions) {
    Instr vpu = makeInstr("vmov", 2, 0);
    Footprint reader;
    reader.accesses.push_back({Res::MReg, false, 7, 1, 10, 1});
    ReservationTable table;
    table.reserve(vpu, reader, 0);
    CHECK(table.conflict(vpu, reader, 0).empty());  // matching reads can share
    table.extendForWait(5);
    CHECK(!table.conflict(vpu, reader, 0).empty());

    Footprint writer;
    writer.accesses.push_back({Res::MReg, true, 2, 1, 0, 1});
    CHECK(!table.conflict(makeInstr("vload"), writer, 6).empty());
    CHECK(table.conflict(makeInstr("vload"), writer, 11).empty());
}

TEST(repeated_dma_waits_preserve_unit_capacity_counts) {
    Instr compute = makeInstr("vmatmul.mxu0");
    Footprint f;
    f.holds.push_back({Unit::MxuCompute, 0, 10, 20});
    ReservationTable table;
    table.reserve(compute, f, 0);
    table.reserve(compute, f, 0);
    table.extendForWait(5);
    table.extendForWait(5);
    table.extendForWait(6);
    Footprint candidate;
    candidate.holds.push_back({Unit::MxuCompute, 0, 0, 0});
    CHECK(table.conflict(compute, candidate, 6).empty());
    table.reserve(compute, candidate, 6);
    CHECK(!table.conflict(compute, candidate, 6).empty());
}

TEST(robust_schedule_survives_dma_stalls_between_read_bursts) {
    AsmProgram original = parseAsm(
        "vredsum.bf16 m4, m0\ndma.config.ch0 x5\ndelay 26 # keep\n"
        "dma.wait.ch0\ndelay 140\naddi x5, x0, 0\nvstore m32, 0(x5)\n");
    AsmProgram optimized = optimize(original);
    for (double scale : {0.1, 0.5, 1.0, 1.7, 3.0, 10.0, 100.0}) {
        SimOptions options;
        options.dmaLatencyScale = scale;
        CHECK(simulate(original, options).violations.empty());
        SimResult result = simulate(optimized, options);
        CHECK(result.violations.empty());
        CHECK(result.stopReason.empty());
    }
}

TEST(halt_stops_before_delay_stalls_and_does_not_retire) {
    for (const std::string halt : {"ecall", "ebreak"}) {
        SimResult empty = simulate(parseAsm(halt + "\n"));
        CHECK_EQ(empty.cycles, 2);
        CHECK_EQ(empty.issued, 0);
        SimResult unsafe = simulate(parseAsm("lw x1, 0(x0)\ndelay 2\n" + halt + "\n"));
        CHECK_EQ(unsafe.cycles, 4);
        CHECK_EQ(unsafe.issued, 2);
        CHECK(!unsafe.violations.empty());
        AsmProgram safe = optimize(parseAsm("lw x1, 0(x0)\n" + halt + "\n"));
        SimResult result = simulate(safe);
        CHECK_EQ(result.cycles, 6);
        CHECK_EQ(result.issued, 3);
        CHECK(result.violations.empty());
        CHECK(printAsm(optimize(safe)) == printAsm(safe));
    }
    // Completion on the halt tick is safe.
    SimResult sameTick = simulate(parseAsm("sw x1, 0(x0)\necall\n"));
    CHECK_EQ(sameTick.cycles, 3);
    CHECK(sameTick.violations.empty());
    CHECK_EQ(simulatedCycles("lw x1, 0(x0)\n"), 5);
    CHECK_EQ(simulatedCycles("delay 5\n"), 7);
}

TEST(halt_guards_cover_block_boundaries_kept_delays_and_long_waits) {
    for (const std::string halt : {"ecall", "ebreak"}) {
        for (const std::string delay : {"", "delay 100 # keep\n", "delay 4095 # keep\n"}) {
            AsmProgram out = optimize(parseAsm("lw x1, 0(x0)\n" + delay + "exit:\n" + halt + "\n"));
            CHECK(isNop(out.instrs[out.instrs.size() - 2]));
            CHECK(out.labels[out.instrs.size() - 1] == std::vector<std::string>{"exit"});
            CHECK(simulate(out).violations.empty());
            CHECK(printAsm(optimize(out)) == printAsm(out));
        }
    }
    Block b;
    b.body = {makeInstr("lw", 1)};
    b.issue = {0};
    b.terminator = makeInstr("ecall");
    b.terminatorCycle = 9000;
    b.endCycle = 9001;
    b.scheduled = true;
    Code code;
    code.blocks.push_back(b);
    AsmProgram out = flatten(code);
    CHECK_EQ(simulate(out).cycles, 9002);
    CHECK(simulate(out).violations.empty());
    for (const Instr& in : out.instrs)
        if (in.op->opClass == OpClass::Delay) CHECK(in.imm >= 0 && in.imm <= 4095);

    Code labeled = buildBlocks(parseAsm("delay 100 # keep\nempty:\nnop\nexit:\necall\n"));
    PassContext ctx;
    runPasses(labeled, {}, ctx);
    CHECK_EQ(labeled.blocks[0].endCycle, 102);
    CHECK_EQ(simulate(flatten(labeled)).cycles, 104);
}

// Runs strip-artifacts, rename-registers (with `classes`) and schedule.
static AsmProgram renamed(const std::string& text, const std::string& classes) {
    Code code = buildBlocks(parseAsm(text));
    PassContext ctx;
    ctx.renameClasses = classes;
    runPasses(code, {"strip-artifacts", "rename-registers", "schedule"}, ctx);
    return flatten(code);
}

static const Instr* findInstr(const AsmProgram& p, const std::string& name, int nth = 0) {
    for (const Instr& in : p.instrs)
        if (in.op->name == name && nth-- == 0) return &in;
    return nullptr;
}

TEST(rename_breaks_false_dependences_on_vector_registers) {
    // The second vli.all reuses m0 while vexp still reads it.
    std::string text = "vli.all m0, 1\nvexp.bf16 m2, m0\nvli.all m0, 2\nvexp2.bf16 m4, m0\n";
    AsmProgram plain = renamed(text, "none"), out = renamed(text, "m");
    const Instr* first = findInstr(out, "vli.all", 0);
    const Instr* second = findInstr(out, "vli.all", 1);
    CHECK(first && second && first->rd != second->rd);
    CHECK(first && second && first->rd % 2 == 0 && second->rd % 2 == 0);  // BF16 pairs stay even
    CHECK(simulate(out).violations.empty());
    CHECK(simulate(out).cycles < simulate(plain).cycles);
}

TEST(rename_keeps_registers_live_across_blocks) {
    AsmProgram out = renamed(
        "vli.all m4, 0\naddi x1, x0, 0\naddi x2, x0, 2\n"
        "loop:\nvadd.bf16 m6, m4, m4\nvmov m4, m6\naddi x1, x1, 1\nblt x1, x2, loop\nnop\n",
        "all");
    const Instr* add = findInstr(out, "vadd.bf16");
    const Instr* mov = findInstr(out, "vmov");
    CHECK(add && add->rs1 == 4 && add->rs2 == 4);  // loop-carried value read on entry
    CHECK(mov && mov->rd == 4);                    // and written for the next iteration
    CHECK(mov && add && mov->rs1 == add->rd);
    CHECK(simulate(out).violations.empty());
}

TEST(rename_leaves_dma_operand_registers_alone) {
    // DMA reads x1..x3 when the transfer completes, so they are never renamed.
    std::string text =
        "addi x1, x0, 0\naddi x2, x0, 0\naddi x3, x0, 1024\ndma.load.ch0 x1, x2, x3\ndma.wait.ch0\n"
        "addi x1, x0, 1024\nsrli x5, x1, 2\nvload m0, 0(x5)\nsrli x5, x1, 2\nvload m1, 0(x5)\n";
    AsmProgram out = renamed(text, "x");
    const Instr* load = findInstr(out, "dma.load.ch0");
    CHECK(load && load->rd == 1 && load->rs1 == 2 && load->rs2 == 3);
    const Instr* a = findInstr(out, "vload", 0);
    const Instr* b = findInstr(out, "vload", 1);
    CHECK(a && b && a->rs1 != b->rs1);  // the shared x5 temporary is split
    CHECK(simulate(out).violations.empty());
}

TEST(rename_moves_only_push_pop_accumulators_to_the_other_mxu) {
    // acc0 holds a matmul result across two quantizes that share acc1.
    std::string text =
        "vmatpush.weight.mxu0 w0, m8\nvmatmul.mxu0 acc0, m9, w0\n"
        "vmatpush.acc.bf16.mxu0 acc1, m10\nvmatpop.fp8.acc.mxu0 m2, acc1, e0\n"
        "vmatpush.acc.bf16.mxu0 acc1, m12\nvmatpop.fp8.acc.mxu0 m3, acc1, e0\n"
        "vmatpop.bf16.acc.mxu0 m6, acc0\n";
    AsmProgram out = renamed(text, "xmxu");
    // Matmuls round differently per MXU, so the matmul and its pop stay on MXU0.
    const Instr* matmul = findInstr(out, "vmatmul.mxu0");
    const Instr* result = findInstr(out, "vmatpop.bf16.acc.mxu0");
    CHECK(matmul && result && matmul->rd == result->rs2);
    // The second quantize moves to MXU1, and each push is popped from the same accumulator.
    CHECK(findInstr(out, "vmatpush.acc.bf16.mxu1") != nullptr);
    for (int mxu = 0; mxu < 2; mxu++) {
        std::string s = std::to_string(mxu);
        const Instr* push = findInstr(out, "vmatpush.acc.bf16.mxu" + s);
        const Instr* pop = findInstr(out, "vmatpop.fp8.acc.mxu" + s);
        CHECK((push == nullptr) == (pop == nullptr));
        if (push && pop) CHECK_EQ(push->rd, pop->rs2);
    }
    CHECK(simulate(out).violations.empty());
    CHECK(simulate(out).cycles < simulate(renamed(text, "mxu")).cycles);
}

TEST(all_rtl_match_kernels) {
    std::filesystem::path dir = std::filesystem::path(ATLAS_SOURCE_DIR) / "third_party/npu_model/npu_model/configs/programs/asm";
    if (!std::filesystem::exists(dir)) {
        std::printf("  (skipped: run `git submodule update --init` to fetch the kernels)\n");
        return;
    }
    int count = 0;
    for (const auto& entry : std::filesystem::directory_iterator(dir)) {
        if (entry.path().extension() != ".S") continue;
        count++;
        std::string name = entry.path().filename().string();
        try {
            AsmProgram in = readAsmFile(entry.path().string());
            AsmProgram out = optimize(in);
            SimResult before = simulate(in), after = simulate(out);
            SimOptions slow;
            slow.dmaLatencyScale = 2.0;
            SimResult afterSlow = simulate(out, slow);
            bool ok = after.violations.empty() && afterSlow.violations.empty() && after.stopReason.empty() &&
                      after.cycles <= before.cycles;
            if (!ok) {
                std::printf("  FAIL %s: %lld -> %lld cycles\n", name.c_str(), before.cycles, after.cycles);
                for (const auto& v : after.violations) std::printf("    %s\n", v.c_str());
                for (const auto& v : afterSlow.violations) std::printf("    (slow DMA) %s\n", v.c_str());
                failures++;
            }
        } catch (const std::exception& e) {
            std::printf("  FAIL %s: %s\n", name.c_str(), e.what());
            failures++;
        }
    }
    std::printf("  checked %d kernels\n", count);
}

int main() {
    for (auto& [name, fn] : tests) {
        std::printf("%s\n", name.c_str());
        fn();
    }
    std::printf(failures ? "%d FAILURES\n" : "all tests passed\n", failures);
    return failures ? 1 : 0;
}
