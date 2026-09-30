#include "core/machine.h"
#include <iostream>
int main() {
  std::string a, b;
  while (std::cin >> a >> b) {
    auto x = findOp(a), y = findOp(b);
    if (!x || !y) return 2;
    std::cout << vpuCanOverlap(*x, *y) << '\n';
  }
}
