#include "core/machine.h"

#include <algorithm>
#include <fstream>
#include <map>
#include <stdexcept>

namespace {
MachineModel readProfile(const std::string& path, int mxu, const MachineModel& base) {
    const std::string engine = "MXU" + std::to_string(mxu);
    std::ifstream stream(path);
    auto fail = [&](const std::string& why) { return std::runtime_error(engine + " profile " + path + ": " + why); };
    if (!stream) throw fail("cannot read file");
    std::map<std::string, std::string> fields;
    auto trim = [](const std::string& text) {
        size_t first = text.find_first_not_of(" \t\r\n");
        return first == std::string::npos ? std::string{} : text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
    };
    for (std::string line; std::getline(stream, line);) {
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) throw fail("expected key=value");
        std::string key = trim(line.substr(0, eq)), value = trim(line.substr(eq + 1));
        if (!fields.emplace(key, value).second) throw fail("duplicate field " + key);
    }
    if (!stream.eof()) throw fail("read failed");
    for (const char* key : {"schema", "config", "source_ir_sha256", "evidence_sha256", "overwrite_acc_read_hold"})
        if (!fields.count(key)) throw fail(std::string("missing field ") + key);
    if (mxu == 1 && !fields.count("first_write_age")) throw fail("missing field first_write_age");
    if (fields.size() != (mxu == 1 ? 6u : 5u) || (mxu == 0 && fields.count("first_write_age")))
        throw fail("unknown fields");
    if (fields.at("schema") != "atlas-mxu" + std::to_string(mxu) + "-profile-v1" || fields.at("config") != "EE290SimConfig")
        throw fail("unsupported schema or configuration");
    for (const char* key : {"source_ir_sha256", "evidence_sha256"}) {
        const std::string& hash = fields.at(key);
        if (hash.size() != 64 || !std::all_of(hash.begin(), hash.end(), [](char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        })) throw fail(std::string("invalid SHA-256 identity in ") + key);
    }
    MachineModel model = base;
    if (!base.sourceIrSha256.empty() && base.sourceIrSha256 != fields.at("source_ir_sha256"))
        throw fail("cannot combine profiles from different hardware IR");
    if (mxu == 1) {
        const std::string& age = fields.at("first_write_age");
        if (age.empty() || age.size() > 2 || !std::all_of(age.begin(), age.end(), [](char c) { return c >= '0' && c <= '9'; }))
            throw fail("invalid first_write_age");
        model.mxu1FirstWriteAge = std::stoi(age);
        // The unchanged two-entry FIFO model requires latency shorter than a tile.
        if (model.mxu1FirstWriteAge < 2 || model.mxu1FirstWriteAge > 32)
            throw fail("first_write_age outside supported range 2..32");
    }
    const std::string& hold = fields.at("overwrite_acc_read_hold");
    if (hold != "0" && hold != "1") throw fail("overwrite_acc_read_hold must be 0 or 1");
    if (mxu == 1) model.mxu1OverwriteAccReadHold = hold == "1";
    else model.mxu0OverwriteAccReadHold = hold == "1";
    // Hashes identify the accompanying evidence; this loader does not certify it.
    model.sourceIrSha256 = fields.at("source_ir_sha256");
    model.name = (base.sourceIrSha256.empty() ? "EE290SimConfig/" : base.name + "; ") +
        engine + " evidence " + fields.at("evidence_sha256");
    return model;
}
} // namespace

MachineModel readExperimentalMxu1Profile(const std::string& path, const MachineModel& base) {
    return readProfile(path, 1, base);
}

MachineModel readExperimentalMxu0Profile(const std::string& path, const MachineModel& base) {
    return readProfile(path, 0, base);
}

MachineModel readExperimentalDmaProfile(const std::string& path, const MachineModel& base) {
    std::ifstream stream(path);
    auto fail = [&](const std::string& why) { return std::runtime_error("DMA profile " + path + ": " + why); };
    if (!stream) throw fail("cannot read file");
    std::map<std::string, std::string> fields;
    auto trim = [](const std::string& text) {
        size_t first = text.find_first_not_of(" \t\r\n");
        return first == std::string::npos ? std::string{} : text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
    };
    for (std::string line; std::getline(stream, line);) {
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) throw fail("expected key=value");
        std::string key = trim(line.substr(0, eq)), value = trim(line.substr(eq + 1));
        if (!fields.emplace(key, value).second) throw fail("duplicate field " + key);
    }
    if (!stream.eof()) throw fail("read failed");
    const bool ranges = fields.count("schema") && fields.at("schema") == "atlas-dma-profile-v2";
    std::map<std::string, std::string> supported = {
        {"schema", ranges ? "atlas-dma-profile-v2" : "atlas-dma-profile-v1"}, {"config", "EE290SimConfig"},
        {"operand_capture", "issue"}, {"config_update", "issue"},
        {"vmem_word_address_low_bit", "3"}, {"vmem_line_address_bits", "16"},
        {"transfer_size_bits", "13"}, {"vmem_line_bytes", "32"},
        {"vmem_lines", "49152"}, {"channels", "8"}, {"command_slots", "8"},
        {"completion", "explicit-wait"}, {"lsu_priority_over_dma", "1"},
        // A supported subset, not a consequence of the 13-bit hardware field.
        {"supported_max_transfer_bytes", "4096"},
    };
    if (ranges) {
        supported.insert({{"dram_address", "base32-concat-low32"}, {"dram_address_bits", "37"},
                          {"dram_alignment_bytes", "32"}, {"dram_base_reset", "0"},
                          {"dram_wrap", "conservative-alias"}});
    }
    for (const auto& [key, expected] : supported) {
        auto found = fields.find(key);
        if (found == fields.end()) throw fail("missing field " + key);
        if (found->second != expected) throw fail("unsupported " + key + "=" + found->second);
    }
    for (const char* key : {"source_ir_sha256", "evidence_sha256"}) {
        auto found = fields.find(key);
        if (found == fields.end()) throw fail(std::string("missing field ") + key);
        const std::string& hash = found->second;
        if (hash.size() != 64 || !std::all_of(hash.begin(), hash.end(), [](char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        })) throw fail(std::string("invalid SHA-256 identity in ") + key);
    }
    if (fields.size() != supported.size() + 2) throw fail("unknown fields");
    if (!base.sourceIrSha256.empty() && base.sourceIrSha256 != fields.at("source_ir_sha256"))
        throw fail("cannot combine profiles from different hardware IR");
    MachineModel model = base;
    model.rtlDma = true;
    model.rtlDmaRanges = ranges;  // loading v1 never inherits an unproven capability
    // Like the MXU loaders, these identities name evidence without certifying it.
    model.sourceIrSha256 = fields.at("source_ir_sha256");
    model.name = (base.sourceIrSha256.empty() ? "EE290SimConfig/" : base.name + "; ") +
        std::string("DMA evidence ") + fields.at("evidence_sha256");
    return model;
}
