# Functional / Executable Assembly Contract

**Status:** draft v0, building on the PI's proposal to split functional from
executable assembly.

```
model ──model mapping──▶ kernel.fs.S ──atlas-opt──▶ kernel.es.S ──▶ perf model / RTL
                          functional                 executable
```

- **Model mapping** decides *what* the NPU computes and *where data lives*: fusion,
  tiling, loops, layout, buffer allocation.
- **atlas-opt** decides *when* each instruction issues, so the program runs correctly
  and as fast as possible.
- The functional assembly (FS) is the only interface. It is correct when run one
  instruction at a time and contains no timing.

## 1. Formats

| | Functional (`.fs.S`) | Executable (`.es.S`) |
|---|---|---|
| Written by | model mapping | atlas-opt |
| Semantics | sequential: each instruction finishes before the next starts | npu_model `rtl-match` timing |
| `delay` | not allowed | holds the next instruction N cycles |
| Branches | take effect immediately; no delay slot | one delay slot after a taken branch or jump |
| `dma.wait` | see §2, rule 5 | holds the frontend until the channel is idle |
| Hardware rules | none | every RTL assertion holds (MREG reservations and ports, VPU slots, MXU ports, LSU paths, VMEM banks) |

Both use today's instruction set and syntax (`li`, `nop` and labels allowed). The
double extension keeps existing `.S` tooling working and sorts the two versions of a
kernel together; the build rule is `%.es.S: %.fs.S ; atlas-opt $< -o $@`. Each file
starts with a version comment: `# atlas-fs 0` or `# atlas-es 0`.

Today's hand-scheduled kernels are executable assembly;
`scripts/strip_delays.py kernel.S -o kernel.fs.S` converts one for testing.

**Equivalence:** the `.es.S` leaves the same DRAM contents at exit as the `.fs.S` run
sequentially. Only DRAM is live at exit; registers, VMEM, weight slots and
accumulators may differ. (The tests are stricter today; see §4, item 7.)

## 2. Rules for functional assembly

1. Runs correctly one instruction at a time and `dma.store`s every output to DRAM.
2. No `delay`, and no reliance on a branch delay slot.
3. No dependence on instruction addresses: no `auipc`, and cycle-counter CSRs only for
   diagnostics.
4. Obeys the ISA legality rules: even BF16 register pairs, no pair at m63, `vload` /
   `vstore` 1 KiB aligned within one 256 KiB VMEM bank, and no instruction that
   conflicts with itself (e.g. `vadd.bf16 m4, m0, m32`: m0 and m32 share a physical
   MREG bank).
5. **DMA, v0 (today):** a `dma.wait.chN` follows each `dma.load` / `dma.store` /
   `dma.config` on channel N before anything uses that data, touches that VMEM range,
   or reuses channel N. A DMA's registers (`rd`, `rs1`, `rs2`) stay unchanged until
   its wait, because npu_model reads them when the transfer completes.
   **v1 (PI's target, to be agreed):** no `dma.wait`; a DMA completes when it issues,
   and atlas-opt inserts the waits and picks channels. The functional model must
   define DMA the same way.
6. **Completion:** signal that results are ready with `atlas.complete <value>, <CSR>`
   (immediate 0–31, emitted as `csrrwi`) or `atlas.complete xN, <CSR>` (emitted as
   `csrrw`). An ordinary CSR write is not a completion. Sequentially, all earlier work
   has finished; in v0, every DMA channel that may be pending needs a matching
   `dma.wait` on every path to the completion. A completion gives no host
   acknowledgment, buffer ownership, or proof that the kernel has left its IMEM slot.
   What state is observable at a completion is open (§4, item 7).

atlas-opt rejects `delay`, `auipc`, ISA violations, and a completion that may run with
DMA pending, with the line number. It cannot detect reliance on a delay slot or
violations of rules 1 and 5; the equivalence harness catches those.

## 3. Responsibilities

"May" marks what the contract allows atlas-opt to do; several of these are roadmap
items, not built yet.

| Concern | Model mapping | atlas-opt |
|---|---|---|
| Fusion, tiling, tile sizes | owns | no change |
| Loop order, unrolling, trip counts | owns | keeps loops as written |
| Overlapping iterations (double buffering) | allocates ping-pong buffers, unrolls by 2 | overlaps the unrolled iterations |
| DRAM and VMEM addresses | owns | no change (moving buffers between VMEM banks is open, §4) |
| Which data moves (`dma.load/store`, sizes) | owns | may drop a load whose data is provably still in VMEM |
| `dma.config` / `dma.base` | owns | keeps them, in order |
| DMA channels | any | may reassign |
| `dma.wait` placement | v0: places them (rule 5) | v0: keeps each wait, reorders independent work around it; v1: inserts all |
| Registers (x, m, e) | assigns | may rename any |
| MXU, weight slots, accumulators | any | may move a matmul group to the other MXU; MXU0 and MXU1 round slightly differently, so goldens need a tolerance |
| ISA legality (rule 4) | owns | rejects violations; may later fix self-conflicts by renaming |
| VLS word addresses (`srli x31, x4, 2`) | writes them correctly | may fold the conversion into the offset |
| Instruction order | any correct sequential order | reorders freely within dependences |
| `delay`s and branch delay slots | never writes or relies on them | inserts and fills all of them |
| Finishing in-flight work before `ecall` / `ebreak` | nothing | handles it |
| Completion (`atlas.complete`) | places it after the work it publishes (rule 6) | makes all earlier work, DMA included, finish before the CSR executes; never puts it in a delay slot |
| No-ops | may leave them | removes them |
| Numerics (goldens, tolerances) | owns | no change, except the MXU choice above |
| FS correctness | checked against the golden reference (functional model owner TBD) | nothing |
| ES equivalent to FS | nothing | checked by the equivalence harness on npu_model |

**Rule of thumb:** changing *which* operations run on *which* data belongs to model
mapping. Changing only *when* they run, or which interchangeable resource (register,
channel, MXU, delay slot) they use, belongs to atlas-opt.

## 4. Open items

1. Adopt the `.fs.S` / `.es.S` extensions and the `# atlas-fs 0` header?
2. DMA v0 to v1: when, and who updates the functional model?
3. Who owns the functional model that runs `.fs.S` sequentially? Until it exists, FS
   is only checked by running atlas-opt's output on npu_model.
4. May atlas-opt move VMEM buffers between banks so loads and stores overlap? (Not
   today.)
5. Declaring scratch DRAM: if model mapping marks regions dead at exit, atlas-opt can
   drop stores to them and loads of data it just stored.
6. Where shared pieces live (parser, ISA table, both models); several near-copies
   exist today.
7. **What is live at exit and at a completion?** This contract says only DRAM, but
   the equivalence harness also compares VMEM, a regression test compares scalar
   registers, and the publication tests read MREGs at the completion. Pick one
   definition (DRAM only, or DRAM plus declared output regions) and make the tests
   follow it. It decides which registers atlas-opt may rename and which loads and
   stores it may drop.
