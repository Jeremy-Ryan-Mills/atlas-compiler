cmake_minimum_required(VERSION 3.20)
project(atlas_rtlgraph_query LANGUAGES C CXX)
find_package(CIRCT REQUIRED CONFIG)
add_executable(rtlgraph_export rtlgraph_query.cpp)
target_include_directories(rtlgraph_export SYSTEM PRIVATE
  ${CIRCT_INCLUDE_DIRS} ${MLIR_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS})
target_compile_features(rtlgraph_export PRIVATE cxx_std_17)
target_compile_options(rtlgraph_export PRIVATE -fno-rtti)
target_link_libraries(rtlgraph_export PRIVATE
  CIRCTHW CIRCTComb CIRCTSeq CIRCTSV CIRCTVerif CIRCTLTL CIRCTEmit CIRCTOM
  CIRCTSim MLIRParser MLIRIR LLVMSupport)
