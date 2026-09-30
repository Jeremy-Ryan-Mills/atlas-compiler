#include "core/model_dump.h"

#include <sstream>
#include <stdexcept>

#include "core/depgraph.h"
#include "core/dma_flow.h"

namespace {
std::string quote(const std::string& text) {
    std::string result = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (unsigned char c : text) {
        if (c == '"' || c == '\\') result += '\\', result += c;
        else if (c < 0x20) {
            result += "\\u00";
            result += hex[c >> 4];
            result += hex[c & 15];
        } else result += c;
    }
    return result + '"';
}

const char* resourceName(Res resource) {
    switch (resource) {
        case Res::XReg: return "XReg";
        case Res::EReg: return "EReg";
        case Res::MReg: return "MReg";
        case Res::Acc: return "Acc";
        case Res::Weight: return "Weight";
        case Res::Vmem: return "Vmem";
        case Res::DmaBase: return "DmaBase";
        case Res::Dram: return "Dram";
    }
    throw std::runtime_error("unhandled footprint resource");
}

template <typename T>
void numbers(std::ostream& out, const std::vector<T>& values) {
    out << '[';
    for (size_t i = 0; i < values.size(); ++i) out << (i ? "," : "") << values[i];
    out << ']';
}

void writeFootprint(std::ostream& out, const Footprint& f) {
    out << "{\"accesses\":[";
    for (size_t i = 0; i < f.accesses.size(); ++i) {
        const Access& a = f.accesses[i];
        out << (i ? "," : "") << "{\"resource\":" << quote(resourceName(a.res))
            << ",\"write\":" << a.write << ",\"first\":" << a.first << ",\"count\":" << a.count
            << ",\"age\":" << a.age << ",\"step\":" << a.step << ",\"anywhere\":" << a.anywhere
            << ",\"at_completion\":" << a.atCompletion << ",\"dram_first_byte\":" << a.dramFirstByte
            << ",\"dram_bytes\":" << a.dramBytes << '}';
    }
    out << "],\"holds\":[";
    for (size_t i = 0; i < f.holds.size(); ++i) {
        const Hold& h = f.holds[i];
        out << (i ? "," : "") << "{\"unit\":" << quote(unitName(h.unit))
            << ",\"index\":" << h.index << ",\"from\":" << h.from << ",\"to\":" << h.to
            << ",\"alt\":" << h.alt << ",\"capacity\":" << unitCapacity(h.unit, h.index) << '}';
    }
    out << "],\"mreg_reads\":";
    numbers(out, f.mregReads);
    out << ",\"mreg_writes\":";
    numbers(out, f.mregWrites);
    out << ",\"read_release\":" << f.readRelease << ",\"write_release\":" << f.writeRelease
        << ",\"write_during_read\":" << f.writeDuringRead << ",\"vpu_live\":" << f.vpuLive
        << ",\"done_age\":" << f.doneAge << ",\"dma_cycles_estimate\":" << f.dmaCycles << '}';
}
}  // namespace

std::string dumpFootprints(const AsmProgram& program, const MachineModel& model) {
    const Code code = buildBlocks(program);
    const auto entries = blockEntryValues(code, model.rtlDmaRanges);
    const uint32_t dmaRegs = dmaOperandRegisters(program.instrs);
    if (model.rtlDma) validateDmaFlow(code, model);
    const DmaFlow dmaFlow = model.rtlDma ? analyzeDmaFlow(code, model) : DmaFlow{};
    std::ostringstream out;
    out << std::boolalpha << "{\"schema\":\"atlas.footprints.v1\",\"schedule_validated\":false,"
        << "\"model\":{\"name\":" << quote(model.name)
        << ",\"source_ir_sha256\":" << quote(model.sourceIrSha256)
        << ",\"mxu0_first_write_age\":" << model.mxu0FirstWriteAge
        << ",\"mxu1_first_write_age\":" << model.mxu1FirstWriteAge
        << ",\"mxu1_overwrite_acc_read_hold\":" << model.mxu1OverwriteAccReadHold
        << ",\"mxu0_overwrite_acc_read_hold\":" << model.mxu0OverwriteAccReadHold
        << ",\"rtl_dma\":" << model.rtlDma << ",\"rtl_dma_ranges\":" << model.rtlDmaRanges
        << ",\"rtl_lsu\":" << model.rtlLsu << ",\"rtl_scalar_lsu\":" << model.rtlScalarLsu
        << ",\"scalar_memory_age\":" << model.scalarMemoryAge
        << ",\"scalar_load_write_age\":" << model.scalarLoadWriteAge
        << ",\"scalar_load_first_free_age\":" << model.scalarLoadFirstFreeAge
        << ",\"vload_read_age\":" << model.vloadReadAge << ",\"vload_write_age\":" << model.vloadWriteAge
        << ",\"vload_first_free_age\":" << model.vloadFirstFreeAge
        << ",\"vstore_read_age\":" << model.vstoreReadAge << ",\"vstore_write_age\":" << model.vstoreWriteAge
        << ",\"vstore_first_free_age\":" << model.vstoreFirstFreeAge
        << ",\"rtl_xlu\":" << model.rtlXlu
        << ",\"xlu_read_age\":" << model.xluReadAge << ",\"xlu_write_age\":" << model.xluWriteAge
        << ",\"xlu_first_free_age\":" << model.xluFirstFreeAge
        << ",\"xlu_read_release_age\":" << model.xluReadReleaseAge
        << ",\"xlu_write_release_age\":" << model.xluWriteReleaseAge
        << ",\"rtl_vpu\":" << model.rtlVpu << ",\"vpu_read_age\":" << model.vpuReadAge
        << ",\"vpu_simple_write_age\":" << model.vpuSimpleWriteAge
        << ",\"vpu_row_sum_write_age\":" << model.vpuRowSumWriteAge
        << ",\"vpu_column_write_age\":" << model.vpuColumnWriteAge
        << ",\"vpu_pack_write_age\":" << model.vpuPackWriteAge
        << ",\"vpu_unpack_write_age\":" << model.vpuUnpackWriteAge
        << ",\"vpu_immediate_write_age\":" << model.vpuImmediateWriteAge << "},"
        << "\"semantics\":{\"age_origin\":\"instruction issue\",\"hold_end\":\"inclusive\","
        << "\"entry_state\":\"blockEntryValues: zero scalar registers at program entry, joined CFG values thereafter\","
        << "\"unknown_value\":null,\"lsu_scope\":"
        << quote(model.rtlLsu ? "derived VLOAD/VSTORE access and physical hold timing; logical MREG reservation and visibility rules inherited"
                             : "inherited VLOAD/VSTORE timing and rules")
        << ",\"xlu_scope\":"
        << quote(model.rtlXlu ? "derived VTRPOSE.XLU timing; logical reservation releases enforce frontend assertions, with conservative floors for legacy profiles; same-cycle visibility remains strict"
                             : "inherited VTRPOSE.XLU timing and rules")
        << ",\"scalar_lsu_scope\":"
        << quote(model.rtlScalarLsu ? "derived accepted-command request and ScalarCore writeback timing; one-cycle VMEM response assumed"
                                  : "inherited scalar LSU timing")
        << ",\"vpu_scope\":"
        << quote(model.rtlVpu ? "derived access timing; inherited logical reservation floors, slot and lane exclusion rules"
                            : "inherited VPU timing and rules")
        << ",\"at_completion\":"
        << quote(model.rtlDma ? "memory lifetime until explicit matching DMA.WAIT; age is not a bound"
                              : "access at modeled DMA completion")
        << ",\"dma_cycles\":\"cost estimate, not a completion guarantee\","
        << "\"required_consumer_checks\":[\"optimizer admission\",\"ReservationTable\",\"simulate\"]},"
        << "\"dma_launch_sites\":[";
    for (size_t i = 0; i < dmaFlow.commands.size(); ++i) {
        if (i) out << ',';
        writeFootprint(out, dmaFlow.commands[i]);
    }
    out << "],\"blocks\":[";
    for (size_t bi = 0; bi < code.blocks.size(); ++bi) {
        const Block& block = code.blocks[bi];
        const IncomingDma incoming{model.rtlDma ? dmaFlow.before[bi].front() : PendingDma{}, &dmaFlow.commands};
        const DepGraph graph = buildGraph(blockInstructions(block), entries[bi], dmaRegs, model,
                                          model.rtlDma ? &incoming : nullptr);
        out << (bi ? "," : "") << "{\"index\":" << bi << ",\"labels\":[";
        for (size_t i = 0; i < block.labels.size(); ++i) out << (i ? "," : "") << quote(block.labels[i]);
        out << "],\"successors\":";
        numbers(out, block.succs);
        out << ",\"unknown_successors\":" << block.unknownSuccs << ",\"entry\":{\"xregs\":[";
        for (size_t i = 0; i < entries[bi].size(); ++i) {
            out << (i ? "," : "");
            if (entries[bi][i]) out << *entries[bi][i];
            else out << "null";
        }
        out << "],\"dma_base\":";
        if (entries[bi].dmaBase) out << *entries[bi].dmaBase;
        else out << "null";
        out << ",\"pending_dma\":[";
        bool comma = false;
        for (int channel = 0; channel < 8; ++channel)
            for (auto [site, age] : incoming.pending[channel]) {
                out << (comma ? "," : "") << "{\"channel\":" << channel << ",\"site\":" << site
                    << ",\"later_launches\":" << age << '}';
                comma = true;
            }
        out << "]},\"instructions\":[";
        for (size_t i = 0; i < graph.nodes.size(); ++i) {
            const Instr& in = graph.nodes[i];
            const Footprint& f = graph.footprints[i];
            if (!f.error.empty()) throw std::runtime_error("line " + std::to_string(in.line) + ": " + f.error);
            out << (i ? "," : "") << "{\"index\":" << i << ",\"line\":" << in.line
                << ",\"assembly\":" << quote(formatInstr(in)) << ",\"opcode\":" << quote(in.op->name)
                << ",\"dma_channel\":" << in.op->channel << ",\"barrier\":" << isBarrier(in)
                << ",\"release\":" << in.release << ",\"keep\":" << in.keep
                << ",\"delay_slot\":" << (hasDelaySlot(block) && i == block.body.size() + 1)
                << ",\"footprint\":";
            writeFootprint(out, f);
            out << '}';
        }
        out << "],\"edges\":[";
        for (size_t i = 0; i < graph.edges.size(); ++i) {
            const Edge& edge = graph.edges[i];
            out << (i ? "," : "") << "{\"from\":" << edge.from << ",\"to\":" << edge.to
                << ",\"distance\":" << edge.distance << ",\"kind\":" << quote(edgeKindName(edge.kind))
                << ",\"reason\":" << quote(edge.reason) << '}';
        }
        out << "]}";
    }
    return out.str() + "]}\n";
}
