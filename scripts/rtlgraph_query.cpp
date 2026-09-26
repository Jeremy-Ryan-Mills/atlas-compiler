// Export actual CIRCT/MLIR SSA identities, including multi-result indices.
// This is structural evidence only; it does not infer timing or prove guards.
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/SV/SVDialect.h"
#include "circt/Dialect/Verif/VerifDialect.h"
#include "circt/Dialect/LTL/LTLDialect.h"
#include "circt/Dialect/Emit/EmitDialect.h"
#include "circt/Dialect/OM/OMDialect.h"
#include "circt/Dialect/Sim/SimDialect.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Verifier.h"
#include "mlir/Parser/Parser.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/raw_ostream.h"
#include <set>
#include <map>
#include <string>

using namespace mlir;
using namespace circt;

template <typename T> static std::string print(T value) {
  std::string text;
  llvm::raw_string_ostream os(text);
  os << value;
  return text;
}

static llvm::json::Object exportModule(hw::HWModuleOp mod) {
  llvm::DenseMap<Operation *, std::string> opIds;
  llvm::DenseMap<Value, std::string> valueIds;
  llvm::json::Array arguments;
  unsigned nextOp = 0, nextBlock = 0;
  mod->walk<WalkOrder::PreOrder>([&](Operation *op) {
    if (op != mod.getOperation()) opIds[op] = "op" + std::to_string(nextOp++);
    for (auto result : op->getResults())
      valueIds[result] = opIds[op] + ".r" + std::to_string(result.getResultNumber());
    for (auto &region : op->getRegions()) {
      for (auto &block : region) {
        unsigned blockNumber = nextBlock++;
        for (auto arg : block.getArguments()) {
          auto id = "b" + std::to_string(blockNumber) + ".a" + std::to_string(arg.getArgNumber());
          valueIds[arg] = id;
          arguments.push_back(llvm::json::Object{
              {"id", id}, {"block", blockNumber}, {"index", arg.getArgNumber()},
              {"type", print(arg.getType())}, {"location", print(arg.getLoc())}});
        }
      }
    }
  });
  llvm::json::Array ports;
  auto &body = mod->getRegion(0).front();
  auto output = cast<hw::OutputOp>(body.getTerminator());
  for (const auto &port : mod.getPortList()) {
    Value value = port.isOutput() ? output.getOperand(port.argNum)
                                 : body.getArgument(port.argNum);
    ports.push_back(llvm::json::Object{
        {"name", port.name.getValue()},
        {"direction", port.isOutput() ? "output" : port.isInput() ? "input" : "inout"},
        {"index", static_cast<int64_t>(port.argNum)}, {"type", print(port.type)},
        {"value", valueIds.lookup(value)}});
  }
  llvm::json::Array operations;
  mod->walk<WalkOrder::PreOrder>([&](Operation *op) {
    if (op == mod.getOperation()) return;
    llvm::json::Array operands, results, resultTypes;
    for (auto operand : op->getOperands()) operands.push_back(valueIds.lookup(operand));
    for (auto result : op->getResults()) {
      results.push_back(valueIds.lookup(result));
      resultTypes.push_back(print(result.getType()));
    }
    llvm::json::Object attrs;
    for (auto attr : op->getAttrs()) {
      auto string = dyn_cast<StringAttr>(attr.getValue());
      attrs[attr.getName().str()] = string ? string.getValue().str() : print(attr.getValue());
    }
    llvm::json::Object item{
        {"id", opIds.lookup(op)}, {"kind", op->getName().getStringRef()},
        {"location", print(op->getLoc())}, {"attributes", std::move(attrs)},
        {"operands", std::move(operands)}, {"results", std::move(results)},
        {"result_types", std::move(resultTypes)},
        {"combinational", hw::isCombinational(op)},
        {"identity_wire", isa<hw::WireOp>(op)},
        {"has_regions", op->getNumRegions() != 0}};
    if (auto inst = dyn_cast<hw::InstanceOp>(op)) {
      llvm::json::Array inputs, outputs;
      for (auto name : inst.getArgNames()) inputs.push_back(cast<StringAttr>(name).getValue());
      for (auto name : inst.getResultNames()) outputs.push_back(cast<StringAttr>(name).getValue());
      item["instance"] = llvm::json::Object{
          {"name", inst.getInstanceName()}, {"module", inst.getModuleName()},
          {"input_names", std::move(inputs)}, {"output_names", std::move(outputs)}};
    }
    operations.push_back(std::move(item));
  });
  return llvm::json::Object{{"name", mod.getName()}, {"location", print(mod.getLoc())},
          {"ports", std::move(ports)}, {"arguments", std::move(arguments)},
          {"operations", std::move(operations)}};
}

template <typename Module> static llvm::json::Object exportDeclaration(Module mod) {
  llvm::json::Array ports;
  for (const auto &port : mod.getPortList())
    ports.push_back(llvm::json::Object{
        {"name", port.name.getValue()},
        {"direction", port.isOutput() ? "output" : port.isInput() ? "input" : "inout"},
        {"index", static_cast<int64_t>(port.argNum)}, {"type", print(port.type)},
        {"location", print(port.loc)}});
  return llvm::json::Object{{"name", mod.getName()},
      {"kind", mod->getName().getStringRef()}, {"location", print(mod.getLoc())},
      {"verilog_name", mod.getVerilogModuleName()}, {"ports", std::move(ports)}};
}

int main(int argc, char **argv) {
  if (argc < 3) {
    llvm::errs() << "usage: rtlgraph_export INPUT.mlir MODULE [MODULE ...]\n";
    return 2;
  }
  DialectRegistry registry;
  registry.insert<hw::HWDialect, comb::CombDialect, seq::SeqDialect, sv::SVDialect,
                  verif::VerifDialect, ltl::LTLDialect, emit::EmitDialect,
                  om::OMDialect, sim::SimDialect>();
  MLIRContext context(registry);
  context.disableMultithreading();
  auto input = parseSourceFile<ModuleOp>(argv[1], &context);
  if (!input || failed(verify(*input))) return 1;
  std::set<std::string> requested;
  for (int i = 2; i < argc; ++i) requested.insert(argv[i]);
  llvm::json::Array modules, inventory, declarations, instances;
  std::map<std::string, int64_t> operationCounts, dialectCounts;
  input->walk([&](Operation *op) {
    ++operationCounts[op->getName().getStringRef().str()];
    ++dialectCounts[op->getName().getDialectNamespace().str()];
    if (auto mod = dyn_cast<hw::HWModuleExternOp>(op))
      declarations.push_back(exportDeclaration(mod));
    if (auto mod = dyn_cast<hw::HWModuleGeneratedOp>(op))
      declarations.push_back(exportDeclaration(mod));
    if (auto inst = dyn_cast<hw::InstanceOp>(op)) {
      auto parent = op->getParentOfType<hw::HWModuleOp>();
      instances.push_back(llvm::json::Object{
          {"parent_module", parent ? parent.getName() : StringRef()},
          {"name", inst.getInstanceName()}, {"module", inst.getModuleName()},
          {"location", print(inst.getLoc())}});
    }
  });
  input->walk([&](hw::HWModuleOp mod) {
    inventory.push_back(mod.getName());
    if (requested.erase(mod.getName().str())) modules.push_back(exportModule(mod));
  });
  llvm::json::Array missing;
  for (auto &name : requested) missing.push_back(name);
  llvm::json::Object opCounts, dialects;
  for (const auto &item : operationCounts) opCounts[item.first] = item.second;
  for (const auto &item : dialectCounts) dialects[item.first] = item.second;
  llvm::json::Object output{{"schema_version", 1}, {"producer", "CIRCT typed SSA traversal"},
      {"modules", std::move(modules)}, {"available_modules", std::move(inventory)},
      {"missing_modules", std::move(missing)},
      {"census", llvm::json::Object{{"operation_counts", std::move(opCounts)},
          {"dialect_counts", std::move(dialects)},
          {"external_or_generated_modules", std::move(declarations)},
          {"instance_sites", std::move(instances)}}}};
  llvm::outs() << llvm::formatv("{0:2}\n", llvm::json::Value(std::move(output)));
  return 0;
}
