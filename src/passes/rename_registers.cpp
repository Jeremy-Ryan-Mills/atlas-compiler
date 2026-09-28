// rename-registers: gives every value that lives entirely inside one block the
// register that has been idle longest, so reuse of a register for unrelated values
// (false WAR/WAW dependences) stops constraining the schedule.
//
// Classes (--rename): x (scalar), m (vector; BF16 pairs stay even-aligned pairs),
// mxu (accumulators and weight slots, on the same MXU so numerics are unchanged), and
// xmxu (mxu, plus accumulators that are only pushed and popped may move to the other
// MXU: push and pop are the same on both, only matmuls round differently).
// Values live into or out of a block keep their register, as do the x registers DMA
// commands read (they read them when the transfer completes) and every register at an
// atlas.complete. A name is never chosen if it would make an instruction impossible
// on its own (e.g. two operands in one physical MREG bank).
#include <algorithm>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <sstream>

#include "core/depgraph.h"
#include "core/reservations.h"
#include "passes/pass.h"

namespace {

enum class Kind { X, E, M, Acc, W };

// One unit per architectural register.
const int kX = 0, kE = 32, kM = 64, kAcc = 128, kW = 132, kUnits = 136;

struct Operand {
    int Instr::*field;
    Kind kind;
    int width;  // 2 for a BF16 register pair
    bool use, def;
};

int unitOf(Kind k, int reg, int mxu) {
    switch (k) {
        case Kind::X: return kX + reg;
        case Kind::E: return kE + reg;
        case Kind::M: return kM + reg;
        case Kind::Acc: return kAcc + mxu * 2 + reg;
        case Kind::W: return kW + mxu * 2 + reg;
    }
    return -1;
}

// Is operand `field` of `op` a BF16 register pair?
bool isPair(const OpInfo& op, int Instr::*field) {
    bool rd = field == &Instr::rd;
    switch (op.opClass) {
        case OpClass::VpuElementwise:
        case OpClass::VpuRowReduce:
        case OpClass::VpuColReduce: return true;
        case OpClass::VpuPack: return !rd;
        case OpClass::VpuUnpack:
        case OpClass::VpuLoadImmPair:
        case OpClass::PopBf16: return rd;
        case OpClass::AccPushBf16: return !rd;
        default: return false;
    }
}

// The register operands of `in`, from its opcode's operand list.
std::vector<Operand> operandsOf(const Instr& in) {
    const OpInfo& op = *in.op;
    std::vector<Operand> out;
    std::istringstream spec(op.operands);
    bool dma = op.opClass == OpClass::DmaLoad || op.opClass == OpClass::DmaStore;
    bool csrImm = op.opClass == OpClass::Csr && op.name.back() == 'i';
    for (std::string tok; spec >> tok;) {
        if (tok == "@") {
            out.push_back({&Instr::rs1, Kind::X, 1, true, false});
            continue;
        }
        if (tok.size() != 2) continue;  // i, t
        int Instr::*field = tok[1] == 'd' ? &Instr::rd : tok[1] == '1' ? &Instr::rs1 : &Instr::rs2;
        bool dest = tok[1] == 'd';
        switch (tok[0]) {
            case 'x':
                if (csrImm && field == &Instr::rs1) break;  // an immediate, not a register
                out.push_back({field, Kind::X, 1, !dest || dma, dest && !dma});
                break;
            case 'e': out.push_back({field, Kind::E, 1, !dest, dest}); break;
            case 'm': {
                bool store = op.opClass == OpClass::VStore;
                out.push_back({field, Kind::M, isPair(op, field) ? 2 : 1, !dest || store, dest && !store});
                break;
            }
            case 'a': out.push_back({field, Kind::Acc, 1, !dest || op.opClass == OpClass::MatMulAcc, dest}); break;
            case 'w': out.push_back({field, Kind::W, 1, !dest, dest}); break;
        }
    }
    // Writes to x0 are discarded and reads of x0 are constant, so x0 is not a register value.
    std::erase_if(out, [&](const Operand& o) { return o.kind == Kind::X && in.*o.field == 0; });
    return out;
}

struct Options {
    bool x = false, m = false, mxu = false;
    bool crossMxu = false;  // accumulators only pushed and popped (no matmul) may move to the other MXU
};

Options parseClasses(const std::string& text) {
    Options o;
    std::stringstream list(text);
    for (std::string c; std::getline(list, c, ',');) {
        if (c == "x") o.x = true;
        else if (c == "m") o.m = true;
        else if (c == "mxu") o.mxu = true;
        else if (c == "xmxu") o.mxu = o.crossMxu = true;
        else if (c == "all") o.x = o.m = o.mxu = o.crossMxu = true;
        else if (c != "none" && !c.empty()) throw std::runtime_error("unknown register class '" + c + "' (use x, m, mxu, xmxu)");
    }
    return o;
}

// Points operand `o` of `in` at register unit `u`. An accumulator on the other MXU
// also switches the opcode to that MXU.
void setOperand(Instr& in, const Operand& o, int u) {
    if (o.kind == Kind::Acc && (u - kAcc) / 2 != in.op->mxu) {
        std::string name = in.op->name;
        name.back() = (char)('0' + (u - kAcc) / 2);  // ".mxu0" <-> ".mxu1"
        in.op = findOp(name);
    }
    in.*o.field = u - unitOf(o.kind, 0, in.op->mxu);
}

// Instructions of a block in program order, as pointers so they can be rewritten.
std::vector<Instr*> instructionsOf(Block& b) {
    std::vector<Instr*> out;
    for (Instr& in : b.body) out.push_back(&in);
    if (b.terminator) out.push_back(&*b.terminator);
    if (b.slot) out.push_back(&*b.slot);
    return out;
}

using UnitSet = std::vector<bool>;

// Registers live on entry to each block (only DRAM is live at program exit;
// atlas.complete makes every register observable).
std::vector<UnitSet> liveIn(Code& code) {
    size_t n = code.blocks.size();
    std::vector<UnitSet> use(n, UnitSet(kUnits, false)), def(n, UnitSet(kUnits, false)), in(n, UnitSet(kUnits, false));
    for (size_t b = 0; b < n; b++)
        for (Instr* ins : instructionsOf(code.blocks[b])) {
            if (ins->release)
                for (int u = 0; u < kUnits; u++)
                    if (!def[b][u]) use[b][u] = true;
            for (const Operand& o : operandsOf(*ins))
                for (int k = 0; k < o.width; k++) {
                    int u = unitOf(o.kind, ins->*o.field + k, ins->op->mxu);
                    if (o.use && !def[b][u]) use[b][u] = true;
                }
            for (const Operand& o : operandsOf(*ins))
                for (int k = 0; k < o.width; k++)
                    if (o.def) def[b][unitOf(o.kind, ins->*o.field + k, ins->op->mxu)] = true;
        }
    for (bool changed = true; changed;) {
        changed = false;
        for (size_t b = n; b-- > 0;) {
            UnitSet out(kUnits, code.blocks[b].unknownSuccs);
            for (int s : code.blocks[b].succs)
                for (int u = 0; u < kUnits; u++) out[u] = out[u] || in[s][u];
            for (int u = 0; u < kUnits; u++) {
                bool live = use[b][u] || (out[u] && !def[b][u]);
                if (live && !in[b][u]) in[b][u] = true, changed = true;
            }
        }
    }
    return in;
}

// A value: one write of one register (or the value a register holds on block entry),
// with the program-order interval it must survive.
struct Value {
    int unit;
    int def, end;  // def = -1 for a live-in value; end = last read (or def if never read)
    bool pinned;
    int parent, off;  // union-find: name(this) = name(parent) + off
};

struct Ref {
    int instr;
    int operand;
    int value;  // the value the operand's field (its low register) names
};

class Renamer {
public:
    Renamer(const Options& o, uint32_t dmaRegs) : opt_(o), dmaRegs_(dmaRegs) {}

    // Returns the number of values that got a new register.
    int renameBlock(Block& block, const UnitSet& liveOut) {
        values_.clear();
        refs_.clear();
        std::vector<Instr*> ins = instructionsOf(block);
        int n = (int)ins.size();
        std::vector<int> cur(kUnits, -1);
        auto current = [&](int u) {
            if (cur[u] < 0) cur[u] = add({u, -1, -1, true, 0, 0});
            return cur[u];
        };

        for (int i = 0; i < n; i++) {
            const Instr& in = *ins[i];
            std::vector<Operand> ops = operandsOf(in);
            if (in.release)
                for (int u = 0; u < kUnits; u++) {
                    int v = current(u);
                    values_[v].end = i;
                    values_[v].pinned = true;
                }
            std::vector<int> used(ops.size(), -1);
            for (size_t j = 0; j < ops.size(); j++) {
                const Operand& o = ops[j];
                if (!o.use) continue;
                int lo = -1;
                for (int k = 0; k < o.width; k++) {
                    int v = current(unitOf(o.kind, in.*o.field + k, in.op->mxu));
                    values_[v].end = i;
                    if (!renamable(in, o)) values_[v].pinned = true;
                    if (k == 0) lo = v;
                    else link(lo, v, 1);
                }
                used[j] = lo;
                refs_.push_back({i, (int)j, lo});
            }
            for (size_t j = 0; j < ops.size(); j++) {
                const Operand& o = ops[j];
                if (!o.def) continue;
                int lo = -1;
                for (int k = 0; k < o.width; k++) {
                    int u = unitOf(o.kind, in.*o.field + k, in.op->mxu);
                    int v = add({u, i, i, !renamable(in, o), 0, 0});
                    cur[u] = v;
                    if (k == 0) lo = v;
                    else link(lo, v, 1);
                }
                if (used[j] >= 0) link(used[j], lo, 0);  // read and written through one field
                else refs_.push_back({i, (int)j, lo});
            }
        }
        for (int u = 0; u < kUnits; u++)
            if (liveOut[u]) {
                int v = current(u);
                values_[v].end = n;
                values_[v].pinned = true;
            }
        // Pair operands must start on an even register.
        std::vector<int> parity(values_.size(), -1);  // required parity of each group's base register
        for (const Ref& r : refs_) {
            Operand o = operandsOf(*ins[r.instr])[r.operand];
            if (o.width != 2) continue;
            auto [root, rel] = find(r.value);
            int want = ((rel % 2) + 2) % 2;  // base + rel must be even
            if (parity[root] >= 0 && parity[root] != want) bad_[root] = true;
            parity[root] = want;
        }
        return allocate(ins, parity);
    }

private:
    int add(Value v) {
        v.parent = (int)values_.size();
        values_.push_back(v);
        bad_.push_back(false);
        return v.parent;
    }

    std::pair<int, int> find(int v) {
        int off = 0;
        while (values_[v].parent != v) off += values_[v].off, v = values_[v].parent;
        return {v, off};
    }

    // name(b) = name(a) + d
    void link(int a, int b, int d) {
        auto [ra, oa] = find(a);
        auto [rb, ob] = find(b);
        if (ra == rb) {
            if (ob - oa != d) bad_[ra] = true;
            return;
        }
        values_[rb].parent = ra;
        values_[rb].off = oa + d - ob;
        bad_[ra] = bad_[ra] || bad_[rb];
    }

    bool renamable(const Instr& in, const Operand& o) const {
        const OpInfo& op = *in.op;
        switch (o.kind) {
            case Kind::X:
                if (!opt_.x || op.engine == Engine::Dma || op.opClass == OpClass::Csr || op.opClass == OpClass::Jump)
                    return false;
                return !(dmaRegs_ >> (in.*o.field) & 1);
            case Kind::M: return opt_.m;
            case Kind::Acc:
            case Kind::W: return opt_.mxu;
            case Kind::E: return false;
        }
        return false;
    }

    // Candidate registers (as units) for a group whose root lives in `unit`.
    std::vector<int> candidates(int unit) const {
        std::vector<int> out;
        auto range = [&](int from, int to) {
            for (int u = from; u < to; u++) out.push_back(u);
        };
        if (unit >= kW) range(kW + (unit - kW) / 2 * 2, kW + (unit - kW) / 2 * 2 + 2);
        else if (unit >= kAcc) range(kAcc + (unit - kAcc) / 2 * 2, kAcc + (unit - kAcc) / 2 * 2 + 2);
        else if (unit >= kM) range(kM, kM + 64);
        else if (unit >= kE) range(kE, kE + 32);
        else
            for (int r = 1; r < 32; r++)
                if (!(dmaRegs_ >> r & 1)) out.push_back(kX + r);
        return out;
    }

    int allocate(const std::vector<Instr*>& ins, const std::vector<int>& parity) {
        int nv = (int)values_.size();
        std::vector<std::vector<int>> members(nv);
        std::vector<int> rel(nv), rootOf(nv);
        for (int v = 0; v < nv; v++) {
            auto [root, r] = find(v);
            members[root].push_back(v);
            rel[v] = r;
            rootOf[v] = root;
            if (values_[v].pinned) values_[root].pinned = true;
        }
        for (int v = 0; v < nv; v++)
            if (bad_[v]) values_[v].pinned = true;

        struct Interval { int from, to; };
        std::vector<std::vector<Interval>> busy(kUnits);
        auto conflicts = [&](int unit, const Value& v) {
            int end = std::max(v.end, v.def);
            for (const Interval& b : busy[unit])
                if (v.def < b.to && b.from < end) return true;
            return false;
        };
        std::vector<int> name(nv, -1);
        std::vector<int> groups;
        for (int root = 0; root < nv; root++) {
            if (members[root].empty()) continue;
            if (values_[root].pinned) {
                for (int v : members[root]) {
                    name[v] = values_[v].unit;
                    busy[name[v]].push_back({values_[v].def, std::max(values_[v].end, values_[v].def)});
                }
            } else {
                groups.push_back(root);
            }
        }
        auto firstDef = [&](int root) {
            int d = INT_MAX;
            for (int v : members[root]) d = std::min(d, values_[v].def);
            return d;
        };
        std::stable_sort(groups.begin(), groups.end(), [&](int a, int b) { return firstDef(a) < firstDef(b); });

        // A name must not make an instruction impossible on its own (e.g. two operands in one MREG bank).
        std::vector<std::vector<int>> refsAt(ins.size()), instrsOf(nv);
        for (int k = 0; k < (int)refs_.size(); k++) {
            refsAt[refs_[k].instr].push_back(k);
            std::vector<int>& at = instrsOf[rootOf[refs_[k].value]];
            if (at.empty() || at.back() != refs_[k].instr) at.push_back(refs_[k].instr);
        }
        auto issuesAlone = [](const Instr& in) {
            return ReservationTable().conflict(in, footprintOf(in, unknownRegs()), 0).empty();
        };
        std::vector<bool> legalAsWritten(ins.size());
        for (size_t i = 0; i < ins.size(); i++) legalAsWritten[i] = issuesAlone(*ins[i]);
        auto legalWith = [&](int i, int group, int base) {
            if (!legalAsWritten[i]) return true;  // the scheduler reports it
            Instr in = *ins[i];
            std::vector<Operand> ops = operandsOf(in);
            for (int k : refsAt[i]) {
                const Ref& r = refs_[k];
                int u = rootOf[r.value] == group ? base + rel[r.value] : name[r.value] >= 0 ? name[r.value] : values_[r.value].unit;
                setOperand(in, ops[r.operand], u);
            }
            return issuesAlone(in);
        };

        int renamed = 0;
        for (int root : groups) {
            int minRel = INT_MAX, maxRel = INT_MIN;
            for (int v : members[root]) minRel = std::min(minRel, rel[v]), maxRel = std::max(maxRel, rel[v]);
            std::vector<int> units = candidates(values_[root].unit);
            if (opt_.crossMxu && values_[root].unit >= kAcc && values_[root].unit < kW) {
                // Push and pop move data the same way on both MXUs; only matmuls round differently.
                bool onlyMoves = true;
                for (int i : instrsOf[root]) {
                    OpClass c = ins[i]->op->opClass;
                    onlyMoves = onlyMoves && (c == OpClass::AccPushBf16 || c == OpClass::AccPushFp8 ||
                                              c == OpClass::PopBf16 || c == OpClass::PopFp8);
                }
                if (onlyMoves) units = {kAcc, kAcc + 1, kAcc + 2, kAcc + 3};
            }
            int original = values_[root].unit;  // name(root) in the input program
            int best = -1;
            long long bestScore = -1;
            for (int base : units) {  // base = name(root)
                if (base + minRel < units.front() || base + maxRel > units.back()) continue;
                if (parity[root] >= 0 && ((base - kX + parity[root]) % 2 != 0)) continue;  // unit bases are even
                bool ok = true;
                long long score = LLONG_MAX;
                for (int v : members[root]) {
                    int u = base + rel[v];
                    if (std::find(units.begin(), units.end(), u) == units.end() || conflicts(u, values_[v])) {
                        ok = false;
                        break;
                    }
                    // How long the register sits idle around this value: longer means fewer false dependences.
                    long long before = LLONG_MAX / 4, after = LLONG_MAX / 4;
                    int end = std::max(values_[v].end, values_[v].def);
                    for (const Interval& b : busy[u]) {
                        if (b.to <= values_[v].def) before = std::min<long long>(before, values_[v].def - b.to);
                        if (b.from >= end) after = std::min<long long>(after, b.from - end);
                    }
                    score = std::min({score, before, after});
                }
                for (int i : instrsOf[root]) ok = ok && legalWith(i, root, base);
                if (!ok) continue;
                if (score > bestScore || (score == bestScore && base == original)) best = base, bestScore = score;
            }
            if (best < 0) {  // no room: keep the block as written
                if (std::getenv("ATLAS_RENAME_DEBUG"))
                    std::fprintf(stderr, "  rename: no register for value at instr %d (unit %d, %zu members)\n",
                                 firstDef(root), values_[root].unit, members[root].size());
                return -1;
            }
            for (int v : members[root]) {
                name[v] = best + rel[v];
                busy[name[v]].push_back({values_[v].def, std::max(values_[v].end, values_[v].def)});
            }
            if (best != original) renamed++;
        }

        for (const Ref& r : refs_) {
            Instr& in = *ins[r.instr];
            Operand o = operandsOf(in)[r.operand];
            setOperand(in, o, name[r.value]);
        }
        return renamed;
    }

    Options opt_;
    uint32_t dmaRegs_;
    std::vector<Value> values_;
    std::vector<bool> bad_;
    std::vector<Ref> refs_;
};

}  // namespace

void renameRegisters(Code& code, PassContext& ctx) {
    Options opt = parseClasses(ctx.renameClasses);
    uint32_t dmaRegs = dmaOperandRegisters(flatten(code).instrs);
    std::vector<UnitSet> in = liveIn(code);
    Renamer renamer(opt, dmaRegs);
    int renamed = 0, skipped = 0;
    for (size_t b = 0; b < code.blocks.size(); b++) {
        UnitSet out(kUnits, code.blocks[b].unknownSuccs);
        for (int s : code.blocks[b].succs)
            for (int u = 0; u < kUnits; u++) out[u] = out[u] || in[s][u];
        Block copy = code.blocks[b];
        int r = renamer.renameBlock(copy, out);
        // A new name can put two operands of one instruction in the same MREG bank.
        std::vector<Instr> before = blockInstructions(code.blocks[b]), after = blockInstructions(copy);
        for (size_t i = 0; r >= 0 && i < after.size(); i++) {
            auto alone = [](const Instr& in) {
                return ReservationTable().conflict(in, footprintOf(in, unknownRegs()), 0).empty();
            };
            if (!alone(after[i]) && alone(before[i])) {
                if (std::getenv("ATLAS_RENAME_DEBUG"))
                    std::fprintf(stderr, "  rename: %s can never issue\n", formatInstr(after[i]).c_str());
                r = -1;
            }
        }
        if (r < 0) {
            skipped++;
            continue;
        }
        code.blocks[b] = copy;
        renamed += r;
    }
    ctx.log.push_back("rename-registers: renamed " + std::to_string(renamed) + " values (" + ctx.renameClasses + ")" +
                      (skipped ? ", left " + std::to_string(skipped) + " blocks as written" : ""));
}
