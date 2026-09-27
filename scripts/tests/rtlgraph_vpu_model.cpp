// Probe the linked compiler's policy; the Python join supplies source mappings.
#include "core/asm.h"
#include "core/machine.h"

#include <iostream>
#include <set>
#include <string>
#include <vector>

static const char* vpuClass(OpClass c) {
    switch (c) {
    case OpClass::VpuElementwise: return "elementwise";
    case OpClass::VpuPack: return "pack";
    case OpClass::VpuUnpack: return "unpack";
    case OpClass::VpuRowReduce: return "row_reduce";
    case OpClass::VpuColReduce: return "column_reduce";
    case OpClass::VpuLoadImmPair: return "immediate_pair";
    case OpClass::VpuLoadImmSingle: return "immediate_single";
    default: return nullptr;
    }
}

int main(int argc, char** argv) {
    std::vector<const OpInfo*> ops;
    std::set<std::string> seen;
    if (argc < 2) return 2;
    for (int i = 1; i < argc; ++i) {
        std::string name = argv[i];
        // Only these characters are emitted in JSON; no unescaped user strings.
        if (name.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789._") != std::string::npos)
            return 2;
        const OpInfo* op = findOp(name);
        if (!op || op->name != name || op->engine != Engine::Vpu || !vpuClass(op->opClass)
            || !seen.insert(name).second) return 2;
        ops.push_back(op);
    }
    std::cout << "{\"schema_version\":1,\"kind\":\"atlas-vpu-compiler-probe\",\"operations\":[";
    for (size_t i = 0; i < ops.size(); ++i) {
        if (i) std::cout << ',';
        std::cout << "{\"name\":\"" << ops[i]->name << "\",\"class\":\""
                  << vpuClass(ops[i]->opClass) << "\",\"both_slots\":"
                  << (vpuUsesBothSlots(*ops[i]) ? "true" : "false") << '}';
    }
    std::cout << "],\"can_overlap\":[";
    for (size_t i = 0; i < ops.size(); ++i) {
        if (i) std::cout << ',';
        std::cout << '[';
        for (size_t j = 0; j < ops.size(); ++j) {
            if (j) std::cout << ',';
            std::cout << (vpuCanOverlap(*ops[i], *ops[j]) ? "true" : "false");
        }
        std::cout << ']';
    }
    std::cout << "]}\n";
}
