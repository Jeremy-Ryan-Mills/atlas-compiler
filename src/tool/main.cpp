#include <cstdio>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>

#include "core/simulator.h"
#include "passes/pass.h"
#include "tool/viewer.h"

static void usage() {
    std::cerr << "usage: atlas-opt [options] input.S\n"
                 "Turns functional assembly (no delays, no branch delay slots) into executable assembly.\n"
                 "  -o FILE            write the executable assembly to FILE (default: stdout)\n"
                 "  --viz FILE.html    write the dependency graph viewer (input order vs. schedule)\n"
                 "  --passes a,b,c     run only these passes (default: all; see --list-passes)\n"
                 "  --list-passes      list the passes in the order they run\n"
                 "  --dma-timing MODE  robust (default): valid for any DMA latency;\n"
                 "                     model: trust npu_model's DMA latency\n"
                 "  -q                 print nothing unless something is wrong\n";
}

static bool writeFile(const std::string& path, const std::string& text) {
    std::ofstream f(path);
    if (!f) std::cerr << "atlas-opt: cannot write " << path << "\n";
    f << text;
    return (bool)f;
}

static void printProblems(const char* what, const SimResult& r) {
    for (const std::string& v : r.violations) std::cerr << "  " << what << ": " << v << "\n";
    if (!r.stopReason.empty()) std::cerr << "  " << what << ": simulation stopped: " << r.stopReason << "\n";
}

int main(int argc, char** argv) {
    std::string input, output, vizPath;
    std::vector<std::string> passNames;
    PassContext ctx;
    bool quiet = false;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        bool hasValue = i + 1 < argc;
        if (a == "-o" && hasValue) output = argv[++i];
        else if (a == "--viz" && hasValue) vizPath = argv[++i];
        else if (a == "--passes" && hasValue) {
            std::stringstream list(argv[++i]);
            for (std::string name; std::getline(list, name, ',');) passNames.push_back(name);
        } else if (a == "--dma-timing" && hasValue) ctx.robustDma = std::string(argv[++i]) != "model";
        else if (a == "--list-passes") {
            for (const Pass& p : allPasses()) std::cout << p.name << "\t" << p.description << "\n";
            return 0;
        } else if (a == "-q") quiet = true;
        else if (a[0] != '-' && input.empty()) input = a;
        else {
            usage();
            return 2;
        }
    }
    if (input.empty()) {
        usage();
        return 2;
    }

    try {
        AsmProgram functional = readAsmFile(input);
        Code code = buildBlocks(functional);
        runPasses(code, passNames, ctx);
        AsmProgram executable = flatten(code);

        // Check the result, and (for robust schedules) that it still holds when DMA is slower than modeled.
        SimResult result = simulate(executable);
        SimOptions slowDma;
        slowDma.dmaLatencyScale = 1.7;
        SimResult slow = ctx.robustDma ? simulate(executable, slowDma) : result;

        if (!output.empty()) {
            if (!writeFile(output, printAsm(executable))) return 1;
        } else if (vizPath.empty()) {
            std::cout << printAsm(executable);
        }
        if (!vizPath.empty() && !writeFile(vizPath, renderHtml(buildProgramView(input, functional, code, result))))
            return 1;

        bool bad = !result.violations.empty() || !result.stopReason.empty() || !slow.violations.empty();
        if (!quiet || bad) {
            for (const std::string& line : ctx.log) std::cerr << "  " << line << "\n";
            char buf[512];
            std::snprintf(buf, sizeof buf, "%s: %zu instructions -> %lld cycles (%lld issued, %lld of them delays)\n",
                          input.c_str(), functional.instrs.size(), result.cycles, result.issued, result.delays);
            std::cerr << buf;
        }
        printProblems("output", result);
        if (ctx.robustDma) printProblems("output with slower DMA", slow);
        return bad ? 1 : 0;
    } catch (const std::exception& e) {
        std::cerr << "atlas-opt: " << e.what() << "\n";
        return 1;
    }
}
