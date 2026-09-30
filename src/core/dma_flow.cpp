#include "core/dma_flow.h"

#include <algorithm>
#include <deque>
#include <stdexcept>

#include "core/depgraph.h"
#include "core/values.h"

bool isDmaCommand(const Instr& in, const MachineModel& model) {
    return in.op->engine == Engine::Dma && in.op->opClass != OpClass::DmaWait &&
           !(model.rtlDma && in.op->opClass == OpClass::DmaConfig);
}

unsigned pendingDmaChannels(const PendingDma& pending) {
    unsigned mask = 0;
    for (int channel = 0; channel < 8; ++channel)
        if (!pending[channel].empty()) mask |= 1u << channel;
    return mask;
}

void clearDmaChannels(PendingDma& pending, unsigned mask) {
    for (int channel = 0; channel < 8; ++channel)
        if (mask & (1u << channel)) pending[channel].clear();
}

Code withDmaExit(const Code& code) {
    Code result = code;
    const int exit = (int)result.blocks.size();
    for (int index = 0; index < exit; ++index) {
        Block& block = result.blocks[index];
        if (block.terminator && isControlFlow(*block.terminator->op) &&
            std::find(code.endLabels.begin(), code.endLabels.end(), block.terminator->target) != code.endLabels.end())
            block.succs.push_back(exit);
        if (index + 1 == exit && (!block.terminator || block.terminator->op->opClass == OpClass::Branch))
            block.succs.push_back(exit);
    }
    Block end;
    end.labels = result.endLabels;
    result.endLabels.clear();
    result.blocks.push_back(std::move(end));
    return result;
}

namespace {

bool joinPending(PendingDma& into, const PendingDma& from) {
    bool changed = false;
    for (int channel = 0; channel < 8; ++channel)
        for (auto [site, age] : from[channel]) {
            auto [it, inserted] = into[channel].emplace(site, age);
            if (inserted || age > it->second) changed = true;
            it->second = std::max(it->second, age);
        }
    return changed;
}

}  // namespace

DmaFlow analyzeDmaFlow(const Code& code, const MachineModel& model, const DmaWaitPlan& waits) {
    const size_t count = code.blocks.size();
    DmaFlow flow{std::vector<std::vector<PendingDma>>(count), std::vector<bool>(count, false), {}};
    if (!count) return flow;
    if (!waits.empty() && waits.size() != count) throw std::runtime_error("invalid DMA wait plan");
    const auto entry = blockEntryValues(code, model.rtlDmaRanges);
    std::vector<std::vector<int>> sites(count);
    for (size_t b = 0; b < count; ++b) {
        RegValues regs = entry[b];
        const auto instructions = blockInstructions(code.blocks[b]);
        if (!waits.empty() && waits[b].size() != instructions.size() + 1)
            throw std::runtime_error("invalid DMA wait-plan boundary count");
        for (const Instr& in : instructions) {
            int site = -1;
            if (isDmaCommand(in, model)) {
                site = (int)flow.commands.size();
                flow.commands.push_back(footprintOf(in, regs, model));
            }
            sites[b].push_back(site);
            applyScalar(in, regs);
        }
        flow.before[b].resize(instructions.size() + 1);
    }

    std::vector<PendingDma> incoming(count);
    std::vector<bool> queued(count, false);
    std::deque<int> work{0};
    flow.reached[0] = queued[0] = true;
    while (!work.empty()) {
        const int b = work.front();
        work.pop_front();
        queued[b] = false;
        const Block& block = code.blocks[b];
        if (block.unknownSuccs) throw std::runtime_error("DMA analysis requires known control-flow successors");
        PendingDma pending = incoming[b];
        const auto instructions = blockInstructions(block);
        for (size_t i = 0; i <= instructions.size(); ++i) {
            flow.before[b][i] = pending;
            if (!waits.empty()) clearDmaChannels(pending, waits[b][i]);
            if (i == instructions.size()) break;
            const Instr& in = instructions[i];
            if (in.op->opClass == OpClass::DmaWait) clearDmaChannels(pending, 1u << in.op->channel);
            else if (sites[b][i] >= 0) {
                if (model.rtlDma)
                    for (auto& channel : pending)
                        for (auto& [site, age] : channel) age = std::min(8u, age + 1);
                pending[in.op->channel][sites[b][i]] = 0;
            }
        }
        for (int successor : block.succs) {
            if (successor < 0 || successor >= (int)count)
                throw std::runtime_error("DMA analysis encountered an invalid control-flow successor");
            const bool changed = joinPending(incoming[successor], pending);
            if (!flow.reached[successor] || changed) {
                flow.reached[successor] = true;
                if (!queued[successor]) work.push_back(successor), queued[successor] = true;
            }
        }
    }
    return flow;
}

unsigned requiredDmaWaits(const PendingDma& pending, const Instr& in, const Footprint& footprint,
                          const DmaFlow& flow, const MachineModel& model) {
    if (in.release || in.op->opClass == OpClass::Halt) return pendingDmaChannels(pending);
    unsigned mask = 0;
    for (int channel = 0; channel < 8; ++channel) {
        if (pending[channel].empty()) continue;
        if (isDmaCommand(in, model) && in.op->channel == channel) mask |= 1u << channel;
        for (auto [site, age] : pending[channel]) {
            EdgeKind kind;
            if ((model.rtlDma && isDmaCommand(in, model) && age >= 7) ||
                conflictsAtCompletion(flow.commands[site], footprint, kind, model))
                mask |= 1u << channel;
        }
    }
    return mask;
}

void validateDmaFlow(const Code& code, const MachineModel& model) {
    const Code candidate = withDmaExit(code);
    const DmaFlow flow = analyzeDmaFlow(candidate, model);
    const auto entries = blockEntryValues(candidate, model.rtlDmaRanges);
    for (size_t b = 0; b < candidate.blocks.size(); ++b) {
        if (!flow.reached[b]) continue;
        const Block& block = candidate.blocks[b];
        if (block.slot && block.slot->op->engine == Engine::Dma)
            throw std::runtime_error("DMA commands in branch delay slots are not supported");
        RegValues regs = entries[b];
        const auto instructions = blockInstructions(block);
        for (size_t i = 0; i < instructions.size(); ++i) {
            const Instr& in = instructions[i];
            if (requiredDmaWaits(flow.before[b][i], in, footprintOf(in, regs, model), flow, model))
                throw std::runtime_error("line " + std::to_string(in.line) + " (" + formatInstr(in) +
                    "): pending DMA requires a matching wait before a conflict, channel/ring reuse, or completion");
            applyScalar(in, regs);
        }
        if (b + 1 == candidate.blocks.size() && pendingDmaChannels(flow.before[b].back()))
            throw std::runtime_error("pending DMA requires a matching wait before program exit");
    }
}
