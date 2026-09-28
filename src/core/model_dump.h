#pragma once

#include <string>

#include "core/machine.h"

// Resolved input model for inspection; exporting does not validate a schedule.
std::string dumpFootprints(const AsmProgram& program, const MachineModel& model = {});
