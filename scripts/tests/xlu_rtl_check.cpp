// Independent Verilator witness for XluEngine exported from the pinned CIRCT artifact.
#include "VXluEngine.h"
#include <array>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
using Row = std::array<uint32_t,8>;
using Tile = std::array<Row,32>;
using Memory = std::array<Tile,64>;
void require(bool ok,const std::string& what) { if(!ok) throw std::runtime_error(what); }
uint8_t byte(const Row& row,int col) { return (row[col/4]>>(8*(col%4)))&255; }
int main() {
 try {
  VXluEngine dut;
  dut.clock=0; dut.reset=1; dut.io_cmd_valid=0; dut.io_mregReadResp_valid=0;
  for(int i=0;i<2;++i) { dut.clock=0; dut.eval(); dut.clock=1; dut.eval(); }
  dut.clock=0; dut.reset=0; dut.eval();
  Memory mem{};
  uint32_t random=0x4567;
  int cases=0, comparisons=0;
  // Every source ID, in-place and distinct destinations, without resetting between launches.
  for(int latency : {1,2}) for(int src=0;src<64;++src) for(int inplace=0;inplace<2;++inplace) {
   int dst=inplace?src:(src+17)%64;
   for(auto& row:mem[src]) for(auto& word:row) {
    random^=random<<13; random^=random>>17; random^=random<<5; word=random;
   }
   Tile input=mem[src];
   std::array<Row,100> responses{};
   std::array<bool,100> valid{};
   int reads=0,writes=0;
   int firstWrite=33+latency,freeAge=65+latency;
   for(int age=0;age<=freeAge;++age) {
    dut.clock=0;
    dut.io_cmd_valid=age==0 || age==7 || age==freeAge-1;
    dut.io_cmd_bits_op=0;
    // The injected busy commands and changing operands must not replace the active command.
    dut.io_cmd_bits_srcMregId=age==0?src:(src+1)%64;
    dut.io_cmd_bits_dstMregId=age==0?dst:(dst+1)%64;
    dut.io_mregReadResp_valid=valid[age];
    for(int w=0;w<8;++w) dut.io_mregReadResp_bits[w]=responses[age][w];
    dut.eval();
    require(bool(dut.io_busy)==(age>0 && age<freeAge),"busy age "+std::to_string(age));
    require(bool(dut.io_mregReadReq_valid)==(age>=1 && age<=32),"read window");
    require(bool(dut.io_mregWriteReq_valid)==(age>=firstWrite && age<freeAge),"write window");
    if(dut.io_mregReadReq_valid) {
     require(dut.io_mregReadReq_bits_mregId==src,"captured source");
     require(dut.io_mregReadReq_bits_row==reads,"source row order");
     responses[age+latency]=mem[src][reads++]; valid[age+latency]=true;
    }
    if(dut.io_mregWriteReq_valid) {
     require(dut.io_mregWriteReq_bits_mregId==dst,"captured destination");
     require(dut.io_mregWriteReq_bits_row==writes,"destination row order");
     Row row{}; for(int w=0;w<8;++w) row[w]=dut.io_mregWriteReq_bits_data[w];
     for(int col=0;col<32;++col) {
      require(byte(row,col)==byte(input[col],writes),"byte transpose"); ++comparisons;
     }
     mem[dst][writes++]=row;
    }
    // Leave the first-free cycle before its edge; the next case can launch at this same edge.
    if(age<freeAge) { dut.clock=1; dut.eval(); }
   }
   require(reads==32 && writes==32,"exact row counts"); ++cases;
  }
  std::cout<<"{\"status\":\"passed\",\"cases\":"<<cases<<",\"byte_comparisons\":"<<comparisons
           <<",\"response_latencies\":[1,2],\"read_age\":1,\"write_age\":34,\"first_free_age\":66}\n";
 } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 1; }
}
