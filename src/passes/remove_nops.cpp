// remove-nops: drops instructions that have no effect (scalar ALU ops writing x0).
#include "passes/pass.h"

void removeNops(Code& code, PassContext& ctx) {
    int removed = 0;
    for (Block& b : code.blocks) {
        std::vector<Instr> kept;
        for (const Instr& in : b.body) {
            if (isNop(in)) removed++;
            else kept.push_back(in);
        }
        b.body = kept;
    }
    ctx.log.push_back("remove-nops: removed " + std::to_string(removed) + " no-op instructions");
}
