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
| Branches and jumps | take effect immediately; no delay slot | one delay slot after each branch or jump, filled with independent scalar work or a `nop` |
| `dma.wait` | none | wherever the program needs a transfer finished |
| Hardware rules | none | every RTL assertion holds (MREG reservations and ports, VPU slots, MXU ports, LSU paths, VMEM banks), for any DMA latency by default |

Both use today's instruction set and syntax (`li`, `nop` and labels allowed). The
extensions are a naming convention; atlas-opt takes any path
(`atlas-opt kernel.fs.S -o kernel.es.S`).

**Version header:** a file may start with `# atlas-fs 0` or `# atlas-es 0`. The
header is optional. atlas-opt rejects one of the wrong kind or for another contract
version, and starts its output with `# atlas-es 0`.

**Equivalence:** the executable program leaves the same DRAM contents as the
functional one, at exit and at every completion. Only DRAM is live; registers, VMEM,
weight slots and accumulators may differ, and the tests compare DRAM alone.

## 2. Rules for functional assembly

1. Runs correctly one instruction at a time and `dma.store`s every output to DRAM.
2. No `delay`, no `dma.wait`, and no delay slots: a branch or jump takes effect
   immediately, and the instructions after it run only when it is not taken.
3. No dependence on instruction addresses: no `auipc`, `jalr`, or `jal` with a
   nonzero link register, and cycle-counter CSRs only for diagnostics.
4. Obeys the ISA legality rules: even BF16 register pairs, no pair at m63, `vload` /
   `vstore` 1 KiB aligned within one 256 KiB VMEM bank, and no instruction that
   conflicts with itself (e.g. `vadd.bf16 m4, m0, m32`: m0 and m32 share a physical
   MREG bank).
5. **DMA:** write `dma.load` / `dma.store` / `dma.config` on any channel and use the
   data, registers and channel right after, as if the transfer had finished. atlas-opt
   inserts `dma.wait.chN`, on every path including loop back-edges, before any
   instruction that:
   - touches the transfer's VMEM range (an unknown address counts as touching it);
   - writes a register the DMA reads (`rd`, `rs1`, `rs2`, or `dma.config`'s source,
     all read when the transfer completes);
   - issues another command on channel N, or a DMA whose VMEM range overlaps it;
   - is a completion (rule 6), `ecall` or `ebreak`, or the end of the program.

   Channels are kept as written for now, and commands on different channels keep
   their queue order (including `dma.config`). The DMA queue is assumed idle at
   program entry.
6. **Completion:** signal that results are ready with `atlas.complete <value>, <CSR>`
   (an immediate 0–31) or `atlas.complete xN, <CSR>`, e.g. `atlas.complete 1, 0xC10`.
   atlas-opt emits it as `csrrwi` / `csrrw` tagged `# atlas.complete`. A plain CSR
   write is not a completion. Only DRAM is observable at a completion: every earlier
   `dma.store` has reached DRAM before it executes (atlas-opt also lets all other
   earlier work finish). A completion gives no host acknowledgment, buffer ownership,
   or proof that the kernel has left its IMEM slot.

atlas-opt rejects `delay`, address-dependent instructions and ISA violations with the
line number. It cannot detect a program that is wrong when run one instruction at a time;
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
| ISA legality (rule 4) | owns | rejects violations |
| VLS word addresses (`srli x31, x4, 2`) | writes them correctly | no change |
| Instruction order | any correct sequential order | reorders within dependences in each basic block |
| `delay`s | never writes them | inserts the minimum |
| Branch delay slots | never writes or relies on them | adds one after each branch or jump and fills it with independent scalar work |
| Finishing in-flight work before `ecall` / `ebreak` | nothing | drains fixed-latency work and waits for DMA |
| Completion (`atlas.complete`) | writes it after the work it publishes (rule 6) | finishes all earlier work, DMA included, before it |
| No-ops | may leave them | removes filler no-ops |
| Numerics (goldens, tolerances) | owns | no change |
| FS correctness | checked against the golden reference with a functional model (needed; owner TBD, §4) | nothing |
| ES equivalent to FS | nothing | checked on npu_model by comparing DRAM: the equivalence harness and the wait-insertion tests (at 1x and 100x DMA latency) |

**Rule of thumb:** changing *which* operations run on *which* data belongs to model
mapping. Changing only *when* they run, or which interchangeable resource they use,
belongs to atlas-opt.

The contract also allows atlas-opt, later, to rename registers, reassign DMA
channels, move matmul groups between MXUs, fold VLS address conversions, and drop
loads whose data is provably still in VMEM. None of these is implemented yet.

## 4. Open items

1. **Functional model.** A model that runs `.fs.S` one instruction at a time, with a
   DMA complete when it issues (rule 5), is needed. Who builds and owns it? Until it
   exists, FS is only checked by running atlas-opt's output on npu_model.
2. Where shared pieces live (parser, ISA table, both models); several near-copies
   exist today.
