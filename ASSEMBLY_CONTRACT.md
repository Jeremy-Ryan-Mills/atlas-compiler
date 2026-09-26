# Functional / Executable Assembly Contract

**Status:** draft v0 for the team meeting. It builds on the PI's proposal to split
functional assembly from executable assembly.

```
model (PyTorch / MLIR) ──model mapping──▶  kernel.fs.S  ──atlas-opt──▶  kernel.es.S  ──perf model──▶ numbers
                         (high level)      functional      (low level)   executable
```

- **Model mapping** decides *what* the NPU computes and *where the data lives*:
  fusion, tiling, loop structure, data layout, buffer allocation.
- **atlas-opt** decides *when* each instruction issues, so the hardware runs the
  functional program correctly and as fast as possible.
- The functional assembly (FS) is the only thing the two sides exchange. It must be
  correct when run one instruction at a time, and it knows nothing about timing.

## 1. File extensions

| Format | Extension | Example |
|---|---|---|
| Functional assembly (model mapping → atlas-opt) | `.fs.S` | `gemma_mlp.fs.S` |
| Executable assembly (atlas-opt → perf model / RTL) | `.es.S` | `gemma_mlp.es.S` |

Why a double extension ending in `.S`:
- **Tooling keeps working.** npu_model's editor extension and language server
  already recognize `.S`, and npu_model's `load_asm` takes any path. A bare `.fs` would
  need tooling changes, and it collides with F# and GLSL fragment shaders (`.es` with
  ECMAScript).
- **The two versions of a kernel sort next to each other**, and a build rule is one
  line: `%.es.S: %.fs.S ; atlas-opt $< -o $@`.
- **It uses the PI's FS / ES names.**

Existing hand-scheduled kernels (today's `*.S` in npu_model) are executable
assembly. `scripts/strip_delays.py kernel.S -o kernel.fs.S` turns one into
functional assembly, for testing.

Each file should start with a one-line version comment so tools can reject a file
written for a different version of this contract: `# atlas-fs 0` or `# atlas-es 0`.

## 2. The two formats

**Functional assembly (`.fs.S`)** has *sequential semantics*. Every instruction
finishes before the next one starts, so a later instruction always sees the effect
of an earlier one.
- Same instruction set and syntax as today's assembly. `li`, `nop` and labels are
  allowed.
- **No `delay`.**
- **No branch delay slot.** A branch or jump takes effect immediately. The
  instruction after a branch runs only if the branch is not taken.
- **DMA:** see §4 (v0 keeps `dma.wait`; v1 removes it).

**Executable assembly (`.es.S`)** has the timing semantics of the hardware
(npu_model `rtl-match`):
- `delay N` holds the next instruction for N cycles.
- A taken branch or jump runs exactly one delay-slot instruction first.
- `dma.wait` holds the frontend until its channel is idle.
- Every rule the RTL asserts on holds: MREG reservations and ports, VPU slots, MXU
  ports, LSU paths, VMEM banks.

**Equivalence.** Running the `.es.S` leaves the same DRAM contents at program exit as
running the `.fs.S` one instruction at a time. Only DRAM is live at exit.
Registers, VMEM, MXU weight slots and accumulators may end up different. (The
equivalence harness is stricter today and also compares VMEM.)

## 3. Who handles what

"May" in the atlas-opt column means the contract allows it. Several of those
(dropping loads, reassigning channels, renaming registers, switching MXUs, folding
address conversions) are on atlas-opt's roadmap rather than built today.

| Concern | Model mapping (writes `.fs.S`) | atlas-opt (writes `.es.S`) |
|---|---|---|
| Operator fusion, tiling, tile sizes | ✅ owns | — |
| Loop structure: order, unrolling, trip counts | ✅ owns | keeps loops as written (no unrolling, no new loops) |
| Overlapping loop iterations (double buffering) | allocates the ping-pong buffers and unrolls by 2 | overlaps the unrolled iterations |
| DRAM layout and addresses | ✅ owns | never changes them |
| VMEM buffer allocation and addresses | ✅ owns | never changes them (moving buffers between banks is open, §5) |
| Which data to move (`dma.load/store`, sizes) | ✅ owns | may drop a load whose data is provably still in VMEM |
| `dma.config` / `dma.base` | ✅ owns | keeps them, in order |
| DMA channel numbers | picks any channels | may reassign channels |
| `dma.wait` placement | v0: places a wait before data is used (§4) | v0: keeps each wait and reorders independent work around it; v1: inserts all waits |
| Register allocation (x, m, e) | assigns registers | may rename any register |
| MXU choice (`.mxu0` / `.mxu1`), weight slots, accumulators | picks any | may move a matmul group to the other MXU. MXU0 and MXU1 are treated as equivalent, but their rounding differs slightly, so goldens need a tolerance |
| ISA legality of each instruction (even BF16 register pairs, no pair at m63, `vload`/`vstore` 1 KiB aligned within one 256 KiB VMEM bank) | ✅ owns; atlas-opt rejects violations | — |
| An instruction that conflicts with itself (e.g. `vadd.bf16 m4, m0, m32`: m0 and m32 share a physical MREG bank) | avoids it for now | rejects it today; could fix it by renaming later |
| Word vs. byte addresses (`vload`/`vstore` take word addresses, e.g. `srli x31, x4, 2`) | writes correct addresses | may fold the conversion into the offset |
| Instruction order within the dependencies | writes any correct sequential order | reorders freely |
| `delay`s | never writes them | ✅ inserts all of them |
| Branch delay slots | never relies on them | ✅ adds and fills them |
| Letting in-flight work finish before `ecall` / `ebreak` | — | ✅ |
| No-op instructions | may leave them | removes them |
| Numerics | ✅ owns (goldens, tolerances) | never changes them, except the MXU choice above |
| Correctness of the functional program | ✅ checked against the golden reference (with a functional model once one exists; owner TBD) | — |
| ES equivalent to FS | — | ✅ checked by the equivalence harness on npu_model |

Rule of thumb: if a change alters *which* operations run on *which* data, it
belongs to model mapping. If it only alters *when* they run, or *which* physically
interchangeable resource (a register, channel, MXU or delay slot) they use, it
belongs to atlas-opt.

## 4. Rules for functional assembly

A `.fs.S` file must:
1. Run correctly one instruction at a time, and leave its results in DRAM (a
   `dma.store` of every output).
2. Contain no `delay`, and never rely on a branch delay slot.
3. Not depend on instruction addresses: no `auipc`, and no reading the cycle-counter
   CSRs for anything but diagnostics. atlas-opt changes both.
4. Satisfy the ISA legality rules in §3.
5. **DMA, v0 (today):**
   - put a `dma.wait.chN` after each `dma.load` / `dma.store` / `dma.config` on
     channel N before anything uses that data, touches that VMEM range, or reuses
     channel N;
   - not change a DMA's source registers (`rd`, `rs1`, `rs2`) before its wait. npu_model
     reads them when the transfer completes.

   **DMA, v1 (the PI's target, to be agreed):** no `dma.wait` at all. A DMA
   completes the moment it issues, and atlas-opt inserts the waits (and picks the
   channels). This needs the functional model to define DMA the same way.

atlas-opt rejects `delay`, `auipc`, and instructions that break the ISA rules,
with the line number. It cannot tell whether a program relies on a delay slot or
breaks rule 1 or 5; the equivalence harness catches those.

## 5. Open items for the meeting

1. **Extensions and the version header**: adopt `.fs.S` / `.es.S` and `# atlas-fs 0`?
2. **`dma.wait` in functional assembly** (v0 → v1 above): when, and who changes the
   functional model.
3. **Who owns the functional model** that runs `.fs.S` one instruction at a time
   (PI's point 2). Until it exists, FS can only be checked by running atlas-opt's
   output on npu_model.
4. **May atlas-opt move VMEM buffers between banks**, so loads and stores in
   different banks overlap? Today it may not.
5. **Declaring scratch DRAM.** If model mapping marks DRAM regions that are dead at
   exit, atlas-opt could drop stores to them and loads of data it just stored.
6. **Where the shared pieces live** (parser, ISA table, both models). That's the PI's
   point 3: several copies of very similar code today.
