#define main vpu_search_cli_main
#include "../rtlgraph_vpu_search.cpp"
#undef main

#include <functional>
#include <random>

static bool clausesHold(const std::vector<Clause>& clauses, const std::vector<int>& starts) {
    for (const auto& clause : clauses) {
        bool satisfied = false;
        for (const auto& a : clause)
            satisfied |= starts[a.to] >= starts[a.from] + a.distance;
        if (!satisfied) return false;
    }
    return true;
}

static bool bruteForce(const DepGraph& graph, const Matrix& base,
                       const std::vector<Clause>& clauses, int bound) {
    std::vector<int> times(graph.nodes.size());
    std::function<bool(int)> visit = [&](int node) {
        if (node == static_cast<int>(times.size())) {
            for (size_t i = 0; i < times.size(); ++i)
                for (size_t j = 0; j < times.size(); ++j)
                    if (base[i][j] >= 0 && times[j] < times[i] + base[i][j]) return false;
            return clausesHold(clauses, times);
        }
        for (int t = 0; t + graph.footprints[node].doneAge <= bound; ++t) {
            times[node] = t;
            if (visit(node + 1)) return true;
        }
        return false;
    };
    return visit(0);
}

int main() {
    try {
        std::mt19937 rng(194);
        int comparisons = 0;
        for (int problem = 0; problem < 200; ++problem) {
            int n = 1 + rng() % 4;
            DepGraph graph; graph.nodes.resize(n); graph.footprints.resize(n);
            Matrix base(n, std::vector<int>(n, absent));
            for (int i = 0; i < n; ++i) {
                base[i][i] = 0;
                graph.footprints[i].doneAge = rng() % 4;
                graph.footprints[i].vpuLive = 1 + rng() % 4;
            }
            std::vector<Clause> clauses;
            for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j) {
                if (rng() % 3 == 0)
                    require(addArc(base, i, j, 1 + rng() % 3), "random forward arc rejected");
                clauses.push_back({{i, j, 1 + static_cast<int>(rng() % 4)},
                                   {j, i, 1 + static_cast<int>(rng() % 4)}});
            }
            for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j) for (int k = j + 1; k < n; ++k) {
                Clause clause;
                for (int a : {i, j, k}) for (int b : {i, j, k}) if (a != b)
                    clause.push_back({a, b, graph.footprints[a].vpuLive});
                clauses.push_back(clause);
            }
            for (int horizon = 0; horizon <= 8; ++horizon) {
                Search search{graph, clauses, horizon,
                              std::chrono::steady_clock::now() + std::chrono::seconds(10), 0, false, {}};
                require(search.solve(base) == bruteForce(graph, base, clauses, horizon),
                        "disjunctive solver disagrees with exhaustive integer placements");
                require(!search.timedOut, "small test timed out");
                ++comparisons;
            }
        }

        const std::vector<std::string> families = {
            "vsquare.bf16 m2, m0\nvtanh.bf16 m6, m4\nvexp.bf16 m10, m8\n",
            "vli.all m12, 12288\nvsquare.bf16 m2, m0\nvtanh.bf16 m6, m4\n",
            "vredsum.row.bf16 m2, m0\nvmul.bf16 m8, m4, m6\nvexp.bf16 m12, m10\n"};
        const std::vector<int> ages = {0, 1, 2, 32, 38, 63, 64, 65, 66, 67, 128};
        int reservationChecks = 0;
        for (const auto& source : families) {
            auto graph = buildGraph(parseAsm(source).instrs, unknownRegs(), 0);
            admitted(graph);
            const auto clauses = constraints(graph);
            for (int a : ages) for (int b : ages) for (int c : ages) {
                std::vector<int> starts{a, b, c}, order{0, 1, 2};
                std::sort(order.begin(), order.end(), [&](int x, int y) { return starts[x] < starts[y]; });
                ReservationTable table;
                bool legal = true;
                int previous = -1;
                for (int i : order) {
                    legal &= starts[i] > previous && table.conflict(graph.nodes[i], graph.footprints[i], starts[i]).empty();
                    table.reserve(graph.nodes[i], graph.footprints[i], starts[i]);
                    previous = starts[i];
                }
                require(clausesHold(clauses, starts) == legal,
                        "pair/triple clauses disagree with complete reservation table");
                ++reservationChecks;
            }
        }

        auto graph = buildGraph(parseAsm(families.front()).instrs, unknownRegs(), 0);
        auto clauses = constraints(graph);
        require(!clausesHold(clauses, {0, 1, 2}), "third overlapping VPU instruction admitted");
        auto changed = graph;
        changed.footprints.front().holds.push_back({Unit::MxuCompute, 1, 0, 10});
        bool rejected = false;
        try { admitted(changed); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "unsupported extra capacity admitted");
        changed = graph;
        changed.footprints[0].holds.push_back({Unit::MxuAccRead, 0, 5, 5});
        changed.footprints[1].holds.push_back({Unit::MxuAccRead, 0, 2, 2});
        rejected = false;
        try { gap(changed, 0, 1); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "nonmonotone conflict admitted");

        Matrix base(3, std::vector<int>(3, absent));
        for (int i = 0; i < 3; ++i) base[i][i] = 0;
        Search expired{graph, clauses, 100,
                       std::chrono::steady_clock::now() - std::chrono::seconds(1), 0, false, {}};
        require(!expired.solve(base) && expired.timedOut, "timeout reported as infeasibility");
        std::cout << "PASS: " << comparisons << " exhaustive integer-search comparisons, "
                  << reservationChecks << " complete reservation comparisons, capacity/nonmonotone/timeout checks\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
