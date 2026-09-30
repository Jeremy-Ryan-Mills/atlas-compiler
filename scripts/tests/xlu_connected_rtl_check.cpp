// Numerical witness against the actual CIRCT-lowered XLU, MREG, and LSU.
#include "VXluConnected.h"
#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
using Row = std::array<uint32_t, 8>;
using Tile = std::array<Row, 32>;
void require(bool ok, const std::string& text) { if (!ok) throw std::runtime_error(text); }
uint8_t byte(const Row& row, int col) { return (row[col / 4] >> (8 * (col % 4))) & 255; }
void edge(VXluConnected& d) { d.clock = 0; d.eval(); d.clock = 1; d.eval(); d.clock = 0; d.eval(); }
void reset(VXluConnected& d) {
 d.cmd = d.store_cmd = d.init_write = d.contend = 0;
 d.reset = 1; edge(d); edge(d); d.reset = 0; edge(d);
}
void fill(VXluConnected& d, int id, const Tile& tile) {
 d.init_write = 1; d.init_id = id;
 for (int row = 0; row < 32; ++row) {
  d.init_row = row;
  for (int word = 0; word < 8; ++word) d.init_data[word] = tile[row][word];
  edge(d);
 }
 d.init_write = 0;
}
int main() {
 try {
  VXluConnected dut;
  uint32_t random = 0x349517ab;
  int cases = 0, comparisons = 0, boundaryCollisions = 0, boundaryMismatches = 0;
  // Every source ID, in-place, separate bank, and paired high/low physical bank.
  for (int src = 0; src < 64; ++src) for (int mode = 0; mode < 3; ++mode) {
   reset(dut);
   Tile input{}, poison{};
   for (auto& row : input) for (auto& word : row) {
    random ^= random << 13; random ^= random >> 17; random ^= random << 5; word = random;
   }
   int dst = mode == 0 ? src : mode == 1 ? (src + 17) % 64 : src ^ 32;
   fill(dut, dst, poison); fill(dut, src, input);
   dut.src = src; dut.dst = dst;
   int reads = 0, responses = 0, writes = 0, stores = 0;
   for (int age = 0; age <= 69; ++age) {
    dut.cmd = age == 0; dut.store_cmd = age == 34;
    // Reuse each source row on the edge after XLU reads it, even while the
    // register-wide activeRead signal remains high.
    dut.init_write = age >= 2 && age <= 33; dut.init_id = src; dut.init_row = (age + 30) % 32;
    for (int word = 0; word < 8; ++word) dut.init_data[word] = 0;
    dut.eval();
    require(bool(dut.read_valid) == (age >= 1 && age <= 32), "XLU read window");
    require(bool(dut.response_valid) == (age >= 2 && age <= 33), "MREG response window");
    require(bool(dut.write_valid) == (age >= 34 && age <= 65), "XLU write window");
    require(bool(dut.store_read_valid) == (age >= 35 && age <= 66), "LSU read window");
    require(bool(dut.store_write_valid) == (age >= 37 && age <= 68), "LSU write window");
    require(!dut.collision, "Legal schedule read-under-write");
    reads += dut.read_valid; responses += dut.response_valid; writes += dut.write_valid;
    if (dut.store_write_valid) {
     Row row{}; for (int word = 0; word < 8; ++word) row[word] = dut.store_data[word];
     for (int col = 0; col < 32; ++col) {
      require(byte(row, col) == byte(input[col], stores), "Connected VSTORE byte mismatch");
      ++comparisons;
     }
     ++stores;
    }
    edge(dut);
   }
   require(reads == 32 && responses == 32 && writes == 32 && stores == 32, "Row count");
   ++cases;
  }
  // One cycle earlier reads the row on its write edge. Detect this independently
  // of whichever concrete value this two-state simulator chooses for undefined RUW.
  reset(dut);
  Tile input{}, poison{};
  for (int row = 0; row < 32; ++row) for (auto& word : input[row]) word = 0x01010101U * (row + 1);
  fill(dut, 3, poison); fill(dut, 2, input); dut.src = 2; dut.dst = 3;
  int stores = 0;
  for (int age = 0; age < 70; ++age) {
   dut.cmd = age == 0; dut.store_cmd = age == 33; dut.eval();
   boundaryCollisions += dut.collision;
   if (dut.store_write_valid) {
    Row row{}; for (int word = 0; word < 8; ++word) row[word] = dut.store_data[word];
    for (int col = 0; col < 32; ++col) boundaryMismatches += byte(row, col) != byte(input[col], stores);
    ++stores;
   }
   edge(dut);
  }
  require(boundaryCollisions == 32, "Missing negative boundary collisions");
  // A competing higher-priority request loses an XLU response instead of stalling/retrying.
  reset(dut); fill(dut, 2, input); dut.src = 2; dut.dst = 3;
  int responses = 0, writes = 0;
  for (int age = 0; age < 140; ++age) {
   dut.cmd = age == 0; dut.contend = age == 1; dut.eval();
   responses += dut.response_valid; writes += dut.write_valid; edge(dut);
  }
  require(responses == 31 && writes == 0 && dut.busy, "Conflicting read must lose progress");
  std::cout << "{\"status\":\"passed\",\"cases\":" << cases << ",\"byte_comparisons\":" << comparisons
            << ",\"vstore_issue_gap\":34,\"first_vmem_write\":37,\"last_vmem_write\":68"
            << ",\"source_reuse\":\"one cycle after each read\""
            << ",\"gap33_collisions\":" << boundaryCollisions << ",\"gap33_observed_byte_mismatches\":" << boundaryMismatches
            << ",\"competing_read_responses\":" << responses << "}\n";
 } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
