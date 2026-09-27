// Experimental fixed-instruction search linked against the real compiler model.
// Pairwise resource constraints form a relaxation; every solution is checked
// against the complete ReservationTable and simulator before it is exported.
#include <algorithm>
#include <chrono>
#include <climits>
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
struct Pair { int a, b, ab, ba; };
struct Search {
    const DepGraph& graph;
    std::vector<Pair> pairs;
    bool completion;
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
                latest[i] = std::min(latest[i], bound - (completion ? graph.footprints[j].doneAge : 0) - d[i][j]);
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
            for (const auto& p : pairs) {
                if (d[p.a][p.b] >= p.ab || d[p.b][p.a] >= p.ba) continue;
                bool ab = d[p.b][p.a] >= 0 || early[p.a] + p.ab > late[p.b];
                bool ba = d[p.a][p.b] >= 0 || early[p.b] + p.ba > late[p.a];
                if (ab && ba) return false;
                if (ab || ba) {
                    if (!(ab ? addArc(d, p.b, p.a, p.ba) : addArc(d, p.a, p.b, p.ab))) return false;
                    changed = true;
                    break;
                }
            }
            if (!changed) break;
        }
        // If the earliest feasible assignment already satisfies all disjunctions,
        // orienting the remaining pairs cannot improve this feasibility result.
        const Pair* choice = nullptr;
        int score = INT_MIN;
        for (const auto& p : pairs) {
            if (early[p.b] >= early[p.a] + p.ab || early[p.a] >= early[p.b] + p.ba) continue;
            int candidate = 1000 * std::min(p.ab, p.ba) - (late[p.a] - early[p.a]) - (late[p.b] - early[p.b]);
            if (candidate > score) { score = candidate; choice = &p; }
        }
        if (!choice) { solution = early; return true; }
        const auto p = *choice;
        Matrix left = d;
        if (addArc(left, p.a, p.b, p.ab) && solve(std::move(left))) return true;
        if (timedOut) return false;
        if (addArc(d, p.b, p.a, p.ba) && solve(std::move(d))) return true;
        return false;
    }
};

// Restrict the executable to the experimentally covered family. In particular,
// a push's alternate P0 cannot make pairwise exclusion spuriously stronger:
// all pushes occupy the sole weight stream at ages1..32, hence their preferred
// P1 windows0..31 never overlap; no other admitted operation needs P1.
void admitted(const DepGraph& g) {
    require(!g.nodes.empty() && g.nodes.size() <= 40, "expected 1..40 fixed instructions");
    for (size_t i = 0; i < g.nodes.size(); ++i) {
        const auto& in = g.nodes[i]; const auto& f = g.footprints[i]; auto op = in.op->opClass;
        require(in.op->mxu == 1 && (op == OpClass::WeightPush || op == OpClass::MatMul ||
                op == OpClass::MatMulAcc || op == OpClass::PopFp8), "unsupported instruction family");
        require(f.error.empty() && naturalGap(in) == 1, "invalid instruction footprint");
        int altCount = 0, streamCount = 0;
        for (const auto& h : f.holds) {
            if (h.alt >= 0) {
                ++altCount;
                require(op == OpClass::WeightPush && h.unit == Unit::MxuPort && h.index == 5 && h.alt == 4
                        && h.from == 0 && h.to == 31, "unsupported alternative-port shape");
            }
            if (h.unit == Unit::MxuWeightStream) {
                ++streamCount;
                require(op == OpClass::WeightPush && h.index == 1 && h.from == 1 && h.to == 32
                        && unitCapacity(h.unit, h.index) == 1, "unsupported weight-stream shape");
            }
            if (h.unit == Unit::MxuPort && op != OpClass::WeightPush)
                require(h.index != 5 && h.alt != 5, "non-push uses preferred P1 port");
        }
        require(altCount == int(op == OpClass::WeightPush) && streamCount == altCount,
                "unrecognized push reservation contract");
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
int objective(const DepGraph& g, const std::vector<int>& start, bool completion) {
    int value = 0;
    for (size_t i = 0; i < start.size(); ++i)
        value = std::max(value, start[i] + (completion ? g.footprints[i].doneAge : 0));
    return value;
}
Code scheduled(const DepGraph& g, const std::vector<int>& start) {
    std::vector<int> order(g.nodes.size()); std::iota(order.begin(), order.end(), 0);
    std::sort(order.begin(), order.end(), [&](int a, int b) { return start[a] < start[b]; });
    Block block; block.scheduled = true;
    for (int i : order) { block.body.push_back(g.nodes[i]); block.issue.push_back(start[i]); }
    block.endCycle = block.issue.back() + 1;
    Code out; out.blocks.push_back(block); return out;
}
void validate(const DepGraph& g, const std::vector<int>& start, const MachineModel& model) {
    auto code = scheduled(g, start); ReservationTable table;
    int previous = -1;
    for (size_t k = 0; k < code.blocks[0].body.size(); ++k) {
        const auto& in = code.blocks[0].body[k]; int cycle = code.blocks[0].issue[k];
        require(cycle > previous, "candidate issues more than one instruction per cycle"); previous = cycle;
        auto f = footprintOf(in, unknownRegs(), model);
        require(table.conflict(in, f, cycle).empty(), "pairwise-relaxed candidate fails complete reservations");
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
        std::string source, profile, output, report; bool completion = false; double seconds = 60;
        for (int i = 1; i < argc; ++i) {
            std::string arg = argv[i]; require(i + 1 < argc, "missing option value"); std::string value = argv[++i];
            if (arg == "--source") source = value; else if (arg == "--profile") profile = value;
            else if (arg == "--output") output = value; else if (arg == "--report") report = value;
            else if (arg == "--seconds") seconds = std::stod(value);
            else if (arg == "--objective") { require(value == "issue" || value == "completion", "unknown objective"); completion = value == "completion"; }
            else throw std::runtime_error("unknown option: " + arg);
        }
        require(!source.empty() && !profile.empty() && !output.empty() && !report.empty() && seconds > 0,
                "required: --source BODY --profile PROFILE --output BODY --report JSON [--objective issue|completion] [--seconds 60]");
        require(output != report && !std::filesystem::exists(output) && !std::filesystem::exists(report),
                "output and report must be distinct new files");
        PassContext ctx; ctx.model = readExperimentalMxu1Profile(profile);
        auto code = buildBlocks(readAsmFile(source)); runPasses(code, {"strip-artifacts"}, ctx);
        require(code.blocks.size() == 1 && !code.blocks[0].terminator && code.blocks[0].labels.empty(), "requires one unlabeled body");
        auto graph = buildGraph(code.blocks[0].body, unknownRegs(), 0, ctx.model); admitted(graph);
        int n = graph.nodes.size(); Matrix base(n, std::vector<int>(n, absent));
        for (int i = 0; i < n; ++i) base[i][i] = 0;
        for (const auto& edge : graph.edges) require(addArc(base, edge.from, edge.to, edge.distance), "cyclic dependency graph");
        std::vector<Pair> pairs;
        for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j) pairs.push_back({i, j, gap(graph, i, j), gap(graph, j, i)});
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
            Search search{graph, pairs, completion, upper - 1, deadline, 0, false, {}};
            if (search.solve(base)) {
                validate(graph, search.solution, ctx.model); best = search.solution;
                upper = objective(graph, best, completion);
            } else if (search.timedOut) { timeout = true; }
            else { infeasible = upper - 1; lower = upper; proven = true; }
            visited += search.visited;
            if (timeout || proven) break;
        }
        proven = proven || lower == upper;
        std::ofstream body(output); require(bool(body), "cannot write output"); body << printAsm(flatten(scheduled(graph, best)));
        std::ofstream json(report); require(bool(json), "cannot write report");
        json << "{\n  \"schema\": \"atlas.rtlgraph.schedule-search.v1\",\n  \"objective\": " << quote(completion ? "last_resource_use_age" : "last_issue_age")
             << ",\n  \"status\": " << quote(proven ? "model_optimum_proven" : "search_incomplete")
             << ",\n  \"scope\": \"Fixed instruction identities/operands and existing dependency graph; frozen partial model only, not an RTL lower bound. Pair constraints come from ReservationTable; full candidates are model-validated.\""
             << ",\n  \"source\": " << quote(source) << ",\n  \"profile\": " << quote(profile)
             << ",\n  \"instruction_count\": " << n << ",\n  \"initial_upper_bound\": " << initial << ",\n  \"best_objective\": " << upper
             << ",\n  \"lower_bound\": " << lower << ",\n  \"exhausted_infeasible_horizon\": " << infeasible
             << ",\n  \"visited_search_nodes\": " << visited << ",\n  \"timed_out\": " << (timeout ? "true" : "false")
             << ",\n  \"elapsed_seconds\": " << std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()
             << ",\n  \"last_issue_age\": " << objective(graph, best, false) << ",\n  \"last_resource_use_age\": " << objective(graph, best, true)
             << ",\n  \"nodes\": [";
        for (int i = 0; i < n; ++i) json << (i ? "," : "") << "\n    {\"id\":" << i << ",\"instruction\":" << quote(formatInstr(graph.nodes[i])) << ",\"issue\":" << best[i] << ",\"done_age\":" << graph.footprints[i].doneAge << "}";
        json << "\n  ],\n  \"dependencies\": [";
        for (size_t i = 0; i < graph.edges.size(); ++i) { const auto& e = graph.edges[i]; json << (i ? "," : "") << "\n    {\"from\":" << e.from << ",\"to\":" << e.to << ",\"distance\":" << e.distance << ",\"reason\":" << quote(e.reason) << "}"; }
        json << "\n  ],\n  \"pairwise_resource_separation\": [";
        for (size_t i = 0; i < pairs.size(); ++i) { const auto& p = pairs[i]; json << (i ? "," : "") << "\n    {\"a\":" << p.a << ",\"b\":" << p.b << ",\"a_before_b\":" << p.ab << ",\"b_before_a\":" << p.ba << "}"; }
        json << "\n  ]\n}\n";
        std::cout << "nodes=" << n << " initial=" << initial << " best=" << upper << " lower=" << lower << " visited=" << visited << " status=" << (proven ? "model_optimum_proven" : "search_incomplete") << '\n';
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
