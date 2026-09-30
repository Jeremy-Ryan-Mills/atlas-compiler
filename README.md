# atlas-compiler-experiments

`atlas-opt` reads Atlas NPU assembly, builds dependency graphs, schedules instructions against the selected timing/resource model, and emits explicit `delay`s. The built-in model follows [`npu_model`'s `rtl-match` branch](https://github.com/ucb-ee194-tapeout/npu_model/tree/rtl-match). [PLAN.md](.agents/notes/PLAN.md) describes the compiler, and [OPEN_QUESTIONS.md](.agents/notes/OPEN_QUESTIONS.md) tracks unresolved model assumptions.

```sh
git submodule update --init -- third_party/npu_model
cmake -S . -B build -G Ninja && cmake --build build
build/atlas-opt kernel.S -o kernel.opt.S --viz kernel.html
```

The [RTL extraction plan](.agents/notes/RTL_GRAPH_PLAN.md) and [profile guide](docs/rtlgraph-model.md) describe extracted timing/control facts for all six components of [`EE290SimConfig`](https://github.com/ucb-ee194-tapeout/bringup-chipyard/blob/main/generators/chipyard/src/main/scala/EE290Configs.scala#L13-L26), supplied as optional partial profiles under `profiles/EE290SimConfig/{mxu0,mxu1,dma,lsu,xlu,vpu}/`. Unselected rules keep the built-in model. The [lowering walkthrough](docs/rtlgraph-lowering.md) follows hardware IR through extraction, footprints, and scheduling. The [kernel regression](docs/rtlgraph-perf-validation.md) records numerical checks, measured improvements, and environment-dependent regressions. The [assembly handoff](docs/rtlgraph-contract.md) bundles `before.S`, profiles, resolved footprints, and compiler identity for future external consumers, including [Merlin](https://github.com/ucb-bar/merlin).

| Option | What it does |
|---|---|
| `-o FILE` | write the optimized program (otherwise it is printed) |
| `--viz FILE.html` | before/after dependency graph viewer (open it in a browser) |
| `--passes a,b,c` / `--list-passes` | run only some passes / list them |
| `--check` | validate the input without optimizing it |
| `--validation static` | check hazards across known CFG paths without evaluating numerical branches; dynamic simulation is the default |
| `--schedule-priority input` | prefer input order among ready instructions; critical-path priority is the default |
| `--dump-footprints FILE` | export the input's resolved accesses, holds, and dependencies without scheduling |
| `--dma-timing model` | trust npu_model's DMA latency instead of staying valid for any latency |
| `--experimental-mxu1-profile FILE` | load the optional partial MXU1 timing/resource projection; other rules remain built in |
| `--experimental-mxu0-profile FILE` | load the optional MXU0 accumulator-read projection; may compose with an MXU1 profile from the same hardware IR |
| `--rtl-dma-profile FILE` | extend the model with RTL DMA capture, address, and explicit-completion rules; requires robust timing |
| `--rtl-lsu-profile FILE` | load derived vector and scalar LSU access/path timing from the same hardware IR as other selected profiles |
| `--rtl-vpu-profile FILE` | load partial access timing for 29 implemented VPU commands |
| `--rtl-xlu-profile FILE` | load conditional `VTRPOSE.XLU` row timing and engine occupancy from the same hardware IR |

By default, `atlas-opt` simulates the result at the modeled DMA speed and with slower DMA, and returns an error on hazards or incomplete execution. `--validation static` instead checks block dependencies, resource reservations, fixed-engine drains, and reachable DMA lifetimes. It supports numerical load-dependent branches without guessing their outcomes, but does not establish numerical correctness or dynamic latency. The viewer uses dynamic simulation and shows instruction dependencies and issue cycles.

Optimization supports conditional branches and `jal x0, label`. It rejects `jalr`, `jal` with a nonzero link register, and `auipc`, including in delay slots, because address relocation is unsupported. Ordinary slot delays require `strip-artifacts` (enabled by default); slot delays marked `# keep` and slot halts are rejected. `--check` can inspect these instructions without rewriting.

Scheduled halts use a NOP guard after delays, including across labels; `# keep` preserves the delay immediate. The checker reports unfinished work at halt. Robust DMA scheduling reserves remaining port use across waits and gaps.

Mark a completion CSR with `# atlas.release`:

```asm
vstore m0, 0(x0)
csrrwi x0, x1, 0xC10 # atlas.release
```

Prior fixed-latency work must finish before the CSR executes. Each possibly pending DMA channel needs a matching wait on every path to release and before reuse. Releases in delay slots or pipelines without `schedule` are rejected. Preserve the exact, case-sensitive, whitespace-delimited token during preprocessing; printing and reoptimization retain it.

Releases assume idle entry; unmarked CSR behavior is unchanged. A release provides no host acknowledgment, buffer ownership, or IMEM-slot exit proof. `--check` checks modeled completion; optimization additionally requires explicit DMA waits on every path.

The optional RTL DMA profile uses issue-captured operands and `DMA.CONFIG`, VMEM word pointers, configured 37-bit DRAM ranges, and explicit completion lifetimes. Matching waits may cross known branches, joins, and loops; `--passes strip-artifacts,insert-dma-waits,fill-delay-slots,schedule` inserts missing waits using the selected model. Wait insertion is opt-in and computes per-channel masks across the CFG before the dependency graph binds uses to waits; a join alone does not force every DMA channel to complete. Fixed-latency engines still drain at block boundaries, and unknown branch targets or DMA delay slots are rejected. See the [DMA guide](docs/rtlgraph-dma-integration.md) for the admission rules.

## Layout

| Folder | Contents |
|---|---|
| `src/core/` | Everything about programs and the machine: assembly parsing and printing (`asm`), basic blocks (`blocks`), known register values (`values`), the rtl-match timing rules (`machine`, `reservations`), dependency graphs (`depgraph`), and dynamic and static hazard checks (`simulator`) |
| `src/passes/` | The optimization passes, one file each, plus `registry.cpp` listing them in order. **See [src/passes/README.md](src/passes/README.md) to add a pass.** |
| `src/tool/` | The `atlas-opt` command line and the HTML viewer |
| `tests/` | C++ timing tests and Python equivalence and regression tests |

## Equivalence tests

`tests/` also has a pytest harness that runs every kernel registered in `third_party/npu_model` (a submodule on its `rtl-match` branch) before and after optimization. It fails if the optimized kernel errors (e.g. breaks a timing rule), doesn't finish, or leaves different bytes in the kernel's DRAM output, its DRAM inputs, or VMEM.

```sh
python -m pytest                              # uses build/atlas-opt if it exists
python -m pytest --atlas-opt=identity         # compare unchanged kernels
python -m pytest --atlas-opt=strip-delays     # exercise missing-delay detection
```

Run it with a Python that has npu_model's dependencies: `uv run pytest` (using this repo's `pyproject.toml`), or `~/Projects/npu_model/.venv/bin/python -m pytest`. The optimizer is invoked as `<cmd> in.S -o out.S` (settable via `--atlas-opt` / `$ATLAS_OPT`, extra flags via `--atlas-opt-args` / `$ATLAS_OPT_ARGS`). `--artifacts-dir DIR` keeps each kernel's `before.S`/`after.S`, and `--max-cycles` sets the cycle budget. A before→after cycle table is printed at the end. `SmolVLARmsNormProgram` has a known baseline golden-output failure. Built-in optimizers skip compiler-specific tests; harness self-tests run independently of `--atlas-opt`.

`tests/test_regressions.py` covers relocation, both branch paths, halt guards, and variable DMA latency. `tests/test_publication*.py` checks data and engine state at the expected completion signal, including MXU source reuse, branches, loops, and DMA channel reuse. C++ tests check timing, metadata, and reservations.
