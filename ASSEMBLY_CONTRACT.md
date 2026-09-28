# Functional / Executable Assembly Contract

```
model ──model mapping──▶ kernel.fs.S ──atlas-opt──▶ kernel.es.S ──▶ perf model / RTL
                          functional                 executable
```

- **Model mapping** decides *what* the NPU computes and *where data lives*: fusion,
  tiling, loops, layout, buffer allocation.
- **atlas-opt** decides *when* each instruction issues: it reorders the program within
  its dependences so the engines overlap, inserts every `dma.wait`, fills every branch
  delay slot, and adds the minimum `delay`s.
- The functional assembly (FS) is the only interface. It contains no timing.

## 1. Formats

| | Functional (`.fs.S`) | Executable (`.es.S`) |
|---|---|---|
| Written by | model mapping | atlas-opt |
| Semantics | one instruction at a time; a DMA is complete when it issues | the hardware's timing, as modeled by npu_model |
| `delay` | none | the minimum needed |
| Branches and jumps | followed by a `nop`, the empty delay slot | slot filled with independent scalar work, or left `nop` |
| `dma.wait` | none | wherever the program needs a transfer finished |
| Hardware rules | none | every RTL assertion holds (MREG reservations and ports, VPU slots, MXU ports, LSU paths, VMEM banks), for any DMA latency by default |

Both use today's instruction set and syntax (`li`, `nop` and labels allowed). The
extensions are a naming convention; atlas-opt takes any path
(`atlas-opt kernel.fs.S -o kernel.es.S`).

**Equivalence:** the executable program leaves the same DRAM contents at exit as the
functional one. Only DRAM is live at exit; registers, VMEM, weight slots and
accumulators may differ. (The tests are stricter today; see §4, item 3.)

## 2. Rules for functional assembly

1. Runs correctly one instruction at a time and `dma.store`s every output to DRAM.
2. No `delay` and no `dma.wait`.
3. Every branch and jump is followed by a `nop`. atlas-opt owns the delay slot and
   decides what runs in it; model mapping never puts work there.
4. No dependence on instruction addresses: no `auipc`, `jalr`, or `jal` with a
   nonzero link register, and cycle-counter CSRs only for diagnostics.
5. Obeys the ISA legality rules: even BF16 register pairs, no pair at m63, `vload` /
   `vstore` 1 KiB aligned within one 256 KiB VMEM bank, and no instruction that
   conflicts with itself (e.g. `vadd.bf16 m4, m0, m32`: m0 and m32 share a physical
   MREG bank).
6. **DMA:** write `dma.load` / `dma.store` / `dma.config` on any channel and use the
   data, registers and channel right after, as if the transfer had finished. atlas-opt
   inserts `dma.wait.chN`, on every path including loop back-edges, before any
   instruction that:
   - touches the transfer's VMEM range (an unknown address counts as touching it);
   - writes a register the DMA reads (`rd`, `rs1`, `rs2`, or `dma.config`'s source,
     all read when the transfer completes);
   - issues another command on channel N, or a DMA whose VMEM range overlaps it;
   - is a completion (rule 7), `ecall` or `ebreak`, or the end of the program.

   Channels are kept as written for now, and commands on different channels keep
   their queue order (including `dma.config`). The DMA queue is assumed idle at
   program entry.
7. **Completion:** mark the CSR write that signals completion with `# atlas.release`,
   e.g. `csrrwi x0, x1, 0xC10 # atlas.release`. Unmarked CSR writes are ordinary.
   Before the marked CSR executes, all earlier work has finished, DMA included. A
   completion gives no host acknowledgment, buffer ownership, or proof that the kernel
   has left its IMEM slot. What state is observable at a completion is open (§4,
   item 3).

atlas-opt rejects address-dependent instructions and ISA violations with the line
number. It cannot detect a program that is wrong when run one instruction at a time;
the equivalence harness catches that.

## 3. Responsibilities

| Concern | Model mapping | atlas-opt |
|---|---|---|
| Fusion, tiling, tile sizes | owns | no change |
| Loop order, unrolling, trip counts | owns | keeps loops as written |
| Overlapping iterations (double buffering) | allocates ping-pong buffers, unrolls | overlaps work within a basic block; fixed-latency engines drain at block ends, DMA stays in flight until its wait |
| DRAM and VMEM addresses | owns | no change |
| Which data moves (`dma.load/store`, sizes) | owns | no change |
| `dma.config` / `dma.base` | owns | keeps them, in queue order |
| DMA channels | writes any channel (the syntax requires one) | may reassign; not implemented yet (channels are kept as written) |
| `dma.wait` | never writes them | inserts all of them, placed so independent work overlaps the transfer |
| Registers (x, m, e), MXU choice, weight slots, accumulators | assigns | no change |
| ISA legality (rule 5) | owns | rejects violations |
| VLS word addresses (`srli x31, x4, 2`) | writes them correctly | no change |
| Instruction order | any correct sequential order | reorders within dependences in each basic block |
| `delay`s | never writes them | inserts the minimum |
| Branch delay slots | writes a `nop` after each branch or jump | fills each slot with independent scalar work |
| Finishing in-flight work before `ecall` / `ebreak` | nothing | drains fixed-latency work and waits for DMA |
| Completion (`# atlas.release`) | marks the CSR (rule 7) | finishes all earlier work, DMA included, before it |
| No-ops | may leave them | removes filler no-ops |
| Numerics (goldens, tolerances) | owns | no change |
| FS correctness | checked against the golden reference (functional model owner TBD) | nothing |
| ES equivalent to FS | nothing | checked on npu_model by the equivalence harness and the wait-insertion tests (at 1x and 100x DMA latency) |

**Rule of thumb:** changing *which* operations run on *which* data belongs to model
mapping. Changing only *when* they run, or which interchangeable resource they use,
belongs to atlas-opt.

The contract also allows atlas-opt, later, to rename registers, reassign DMA
channels, move matmul groups between MXUs, fold VLS address conversions, and drop
loads whose data is provably still in VMEM. None of these is implemented yet.

## 4. Open items

1. **Delay-slot placeholder.** Rule 3 still asks model mapping for a `nop` after each
   branch or jump. Dropping it, so a branch in `.fs.S` takes effect immediately, would
   leave delay slots entirely to atlas-opt.
2. **Functional model.** Who owns the model that runs `.fs.S` one instruction at a
   time, with a DMA complete when it issues (rule 6)? Until it exists, FS is only
   checked by running atlas-opt's output on npu_model.
3. **What is live at exit and at a completion?** This contract says only DRAM, but
   the equivalence harness also compares VMEM, a regression test compares scalar
   registers, and the publication tests read MREGs at the completion. Pick one
   definition (DRAM only, or DRAM plus declared output regions) and make the tests
   follow it. It decides which registers atlas-opt may rename and which loads and
   stores it may drop.
4. A version header (`# atlas-fs 0`) so tools can reject files for another contract
   version. Not implemented.
5. Where shared pieces live (parser, ISA table, both models); several near-copies
   exist today.
