#include <stdexcept>

#include "core/dma_flow.h"
#include "passes/pass.h"

namespace {

template <typename Fn>
void visitInstructions(const Block& block, Fn visit) {
    for (const Instr& in : block.body) visit(in);
    if (block.terminator) visit(*block.terminator);
    if (block.slot) visit(*block.slot);
}

void validateReleaseDma(const Code& code, bool checkPending = true) {
    for (const Block& block : code.blocks) {
        visitInstructions(block, [](const Instr& in) {
            if (in.release && in.op->opClass != OpClass::Csr)
                throw std::runtime_error("line " + std::to_string(in.line) + ": atlas.complete is only supported on CSR instructions");
        });
        if (block.slot && block.slot->release)
            throw std::runtime_error("line " + std::to_string(block.slot->line) +
                                     ": atlas.complete in a delay slot is not supported by the optimizer");
    }
    if (!checkPending || code.blocks.empty()) return;

    // Share insertion's path analysis; pending transfers still require waits.
    DmaFlow flow = analyzeDmaFlow(code);
    for (size_t index = 0; index < code.blocks.size(); index++) {
        if (!flow.reached[index]) continue;
        std::vector<Instr> instructions = blockInstructions(code.blocks[index]);
        for (size_t i = 0; i < instructions.size(); i++) {
            const Instr& in = instructions[i];
            unsigned pending = pendingDmaChannels(flow.before[index][i]);
            // A channel flag cannot count overlapping commands; require idle reuse.
            if (in.op->engine == Engine::Dma && in.op->opClass != OpClass::DmaWait &&
                (pending & (1u << in.op->channel))) {
                std::string channel = "ch" + std::to_string(in.op->channel);
                throw std::runtime_error("line " + std::to_string(in.line) +
                                         ": atlas.complete cannot prove DMA channel " + channel +
                                         " idle before " + in.op->name + "; add dma.wait." + channel +
                                         " before channel reuse");
            }
            if (in.release && pending != 0) {
                std::string channels;
                for (int channel = 0; channel < 8; channel++)
                    if (pending & (1u << channel)) {
                        if (!channels.empty()) channels += ", ";
                        channels += "ch" + std::to_string(channel);
                    }
                throw std::runtime_error("line " + std::to_string(in.line) +
                                         ": atlas.complete may publish with pending DMA on " + channels +
                                         "; add matching dma.wait instructions on every reaching path");
            }
        }
    }
}

}  // namespace

const std::vector<Pass>& allPasses() {
    // Add new passes here. `schedule` must stay last: it picks the issue cycles
    // and delays for whatever the earlier passes produced.
    static const std::vector<Pass> passes = {
        {"remove-nops", "drop instructions that have no effect (writes to x0)", removeNops},
        {"insert-dma-waits", "insert DMA waits before dependent accesses, channel reuse, and completion", insertDmaWaits},
        {"fill-delay-slots", "move an independent scalar instruction into each empty branch delay slot", fillDelaySlots},
        {"schedule", "list-schedule every block and choose the delays", schedule},
    };
    return passes;
}

void runPasses(Code& code, const std::vector<std::string>& names, PassContext& ctx) {
    bool insertsDmaWaits = names.empty(), schedules = names.empty(), hasRelease = false;
    int firstReleaseLine = 0;
    for (const std::string& name : names) {
        insertsDmaWaits |= name == "insert-dma-waits";
        schedules |= name == "schedule";
    }
    // The input must be functional assembly, and passes move code, so nothing may
    // depend on an instruction's address.
    for (const Block& block : code.blocks)
        visitInstructions(block, [&](const Instr& in) {
            std::string reason;
            if (in.op->opClass == OpClass::Delay)
                reason = "the input must be functional assembly, without delays (convert hand-scheduled code with scripts/to_functional.py)";
            else if (in.op->name == "auipc")
                reason = "auipc is not supported by the optimizer (PC-relative values cannot be relocated)";
            else if (in.op->name == "jalr")
                reason = "jalr is not supported by the optimizer (indirect targets cannot be relocated)";
            else if (in.op->name == "jal" && in.rd != 0)
                reason = "jal with a nonzero link register is not supported by the optimizer (link values cannot be relocated)";
            if (!reason.empty()) throw std::runtime_error("line " + std::to_string(in.line) + ": " + reason);
            if (in.release && !hasRelease) firstReleaseLine = in.line;
            hasRelease |= in.release;
        });
    if (insertsDmaWaits) requireKnownSuccessors(code, "insert-dma-waits");
    for (const std::string& name : names) {
        bool known = false;
        for (const Pass& p : allPasses()) known |= name == p.name;
        if (!known) throw std::runtime_error("unknown pass '" + name + "'");
    }
    if (insertsDmaWaits && !schedules)
        throw std::runtime_error("insert-dma-waits requires the schedule pass");
    if (hasRelease) {
        if (!schedules)
            throw std::runtime_error("line " + std::to_string(firstReleaseLine) +
                                     ": atlas.complete requires the schedule pass");
        validateReleaseDma(code, !insertsDmaWaits);
    }
    bool waitsReady = !insertsDmaWaits;
    for (const Pass& p : allPasses()) {
        bool selected = names.empty();
        for (const std::string& name : names) selected |= name == p.name;
        if (selected) {
            p.run(code, ctx);
            waitsReady |= std::string(p.name) == "insert-dma-waits";
            if (hasRelease) validateReleaseDma(code, waitsReady);
        }
    }
}
