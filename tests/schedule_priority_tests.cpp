#include <iostream>
#include <stdexcept>

#include "core/simulator.h"
#include "passes/pass.h"

static void check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

static Code optimized(const std::string& source, PassContext ctx = {}) {
    Code code = buildBlocks(parseAsm(source));
    runPasses(code, {"strip-artifacts", "schedule"}, ctx);
    SimOptions options;
    options.model = ctx.model;
    SimResult result = simulate(flatten(code), options);
    check(result.violations.empty() && result.stopReason.empty(), "priority produced an illegal model schedule");
    return code;
}

int main() {
    try {
        PassContext input, critical;
        input.schedulePriority = SchedulePriority::Input;
        critical.schedulePriority = SchedulePriority::Critical;
        const std::string scalar = "addi x1, x0, 1\naddi x2, x0, 2\nadd x3, x2, x2\n";
        auto before = optimized(scalar);
        check(printAsm(flatten(before)) == printAsm(flatten(optimized(scalar, critical))),
              "default no longer matches explicit critical priority");
        check(before.blocks[0].body[0].rd == 2, "critical priority should start the longer dependency path");
        auto ordered = optimized(scalar, input);
        check(ordered.blocks[0].body[0].rd == 1 && ordered.blocks[0].body[1].rd == 2 &&
              ordered.blocks[0].body[2].rd == 3, "input priority did not preserve ready input order");

        auto skipped = optimized("vmatmul.mxu1 acc0, m0, w0\n"
                                 "vmatpop.fp8.acc.mxu1 m8, acc0, e0\n"
                                 "addi x1, x0, 1\n", input);
        check(skipped.blocks[0].body[1].op->name == "addi" && skipped.blocks[0].issue[1] == 1,
              "input priority waited instead of issuing independent ready work");
        check(skipped.blocks[0].issue[2] >= 4, "input priority bypassed a data dependency");

        auto ports = optimized("vmatpush.weight.mxu1 w0, m4\n"
                               "vmatpush.weight.mxu1 w1, m6\n", input);
        check(ports.blocks[0].issue[1] >= 32, "input priority bypassed shared-resource exclusion");

        // Measured-body operations from the existing 64x64x64 MXU1 witness.
        // All four combinations must be legal in their selected model. The
        // comparison separates model precision from list-scheduling priority.
        const std::string kernel =
            "vmatpush.weight.mxu1 w0, m4\n"
            "vmatmul.mxu1 acc0, m0, w0\n"
            "vmatpush.weight.mxu1 w1, m6\n"
            "vmatmul.mxu1 acc1, m2, w0\n"
            "vmatmul.acc.mxu1 acc0, m1, w1\n"
            "vmatpush.weight.mxu1 w0, m5\n"
            "vmatmul.acc.mxu1 acc1, m3, w1\n"
            "vmatpop.fp8.acc.mxu1 m8, acc0, e0\n"
            "vmatmul.mxu1 acc0, m0, w0\n"
            "vmatpush.weight.mxu1 w1, m7\n"
            "vmatpop.fp8.acc.mxu1 m10, acc1, e0\n"
            "vmatmul.mxu1 acc1, m2, w0\n"
            "vmatmul.acc.mxu1 acc0, m1, w1\n"
            "vmatmul.acc.mxu1 acc1, m3, w1\n"
            "vmatpop.fp8.acc.mxu1 m9, acc0, e0\n"
            "vmatpop.fp8.acc.mxu1 m11, acc1, e0\n";
        PassContext profileCritical = critical, profileInput = input;
        profileCritical.model.mxu1OverwriteAccReadHold = false;
        profileInput.model = profileCritical.model;
        int defaultCritical = optimized(kernel, critical).blocks[0].endCycle + 1;
        int defaultInput = optimized(kernel, input).blocks[0].endCycle + 1;
        int derivedCritical = optimized(kernel, profileCritical).blocks[0].endCycle + 1;
        auto orderedProfile = optimized(kernel, profileInput);
        int derivedInput = orderedProfile.blocks[0].endCycle + 1;
        check(defaultCritical == 294 && defaultInput == 291 && derivedCritical == 294 && derivedInput == 291,
              "unexpected K64 dispatch-window model/priority comparison");
        check(printAsm(flatten(optimized(kernel, input))) != printAsm(flatten(orderedProfile)),
              "models should choose different input-priority schedules despite equal window length");
        auto originalOrder = parseAsm(kernel);
        for (size_t i = 0; i < originalOrder.instrs.size(); i++)
            check(formatInstr(orderedProfile.blocks[0].body[i]) == formatInstr(originalOrder.instrs[i]),
                  "partial profile plus input priority should recover original K64 operation order");
        check(printAsm(flatten(optimized(kernel))) == printAsm(flatten(optimized(kernel, critical))),
              "default K64 schedule changed");
        std::cout << "PASS: default isolation, ready-work ordering, dependencies, resources, and K64 model/priority comparison\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
