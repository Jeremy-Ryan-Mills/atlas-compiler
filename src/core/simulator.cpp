#include "core/simulator.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <deque>
#include <map>

#include "core/machine.h"
#include "core/reservations.h"
#include "core/values.h"

namespace {

struct InFlight {
    Instr in;
    Footprint f;
    long long issue;
};

struct QueuedDma {
    Instr in;
    Footprint f;
    long long issue, complete;  // RTL mode uses complete only as a cost estimate
    int slot = -1;             // RTL enqueue ring slot; reusable after explicit wait
};

std::string where(const Instr& in) { return "line " + std::to_string(in.line) + " (" + formatInstr(in) + ")"; }

const Access* dmaVmem(const Footprint& f) {
    for (const Access& a : f.accesses)
        if (a.res == Res::Vmem && a.atCompletion) return &a;
    return nullptr;
}

// First and last cycle at which access `a` (of an instruction issued at `issue`)
// touches an element that `range` also covers. Returns false if they don't overlap.
bool overlapCycles(const Access& a, long long issue, const Access& range, long long& first, long long& last) {
    if (a.res != range.res) return false;
    if (a.anywhere || range.anywhere) {
        first = issue + a.age, last = issue + a.lastAge();
        return true;
    }
    long long lo = std::max(a.first, range.first), hi = std::min(a.first + a.count, range.first + range.count) - 1;
    if (lo > hi) return false;
    first = issue + a.age + (lo - a.first) * a.step;
    last = issue + a.age + (hi - a.first) * a.step;
    return true;
}

bool evaluateBranch(const Instr& in, const RegValues& regs, bool& taken) {
    if (!regs[in.rs1] || !regs[in.rs2]) return false;
    uint32_t a = *regs[in.rs1], b = *regs[in.rs2];
    const std::string& n = in.op->name;
    if (n == "beq") taken = a == b;
    else if (n == "bne") taken = a != b;
    else if (n == "blt") taken = (int32_t)a < (int32_t)b;
    else if (n == "bge") taken = (int32_t)a >= (int32_t)b;
    else if (n == "bltu") taken = a < b;
    else taken = a >= b;  // bgeu
    return true;
}

}  // namespace

SimResult simulate(const AsmProgram& prog, const SimOptions& opt) {
    SimResult r;
    int n = (int)prog.instrs.size();
    std::map<std::string, int> labelIndex;
    for (int i = 0; i < (int)prog.labels.size(); i++)
        for (const std::string& l : prog.labels[i]) labelIndex[l] = i;

    RegValues regs = zeroRegs();
    std::deque<InFlight> active;
    std::vector<QueuedDma> dma;
    std::array<long long, 8> channelComplete;
    channelComplete.fill(-1);
    long long lastDmaComplete = 0;
    long long dmaLaunches = 0;
    ReservationTable table;

    auto violation = [&](const std::string& text) {
        if ((int)r.violations.size() < opt.maxViolations) r.violations.push_back(text);
    };

    // Validate metadata independently, including untaken slots.
    for (int i = 0; i < n; i++) {
        const Instr& in = prog.instrs[i];
        if (!in.release) continue;
        if (in.op->opClass != OpClass::Csr) {
            violation(where(in) + ": atlas.release is only valid on a CSR instruction");
            r.stopReason = "invalid release annotation";
            return r;
        }
        if (i > 0 && isControlFlow(*prog.instrs[i - 1].op)) {
            violation(where(in) + ": atlas.release in a delay slot is not supported");
            r.stopReason = "unsupported release delay slot";
            return r;
        }
    }

    long long t = 2;  // npu_model: word 0 is fetched in cycle 1 and issues in cycle 2
    // Correctness uses the shortest possible dispatch spacing. A guessed DMA
    // stall must not retire fixed work or establish a safe operand distance.
    long long minimumT = t;
    long long lastIssue = 0, end = 0;
    int pc = 0, redirect = -1;
    bool inSlot = false;

    while (pc >= 0 && pc < n) {
        const Instr& in = prog.instrs[pc];
        const OpInfo& op = *in.op;
        if (op.opClass == OpClass::DmaWait && channelComplete[op.channel] >= 0)
            t = std::max(t, channelComplete[op.channel] + 2);  // the busy flag clears the tick after completion
        if (t > opt.maxCycles) {
            r.stopReason = "cycle limit reached";
            break;
        }
        if (inSlot && isControlFlow(op)) {
            violation(where(in) + ": branch or jump in a delay slot");
            r.stopReason = "illegal instruction";
            break;
        }
        const long long hazardT = opt.model.rtlDma ? minimumT : t;
        if (opt.model.rtlDma && op.opClass == OpClass::DmaWait) {
            std::erase_if(dma, [&](const QueuedDma& d) { return d.in.op->channel == op.channel; });
            channelComplete[op.channel] = -1;
        }

        // Check live work before dispatch; same-tick completion is too late.
        if (in.release) {
            for (const InFlight& a : active)
                if (a.issue + a.f.doneAge >= hazardT)
                    violation(where(in) + ": atlas.release publishes before " + where(a.in) +
                              " completes (cycle " + std::to_string(a.issue + a.f.doneAge) + ")");
            for (const QueuedDma& d : dma)
                if (opt.model.rtlDma || d.complete >= t)
                    violation(where(in) + ": atlas.release publishes before " + where(d.in) +
                              (opt.model.rtlDma ? " has an explicit completion wait" :
                               " completes (cycle " + std::to_string(d.complete) + ")"));
        }

        // Halt neither retires nor drains; only completions through this tick count.
        if (op.opClass == OpClass::Halt) {
            for (const InFlight& a : active)
                if (a.issue + a.f.doneAge > hazardT)
                    violation(where(in) + ": halts before " + where(a.in) + " completes (cycle " +
                              std::to_string(a.issue + a.f.doneAge) + ")");
            for (const QueuedDma& d : dma)
                if (opt.model.rtlDma || d.complete > t)
                    violation(where(in) + ": halts before " + where(d.in) + " completes (cycle " +
                              (opt.model.rtlDma ? "unknown; missing explicit DMA.WAIT)" : std::to_string(d.complete) + ")"));
            r.cycles = t;
            return r;
        }

        Footprint f = footprintOf(in, regs, opt.model);
        if (!f.error.empty()) violation(where(in) + ": " + f.error);

        // Forget work that can no longer constrain anything.
        std::erase_if(active, [&](const InFlight& a) { return hazardT - a.issue > a.f.doneAge + 1; });
        if (!opt.model.rtlDma) std::erase_if(dma, [&](const QueuedDma& d) { return d.complete < t; });
        if (r.issued % 256 == 0) table.forgetBefore((int)std::max(0LL, hazardT - 2));

        for (const InFlight& a : active) {
            Dependence d = dependence(a.in, a.f, in, f, opt.model);
            if (d.distance > 0 && hazardT - a.issue < d.distance)
                violation(where(in) + " issued " + std::to_string(hazardT - a.issue) + " cycles after " + where(a.in) +
                          ", needs " + std::to_string(d.distance) + " (" + d.reason + ")");
        }

        // RTL memory lifetimes extend from launch through the explicit wait.
        // The inherited model instead places accesses at estimated completion.
        for (const QueuedDma& d : dma) {
            for (const Access& x : d.f.accesses) {
                if (!x.atCompletion || x.res == Res::DmaBase) continue;
                for (const Access& y : f.accesses) {
                    if ((!opt.model.rtlDma && y.atCompletion) || (!x.write && !y.write)) continue;
                    long long first, last;
                    if (!overlapCycles(y, hazardT, x, first, last)) continue;
                    if (opt.model.rtlDma) {
                        violation(where(in) + " conflicts with memory held by " + where(d.in) +
                                  " until its explicit DMA.WAIT");
                        continue;
                    }
                    // Scalar writes happen before the DMA reads in the same cycle; LSU accesses after.
                    bool early = x.res == Res::XReg ? first <= d.complete : first < d.complete;
                    if (early)
                        violation(where(in) + " touches data of " + where(d.in) + " before that transfer completes (cycle " +
                                  std::to_string(d.complete) + ")");
                }
            }
        }

        bool isDmaCommand = op.engine == Engine::Dma && op.opClass != OpClass::DmaWait &&
                            (!opt.model.rtlDma || op.opClass != OpClass::DmaConfig);
        long long complete = 0;
        if (isDmaCommand) {
            if (channelComplete[op.channel] >= 0 &&
                (opt.model.rtlDma || t < channelComplete[op.channel] + 2))
                violation(where(in) + ": DMA channel " + std::to_string(op.channel) + " is still busy");
            const int slot = opt.model.rtlDma ? (int)(dmaLaunches++ % 8) : -1;
            if (opt.model.rtlDma)
                for (const QueuedDma& d : dma)
                    if (d.slot == slot)
                        violation(where(in) + ": DMA ring slot " + std::to_string(slot) +
                                  " is still occupied until " + where(d.in) + " has an explicit DMA.WAIT");
            long long bytes = 0;
            if (op.opClass != OpClass::DmaConfig) bytes = regs[in.rs2] ? *regs[in.rs2] : 0;
            long long latency = (long long)std::ceil(dmaTransferCycles(bytes) * opt.dmaLatencyScale);
            complete = std::max(t + latency - 1, lastDmaComplete + latency);  // one transfer at a time, in order
            if (const Access* range = dmaVmem(f)) {
                for (const QueuedDma& d : dma) {
                    if (opt.model.rtlDma) break;  // all RTL memory aliases checked above
                    const Access* other = dmaVmem(d.f);
                    long long first, last;
                    if (other && overlapCycles(*range, t, *other, first, last))
                        violation(where(in) + " overlaps the VMEM range of queued " + where(d.in));
                }
                for (const InFlight& a : active)
                    for (const Access& y : a.f.accesses) {
                        long long first, last;
                        if (y.atCompletion || (!y.write && !range->write) || !overlapCycles(y, a.issue, *range, first, last))
                            continue;
                        if (last >= (opt.model.rtlDma ? hazardT : complete))
                            violation(where(in) + " moves data that " + where(a.in) + " is still accessing");
                    }
            }
            dma.push_back({in, f, t, complete, slot});
            lastDmaComplete = complete;
            channelComplete[op.channel] = complete;
            end = std::max(end, complete);
        }

        std::string conflict = table.conflict(in, f, (int)hazardT);
        if (!conflict.empty()) violation(where(in) + ": " + conflict);
        table.reserve(in, f, (int)hazardT);
        if (opt.model.rtlDma && op.opClass == OpClass::DmaWait) table.extendForWait((int)hazardT);
        active.push_back({in, f, hazardT});
        end = std::max(end, t + f.doneAge);

        r.issued++;
        if (op.opClass == OpClass::Delay) r.delays++;
        lastIssue = t;
        long long nextT = t + (op.opClass == OpClass::Delay ? 1 + (in.imm & 0xFFF) : 1);
        long long nextMinimumT = minimumT + (op.opClass == OpClass::Delay ? 1 + (in.imm & 0xFFF) : 1);

        int nextPc = pc + 1;
        if (inSlot) {
            nextPc = redirect;
            inSlot = false;
        } else if (isControlFlow(op)) {
            bool taken = true;
            if (op.opClass == OpClass::Branch && !evaluateBranch(in, regs, taken)) {
                r.stopReason = where(in) + ": branch depends on a value the simulator does not know";
                break;
            }
            if (taken) {
                if (op.name == "jalr") {
                    if (!regs[in.rs1]) {
                        r.stopReason = where(in) + ": jump target unknown";
                        break;
                    }
                    redirect = (int)(*regs[in.rs1] + signExtend(in.imm, 12));
                } else {
                    redirect = labelIndex.count(in.target) ? labelIndex[in.target] : -1;
                }
                inSlot = true;
            }
        }
        applyScalar(in, regs);
        if (op.opClass == OpClass::Jump && in.rd != 0) regs[in.rd] = (uint32_t)(pc + 1);
        // Halt bypasses delay stalls, including those in branch slots.
        if (op.opClass == OpClass::Delay && nextPc >= 0 && nextPc < n &&
            prog.instrs[nextPc].op->opClass == OpClass::Halt)
            nextT = t + 1, nextMinimumT = minimumT + 1;
        // Falloff still drains the delay counter.
        if (op.opClass == OpClass::Delay) end = std::max(end, t + (in.imm & 0xFFF));
        pc = nextPc;
        t = nextT;
        minimumT = nextMinimumT;
    }
    if (opt.model.rtlDma && (pc < 0 || pc >= n))
        for (const QueuedDma& d : dma)
            violation(where(d.in) + ": program ends without explicit DMA.WAIT for this transfer");
    r.cycles = std::max(lastIssue, end);
    return r;
}
