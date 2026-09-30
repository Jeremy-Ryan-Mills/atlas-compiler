#include "passes/pass.h"

#include <algorithm>
#include <stdexcept>

#include "core/dma_flow.h"
#include "core/values.h"

void insertDmaWaits(Code& code, PassContext& ctx) {
    if (code.blocks.empty()) {
        ctx.log.push_back("insert-dma-waits: inserted 0 waits");
        return;
    }
    if (ctx.model.rtlDma && !ctx.robustDma)
        throw std::runtime_error("RTL DMA wait insertion requires robust variable-wait reservations");
    Code candidate = withDmaExit(code);
    const auto entries = blockEntryValues(candidate, ctx.model.rtlDmaRanges);
    std::vector<std::vector<Footprint>> footprints(candidate.blocks.size());
    DmaWaitPlan waits(candidate.blocks.size());
    for (size_t b = 0; b < candidate.blocks.size(); ++b) {
        const Block& block = candidate.blocks[b];
        if (block.slot && block.slot->op->engine == Engine::Dma)
            throw std::runtime_error("DMA commands in branch delay slots are not supported");
        RegValues regs = entries[b];
        for (const Instr& in : blockInstructions(block)) {
            footprints[b].push_back(footprintOf(in, regs, ctx.model));
            if (!footprints[b].back().error.empty())
                throw std::runtime_error("line " + std::to_string(in.line) + ": " + footprints[b].back().error);
            applyScalar(in, regs);
        }
        waits[b].resize(footprints[b].size() + 1);
    }

    // Wait masks only grow. Finite command sites and saturated ring ages make
    // each CFG analysis terminate, including loops and divergent predecessors.
    DmaFlow flow;
    bool changed;
    do {
        flow = analyzeDmaFlow(candidate, ctx.model, waits);
        changed = false;
        for (size_t b = 0; b < candidate.blocks.size(); ++b) {
            if (!flow.reached[b]) continue;
            const Block& block = candidate.blocks[b];
            const auto instructions = blockInstructions(block);
            for (size_t i = 0; i < instructions.size(); ++i) {
                // A slot executes after the branch, but insertion must happen
                // before the branch so the native delay-slot layout is preserved.
                const size_t boundary = std::min(i, block.body.size());
                PendingDma pending = flow.before[b][i];
                clearDmaChannels(pending, waits[b][boundary]);
                const unsigned needed = requiredDmaWaits(pending, instructions[i], footprints[b][i], flow, ctx.model);
                changed |= (needed & ~waits[b][boundary]) != 0;
                waits[b][boundary] |= needed;
            }
            if (b + 1 == candidate.blocks.size()) {
                const unsigned needed = pendingDmaChannels(flow.before[b].back());
                changed |= (needed & ~waits[b].back()) != 0;
                waits[b].back() |= needed;
            }
        }
    } while (changed);

    int inserted = 0;
    for (size_t b = 0; b < candidate.blocks.size(); ++b) {
        if (!flow.reached[b]) continue;
        Block& block = candidate.blocks[b];
        std::vector<Instr> body;
        for (size_t i = 0; i <= block.body.size(); ++i) {
            const unsigned needed = waits[b][i] & pendingDmaChannels(flow.before[b][i]);
            for (int channel = 0; channel < 8; ++channel)
                if (needed & (1u << channel)) {
                    Instr wait = makeInstr("dma.wait.ch" + std::to_string(channel));
                    wait.line = i < block.body.size() ? block.body[i].line : block.terminator ? block.terminator->line : 0;
                    body.push_back(std::move(wait));
                    ++inserted;
                }
            if (i < block.body.size()) body.push_back(block.body[i]);
        }
        block.body = std::move(body);
        block.scheduled = false;
        block.issue.clear();
    }

    // The synthetic falloff block is only emitted when it contains a wait.
    const int exit = (int)candidate.blocks.size() - 1;
    Block& final = candidate.blocks.back();
    Block& previous = candidate.blocks[exit - 1];
    const bool merge = final.labels.empty() && !previous.terminator;
    if (final.body.empty() || merge) {
        if (merge) previous.body.insert(previous.body.end(), final.body.begin(), final.body.end());
        candidate.endLabels = final.labels;
        candidate.blocks.pop_back();
        for (Block& block : candidate.blocks) std::erase(block.succs, exit);
    }
    validateDmaFlow(candidate, ctx.model);
    if (inserted) code = std::move(candidate);
    ctx.log.push_back("insert-dma-waits: inserted " + std::to_string(inserted) + " waits");
}
