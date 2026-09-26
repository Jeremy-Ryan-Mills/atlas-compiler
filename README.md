# atlas-compiler-experiments

`atlas-opt` turns **functional assembly** into **executable assembly** for the Atlas
NPU. Functional assembly runs correctly one instruction at a time and knows nothing
about the microarchitecture: it has no `delay`s, and a branch takes effect
immediately (no delay slot). `atlas-opt` builds a dependency graph for every basic
block, reorders the code so the engines overlap, fills branch delay slots, and adds
the minimum `delay`s so that every rule of npu_model's `rtl-match` branch holds.
`dma.wait` is still written in the functional assembly for now.

In the toolchain: the model compiler produces functional assembly, `atlas-opt`
turns it into executable assembly, and the performance model runs that.
[PLAN.md](.agents/notes/PLAN.md) has the design and roadmap;
[OPEN_QUESTIONS.md](.agents/notes/OPEN_QUESTIONS.md) lists what waits until the model
is confirmed RTL accurate.

```sh
git submodule update --init -- third_party/npu_model     # npu_model at rtl-match
cmake -S . -B build -G Ninja && cmake --build build
scripts/strip_delays.py kernel.S -o kernel.fs.S          # hand-scheduled kernel -> functional assembly
build/atlas-opt kernel.fs.S -o kernel.es.S --viz kernel.html
```

| Option | What it does |
|---|---|
| `-o FILE` | write the executable assembly (otherwise it is printed) |
| `--viz FILE.html` | dependency graph viewer (open it in a browser) |
| `--passes a,b,c` / `--list-passes` | run only some passes / list them |
| `--dma-timing model` | trust npu_model's DMA latency instead of staying valid for any latency |

Input that still contains `delay`s is rejected, and so are `auipc`, `jalr`, and `jal`
with a nonzero link register: moving code changes instruction addresses, so nothing
may depend on them. After optimizing, atlas-opt simulates the executable assembly (at
npu_model's DMA speed and with slower DMA) and exits with an error if any timing rule
is broken. A halt does not wait for a pending `delay`, so the output ends such a delay
one cycle early with a `nop` guard. The viewer shows each block's dependency graph
twice: the input in program order, and the output placed at the cycle each
instruction issues (one lane per engine). Select an instruction to see what it waits
for and why.

### Completion signals

Mark a completion CSR with `# atlas.release`:

```asm
vstore m0, 0(x0)
csrrwi x0, x1, 0xC10 # atlas.release
```

All earlier fixed-latency work finishes before the CSR executes. Each DMA channel
that may still be busy needs a matching `dma.wait` on every path to the release and
before the channel is reused; atlas-opt rejects the program otherwise. A release
requires the `schedule` pass. Keep the exact, case-sensitive, whitespace-delimited
`atlas.release` token when preprocessing; printing and re-optimizing keep it.
Releases assume nothing is running when the program starts, and unmarked CSR writes
are not completion signals. A release gives no host acknowledgment, buffer
ownership, or proof that execution left an IMEM slot.

## Layout

| Folder | Contents |
|---|---|
| `src/core/` | Everything about programs and the machine: assembly parsing and printing (`asm`), basic blocks (`blocks`), known register values (`values`), the rtl-match timing rules (`machine`, `reservations`), dependency graphs (`depgraph`), and a timing simulator whose cycle counts match npu_model's (`simulator`) |
| `src/passes/` | The optimization passes, one file each, plus `registry.cpp` listing them in order. **See [src/passes/README.md](src/passes/README.md) to add a pass.** |
| `src/tool/` | The `atlas-opt` command line and the HTML viewer |
| `scripts/` | `strip_delays.py`: removes the `delay`s from an assembly file |
| `tests/` | C++ timing tests and Python equivalence and regression tests |

## Equivalence tests

`tests/` also has a pytest harness. For every kernel registered in
`third_party/npu_model` (a submodule on its `rtl-match` branch), it runs npu_model's
hand-scheduled kernel, removes its delays with `scripts/strip_delays.py`, hands the
functional assembly to the optimizer, and runs the executable assembly it returns.
It fails if that errors (e.g. breaks a timing rule), doesn't finish, or leaves
different bytes in the kernel's DRAM output, its DRAM inputs, or VMEM. The cycle
table printed at the end compares against the hand-scheduled kernels.

```sh
python -m pytest                              # uses build/atlas-opt if it exists
python -m pytest --atlas-opt=identity         # harness sanity check: everything fails
```

Run it with a Python that has npu_model's dependencies: `uv run pytest` (using this
repo's `pyproject.toml`), or `~/Projects/npu_model/.venv/bin/python -m pytest`.
The optimizer is invoked as `<cmd> in.S -o out.S` (settable via `--atlas-opt` /
`$ATLAS_OPT`, extra flags via `--atlas-opt-args` / `$ATLAS_OPT_ARGS`).
`--artifacts-dir DIR` keeps each kernel's `original.S`, `functional.S` and
`executable.S`, and `--max-cycles` sets the cycle budget.
`SmolVLARmsNormProgram` fails as a BASELINE error: on rtl-match, the unmodified
kernel already misses its golden output.

`tests/test_regressions.py` covers relocation, both branch paths, halt guards, and
variable DMA latency. `tests/test_publication*.py` checks data and engine state at
the expected completion signal, including MXU source reuse, branches, loops, and DMA
channel reuse.
