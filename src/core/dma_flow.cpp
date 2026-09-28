#include "core/dma_flow.h"

#include <algorithm>
#include <cstdint>
#include <deque>
#include <map>
#include <stdexcept>
#include <tuple>

#include "core/machine.h"
#include "core/values.h"

void requireKnownSuccessors(const Code& code, const std::string& who) {
    if (code.blocks.empty()) return;
    std::vector<bool> reached(code.blocks.size(), false);
    std::deque<int> work{0};
    reached[0] = true;
    while (!work.empty()) {
        const Block& block = code.blocks[work.front()];
        work.pop_front();
        if (block.unknownSuccs) throw std::runtime_error(who + " requires known control-flow successors");
        for (int successor : block.succs) {
            if (successor < 0 || successor >= (int)code.blocks.size())
                throw std::runtime_error(who + " encountered an invalid control-flow successor");
            if (!reached[successor]) work.push_back(successor), reached[successor] = true;
        }
    }
}

unsigned pendingDmaChannels(const PendingDma& pending) {
    unsigned mask = 0;
    for (int channel = 0; channel < 8; channel++)
        if (!pending[channel].empty()) mask |= 1u << channel;
    return mask;
}

namespace {

constexpr size_t maxContexts = 32;
constexpr int maxPredicates = 32;

bool joinPending(PendingDma& into, const PendingDma& from) {
    bool changed = false;
    for (int channel = 0; channel < 8; channel++) {
        size_t size = into[channel].size();
        into[channel].insert(from[channel].begin(), from[channel].end());
        changed |= size != into[channel].size();
    }
    return changed;
}

struct State {
    uint32_t known = 0, truth = 0;
    PendingDma pending;
};

bool covers(const State& a, const State& b) {
    return (a.known & b.known) == a.known && ((a.truth ^ b.truth) & a.known) == 0;
}

bool joinState(std::vector<State>& into, State state) {
    for (State& old : into)
        if (covers(old, state)) return joinPending(old.pending, state.pending);
    for (auto it = into.begin(); it != into.end();) {
        if (covers(state, *it)) {
            joinPending(state.pending, it->pending);
            it = into.erase(it);
        } else ++it;
    }
    if (into.size() >= maxContexts) {
        // Widen branch facts, retaining all possibly pending commands.
        for (const State& old : into) {
            state.known &= old.known & ~(state.truth ^ old.truth);
            joinPending(state.pending, old.pending);
        }
        state.truth &= state.known;
        into.clear();
    }
    into.push_back(std::move(state));
    return true;
}

struct Branch {
    uint32_t bit = 0;
    bool takenTruth = true;
    int target = -1;
};

struct Step {
    Instr in;
    int command = -1;
    uint32_t kills = 0;
};

}  // namespace

DmaFlow analyzeDmaFlow(const Code& code, const DmaWaitPlan& waits) {
    size_t count = code.blocks.size();
    DmaFlow flow{std::vector<std::vector<PendingDma>>(count), std::vector<bool>(count, false), {}};
    if (count == 0) return flow;
    requireKnownSuccessors(code, "DMA analysis");

    std::map<std::string, int> labels;
    for (size_t b = 0; b < count; b++)
        for (const std::string& label : code.blocks[b].labels) labels[label] = (int)b;
    std::map<std::tuple<int, int, int>, int> predicates;
    std::array<uint32_t, 32> killedBy{};
    std::vector<Branch> branches(count);
    for (size_t b = 0; b < count; b++) {
        const auto& term = code.blocks[b].terminator;
        if (!term || term->op->opClass != OpClass::Branch) continue;
        Branch& branch = branches[b];
        auto target = labels.find(term->target);
        if (target != labels.end()) branch.target = target->second;
        const std::string& name = term->op->name;
        int kind = name == "beq" || name == "bne" ? 0 : name == "blt" || name == "bge" ? 1 :
                   name == "bltu" || name == "bgeu" ? 2 : -1;
        if (kind < 0) continue;  // Unknown comparisons retain both paths.
        int a = term->rs1, c = term->rs2;
        if (kind == 0 && a > c) std::swap(a, c);  // equality is symmetric
        auto key = std::make_tuple(kind, a, c);
        auto it = predicates.find(key);
        if (it == predicates.end() && predicates.size() < maxPredicates)
            it = predicates.emplace(key, (int)predicates.size()).first;
        if (it == predicates.end()) continue;
        branch.bit = uint32_t{1} << it->second;
        branch.takenTruth = name == "beq" || name == "blt" || name == "bltu";
        if (a != 0) killedBy[a] |= branch.bit;
        if (c != 0) killedBy[c] |= branch.bit;
    }

    // Number the DMA commands (pending site IDs) and record each one's footprint.
    std::vector<RegValues> entry = blockEntryValues(code);
    std::vector<std::vector<Step>> steps(count);
    int command = 0;
    for (size_t b = 0; b < count; b++) {
        RegValues regs = entry[b];
        for (const Instr& in : blockInstructions(code.blocks[b])) {
            Step step{in};
            if (in.op->engine == Engine::Dma && in.op->opClass != OpClass::DmaWait) {
                step.command = command++;
                flow.commands.push_back(footprintOf(in, regs));
            }
            for (const Access& access : footprintOf(in, unknownRegs()).accesses)
                if (access.res == Res::XReg && access.write && access.first != 0)
                    step.kills |= killedBy[access.first];
            steps[b].push_back(std::move(step));
            applyScalar(in, regs);
        }
        flow.before[b].resize(steps[b].size() + 1);
    }

    std::vector<std::vector<State>> entries(count);
    entries[0].push_back(State{});
    std::deque<int> work{0};
    std::vector<bool> queued(count, false);
    queued[0] = true;

    auto transfer = [&](int b, State state, bool record) {
        const Block& block = code.blocks[b];
        auto boundary = [&](State& current, size_t i) {
            if (record) joinPending(flow.before[b][i], current.pending);
            if (!waits.empty()) {
                unsigned mask = waits[b][i];
                for (int channel = 0; channel < 8; channel++)
                    if (mask & (1u << channel)) current.pending[channel].clear();
            }
        };
        auto instruction = [&](State& current, size_t i) {
            boundary(current, i);
            const Step& step = steps[b][i];
            const OpInfo& op = *step.in.op;
            if (op.engine == Engine::Dma) {
                if (op.opClass == OpClass::DmaWait) current.pending[op.channel].clear();
                else current.pending[op.channel].insert(step.command);
            }
            current.known &= ~step.kills;
            current.truth &= current.known;
        };
        auto propagate = [&](int successor, const State& next) {
            if (!record && joinState(entries[successor], next) && !queued[successor]) {
                work.push_back(successor);
                queued[successor] = true;
            }
        };
        if (block.terminator && block.terminator->op->opClass == OpClass::Branch) {
            size_t term = block.body.size();
            for (size_t i = 0; i <= term; i++) instruction(state, i);
            const Branch& branch = branches[b];
            for (bool taken : {true, false}) {
                State next = state;
                bool truth = taken == branch.takenTruth;
                if ((next.known & branch.bit) && bool(next.truth & branch.bit) != truth) continue;
                next.known |= branch.bit;
                if (truth) next.truth |= branch.bit;
                else next.truth &= ~branch.bit;
                // Decide the branch before applying slot writes.
                for (size_t i = term + 1; i < steps[b].size(); i++) instruction(next, i);
                boundary(next, steps[b].size());
                int successor = taken ? branch.target : b + 1;
                if (std::find(block.succs.begin(), block.succs.end(), successor) != block.succs.end())
                    propagate(successor, next);
            }
        } else {
            for (size_t i = 0; i < steps[b].size(); i++) instruction(state, i);
            boundary(state, steps[b].size());
            for (int successor : block.succs) propagate(successor, state);
        }
    };

    while (!work.empty()) {
        int b = work.front();
        work.pop_front();
        queued[b] = false;
        // Copy: backedges may update this entry during transfer.
        std::vector<State> states = entries[b];
        for (const State& state : states) transfer(b, state, false);
    }
    for (size_t b = 0; b < count; b++) {
        flow.reached[b] = !entries[b].empty();
        for (const State& state : entries[b]) transfer((int)b, state, true);
    }
    return flow;
}
