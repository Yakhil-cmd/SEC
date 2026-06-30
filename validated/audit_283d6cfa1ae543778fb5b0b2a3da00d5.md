### Title
`EvmErc20.sol` `withdrawToNear` Sends Malformed Input to `ExitToNear` Precompile (Input Format Mismatch), Causing Permanent Token Loss — (File: `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

`EvmErc20.sol` `withdrawToNear` burns user tokens first, then calls the `ExitToNear` precompile with input layout `\x01 | amount(32) | recipient`. When the `error_refund` Cargo feature is compiled in, the precompile instead expects `\x01 | refund_address(20) | amount(32) | recipient`. The mismatch causes the precompile to misparse the amount field (treating the first 20 bytes of `amount`