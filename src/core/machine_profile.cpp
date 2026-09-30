#include "core/machine.h"

#include <algorithm>
#include <charconv>
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

int readAge(const Profile& profile, const std::string& key, int maximum, int minimum = 1) {
    const std::string& value = requireField(profile, key);
    int result = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size() || value.size() > 10)
        throw profile.error("invalid " + key);
    if (result < minimum || result > maximum)
        throw profile.error(key + " outside supported range " + std::to_string(minimum) + ".." + std::to_string(maximum));
    return result;
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
    const bool mxu0Timing = mxu == 0 && fields.at("schema") == "atlas-mxu0-profile-v2";
    const bool hasTiming = mxu == 1 || mxu0Timing;
    if (hasTiming) requireField(profile, "first_write_age");
    if (fields.size() != (hasTiming ? 6u : 5u)) throw profile.error("unknown fields");
    requireValue(profile, "schema", "atlas-mxu" + std::to_string(mxu) +
                 (mxu0Timing ? "-profile-v2" : "-profile-v1"));
    requireValue(profile, "config", "EE290SimConfig");
    requireIdentity(profile, base);
    MachineModel model = base;
    if (mxu == 1) {
        // The unchanged two-entry FIFO model requires latency shorter than a tile.
        model.mxu1FirstWriteAge = readAge(profile, "first_write_age", 32, 2);
    } else {
        model.mxu0FirstWriteAge = mxu0Timing ? readAge(profile, "first_write_age", 64, 33) : 63;
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
    const bool scalar = requireField(profile, "schema") == "atlas-lsu-profile-v2";
    const Fields supported = {
        {"schema", scalar ? "atlas-lsu-profile-v2" : "atlas-lsu-profile-v1"}, {"config", "EE290SimConfig"},
        {"rows", "32"}, {"row_step", "1"}, {"operand_capture", "issue"},
    };
    for (const auto& [key, expected] : supported) {
        requireValue(profile, key, expected);
    }
    requireIdentity(profile, base);
    MachineModel model = base;
    model.vloadReadAge = readAge(profile, "vload_read_age", 64);
    model.vloadWriteAge = readAge(profile, "vload_write_age", 64);
    model.vloadFirstFreeAge = readAge(profile, "vload_first_free_age", 128);
    model.vstoreReadAge = readAge(profile, "vstore_read_age", 64);
    model.vstoreWriteAge = readAge(profile, "vstore_write_age", 64);
    model.vstoreFirstFreeAge = readAge(profile, "vstore_first_free_age", 128);
    model.rtlScalarLsu = scalar;
    model.scalarMemoryAge = scalar ? readAge(profile, "scalar_memory_age", 64) : 1;
    model.scalarLoadWriteAge = scalar ? readAge(profile, "scalar_load_write_age", 128) : 3;
    model.scalarLoadFirstFreeAge = scalar ? readAge(profile, "scalar_load_first_free_age", 128) : 3;
    if (model.scalarLoadWriteAge <= model.scalarMemoryAge ||
        model.scalarLoadFirstFreeAge < model.scalarLoadWriteAge)
        throw profile.error("scalar response must follow request and load path must remain held through response");
    if (fields.size() != supported.size() + (scalar ? 11 : 8)) throw profile.error("unknown fields");
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

MachineModel readExperimentalXluProfile(const std::string& path, const MachineModel& base) {
    const Profile profile = loadFields("XLU profile", path);
    const bool connected = requireField(profile, "schema") == "atlas-xlu-profile-v2";
    const Fields supported = {
        {"schema", connected ? "atlas-xlu-profile-v2" : "atlas-xlu-profile-v1"}, {"config", "EE290SimConfig"},
        {"rows", "32"}, {"row_step", "1"}, {"operand_capture", "issue"},
    };
    for (const auto& [key, expected] : supported) requireValue(profile, key, expected);
    requireIdentity(profile, base);
    MachineModel model = base;
    // Match the scheduler's bounded idle search; reservations allocate per cycle.
    constexpr int maxAge = 100000;
    model.xluReadAge = readAge(profile, "read_age", maxAge);
    model.xluWriteAge = readAge(profile, "write_age", maxAge);
    model.xluFirstFreeAge = readAge(profile, "first_free_age", maxAge);
    model.xluReadReleaseAge = connected ? readAge(profile, "read_release_age", maxAge) :
        std::max({33, model.xluReadAge + 32, model.xluWriteAge - 1});
    model.xluWriteReleaseAge = connected ? readAge(profile, "write_release_age", maxAge) :
        std::max({65, model.xluWriteAge + 31, model.xluFirstFreeAge - 1});
    if (profile.fields.size() != supported.size() + (connected ? 7 : 5)) throw profile.error("unknown fields");
    if (model.xluReadReleaseAge < model.xluWriteAge - 1 ||
        model.xluWriteReleaseAge < model.xluFirstFreeAge - 1)
        throw profile.error("logical reservations must cover frontend busy assertions");
    if (connected && model.xluWriteAge < model.xluReadAge + 33)
        throw profile.error("connected XLU write must follow the final one-cycle MREG response");
    if (model.xluWriteAge <= model.xluReadAge + 31 || model.xluFirstFreeAge <= model.xluWriteAge + 31)
        throw profile.error("write must follow all 32 reads and first_free must follow the last write");
    model.rtlXlu = true;
    setIdentity(model, base, profile, "XLU");
    return model;
}

MachineModel readExperimentalVpuProfile(const std::string& path, const MachineModel& base) {
    const Profile profile = loadFields("VPU profile", path);
    const Fields supported = {
        {"schema", "atlas-vpu-profile-v1"}, {"config", "EE290SimConfig"},
        {"rows", "32"}, {"row_step", "1"}, {"operand_capture", "issue"}, {"pack_write_step", "2"},
    };
    for (const auto& [key, expected] : supported) requireValue(profile, key, expected);
    requireIdentity(profile, base);
    MachineModel model = base;
    model.vpuReadAge = readAge(profile, "read_age", 128, 0);
    model.vpuSimpleWriteAge = readAge(profile, "simple_write_age", 256);
    model.vpuRowSumWriteAge = readAge(profile, "row_sum_write_age", 256);
    model.vpuColumnWriteAge = readAge(profile, "column_write_age", 256);
    model.vpuPackWriteAge = readAge(profile, "pack_write_age", 256);
    model.vpuUnpackWriteAge = readAge(profile, "unpack_write_age", 256);
    model.vpuImmediateWriteAge = readAge(profile, "immediate_write_age", 256);
    if (profile.fields.size() != supported.size() + 9) throw profile.error("unknown fields");
    for (int write : {model.vpuSimpleWriteAge, model.vpuRowSumWriteAge,
                      model.vpuPackWriteAge, model.vpuUnpackWriteAge})
        if (write <= model.vpuReadAge) throw profile.error("destination must follow source reads");
    if (model.vpuColumnWriteAge <= model.vpuReadAge + 63)
        throw profile.error("column reduction write must follow its first full source pass");
    model.rtlVpu = true;
    setIdentity(model, base, profile, "VPU");
    return model;
}
