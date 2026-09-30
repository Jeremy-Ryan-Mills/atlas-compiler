# Model comparison fixture: assigned operands, preinitialized weights/accumulator.
# This compiler-only probe is not a standalone numerical RTL workload.
vmatpop.fp8.acc.mxu1 m8, acc0, e0
vmatmul.mxu1 acc0, m0, w0
ecall
