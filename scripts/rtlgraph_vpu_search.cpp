// Fixed-operation VPU scheduling search under the built-in compiler model.
// Pairwise reservation constraints plus explicit three-way VPU-slot clauses
// preserve resource capacity. Every output is checked by full reservations and
// the simulator. This is a model optimum, never a temporal RTL proof.
// The longest-path branching method follows rtlgraph_schedule_search.cpp.
#include <algorithm>
#include <chrono>
#include <climits>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <numeric>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "core/depgraph.h"
#include "core/reservations.h"
#include "core/simulator.h"
#include "passes/pass.h"

namespace {
constexpr int absent = -1000000;
using Matrix = std::vector<std::vector<int>>;
void require(bool ok, const std::string& message) { if (!ok) throw std::runtime_error(message); }
std::string quote(const std::string& value) {
    std::string out = "\"";
    for (char c : value) { if (c == '\\' || c == '"') out += '\\'; if (c == '\n') out += "\\n"; else out += c; }
    return out + "\"";
}
bool addArc(Matrix& d, int a, int b, int gap) {
    if (d[a][b] >= gap) return true;
    if (d[b][a] >= 0) return false;
    int n = d.size();
    // With no positive cycle the new arc can occur at most once in a path.
    auto old = d;
    for (int i = 0; i < n; ++i) if (old[i][a] >= 0)
        for (int j = 0; j < n; ++j) if (old[b][j] >= 0)
            d[i][j] = std::max(d[i][j], old[i][a] + gap + old[b][j]);
    return true;
}
struct Arc { int from, to, distance; };
using Clause = std::vector<Arc>;
struct Search {
    const DepGraph& graph;
    const std::vector<Clause>& clauses;
    int bound;
    std::chrono::steady_clock::time_point deadline;
    unsigned long long visited = 0;
    bool timedOut = false;
    std::vector<int> solution;
    bool bounds(const Matrix& d, std::vector<int>& earliest, std::vector<int>& latest) const {
        int n = d.size(); earliest.assign(n, 0); latest.assign(n, bound);
        for (int i = 0; i < n; ++i) {
            for (int j = 0; j < n; ++j) if (d[j][i] >= 0) earliest[i] = std::max(earliest[i], d[j][i]);
            for (int j = 0; j < n; ++j) if (d[i][j] >= 0)
                latest[i] = std::min(latest[i], bound - graph.footprints[j].doneAge - d[i][j]);
            if (earliest[i] > latest[i]) return false;
        }
        return true;
    }
    bool solve(Matrix d) {
        ++visited;
        if ((visited & 255) == 1 && std::chrono::steady_clock::now() >= deadline) { timedOut = true; return false; }
        std::vector<int> early, late;
        for (;;) {
            if (!bounds(d, early, late)) return false;
            bool changed = false;
            for (const auto& clause : clauses) {
                bool satisfied = false;
                std::vector<Arc> possible;
                for (const auto& a : clause) {
                    if (d[a.from][a.to] >= a.distance) { satisfied = true; break; }
                    if (d[a.to][a.from] < 0 && early[a.from] + a.distance <= late[a.to]) possible.push_back(a);
                }
                if (satisfied) continue;
                if (possible.empty()) return false;
                if (possible.size() == 1) {
                    const auto& a = possible.front();
                    if (!addArc(d, a.from, a.to, a.distance)) return false;
                    changed = true;
                    break;
                }
            }
            if (!changed) break;
        }
        std::vector<Arc> choice;
        int bestScore = INT_MIN;
        for (const auto& clause : clauses) {
            bool satisfied = false;
            std::vector<Arc> possible;
            int minimumDistance = INT_MAX;
            for (const auto& a : clause) {
                if (early[a.to] >= early[a.from] + a.distance) { satisfied = true; break; }
                if (d[a.to][a.from] < 0 && early[a.from] + a.distance <= late[a.to]) {
                    possible.push_back(a); minimumDistance = std::min(minimumDistance, a.distance);
                }
            }
            if (satisfied) continue;
            if (possible.empty()) return false;
            int score = 10000 / static_cast<int>(possible.size()) + minimumDistance;
            if (score > bestScore) { bestScore = score; choice = std::move(possible); }
        }
        if (choice.empty()) { solution = early; return true; }
        for (const auto& a : choice) {
            auto next = d;
            if (addArc(next, a.from, a.to, a.distance) && solve(std::move(next))) return true;
            if (timedOut) return false;
        }
        return false;
    }
};

// This domain has no alternate-port choice or extra capacity resource to make
// pair reduction stronger than full legality. Pair timing must be monotone.
void admitted(const DepGraph& g) {
    require(!g.nodes.empty() && g.nodes.size() <= 40, "expected 1..40 fixed VPU instructions");
    for (size_t i = 0; i < g.nodes.size(); ++i) {
        const auto& in = g.nodes[i]; const auto& f = g.footprints[i];
        require(in.op->engine == Engine::Vpu, "only VPU instructions are supported");
        require(f.error.empty() && naturalGap(in) == 1 && f.vpuLive > 0 && f.dmaCycles == 0,
                "invalid or unsupported VPU footprint");
        require(ReservationTable().conflict(in, f, 0).empty(), "instruction cannot issue alone");
        for (const auto& h : f.holds)
            require(h.alt < 0 && unitCapacity(h.unit, h.index) == 1, "unsupported extra resource capacity or alternative port");
        for (const auto& a : f.accesses)
            require(a.res == Res::MReg && !a.atCompletion && !a.anywhere,
                    "only known MREG accesses are supported");
    }
}
int gap(const DepGraph& g, int a, int b) {
    ReservationTable table; table.reserve(g.nodes[a], g.footprints[a], 0);
    int last = g.footprints[a].doneAge + g.footprints[b].doneAge + 2, first = -1;
    for (int distance = 1; distance <= last; ++distance) {
        bool legal = table.conflict(g.nodes[b], g.footprints[b], distance).empty();
        if (legal && first < 0) first = distance;
        require(legal || first < 0, "non-monotone pair timing requires a richer search domain");
    }
    require(first > 0, "pair never becomes legal");
    return first;
}
std::vector<Clause> constraints(const DepGraph& g) {
    std::vector<Clause> out;
    int n = g.nodes.size();
    for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j)
        out.push_back({{i, j, gap(g, i, j)}, {j, i, gap(g, j, i)}});
    // Capacity two is equivalent to forbidding a common intersection of any
    // three half-open slot intervals [issue, issue + vpuLive). For intervals,
    // that means at least one pair is disjoint: six possible oriented arcs.
    for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j) for (int k = j + 1; k < n; ++k) {
        Clause clause;
        for (int a : {i, j, k}) for (int b : {i, j, k}) if (a != b)
            clause.push_back({a, b, g.footprints[a].vpuLive});
        out.push_back(std::move(clause));
    }
    return out;
}
int objective(const DepGraph& g, const std::vector<int>& start, bool completion) {
    int value = 0;
    for (size_t i = 0; i < start.size(); ++i)
        value = std::max(value, start[i] + (completion ? g.footprints[i].doneAge : 0));
    return value;
}
Code scheduled(const DepGraph& g, const std::vector<int>& start, bool drain = false) {
    std::vector<int> order(g.nodes.size()); std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](int a, int b) { return start[a] < start[b]; });
    Block block; block.scheduled = true;
    for (int i : order) { block.body.push_back(g.nodes[i]); block.issue.push_back(start[i]); }
    block.endCycle = block.issue.back() + 1;
    if (drain) {
        block.terminator = parseAsm("ecall\n").instrs.front();
        block.terminatorCycle = objective(g, start, true) + 1;
        block.endCycle = block.terminatorCycle + 1;
    }
    Code out; out.blocks.push_back(block); return out;
}
void validate(const DepGraph& g, const std::vector<int>& start, const MachineModel& model) {
    auto code = scheduled(g, start, true); ReservationTable table;
    int previous = -1;
    for (size_t k = 0; k < code.blocks[0].body.size(); ++k) {
        const auto& in = code.blocks[0].body[k]; int cycle = code.blocks[0].issue[k];
        require(cycle > previous, "candidate issues more than one instruction per cycle"); previous = cycle;
        auto f = footprintOf(in, unknownRegs(), model);
        require(table.conflict(in, f, cycle).empty(), "candidate fails complete reservations");
        table.reserve(in, f, cycle);
    }
    for (const auto& e : g.edges) require(start[e.to] >= start[e.from] + e.distance, "candidate violates dependency");
    SimOptions options; options.model = model;
    auto result = simulate(flatten(code), options);
    require(result.violations.empty() && result.stopReason.empty(), "candidate fails model simulator");
}
}  // namespace

int main(int argc, char** argv) {
    try {
        std::string source, output, report; constexpr bool completion = true; double seconds = 60;
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i]; require(i + 1 < argc, "missing option value"); std::string value = argv[++i];
            if (arg == "--source") source = value;
            else if (arg == "--output") output = value; else if (arg == "--report") report = value;
            else if (arg == "--seconds") seconds = std::stod(value);
            else throw std::runtime_error("unknown option: " + arg);
        }
        require(!source.empty() && !output.empty() && !report.empty() && std::isfinite(seconds) && seconds > 0 && seconds <= 3600,
                "required: --source BODY --output BODY --report JSON [--seconds 60]");
        require(output != report && !std::filesystem::exists(output) && !std::filesystem::exists(report),
                "output and report must be distinct new files");
        PassContext ctx;
        auto code = buildBlocks(readAsmFile(source)); runPasses(code, {"strip-artifacts"}, ctx);
        require(code.blocks.size() == 1 && code.blocks[0].labels.empty(), "requires one unlabeled body");
        require(!code.blocks[0].terminator || code.blocks[0].terminator->op->opClass == OpClass::Halt,
                "only an optional ECALL terminator is supported");
        code.blocks[0].terminator.reset();
        auto graph = buildGraph(code.blocks[0].body, unknownRegs(), 0, ctx.model); admitted(graph);
        int n = graph.nodes.size(); Matrix base(n, std::vector<int>(n, absent));
        for (int i = 0; i < n; ++i) base[i][i] = 0;
        for (const auto& edge : graph.edges) require(addArc(base, edge.from, edge.to, edge.distance), "cyclic dependency graph");
        auto clauses = constraints(graph);
        std::vector<int> best; int upper = 1000000;
        for (auto priority : {SchedulePriority::Critical, SchedulePriority::Input}) {
            auto seed = code; auto seedCtx = ctx; seedCtx.schedulePriority = priority; schedule(seed, seedCtx);
            std::vector<int> start(n, -1);
            for (int k = 0; k < n; ++k) for (int j = 0; j < n; ++j)
                if (seed.blocks[0].body[k].line == graph.nodes[j].line) start[j] = seed.blocks[0].issue[k];
            require(std::all_of(start.begin(), start.end(), [](int x) { return x >= 0; }), "seed node identity mismatch");
            validate(graph, start, ctx.model);
            int value = objective(graph, start, completion); if (value < upper) { upper = value; best = start; }
        }
        int initial = upper, lower = 0;
        for (int i = 0; i < n; ++i) for (int j = 0; j < n; ++j) if (base[i][j] >= 0)
            lower = std::max(lower, base[i][j] + (completion ? graph.footprints[j].doneAge : 0));
        auto started = std::chrono::steady_clock::now();
        auto deadline = started + std::chrono::milliseconds(static_cast<long long>(1000 * seconds));
        unsigned long long visited = 0; bool timeout = false, proven = false; int infeasible = -1;
        while (lower < upper) {
            Search search{graph, clauses, upper - 1, deadline, 0, false, {}};
            if (search.solve(base)) {
                validate(graph, search.solution, ctx.model); best = search.solution;
                upper = objective(graph, best, completion);
            } else if (search.timedOut) { timeout = true; }
            else { infeasible = upper - 1; lower = upper; proven = true; }
            visited += search.visited;
            if (timeout || proven) break;
        }
        proven = proven || lower == upper;
        std::ofstream body(output); require(bool(body), "cannot write output"); body << printAsm(flatten(scheduled(graph, best, true)));
        std::ofstream json(report); require(bool(json), "cannot write report");
        json << "{\n  \"schema\": \"atlas.rtlgraph.vpu-search.v1\",\n  \"objective\": " << quote(completion ? "last_resource_use_age" : "last_issue_age")
             << ",\n  \"status\": " << quote(proven ? "model_optimum_proven" : "search_incomplete")
             << ",\n  \"scope\": \"Fixed instructions, operands and existing dependency graph; built-in model only, not an RTL lower bound. Full reservation and simulator validation; explicit two-slot capacity clauses.\""
             << ",\n  \"source\": " << quote(source) << ",\n  \"model\": " << quote(ctx.model.name)
             << ",\n  \"instruction_count\": " << n << ",\n  \"initial_upper_bound\": " << initial << ",\n  \"best_objective\": " << upper
             << ",\n  \"lower_bound\": " << lower << ",\n  \"exhausted_infeasible_horizon\": " << infeasible
             << ",\n  \"visited_search_nodes\": " << visited << ",\n  \"timed_out\": " << (timeout ? "true" : "false")
             << ",\n  \"elapsed_seconds\": " << std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()
             << ",\n  \"last_issue_age\": " << objective(graph, best, false) << ",\n  \"last_resource_use_age\": " << objective(graph, best, true)
             << ",\n  \"nodes\": [";
        for (int i = 0; i < n; ++i) json << (i ? "," : "") << "\n    {\"id\":" << i << ",\"instruction\":" << quote(formatInstr(graph.nodes[i])) << ",\"issue\":" << best[i] << ",\"done_age\":" << graph.footprints[i].doneAge << "}";
        json << "\n  ],\n  \"dependencies\": [";
        for (size_t i = 0; i < graph.edges.size(); ++i) { const auto& e = graph.edges[i]; json << (i ? "," : "") << "\n    {\"from\":" << e.from << ",\"to\":" << e.to << ",\"distance\":" << e.distance << ",\"reason\":" << quote(e.reason) << "}"; }
        json << "\n  ],\n  \"reservation_clause_count\": " << clauses.size() << "\n}\n";
        std::cout << "nodes=" << n << " initial=" << initial << " best=" << upper << " lower=" << lower << " visited=" << visited << " status=" << (proven ? "model_optimum_proven" : "search_incomplete") << '\n';
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
