#include <algorithm>
#include <stdexcept>

#include "core/depgraph.h"
#include "core/dma_flow.h"
#include "passes/pass.h"

namespace {

using Pending = PendingDma;

struct Step {
    Instr in;
    Footprint footprint;
    size_t boundary;
};

struct Plan {
    std::vector<Step> steps;
    std::vector<unsigned> waits;
};

void clearChannels(Pending& pending, unsigned mask) {
    for (int channel = 0; channel < 8; channel++)
        if (mask & (1u << channel)) pending[channel].clear();
}

DmaFlow pendingBefore(const Code& code, const std::vector<Plan>& plans) {
    DmaWaitPlan waits(code.blocks.size());
    for (size_t b = 0; b < code.blocks.size(); b++) {
        waits[b].resize(blockInstructions(code.blocks[b]).size() + 1, 0);
        std::copy(plans[b].waits.begin(), plans[b].waits.end(), waits[b].begin());
    }
    return analyzeDmaFlow(code, waits);
}

unsigned requiredWaits(const Pending& pending, const Step& step, const std::vector<Footprint>& commands) {
    const OpInfo& op = *step.in.op;
    if (step.in.release || op.opClass == OpClass::Halt) return pendingDmaChannels(pending);
    unsigned waits = 0;
    for (int channel = 0; channel < 8; channel++) {
        if (pending[channel].empty()) continue;
        if (op.engine == Engine::Dma && op.opClass != OpClass::DmaWait && op.channel == channel) {
            waits |= 1u << channel;
            continue;
        }
        for (int command : pending[channel]) {
            EdgeKind kind;
            if (conflictsAtCompletion(commands[command], step.footprint, kind)) {
                waits |= 1u << channel;
                break;
            }
        }
    }
    return waits;
}

void validateFlow(const Code& code) {
    requireKnownSuccessors(code, "insert-dma-waits");
    for (const Block& block : code.blocks) {
        if (block.slot && block.slot->op->engine == Engine::Dma && block.slot->op->opClass != OpClass::DmaWait)
            throw std::runtime_error("line " + std::to_string(block.slot->line) +
                                     ": insert-dma-waits requires strip-artifacts to move DMA commands out of delay slots");
    }
}

// Add a CFG exit for end labels and final fallthrough.
void addExit(Code& code) {
    int exit = (int)code.blocks.size();
    for (int index = 0; index < exit; index++) {
        Block& block = code.blocks[index];
        if (block.terminator && isControlFlow(*block.terminator->op)) {
            const std::string& target = block.terminator->target;
            if (std::find(code.endLabels.begin(), code.endLabels.end(), target) != code.endLabels.end())
                block.succs.push_back(exit);
        }
        if (index + 1 == exit && (!block.terminator || block.terminator->op->opClass == OpClass::Branch))
            block.succs.push_back(exit);
    }
    Block end;
    end.labels = code.endLabels;
    code.endLabels.clear();
    code.blocks.push_back(std::move(end));
}

}  // namespace

void insertDmaWaits(Code& code, PassContext& ctx) {
    validateFlow(code);
    if (code.blocks.empty()) {
        ctx.log.push_back("insert-dma-waits: inserted 0 waits");
        return;
    }
    Code candidate = code;
    addExit(candidate);
    size_t exit = candidate.blocks.size() - 1;
    std::vector<RegValues> entries = blockEntryValues(candidate);
    std::vector<Plan> plans(candidate.blocks.size());
    for (size_t index = 0; index < candidate.blocks.size(); index++) {
        const Block& block = candidate.blocks[index];
        Plan& plan = plans[index];
        plan.waits.resize(block.body.size() + 1, 0);
        RegValues regs = entries[index];
        auto addStep = [&](const Instr& in, size_t boundary) {
            plan.steps.push_back({in, footprintOf(in, regs), boundary});
            applyScalar(in, regs);
        };
        for (size_t i = 0; i < block.body.size(); i++) addStep(block.body[i], i);
        if (block.terminator) addStep(*block.terminator, block.body.size());
        if (block.slot) addStep(*block.slot, block.body.size());
    }

    // Recompute pending DMA with planned waits until no more waits are needed.
    DmaFlow flow;
    bool changed;
    do {
        flow = pendingBefore(candidate, plans);
        changed = false;
        for (size_t index = 0; index < plans.size(); index++) {
            if (!flow.reached[index]) continue;
            Plan& plan = plans[index];
            for (size_t i = 0; i < plan.steps.size(); i++) {
                const Step& step = plan.steps[i];
                Pending pending = flow.before[index][i];
                unsigned& waits = plan.waits[step.boundary];
                clearChannels(pending, waits);
                unsigned needed = requiredWaits(pending, step, flow.commands);
                changed |= (needed & ~waits) != 0;
                waits |= needed;
            }
            if (index == exit) {
                unsigned needed = pendingDmaChannels(flow.before[index].back());
                changed |= (needed & ~plan.waits.back()) != 0;
                plan.waits.back() |= needed;
            }
        }
    } while (changed);

    int inserted = 0;
    for (size_t index = 0; index < candidate.blocks.size(); index++) {
        if (!flow.reached[index]) continue;
        Block& block = candidate.blocks[index];
        std::vector<Instr> body;
        size_t current = 0;
        auto emitWaits = [&](size_t boundary) {
            unsigned needed = plans[index].waits[boundary] & pendingDmaChannels(flow.before[index][boundary]);
            for (int channel = 0; channel < 8; channel++) {
                if (!(needed & (1u << channel))) continue;
                body.push_back(makeInstr("dma.wait.ch" + std::to_string(channel)));
                inserted++;
            }
        };
        for (const Step& step : plans[index].steps) {
            if (current <= step.boundary) {
                emitWaits(step.boundary);
                current = step.boundary + 1;
            }
            if (step.boundary < block.body.size()) body.push_back(step.in);
        }
        if (current <= block.body.size()) emitWaits(block.body.size());
        block.body = std::move(body);
        block.scheduled = false;
        block.issue.clear();
    }

    Block& final = candidate.blocks.back();
    Block& previous = candidate.blocks[exit - 1];
    bool merge = final.labels.empty() && !previous.terminator;
    if (final.body.empty() || merge) {
        if (merge) previous.body.insert(previous.body.end(), final.body.begin(), final.body.end());
        candidate.endLabels = final.labels;
        candidate.blocks.pop_back();
        for (Block& block : candidate.blocks)
            std::erase(block.succs, (int)exit);
    }
    if (inserted != 0) code = std::move(candidate);
    ctx.log.push_back("insert-dma-waits: inserted " + std::to_string(inserted) + " waits");
}
