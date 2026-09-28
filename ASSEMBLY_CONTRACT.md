# Functional / Executable Assembly Contract

```
model ──model mapping──▶ functional .S ──atlas-opt──▶ executable .S ──▶ perf model / RTL
```

- **Model mapping** decides *what* the NPU computes and *where data lives*: fusion,
  tiling, loops, layout, buffer allocation, DMA channels.
- **atlas-opt** decides *when* each instruction issues, so the program runs correctly
  and as fast as possible (`atlas-opt in.S -o out.S`).
- The functional assembly (FS) is the only interface. It carries no timing that
  atlas-opt relies on.

## 1. Formats

| | Functional (input) | Executable (output) |
|---|---|---|
| Written by | model mapping (today's hand-scheduled kernels are also accepted) | atlas-opt |
| Semantics | one instruction at a time, except the branch delay slot (below) | npu_model `rtl-match` timing |
| `delay` | ignored and removed, except `delay N # keep` | inserted: the minimum needed |
| Branches and jumps | the next instruction is the delay slot: it runs on both paths | same; empty slots are filled with independent scalar work |
| `dma.wait` | optional (§2, rule 5) | present wherever the program needs a transfer finished |
| Hardware rules | none | every RTL assertion holds (MREG reservations and ports, VPU slots, MXU ports, LSU paths, VMEM banks), for any DMA latency by default |

Both use today's instruction set and syntax (`li`, `nop` and labels allowed).

**Equivalence:** the executable program leaves the same DRAM contents at exit as the
functional one. Only DRAM is live at exit; registers, VMEM, weight slots and
accumulators may differ. (The tests are stricter today; see §4, item 6.)

## 2. Rules for functional assembly

1. Runs correctly one instruction at a time (with the delay slot) and `dma.store`s
   every output to DRAM.
2. Correct without its `delay`s: atlas-opt removes them (keeping `delay N # keep`) and
   computes its own timing.
3. No dependence on instruction addresses: no `auipc`, `jalr`, or `jal` with a
   nonzero link register, and cycle-counter CSRs only for diagnostics. A delay slot
   may not hold a halt or a `delay N # keep`.
4. Obeys the ISA legality rules: even BF16 register pairs, no pair at m63, `vload` /
   `vstore` 1 KiB aligned within one 256 KiB VMEM bank, and no instruction that
   conflicts with itself (e.g. `vadd.bf16 m4, m0, m32`: m0 and m32 share a physical
   MREG bank).
5. **DMA:** `dma.wait` is optional; a DMA may be treated as complete once it issues.
   atlas-opt inserts `dma.wait.chN`, on every path including loop back-edges, before
   any instruction that:
   - touches the transfer's VMEM range (an unknown address counts as touching it);
   - writes a register the DMA reads (`rd`, `rs1`, `rs2`, or `dma.config`'s source,
     all read when the transfer completes);
   - issues another command on channel N, or a DMA whose VMEM range overlaps it;
   - is a completion (rule 6), `ecall` or `ebreak`, or the end of the program.

   Written waits are kept. Channels are used as written, and commands on different
   channels keep their queue order (including `dma.config`). The DMA queue is assumed
   idle at program entry.
6. **Completion:** mark the CSR write that signals completion with `# atlas.release`,
   e.g. `csrrwi x0, x1, 0xC10 # atlas.release`. Unmarked CSR writes are ordinary.
   Before the marked CSR executes, all earlier fixed-latency work has finished and
   atlas-opt has inserted waits for any pending DMA. A completion gives no host
   acknowledgment, buffer ownership, or proof that the kernel has left its IMEM slot.
   What state is observable at a completion is open (§4, item 6).

atlas-opt rejects address-dependent instructions, illegal delay slots and ISA
violations with the line number. It cannot detect a program that is wrong when run
sequentially; the equivalence harness catches that.

## 3. Responsibilities

| Concern | Model mapping | atlas-opt |
|---|---|---|
| Fusion, tiling, tile sizes | owns | no change |
| Loop order, unrolling, trip counts | owns | keeps loops as written |
| Overlapping iterations (double buffering) | allocates ping-pong buffers, unrolls | overlaps work within a basic block; fixed-latency engines drain at block ends, DMA stays in flight until its wait |
| DRAM and VMEM addresses | owns | no change |
| Which data moves (`dma.load/store`, sizes) | owns | no change |
| `dma.config` / `dma.base` | owns | keeps them, in queue order |
| DMA channels | picks them | keeps them |
| `dma.wait` | optional | inserts missing waits, keeps written ones, and moves independent work around them |
| Registers (x, m, e), MXU choice, weight slots, accumulators | assigns | no change |
| ISA legality (rule 4) | owns | rejects violations |
| VLS word addresses (`srli x31, x4, 2`) | writes them correctly | no change |
| Instruction order | any correct sequential order | reorders within dependences in each basic block |
| `delay`s and delay slots | may leave them; atlas-opt ignores the delays | inserts the minimum delays; fills empty slots |
| Finishing in-flight work before `ecall` / `ebreak` | nothing | drains fixed-latency work and waits for DMA |
| Completion (`# atlas.release`) | marks the CSR (rule 6) | finishes all earlier work, DMA included, before it; rejects one in a delay slot |
| No-ops | may leave them | removes filler no-ops |
| Numerics (goldens, tolerances) | owns | no change |
| FS correctness | checked against the golden reference (functional model owner TBD) | nothing |
| ES equivalent to FS | nothing | checked on npu_model: the equivalence harness, and wait insertion against explicit-wait references at 1x and 100x DMA latency |

**Rule of thumb:** changing *which* operations run on *which* data belongs to model
mapping. Changing only *when* they run, or which interchangeable resource they use,
belongs to atlas-opt.

The contract also allows atlas-opt, later, to rename registers, reassign DMA
channels, move matmul groups between MXUs, fold VLS address conversions, and drop
loads whose data is provably still in VMEM. None of these is on this branch.

## 4. Open items

1. **Delay slots in functional assembly.** This branch keeps the hardware's delay
   slot in the input; the `clean-up` branch drops it (a branch takes effect
   immediately). Pick one.
2. **Completion syntax.** Main replaced `# atlas.release` with the `atlas.complete`
   instruction (#9); this branch predates it. Rebase onto main.
3. **Functional model.** Who owns the model that runs FS sequentially, and does it
   treat a DMA as complete when it issues (rule 5)? Until it exists, FS is only
   checked by running atlas-opt's output on npu_model.
4. File extensions (`.fs.S` / `.es.S`) and a version header (`# atlas-fs 0`), as on
   `clean-up`. Not implemented here.
5. May atlas-opt move VMEM buffers between banks so loads and stores overlap? Declaring
   scratch DRAM would also let it drop stores to regions dead at exit.
6. **What is live at exit and at a completion?** This contract says only DRAM, but
   the equivalence harness also compares VMEM, a regression test compares scalar
   registers, and the publication tests read MREGs at the completion. Pick one
   definition (DRAM only, or DRAM plus declared output regions) and make the tests
   follow it. It decides which registers atlas-opt may rename and which loads and
   stores it may drop.
7. Where shared pieces live (parser, ISA table, both models); several near-copies
   exist today.
