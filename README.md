# atlas-compiler-experiments

`atlas-opt` reads Atlas NPU assembly, builds dependency graphs, schedules
instructions against a timing/resource model, and emits explicit `delay`s. The
built-in model follows [`npu_model`'s `rtl-match` branch](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match).
[PLAN.md](.agents/notes/PLAN.md) describes the compiler;
[OPEN_QUESTIONS.md](.agents/notes/OPEN_QUESTIONS.md) tracks unresolved assumptions.

```sh
git submodule update --init -- third_party/npu_model
cmake -S . -B build -G Ninja && cmake --build build
build/atlas-opt kernel.S -o kernel.opt.S --viz kernel.html
```

Optional RTL-derived profiles cover six components of `EE290SimConfig`. Start
with the [model guide](docs/rtlgraph-model.md) for usage, timing, and admission
rules. The [lowering walkthrough](docs/rtlgraph-lowering.md) traces a real hardware
predicate into a schedule; the [contract reference](docs/rtlgraph-contract.md)
describes external-consumer bundles; [results](docs/rtlgraph-perf-validation.md)
record measured kernel comparisons. The [RTL extraction plan](.agents/notes/RTL_GRAPH_PLAN.md)
sets the remaining scope.

| Option | What it does |
|---|---|
| `-o FILE` | Write optimized assembly; otherwise print it |
| `--viz FILE.html` | Write a browser dependency-graph viewer |
| `--passes a,b,c` / `--list-passes` | Select passes / list them |
| `--check` | Validate without optimizing |
| `--validation static` | Check known CFG paths without evaluating numerical branches; dynamic validation is the default |
| `--schedule-priority input` | Prefer input order among ready instructions; critical-path priority is the default |
| `--dump-footprints FILE` | Export resolved accesses, holds, and dependencies without scheduling |
| `--dma-timing model` | Use modeled DMA latency instead of robust timing |
| `--experimental-mxu{0,1}-profile FILE` | Select an optional MXU partial projection |
| `--rtl-{dma,lsu,xlu,vpu}-profile FILE` | Select an optional RTL component projection |

Dynamic validation checks the scheduled result at modeled and slower DMA speeds,
reporting hazards or incomplete execution. The viewer also uses dynamic
simulation. Static validation checks dependencies, reservations, fixed-engine
drains, and reachable DMA lifetimes; it does not establish numerical correctness
or dynamic latency.

Optimization supports conditional branches and `jal x0, label`. It rejects
`jalr`, linked `jal`, and `auipc`, including in delay slots, because relocation is
unsupported. Ordinary slot delays require `strip-artifacts` (enabled by default);
slot delays marked `# keep` and slot halts are rejected. `--check` can inspect
these instructions without rewriting. Scheduled halts receive a NOP guard after
delays, including across labels; `# keep` preserves the delay immediate.

Mark a completion CSR with the exact, case-sensitive, whitespace-delimited token
`# atlas.release`; preserve it during preprocessing:

```asm
vstore m0, 0(x0)
csrrwi x0, x1, 0xC10 # atlas.release
```

Before release, fixed-latency work must finish and every possibly pending DMA
channel needs a matching wait on every path. Releases in delay slots or pipelines
without `schedule` are rejected. Printing and reoptimization retain the marker.
Entry assumes idle engines; release establishes modeled completion and adds no
host acknowledgment, buffer ownership, or IMEM-slot exit guarantee. Optimization
requires explicit DMA waits; `--check` checks modeled completion. The
[model guide](docs/rtlgraph-model.md#dma-and-control-flow) describes optional wait
insertion and RTL DMA capture rules.

## Layout

| Folder | Contents |
|---|---|
| `src/core/` | Assembly, blocks, known values, machine footprints, dependency graphs, reservations, and validation |
| `src/passes/` | Optimization passes and registry; see [the pass guide](src/passes/README.md) to add one |
| `src/tool/` | CLI and HTML viewer |
| `profiles/` | Optional component projections and canonical extraction evidence |
| `tests/` | C++ timing tests and Python equivalence/regression tests |

## Equivalence tests

The pytest harness compares registered `npu_model` kernels before and after
optimization, checking completion and bytes in DRAM outputs, DRAM inputs, and
VMEM. Use Python with the model dependencies, such as this repository's `uv`
environment:

```sh
uv run pytest                                # uses build/atlas-opt if present
uv run pytest --atlas-opt=identity           # compare unchanged kernels
uv run pytest --atlas-opt=strip-delays       # exercise missing-delay detection
```

The harness invokes `<cmd> in.S -o out.S`. Set the command with `--atlas-opt` or
`ATLAS_OPT`, and flags with `--atlas-opt-args` or `ATLAS_OPT_ARGS`.
`--artifacts-dir DIR` retains assembly; `--max-cycles` sets the execution budget.
It prints a cycle comparison table. `SmolVLARmsNormProgram` has a known baseline
golden-output failure. Built-in optimizers skip compiler-specific tests; harness
self-tests run independently. Regression tests cover relocation, branch paths,
halt guards, publication, engine reuse, and variable DMA latency; C++ tests cover
timing, metadata, and reservations.
