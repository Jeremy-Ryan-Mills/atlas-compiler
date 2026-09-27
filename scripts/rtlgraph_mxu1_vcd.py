#!/usr/bin/env python3
"""Convert selected VCS signals into settled pre-rising-edge MXU1 samples.

Only actual dumped values are used. Response valid is reconstructed from
registered MREG bank-return tags, never from delaying the expected request.
"""

import argparse
import json
from pathlib import Path
import re

from rtlgraph_s0 import artifact, checked_path


CORE_SCOPE = "TestDriver.testHarness.chiptop0.system.domain.atlasTile.core"
SEQ_SCOPE = CORE_SCOPE + ".mxu1.seq"
_SEQ_SIGNALS = {
    "clock": ("clock", 1), "reset": ("reset", 1),
    "cmd.valid": ("io_cmd_valid", 1), "cmd.op": ("io_cmd_bits_op", 3),
    "cmd.mreg": ("io_cmd_bits_mregId", 6), "cmd.accsel": ("io_cmd_bits_accSel", 1),
    "cmd.wslot": ("io_cmd_bits_weightSlot", 1), "accept_compute": ("acceptCompute", 1),
    "p0.valid": ("p0CmdValid", 1), "p0.op": ("p0Cmd_op", 3),
    "p0.mreg": ("p0Cmd_mregId", 6), "p0.accsel": ("p0Cmd_accSel", 1),
    "p0.wslot": ("p0Cmd_weightSlot", 1), "p0.row": ("p0Row", 6),
    "p0.boundary": ("p0Boundary", 1),
    "mreg_req.valid": ("io_mregReadReq0_valid", 1),
    "mreg_req.mreg": ("io_mregReadReq0_bits_mregId", 6),
    "mreg_req.row": ("io_mregReadReq0_bits_row", 5),
    "acc_read.valid": ("io_accComputeReadEn", 1),
    "acc_read.accsel": ("io_accComputeReadAddr_accSel", 1),
    "acc_read.row": ("io_accComputeReadAddr_rowIdx", 5),
    "compute_valid": ("io_compute_valid", 1), "core_out_valid": ("io_coreOut_valid", 1),
    "acc_write.valid": ("io_accComputeWrite_valid", 1),
    "acc_write.accsel": ("io_accComputeWrite_bits_accSel", 1),
    "acc_write.row": ("io_accComputeWrite_bits_rowIdx", 5),
    "retire": ("popThisCycle", 1), "busy.p0": ("p0IsCompute", 1),
    "busy.inflight0": ("inflightValid_0", 1), "busy.inflight1": ("inflightValid_1", 1),
}
SIGNALS = {key: (SEQ_SCOPE + "." + name, width)
           for key, (name, width) in _SEQ_SIGNALS.items()}
for _bank in range(32):
    SIGNALS[f"bank.{_bank}.valid"] = (f"{CORE_SCOPE}.mreg.bankReadValid_d_{_bank}", 1)
    SIGNALS[f"bank.{_bank}.port"] = (f"{CORE_SCOPE}.mreg.bankReadPort_d_{_bank}", 3)

# Optional perf capture preserves the original signal map and old trace schema.
_PERF_SEQ_SIGNALS = {
    "accept.push_p0": ("acceptPushP0", 1), "accept.push_p1": ("acceptPushP1", 1),
    "accept.bf16_push": ("acceptBF16Push", 1), "accept.pop_fp8": ("acceptPopFP8", 1),
    "accept.pop_bf16": ("acceptPopBF16", 1),
    "weight_write.valid": ("io_weightWriteReq_valid", 1),
    "weight_write.wslot": ("io_weightWriteReq_bits_weightSlot", 1),
    "weight_write.row": ("io_weightWriteReq_bits_laneIdx", 5),
    "acc_store.valid": ("io_accStoreReadEn", 1),
    "acc_store.accsel": ("io_accStoreAddr_accSel", 1),
    "acc_store.row": ("io_accStoreAddr_rowIdx", 5),
    "mreg_write.valid": ("io_mregWriteReq0_valid", 1),
    "mreg_write.mreg": ("io_mregWriteReq0_bits_mregId", 6),
    "mreg_write.row": ("io_mregWriteReq0_bits_row", 5),
}
_PERF_SCALAR_SIGNALS = {
    "scalar.fire": ("s1_fire", 1), "scalar.pc": ("pc_ctrl.io_s1_pc", 32),
    "scalar.instr": ("decoder.io_instr", 32),
    "csr.valid": ("io_csrPort_valid", 1), "csr.addr": ("io_csrPort_addr", 12),
    "csr.cmd": ("io_csrPort_op", 3), "csr.wdata": ("io_csrPort_wdata", 32),
    "csr.rdata": ("io_csrPort_rdata", 32),
}
PERF_SIGNALS = dict(SIGNALS)
PERF_SIGNALS.update({key: (SEQ_SCOPE + "." + name, width)
                     for key, (name, width) in _PERF_SEQ_SIGNALS.items()})
PERF_SIGNALS.update({key: (CORE_SCOPE + ".scalar." + name, width)
                     for key, (name, width) in _PERF_SCALAR_SIGNALS.items()})

# Separate targeted-bank mode; PERF_SIGNALS remains the frozen perf contract.
BANK_SIGNALS = dict(PERF_SIGNALS)
for _port in (0, 1):
    for _key, _suffix, _width in (("valid", "valid", 1), ("mreg", "bits_mregId", 6), ("row", "bits_row", 5)):
        BANK_SIGNALS[f"bank_read.p{_port}.{_key}"] = (
            f"{CORE_SCOPE}.mreg.io_mxu1ReadReq{_port}_{_suffix}", _width)

OPS = {0: "PushWeight", 1: "PushAccFP8", 2: "PushAccBF16", 3: "PopAccFP8",
       4: "PopAccBF16", 5: "Matmul", 6: "MatmulAcc"}


def read_header(stream, signals):
    scopes, declarations, timescale, directive = [], {}, None, []
    for line in stream:
        directive.extend(line.split())
        while "$end" in directive:
            end = directive.index("$end")
            words, directive = directive[:end], directive[end + 1:]
            if not words:
                raise ValueError("Empty VCD header directive")
            command = words[0]
            if command == "$scope":
                if len(words) != 3:
                    raise ValueError("Malformed VCD scope")
                scopes.append(words[2])
            elif command == "$upscope":
                if not scopes:
                    raise ValueError("VCD scope stack underflow")
                scopes.pop()
            elif command == "$var":
                if len(words) < 5:
                    raise ValueError("Malformed VCD variable")
                width, code, name = int(words[2]), words[3], words[4]
                path = ".".join([*scopes, name])
                if path in declarations:
                    raise ValueError(f"Duplicate VCD variable: {path}")
                declarations[path] = (code, width)
            elif command == "$timescale":
                timescale = "".join(words[1:])
                if not re.fullmatch(r"(?:1|10|100)(?:s|ms|us|ns|ps|fs)", timescale):
                    raise ValueError("Unsupported VCD timescale")
            elif command == "$enddefinitions":
                if scopes or directive or timescale is None:
                    raise ValueError("Incomplete VCD header or inline value changes")
                selected = {}
                for key, (path, width) in signals.items():
                    if path not in declarations:
                        raise ValueError(f"Missing trace signal: {path}")
                    code, actual_width = declarations[path]
                    if width != actual_width:
                        raise ValueError(f"Wrong trace width for {path}: {actual_width}, expected {width}")
                    selected[key] = code
                return selected, timescale
            elif command not in ("$date", "$version", "$comment"):
                raise ValueError(f"Unsupported VCD header directive: {command}")
    raise ValueError("Missing VCD enddefinitions")


def value_changes(stream, selected_codes):
    """Yield whole timestamp batches; VCD record order cannot define edge phase."""
    timestamp, changes = None, {}
    comment = False
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        if comment:
            comment = "$end" not in line
            continue
        if line.startswith("$comment"):
            comment = "$end" not in line
            continue
        if line.startswith("#"):
            new_time = int(line[1:])
            if new_time < 0 or (timestamp is not None and new_time < timestamp):
                raise ValueError("Non-monotonic VCD timestamps")
            if timestamp is not None and new_time != timestamp:
                yield timestamp, changes
                changes = {}
            timestamp = new_time
        elif line in ("$dumpvars", "$dumpall", "$end"):
            continue
        elif line.startswith("$"):
            raise ValueError(f"Unsupported VCD value directive: {line}")
        else:
            if timestamp is None:
                raise ValueError("VCD values appear before first timestamp")
            if line[0].lower() == "b":
                fields = line[1:].split()
                if len(fields) != 2:
                    raise ValueError("Malformed VCD binary value")
                bits, code = fields
            elif line[0].lower() in "01xz":
                bits, code = line[0], line[1:]
            else:
                raise ValueError(f"Unsupported VCD value encoding: {line[:40]}")
            if code not in selected_codes:
                continue
            if not bits or not re.fullmatch("[01xXzZ]+", bits):
                raise ValueError("Malformed VCD binary digits")
            value = None if any(bit.lower() in "xz" for bit in bits) else int(bits, 2)
            if code in changes and changes[code] != value:
                raise ValueError("Multiple selected-signal transitions at one timestamp are unsupported")
            changes[code] = value
    if comment:
        raise ValueError("Unterminated VCD comment")
    if timestamp is not None:
        yield timestamp, changes


def edge_samples(stream, selected, signals=SIGNALS):
    """Left limit of a clock edge: read the OLD batch before applying changes."""
    clock_code = selected["clock"]
    values, cycle, last_rise, period = {}, 0, None, None
    for time, changes in value_changes(stream, set(selected.values())):
        before_clock = values.get(clock_code)
        after_clock = changes.get(clock_code, before_clock)
        if before_clock == 0 and after_clock == 1:
            if last_rise is not None:
                gap = time - last_rise
                if period is not None and period != gap:
                    raise ValueError("Nonuniform sequencer clock period")
                period = gap
            last_rise = time
            sample = {key: values.get(code) for key, code in selected.items()}
            for key, value in sample.items():
                if value is not None and value >= (1 << signals[key][1]):
                    raise ValueError(f"Trace value exceeds declared width: {key}")
            yield cycle, time, sample
            cycle += 1
        elif after_clock is None and before_clock is not None:
            raise ValueError("Sequencer clock became unknown")
        values.update(changes)


def sample_record(cycle, time, flat, perf=False, banks=False):
    perf = perf or banks
    sample = {"cycle": cycle, "time_ticks": time}
    keys = list(_SEQ_SIGNALS)
    if perf:
        keys.extend([*_PERF_SEQ_SIGNALS, *_PERF_SCALAR_SIGNALS])
    for key in keys:
        if key == "clock":
            continue
        value = flat[key]
        if key.endswith(".op"):
            value = OPS.get(value)
        if "." in key:
            parent, child = key.split(".")
            sample.setdefault(parent, {})[child] = value
        else:
            sample[key] = value
    hits, p1_hits, known = [], [], True
    for bank in range(32):
        valid, port = flat[f"bank.{bank}.valid"], flat[f"bank.{bank}.port"]
        if valid is None or (valid == 1 and port is None):
            known = False
        if valid == 1 and port == 2:
            hits.append(bank)
        if valid == 1 and port == 3:
            p1_hits.append(bank)
    sample["mreg_resp_banks"] = hits if known else None
    sample["mreg_resp_count"] = len(hits) if known else None
    sample["mreg_resp_valid"] = bool(hits) if known else None
    busy = [flat[key] for key in ("busy.p0", "busy.inflight0", "busy.inflight1")]
    sample["comp_busy"] = any(busy) if all(v is not None for v in busy) else None
    if banks:
        sample["bank_reads"] = {port: {key: flat[f"bank_read.{port}.{key}"] for key in ("valid", "mreg", "row")}
                                for port in ("p0", "p1")}
        sample["p1_resp_banks"] = p1_hits if known else None
    return sample


def convert(source, output, perf=False, banks=False):
    perf = perf or banks
    if source == output or output.exists():
        raise ValueError("Trace output must be a new file distinct from the VCD")
    before = artifact(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    partial = output.with_name(output.name + ".partial")
    with source.open() as stream:
        signals = BANK_SIGNALS if banks else PERF_SIGNALS if perf else SIGNALS
        selected, timescale = read_header(stream, signals)
        with partial.open("x") as target:
            header = {"schema_version": 1, "kind": "atlas-mxu1-cycle-trace",
                      "sampling": "settled_pre_rising_edge", "tile_rows": 32,
                      "vcd": before, "adapter": artifact(checked_path(__file__)),
                      "timescale": timescale, "signal_map": signals,
                      "response_valid_provenance": "reconstructed_from_registered_bank_tags: bankReadValid_d[b] && bankReadPort_d[b] == 2",
                      "compute_busy_provenance": "reconstructed_from_observed_state: p0IsCompute || inflightValid_0 || inflightValid_1"}
            if perf:
                header["perf_capture"] = "mxu1_scalar_issue_push_pop_v1"
                header["scalar_issue_provenance"] = "s1_fire with pc_ctrl.io_s1_pc (instruction index) and decoder.io_instr; direct combinational MXU1 command wiring"
            if banks:
                header["bank_capture"] = "mxu1_mreg_p0_p1_v1"
                header["bank_request_provenance"] = "Actual MregFile io_mxu1ReadReq0/1 valid, mregId and row inputs"
                header["p1_response_provenance"] = "reconstructed_from_registered_bank_tags: bankReadValid_d[b] && bankReadPort_d[b] == 3"
            target.write(json.dumps(header, sort_keys=True) + "\n")
            for cycle, time, sample in edge_samples(stream, selected, signals):
                target.write(json.dumps(sample_record(cycle, time, sample, perf, banks), sort_keys=True) + "\n")
                count += 1
    if before != artifact(source):
        raise ValueError("VCD changed during conversion")
    if count == 0:
        raise ValueError("No sequencer rising edges captured")
    partial.rename(output)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vcd", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--perf", action="store_true", help="Require scalar issue, push/pop and CSR signals")
    parser.add_argument("--banks", action="store_true", help="Add actual MXU1 P0/P1 MregFile reads; implies --perf")
    args = parser.parse_args()
    source, output = map(checked_path, (args.vcd, args.output))
    count = convert(source, output, args.perf, args.banks)
    print(json.dumps({"status": "converted", "samples": count, "output": str(output)}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(f"rtlgraph_mxu1_vcd: {error}")
