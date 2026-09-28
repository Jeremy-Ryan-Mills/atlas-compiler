// schedule: list-schedules every block on a cycle-by-cycle reservation table and
// records the issue cycles; flatten() later turns idle cycles into `delay`s.
//
// The list scheduler is a decoder: given a priority for every instruction, it builds
// one legal schedule. A strategy (`--scheduler`) decides which priorities to try and
// keeps the schedule with the lowest expected block time, dma.wait stalls included.
#include <algorithm>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <numeric>
#include <random>
#include <stdexcept>

#include "core/depgraph.h"
#include "core/reservations.h"
#include "passes/pass.h"

namespace {

std::runtime_error scheduleError(const Instr& in, const std::string& why) {
    return std::runtime_error("line " + std::to_string(in.line) + " (" + formatInstr(in) + "): " + why);
}

// Everything about one block that stays fixed while candidate schedules are built.
struct Problem {
    std::vector<Instr> nodes;
    DepGraph g;
    int nb = 0, n = 0, term = -1, slot = -1;
    std::vector<int> height;
    bool robustDma = true, lastBlock = false, fallthroughHalt = false;
    bool criticalWaits = false;  // a wait whose transfer should be done may beat lower-priority work
};

struct Schedule {
    std::vector<int> issue;  // issue cycle of each body instruction
    int terminatorCycle = -1, endCycle = 0;
    long long cost = LLONG_MAX;  // block time including dma.wait stalls at npu_model's DMA speed
};

// When a ready dma.wait issues if no other instruction fits the current cycle.
enum class WaitPolicy {
    Deferred,  // once nothing else is pending, or its transfer is expected to be done
    Eager,     // right away
    Lazy,      // only once nothing else is pending
};

struct Priorities {
    std::vector<double> value;  // larger issues first; ties keep program order
    WaitPolicy waits = WaitPolicy::Deferred;
};

// A list schedule under construction. Copyable, so a search can try a choice and
// finish the schedule greedily without disturbing the original.
class ListState {
public:
    explicit ListState(const Problem& p) : p_(&p), earliest_(p.n, 0), waitingPreds_(p.n, 0), issue_(p.n, -1) {
        for (const Edge& e : p.g.edges) waitingPreds_[e.to]++;
    }

    bool done() const { return placed_ == p_->nb; }
    int cycle() const { return cycle_; }

    // Advances to the first cycle where the priorities issue something and returns it.
    // `options` (if given) receives every legal choice at that cycle, the returned one
    // first: the other instructions that fit, then a ready dma.wait.
    int next(const Priorities& pr, std::vector<int>* options = nullptr) {
        const Problem& p = *p_;
        const std::vector<double>& prio = pr.value;
        for (;;) {
            int best = -1, bestWait = -1;
            bool otherWork = false;
            for (int i = 0; i < p.nb; i++) {
                if (issue_[i] >= 0 || waitingPreds_[i] > 0) continue;
                bool isWait = p.nodes[i].op->opClass == OpClass::DmaWait;
                if (!isWait) otherWork = true;
                if (earliest_[i] > cycle_) continue;
                if (isWait) {
                    if (bestWait < 0 || release(i) < release(bestWait)) bestWait = i;
                    continue;
                }
                if (best >= 0 && prio[i] <= prio[best]) continue;  // ties keep program order
                if (!table_.conflict(p.nodes[i], p.g.footprints[i], cycle_).empty()) continue;
                best = i;
            }
            // A dma.wait stalls the frontend until its transfer is done, so other work goes first.
            bool waitAllowed = bestWait >= 0 && (pr.waits == WaitPolicy::Eager  ? true
                                                 : pr.waits == WaitPolicy::Lazy ? !otherWork
                                                                                : !otherWork || cycle_ >= release(bestWait));
            // With criticalWaits, a wait that gates more critical work than `best` goes first
            // once its transfer (started in this block) is expected to be done.
            bool critical = p.criticalWaits && waitAllowed && best >= 0 && release(bestWait) > 0 &&
                            cycle_ >= release(bestWait) && prio[bestWait] > prio[best];
            if ((best < 0 || critical) && waitAllowed) {
                // Idle cycles before a wait overlap the transfer; idle cycles after it do not.
                // So issue the wait just before its most critical waiting instruction can go.
                int firstUse = INT_MAX, critical = -1;
                for (int e : p.g.out[bestWait]) {
                    int s = p.g.edges[e].to;
                    if (s >= p.nb || waitingPreds_[s] != 1) continue;
                    int c = std::max(cycle_ + 1, earliest_[s]);
                    while (!table_.conflict(p.nodes[s], p.g.footprints[s], c).empty()) c++;
                    if (critical < 0 || p.height[s] > p.height[critical] ||
                        (p.height[s] == p.height[critical] && c < firstUse))
                        critical = s, firstUse = c;
                }
                if (firstUse == INT_MAX || firstUse - 1 <= cycle_) best = bestWait;
            }
            if (best < 0) {
                cycle_++;
                if (cycle_ - lastPlaced_ > 100000) throw std::runtime_error("scheduler made no progress (internal error)");
                continue;
            }
            if (options) {
                options->assign(1, best);
                if (best != bestWait) {
                    std::vector<int> fit;
                    for (int i = 0; i < p.nb; i++) {
                        if (i == best || issue_[i] >= 0 || waitingPreds_[i] > 0 || earliest_[i] > cycle_) continue;
                        if (p.nodes[i].op->opClass == OpClass::DmaWait) continue;
                        if (table_.conflict(p.nodes[i], p.g.footprints[i], cycle_).empty()) fit.push_back(i);
                    }
                    std::stable_sort(fit.begin(), fit.end(), [&](int a, int b) { return prio[a] > prio[b]; });
                    options->insert(options->end(), fit.begin(), fit.end());
                    if (bestWait >= 0) options->push_back(bestWait);
                }
            }
            return best;
        }
    }

    void place(int best) {
        const Problem& p = *p_;
        issue_[best] = cycle_;
        table_.reserve(p.nodes[best], p.g.footprints[best], cycle_);
        if (p.robustDma && p.nodes[best].op->opClass == OpClass::DmaWait) table_.extendForWait(cycle_);
        if (p.g.footprints[best].dmaCycles > 0) {  // transfers run one at a time, in issue order
            int latency = p.g.footprints[best].dmaCycles;
            dmaQueueEnd_ = std::max(cycle_ + latency - 1, dmaQueueEnd_ + latency);
            channelRelease_[p.nodes[best].op->channel] = dmaQueueEnd_ + 2;
        }
        for (int e : p.g.out[best]) {
            const Edge& ed = p.g.edges[e];
            earliest_[ed.to] = std::max(earliest_[ed.to], cycle_ + ed.distance);
            waitingPreds_[ed.to]--;
        }
        nextFree_ = cycle_ + naturalGap(p.nodes[best]);
        lastPlaced_ = cycle_;
        lastBody_ = best;
        cycle_ = nextFree_;
        placed_++;
    }

    // Places the terminator and delay slot and works out when the next block may start.
    Schedule finish() const {
        const Problem& p = *p_;
        const std::vector<Instr>& nodes = p.nodes;
        Schedule s;
        s.issue.assign(issue_.begin(), issue_.begin() + p.nb);

        // Everything started in this block must finish before the next block starts.
        int drain = 0;
        for (int i = 0; i < p.nb; i++) drain = std::max(drain, issue_[i] + p.g.footprints[i].doneAge + 1);
        // Kept delays need an extra guard cycle before halt, even across blocks.
        bool keptDelay = lastBody_ >= 0 && nodes[lastBody_].op->opClass == OpClass::Delay && nodes[lastBody_].keep;

        if (p.term >= 0) {
            bool halt = nodes[p.term].op->opClass == OpClass::Halt;
            // With a delay slot, the next block starts two cycles after the branch.
            int t = std::max({nextFree_, earliest_[p.term], halt ? drain : drain - 2});
            if (halt && keptDelay) t = std::max(t, nextFree_ + 1);
            if (p.slot >= 0) t = std::max(t, earliest_[p.slot] - 1);
            while (!table_.conflict(nodes[p.term], p.g.footprints[p.term], t).empty() ||
                   (p.slot >= 0 && !table_.conflict(nodes[p.slot], p.g.footprints[p.slot], t + 1).empty()))
                t++;
            s.terminatorCycle = t;
            s.endCycle = p.slot >= 0 ? t + 2 : t + 1;
        } else {
            s.endCycle = p.lastBlock ? nextFree_ : std::max(nextFree_, drain);
            if (p.fallthroughHalt && keptDelay) s.endCycle = std::max(s.endCycle, nextFree_ + 1);
        }
        return s;
    }

private:
    int release(int wait) const { return channelRelease_[p_->nodes[wait].op->channel]; }

    const Problem* p_;
    ReservationTable table_;
    std::vector<int> earliest_, waitingPreds_, issue_;
    // Expected DMA timing (npu_model's estimate), used only to decide when to issue a
    // dma.wait. Correctness never depends on it: after a wait the schedule assumes nothing.
    int channelRelease_[8] = {0, 0, 0, 0, 0, 0, 0, 0};
    int dmaQueueEnd_ = 0;
    int cycle_ = 0, placed_ = 0, nextFree_ = 0, lastPlaced_ = 0, lastBody_ = -1;
};

// Block time when DMA runs at npu_model's speed: the scheduled length plus the cycles
// each dma.wait holds the frontend. The block is assumed to start with an idle DMA queue.
long long expectedCost(const Problem& p, const Schedule& s) {
    std::vector<int> order(p.nb);
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return s.issue[a] < s.issue[b]; });
    long long shift = 0, queueEnd = 0;
    long long complete[8] = {-1, -1, -1, -1, -1, -1, -1, -1};
    for (int i : order) {
        const OpInfo& op = *p.nodes[i].op;
        long long t = s.issue[i] + shift;
        if (op.opClass == OpClass::DmaWait) {
            if (complete[op.channel] >= 0 && t < complete[op.channel] + 2) shift += complete[op.channel] + 2 - t;
        } else if (p.g.footprints[i].dmaCycles > 0) {
            int latency = p.g.footprints[i].dmaCycles;
            queueEnd = std::max(t + latency - 1, queueEnd + latency);
            complete[op.channel] = queueEnd;
        }
    }
    return s.endCycle + shift;
}

Schedule decode(const Problem& p, const Priorities& pr) {
    ListState s(p);
    while (!s.done()) s.place(s.next(pr));
    Schedule out = s.finish();
    out.cost = expectedCost(p, out);
    return out;
}

void keepBetter(Schedule& best, Schedule candidate) {
    if (candidate.cost < best.cost) best = std::move(candidate);
}

// Priority functions ------------------------------------------------------------------

Priorities byHeight(const Problem& p) {
    return {std::vector<double>(p.height.begin(), p.height.end()), WaitPolicy::Deferred};
}

// Number of instructions that (transitively) depend on each instruction.
std::vector<int> descendants(const Problem& p) {
    std::vector<std::vector<bool>> reach(p.n, std::vector<bool>(p.n, false));
    std::vector<int> count(p.n, 0);
    for (int i = p.n - 1; i >= 0; i--) {
        for (int e : p.g.out[i]) {
            int s = p.g.edges[e].to;
            reach[i][s] = true;
            for (int k = 0; k < p.n; k++)
                if (reach[s][k]) reach[i][k] = true;
        }
        count[i] = (int)std::count(reach[i].begin(), reach[i].end(), true);
    }
    return count;
}

// Earliest issue cycle of each instruction if only dependences constrained it.
std::vector<int> asap(const Problem& p) {
    std::vector<int> est(p.n, 0);
    for (int i = 0; i < p.n; i++)
        for (int e : p.g.out[i]) {
            const Edge& ed = p.g.edges[e];
            est[ed.to] = std::max(est[ed.to], est[i] + ed.distance);
        }
    return est;
}

// A handful of classic list-scheduling priority functions, each with every wait policy.
std::vector<Priorities> portfolioPriorities(const Problem& p) {
    std::vector<int> desc = descendants(p), est = asap(p);
    int maxHeight = 1;
    for (int h : p.height) maxHeight = std::max(maxHeight, h);
    std::vector<std::vector<double>> functions;
    functions.push_back(std::vector<double>(p.height.begin(), p.height.end()));  // critical path
    std::vector<double> v(p.n);
    for (int i = 0; i < p.n; i++) v[i] = p.height[i] + desc[i] / (double)(p.n + 1);  // ... then most dependents
    functions.push_back(v);
    for (int i = 0; i < p.n; i++) v[i] = desc[i] + p.height[i] / (double)(maxHeight + 1);  // most dependents
    functions.push_back(v);
    for (int i = 0; i < p.n; i++) v[i] = -est[i] + p.height[i] / (double)(maxHeight + 1);  // earliest start
    functions.push_back(v);
    int critical = 0;
    for (int i = 0; i < p.n; i++) critical = std::max(critical, est[i] + p.height[i]);
    for (int i = 0; i < p.n; i++) v[i] = -(critical - p.height[i] - est[i]) + p.height[i] / (double)(maxHeight + 1);  // least slack
    functions.push_back(v);
    for (int i = 0; i < p.n; i++) v[i] = -i;  // program order
    functions.push_back(v);
    for (int i = 0; i < p.n; i++) v[i] = p.height[i] + 4.0 * p.g.out[i].size();  // critical path + fan-out
    functions.push_back(v);

    std::vector<Priorities> out;
    for (WaitPolicy w : {WaitPolicy::Deferred, WaitPolicy::Eager, WaitPolicy::Lazy})
        for (const std::vector<double>& f : functions) out.push_back({f, w});
    return out;
}

// Strategies --------------------------------------------------------------------------

struct Options {
    std::string strategy = "list";
    int effort = 0;  // strategy-specific: iterations, or rollout width
    unsigned seed = 1;
};

Schedule portfolio(const Problem& p) {
    Schedule best;
    for (const Priorities& pr : portfolioPriorities(p)) keepBetter(best, decode(p, pr));
    return best;
}

// GRASP: the portfolio, then the critical-path priorities with random noise, keeping the best.
Schedule grasp(const Problem& p, int iterations, unsigned seed) {
    Schedule best = portfolio(p);
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> unit(0.0, 1.0);
    const double noise[] = {0.02, 0.05, 0.1, 0.2, 0.4};
    const WaitPolicy waits[] = {WaitPolicy::Deferred, WaitPolicy::Eager, WaitPolicy::Lazy};
    for (int it = 0; it < iterations; it++) {
        double a = noise[rng() % 5];
        Priorities pr = byHeight(p);
        pr.waits = waits[rng() % 3];
        for (double& v : pr.value) v *= 1.0 + a * (2.0 * unit(rng) - 1.0);
        keepBetter(best, decode(p, pr));
    }
    return best;
}

// Simulated annealing over the priority vector ("random keys"), decoded by the list scheduler.
Schedule anneal(const Problem& p, int iterations, unsigned seed) {
    Schedule best = portfolio(p);
    std::mt19937 rng(seed);
    std::uniform_real_distribution<double> unit(0.0, 1.0);
    Priorities current = byHeight(p);
    long long currentCost = decode(p, current).cost;
    double t0 = std::max(1.0, currentCost * 0.005), t = t0;
    double cooling = std::pow(0.01, 1.0 / std::max(1, iterations));
    for (int it = 0; it < iterations; it++, t *= cooling) {
        Priorities trial = current;
        int moves = 1 + (int)(rng() % 3);
        for (int m = 0; m < moves; m++) {
            int kind = (int)(rng() % 10);
            if (kind < 5) {  // swap two instructions' priorities
                int a = (int)(rng() % p.nb), b = (int)(rng() % p.nb);
                std::swap(trial.value[a], trial.value[b]);
            } else if (kind < 9) {  // nudge one instruction
                int a = (int)(rng() % p.nb);
                trial.value[a] *= 1.0 + 0.5 * (2.0 * unit(rng) - 1.0);
            } else {
                trial.waits = (WaitPolicy)(rng() % 3);
            }
        }
        Schedule s = decode(p, trial);
        long long delta = s.cost - currentCost;
        if (delta <= 0 || unit(rng) < std::exp(-delta / t)) current = trial, currentCost = s.cost;
        keepBetter(best, std::move(s));
    }
    return best;
}

// Rollout (pilot method): at every choice, try the `width` best options, finish each
// schedule greedily, and commit to the option whose finished schedule is cheapest.
Schedule rollout(const Problem& p, int width, const Priorities& base) {
    Schedule best = decode(p, base);
    ListState s(p);
    std::vector<int> options;
    while (!s.done()) {
        int pick = s.next(base, &options);
        if (options.size() > 1) {
            long long pickCost = LLONG_MAX;
            for (int k = 0; k < std::min<int>(width, (int)options.size()); k++) {
                ListState trial = s;
                trial.place(options[k]);
                while (!trial.done()) trial.place(trial.next(base));
                Schedule c = trial.finish();
                c.cost = expectedCost(p, c);
                if (c.cost < pickCost) pickCost = c.cost, pick = options[k];
                keepBetter(best, std::move(c));
            }
        }
        s.place(pick);
    }
    Schedule last = s.finish();
    last.cost = expectedCost(p, last);
    keepBetter(best, std::move(last));
    return best;
}

Schedule rolloutBest(const Problem& p, int width) {
    // Seed the rollout with the best portfolio priorities.
    Priorities base = byHeight(p);
    long long baseCost = LLONG_MAX;
    for (const Priorities& pr : portfolioPriorities(p)) {
        long long c = decode(p, pr).cost;
        if (c < baseCost) baseCost = c, base = pr;
    }
    return rollout(p, width, base);
}

Schedule solve(const Problem& p, const Options& o) {
    if (o.strategy == "list" || p.nb == 0) return decode(p, byHeight(p));
    if (o.strategy == "portfolio") return portfolio(p);
    if (o.strategy == "grasp") return grasp(p, o.effort > 0 ? o.effort : 300, o.seed);
    if (o.strategy == "anneal") return anneal(p, o.effort > 0 ? o.effort : 2000, o.seed);
    if (o.strategy == "rollout") return rolloutBest(p, o.effort > 0 ? o.effort : 4);
    throw std::runtime_error("unknown scheduler '" + o.strategy + "'");
}

// criticalHeights, but transfers run one at a time in queue order: a DMA command can
// only complete after every earlier one, so the queue-order edge counts the earlier
// transfer's time. That puts the work feeding the first transfers ahead of everything.
std::vector<int> dmaQueueHeights(const DepGraph& g) {
    int n = (int)g.nodes.size();
    std::vector<int> height(n, 0);
    auto transfer = [&](int i) {
        const OpInfo& op = *g.nodes[i].op;
        return op.engine == Engine::Dma && op.opClass != OpClass::DmaWait;
    };
    for (int i = n - 1; i >= 0; i--) {
        height[i] = g.footprints[i].doneAge + 1;
        for (int e : g.out[i]) {
            const Edge& ed = g.edges[e];
            int d = ed.distance;
            const Instr& to = g.nodes[ed.to];
            if (to.op->opClass == OpClass::DmaWait && to.op->channel == g.nodes[i].op->channel)
                d = std::max(d, g.footprints[i].dmaCycles + 2);
            if (transfer(i) && transfer(ed.to)) d = std::max(d, g.footprints[i].dmaCycles);
            height[i] = std::max(height[i], d + height[ed.to]);
        }
    }
    return height;
}

// Critical path, and the DMA queue: transfers run one at a time in issue order.
long long lowerBound(const Problem& p) {
    long long lb = 0, queue = 0;
    for (int i = 0; i < p.nb; i++) {
        lb = std::max<long long>(lb, p.height[i]);
        if (p.g.footprints[i].dmaCycles == 0) continue;
        queue += p.g.footprints[i].dmaCycles;
        for (int k = i + 1; k < p.nb; k++)
            if (p.nodes[k].op->opClass == OpClass::DmaWait && p.nodes[k].op->channel == p.nodes[i].op->channel) {
                lb = std::max(lb, queue + 1 + p.height[k]);
                break;
            }
    }
    return lb;
}

// Reorders and times one block. The block may assume an idle machine on entry and
// drains (everything it started finishes) before its successors begin.
void scheduleBlock(Block& block, const RegValues& entry, bool robustDma, bool lastBlock, bool fallthroughHalt,
                   uint32_t dmaRegs, const Options& opt, int blockIndex, bool criticalWaits, bool dmaAwareHeights) {
    Problem p;
    p.nodes = blockInstructions(block);
    p.nb = (int)block.body.size();
    p.n = (int)p.nodes.size();
    p.term = block.terminator ? p.nb : -1;
    p.slot = hasDelaySlot(block) ? p.nb + 1 : -1;
    p.robustDma = robustDma;
    p.lastBlock = lastBlock;
    p.fallthroughHalt = fallthroughHalt;
    p.criticalWaits = criticalWaits;

    p.g = buildGraph(p.nodes, entry, dmaRegs);
    for (int i = 0; i < p.n; i++) {
        if (!p.g.footprints[i].error.empty()) throw scheduleError(p.nodes[i], p.g.footprints[i].error);
        std::string alone = ReservationTable().conflict(p.nodes[i], p.g.footprints[i], 0);
        if (!alone.empty()) throw scheduleError(p.nodes[i], "can never issue: " + alone);
    }
    if (p.slot >= 0 && p.g.footprints[p.slot].doneAge > 0)
        throw scheduleError(p.nodes[p.slot], "only single-cycle scalar instructions are supported in a delay slot");
    p.height = dmaAwareHeights ? dmaQueueHeights(p.g) : criticalHeights(p.g);

    auto start = std::chrono::steady_clock::now();
    Schedule s = solve(p, opt);
    if (std::getenv("ATLAS_SCHED_DEBUG") && p.nb > 0) {
        double ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - start).count();
        std::fprintf(stderr, "  block %d: %d instrs, list %lld, %s %lld, bound %lld (%.1f ms)\n", blockIndex, p.nb,
                     decode(p, byHeight(p)).cost, opt.strategy.c_str(), s.cost, lowerBound(p), ms);
    }

    std::vector<int> order(p.nb);
    std::iota(order.begin(), order.end(), 0);
    std::stable_sort(order.begin(), order.end(), [&](int a, int b) { return s.issue[a] < s.issue[b]; });
    block.body.clear();
    block.issue.clear();
    for (int i : order) {
        block.body.push_back(p.nodes[i]);
        block.issue.push_back(s.issue[i]);
    }
    block.terminatorCycle = s.terminatorCycle;
    block.endCycle = s.endCycle;
    block.scheduled = true;
}

// "name" or "name:effort".
Options parseOptions(const std::string& spec) {
    Options o;
    size_t colon = spec.find(':');
    o.strategy = spec.substr(0, colon);
    if (colon != std::string::npos) o.effort = std::stoi(spec.substr(colon + 1));
    return o;
}

}  // namespace

void schedule(Code& code, PassContext& ctx) {
    Options opt = parseOptions(ctx.scheduler);
    std::vector<RegValues> entry = blockEntryValues(code);
    uint32_t dmaRegs = dmaOperandRegisters(flatten(code).instrs);
    for (size_t bi = 0; bi < code.blocks.size(); bi++) {
        try {
            bool lastBlock = bi + 1 == code.blocks.size();
            size_t next = bi + 1;
            // Stripping can leave empty labeled blocks.
            while (next < code.blocks.size() && code.blocks[next].body.empty() && !code.blocks[next].terminator) next++;
            bool fallthroughHalt = next < code.blocks.size() && code.blocks[next].body.empty() &&
                                   code.blocks[next].terminator && code.blocks[next].terminator->op->opClass == OpClass::Halt;
            scheduleBlock(code.blocks[bi], entry[bi], ctx.robustDma, lastBlock, fallthroughHalt, dmaRegs, opt, (int)bi,
                          ctx.criticalWaits, ctx.dmaQueueHeights);
        } catch (const std::runtime_error& e) {
            throw std::runtime_error("block " + std::to_string(bi) + ": " + e.what());
        }
    }
    ctx.log.push_back("schedule: " + opt.strategy + "-scheduled " + std::to_string(code.blocks.size()) + " blocks (" +
                      (ctx.robustDma ? "robust" : "npu_model") + " DMA timing)");
}
