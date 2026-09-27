#define main schedule_search_cli_main
#include "../rtlgraph_schedule_search.cpp"
#undef main

#include <functional>
#include <random>

static bool exhaustive(const DepGraph& g, const Matrix& d, const std::vector<Pair>& pairs,
                       int horizon, bool completion) {
    std::vector<int> assignment(g.nodes.size(), -1);
    std::function<bool(int)> place = [&](int k) {
        if (k == static_cast<int>(assignment.size())) return true;
        int last = horizon - (completion ? g.footprints[k].doneAge : 0);
        for (int t = 0; t <= last; ++t) {
            bool legal = true;
            for (int j = 0; j < k; ++j) {
                if (d[j][k] >= 0 && t < assignment[j] + d[j][k]) legal = false;
                if (d[k][j] >= 0 && assignment[j] < t + d[k][j]) legal = false;
            }
            for (const auto& p : pairs) if (p.b == k && !(t >= assignment[p.a] + p.ab || assignment[p.a] >= t + p.ba)) legal = false;
            if (legal) { assignment[k] = t; if (place(k + 1)) return true; }
        }
        assignment[k] = -1;
        return false;
    };
    return place(0);
}

int main() {
    try {
        std::mt19937 random(194);
        int comparisons = 0;
        for (int problem = 0; problem < 200; ++problem) {
            int n = 1 + random() % 4;
            DepGraph g; g.nodes.resize(n); g.footprints.resize(n);
            Matrix d(n, std::vector<int>(n, absent));
            for (int i = 0; i < n; ++i) { d[i][i] = 0; g.footprints[i].doneAge = random() % 4; }
            std::vector<Pair> pairs;
            for (int i = 0; i < n; ++i) for (int j = i + 1; j < n; ++j) {
                if (random() % 3 == 0) require(addArc(d, i, j, 1 + random() % 3), "random DAG became cyclic");
                pairs.push_back({i, j, 1 + static_cast<int>(random() % 4), 1 + static_cast<int>(random() % 4)});
            }
            for (bool completion : {false, true}) for (int horizon = 0; horizon <= 8; ++horizon) {
                Search search{g, pairs, completion, horizon, std::chrono::steady_clock::now() + std::chrono::seconds(30), 0, false, {}};
                require(search.solve(d) == exhaustive(g, d, pairs, horizon, completion), "solver disagrees with exhaustive integer assignments");
                require(!search.timedOut, "small exact test unexpectedly timed out"); ++comparisons;
            }
        }

        MachineModel model; model.mxu1OverwriteAccReadHold = false;
        const std::vector<std::string> families = {
            "vmatpush.weight.mxu1 w0, m4\nvmatmul.mxu1 acc0, m0, w0\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n",
            "vmatpush.weight.mxu1 w0, m4\nvmatpush.weight.mxu1 w1, m6\nvmatmul.mxu1 acc0, m0, w0\n",
            "vmatmul.mxu1 acc0, m0, w0\nvmatmul.acc.mxu1 acc0, m1, w1\nvmatpop.fp8.acc.mxu1 m8, acc0, e0\n"};
        const std::vector<int> ages = {0, 1, 4, 31, 32, 33, 34, 35, 36, 64, 65, 66, 67};
        int reservationComparisons = 0;
        for (const auto& source : families) {
            auto g = buildGraph(parseAsm(source).instrs, unknownRegs(), 0, model); admitted(g);
            std::vector<Pair> pairs;
            for (int i = 0; i < 3; ++i) for (int j = i + 1; j < 3; ++j) pairs.push_back({i, j, gap(g, i, j), gap(g, j, i)});
            for (int a : ages) for (int b : ages) for (int c : ages) {
                std::vector<int> starts{a, b, c}; bool pairLegal = true;
                for (const auto& p : pairs) pairLegal &= starts[p.b] >= starts[p.a] + p.ab || starts[p.a] >= starts[p.b] + p.ba;
                std::vector<int> order{0, 1, 2}; std::sort(order.begin(), order.end(), [&](int x, int y) { return starts[x] < starts[y]; });
                ReservationTable table; bool fullLegal = true; int previous = -1;
                for (int i : order) {
                    if (starts[i] <= previous || !table.conflict(g.nodes[i], g.footprints[i], starts[i]).empty()) fullLegal = false;
                    table.reserve(g.nodes[i], g.footprints[i], starts[i]); previous = starts[i];
                }
                require(pairLegal == fullLegal, "pair reduction disagrees with complete reservation table"); ++reservationComparisons;
            }
        }

        auto g = buildGraph(parseAsm(families[1]).instrs, unknownRegs(), 0, model);
        auto changed = g;
        for (auto& h : changed.footprints[0].holds) if (h.unit == Unit::MxuWeightStream) h.to = 31;
        bool rejected = false;
        try { admitted(changed); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "unsupported alternative-port reduction admitted");
        DepGraph pulse; pulse.nodes = {g.nodes[0], g.nodes[1]}; pulse.footprints.resize(2);
        pulse.footprints[0].holds.push_back({Unit::MxuAccRead, 2, 5, 5, -1}); pulse.footprints[0].doneAge = 5;
        pulse.footprints[1].holds.push_back({Unit::MxuAccRead, 2, 2, 2, -1}); pulse.footprints[1].doneAge = 2;
        rejected = false; try { gap(pulse, 0, 1); } catch (const std::runtime_error&) { rejected = true; }
        require(rejected, "non-monotone forbidden spacing admitted");
        Matrix d(3, std::vector<int>(3, absent)); for (int i = 0; i < 3; ++i) d[i][i] = 0;
        Search expired{g, {}, false, 100, std::chrono::steady_clock::now() - std::chrono::seconds(1), 0, false, {}};
        require(!expired.solve(d) && expired.timedOut, "timeout was not distinct from infeasibility");
        std::cout << "PASS: " << comparisons << " exhaustive solver comparisons, " << reservationComparisons
                  << " actual reservation comparisons, unsupported-shape and timeout checks\n";
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
