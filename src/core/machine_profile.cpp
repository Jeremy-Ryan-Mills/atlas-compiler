#include "core/machine.h"

#include <algorithm>
#include <fstream>
#include <map>
#include <stdexcept>

namespace {
using Fields = std::map<std::string, std::string>;

struct Profile {
    std::string label, path;
    Fields fields;

    std::runtime_error error(const std::string& why) const {
        return std::runtime_error(label + " " + path + ": " + why);
    }
};

std::string trim(const std::string& text) {
    size_t first = text.find_first_not_of(" \t\r\n");
    return first == std::string::npos ? std::string{} :
        text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
}

Profile loadFields(const std::string& label, const std::string& path) {
    Profile profile{label, path, {}};
    std::ifstream stream(path);
    if (!stream) throw profile.error("cannot read file");
    for (std::string line; std::getline(stream, line);) {
        line = trim(line.substr(0, line.find('#')));
        if (line.empty()) continue;
        size_t eq = line.find('=');
        if (eq == std::string::npos) throw profile.error("expected key=value");
        std::string key = trim(line.substr(0, eq)), value = trim(line.substr(eq + 1));
        if (!profile.fields.emplace(key, value).second)
            throw profile.error("duplicate field " + key);
    }
    if (!stream.eof()) throw profile.error("read failed");
    return profile;
}

const std::string& requireField(const Profile& profile, const std::string& key) {
    auto found = profile.fields.find(key);
    if (found == profile.fields.end()) throw profile.error("missing field " + key);
    return found->second;
}

void requireValue(const Profile& profile, const std::string& key, const std::string& expected) {
    const std::string& value = requireField(profile, key);
    if (value != expected) throw profile.error("unsupported " + key + "=" + value);
}

void requireHash(const Profile& profile, const std::string& key) {
    const std::string& hash = requireField(profile, key);
    if (hash.size() != 64 || !std::all_of(hash.begin(), hash.end(), [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    })) throw profile.error("invalid SHA-256 identity in " + key);
}

void requireIdentity(const Profile& profile, const MachineModel& base) {
    requireHash(profile, "source_ir_sha256");
    requireHash(profile, "evidence_sha256");
    if (!base.sourceIrSha256.empty() && base.sourceIrSha256 != profile.fields.at("source_ir_sha256"))
        throw profile.error("cannot combine profiles from different hardware IR");
}

void setIdentity(MachineModel& model, const MachineModel& base, const Profile& profile,
                 const std::string& component) {
    model.sourceIrSha256 = profile.fields.at("source_ir_sha256");
    model.name = (base.sourceIrSha256.empty() ? "EE290SimConfig/" : base.name + "; ") +
        component + " evidence " + profile.fields.at("evidence_sha256");
}

MachineModel readProfile(const std::string& path, int mxu, const MachineModel& base) {
    const std::string engine = "MXU" + std::to_string(mxu);
    const Profile profile = loadFields(engine + " profile", path);
    const Fields& fields = profile.fields;
    for (const char* key : {"schema", "config", "source_ir_sha256", "evidence_sha256", "overwrite_acc_read_hold"})
        requireField(profile, key);
    if (mxu == 1) requireField(profile, "first_write_age");
    if (fields.size() != (mxu == 1 ? 6u : 5u) || (mxu == 0 && fields.count("first_write_age")))
        throw profile.error("unknown fields");
    if (fields.at("schema") != "atlas-mxu" + std::to_string(mxu) + "-profile-v1" || fields.at("config") != "EE290SimConfig")
        throw profile.error("unsupported schema or configuration");
    requireIdentity(profile, base);
    MachineModel model = base;
    if (mxu == 1) {
        const std::string& age = fields.at("first_write_age");
        if (age.empty() || age.size() > 2 || !std::all_of(age.begin(), age.end(), [](char c) { return c >= '0' && c <= '9'; }))
            throw profile.error("invalid first_write_age");
        model.mxu1FirstWriteAge = std::stoi(age);
        // The unchanged two-entry FIFO model requires latency shorter than a tile.
        if (model.mxu1FirstWriteAge < 2 || model.mxu1FirstWriteAge > 32)
            throw profile.error("first_write_age outside supported range 2..32");
    }
    const std::string& hold = fields.at("overwrite_acc_read_hold");
    if (hold != "0" && hold != "1") throw profile.error("overwrite_acc_read_hold must be 0 or 1");
    if (mxu == 1) model.mxu1OverwriteAccReadHold = hold == "1";
    else model.mxu0OverwriteAccReadHold = hold == "1";
    setIdentity(model, base, profile, engine);
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
    const Profile profile = loadFields("DMA profile", path);
    const Fields& fields = profile.fields;
    const bool ranges = fields.count("schema") && fields.at("schema") == "atlas-dma-profile-v2";
    Fields supported = {
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
        requireValue(profile, key, expected);
    }
    requireIdentity(profile, base);
    if (fields.size() != supported.size() + 2) throw profile.error("unknown fields");
    MachineModel model = base;
    model.rtlDma = true;
    model.rtlDmaRanges = ranges;  // loading v1 never inherits an unproven capability
    setIdentity(model, base, profile, "DMA");
    return model;
}

MachineModel readExperimentalLsuProfile(const std::string& path, const MachineModel& base) {
    const Profile profile = loadFields("LSU profile", path);
    const Fields& fields = profile.fields;
    const Fields supported = {
        {"schema", "atlas-lsu-profile-v1"}, {"config", "EE290SimConfig"},
        {"rows", "32"}, {"row_step", "1"}, {"operand_capture", "issue"},
    };
    for (const auto& [key, expected] : supported) {
        requireValue(profile, key, expected);
    }
    requireIdentity(profile, base);
    auto age = [&](const std::string& key, int maximum) {
        const std::string& value = requireField(profile, key);
        if (value.empty() || value.size() > 3 ||
            !std::all_of(value.begin(), value.end(), [](char c) { return c >= '0' && c <= '9'; }))
            throw profile.error("invalid " + key);
        int result = std::stoi(value);
        if (result < 1 || result > maximum)
            throw profile.error(key + " outside supported range 1.." + std::to_string(maximum));
        return result;
    };
    MachineModel model = base;
    model.vloadReadAge = age("vload_read_age", 64);
    model.vloadWriteAge = age("vload_write_age", 64);
    model.vloadFirstFreeAge = age("vload_first_free_age", 128);
    model.vstoreReadAge = age("vstore_read_age", 64);
    model.vstoreWriteAge = age("vstore_write_age", 64);
    model.vstoreFirstFreeAge = age("vstore_first_free_age", 128);
    if (fields.size() != supported.size() + 8) throw profile.error("unknown fields");
    for (bool load : {true, false}) {
        int read = load ? model.vloadReadAge : model.vstoreReadAge;
        int write = load ? model.vloadWriteAge : model.vstoreWriteAge;
        int free = load ? model.vloadFirstFreeAge : model.vstoreFirstFreeAge;
        if (write <= read || free <= write + 31)
            throw profile.error(std::string(load ? "vload" : "vstore") +
                                " write must follow read and first_free must follow the last write");
    }
    model.rtlLsu = true;
    setIdentity(model, base, profile, "LSU");
    return model;
}
