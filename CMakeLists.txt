cmake_minimum_required(VERSION 3.20)
project(atlas_opt CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -Wpedantic)
option(ATLAS_SANITIZE "Build with AddressSanitizer and UBSan" OFF)
if(ATLAS_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
  add_link_options(-fsanitize=address,undefined)
endif()

# New files in src/core or src/passes are picked up automatically.
file(GLOB CORE_SOURCES CONFIGURE_DEPENDS src/core/*.cpp)
file(GLOB PASS_SOURCES CONFIGURE_DEPENDS src/passes/*.cpp)
add_library(atlas STATIC ${CORE_SOURCES} ${PASS_SOURCES} src/tool/viewer.cpp)
target_include_directories(atlas PUBLIC src)

add_executable(atlas-opt src/tool/main.cpp)
target_link_libraries(atlas-opt atlas)

enable_testing()
add_executable(atlas-tests tests/tests.cpp)
target_link_libraries(atlas-tests atlas)
target_compile_definitions(atlas-tests PRIVATE ATLAS_SOURCE_DIR="${CMAKE_SOURCE_DIR}")
add_test(NAME atlas-tests COMMAND atlas-tests)

add_executable(publication-fixed-tests tests/publication_fixed_tests.cpp)
target_link_libraries(publication-fixed-tests atlas)
add_test(NAME publication-fixed-tests COMMAND publication-fixed-tests)

add_executable(publication-dma-tests tests/publication_dma_tests.cpp)
target_link_libraries(publication-dma-tests atlas)
add_test(NAME publication-dma-tests COMMAND publication-dma-tests)

add_executable(machine-profile-tests tests/machine_profile_tests.cpp)
target_link_libraries(machine-profile-tests atlas)
target_compile_definitions(machine-profile-tests PRIVATE ATLAS_SOURCE_DIR="${CMAKE_SOURCE_DIR}")
add_test(NAME machine-profile-tests COMMAND machine-profile-tests)

add_executable(rtl-dma-tests tests/rtl_dma_tests.cpp)
target_link_libraries(rtl-dma-tests atlas)
add_test(NAME rtl-dma-tests COMMAND rtl-dma-tests)

add_executable(dma-wait-tests tests/dma_wait_tests.cpp)
target_link_libraries(dma-wait-tests atlas)
add_test(NAME dma-wait-tests COMMAND dma-wait-tests)

find_package(Python3 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
  add_test(NAME footprint-dump-tests COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_SOURCE_DIR}/tests/footprint_dump_tests.py $<TARGET_FILE:atlas-opt>)
  add_test(NAME rtlgraph-contract-tests COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_contract.py $<TARGET_FILE:atlas-opt>)
  add_test(NAME rtlgraph-xlu-tests COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_xlu.py)
  add_test(NAME rtlgraph-xlu-connected-tests COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_xlu_connected.py)
  add_test(NAME rtlgraph-perf-tests COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_perf.py)
  add_test(NAME rtlgraph-assembly-tests COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_assembly.py)
  add_test(NAME rtlgraph-instruction-tests COMMAND ${Python3_EXECUTABLE} -B
    ${CMAKE_SOURCE_DIR}/scripts/tests/test_rtlgraph_instruction_timing.py)
endif()
