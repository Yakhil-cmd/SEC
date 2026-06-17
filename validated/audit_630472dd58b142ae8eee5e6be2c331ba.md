### Title
`gas_per_pubdata_limit` Parsed as `u32` While Solidity ABI Declares `uint256` — Type Mismatch in L1→L2 Transaction Parsing - (File: `basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs`)

### Summary

The `gas_per_pubdata_limit` field in `AbiEncodedTransaction` is parsed and stored as `u32` in ZKsync OS, while the canonical Solidity ABI for the same transaction struct declares it as `uint256`. Any L1→L2 priority transaction submitted on-chain with `gasPerPubdataByteLimit > u32::MAX` (4,294,967,295) will fail the `parse_u32` validation step, making the transaction unparseable by the bootloader. Because L1 priority transactions cannot be skipped without halting the chain, this type mismatch is a reachable, attacker-controlled path to a chain-halt condition.

### Finding Description

In `AbiEncodedTransaction`, the field is declared:

```rust
pub gas_per_pubdata_limit: ParsedValue<u32>,
``` [1](#0-0) 

During parsing in `try_from_buffer`, the field is read with:

```rust
let gas_per_pubdata_limit = parser.parse_u32()?;
``` [2](#0-1) 

`parse_u32` reads a full 256-bit ABI word and calls `validate_u32()`, which returns `Err(())` if the value exceeds `u32::MAX`. The entire `try_from_buffer` call propagates this error, making the transaction object impossible to construct. [3](#0-2) 

The Solidity ABI for the same transaction struct (used by `DefaultAccount`, `IAccount`, `TestnetPaymaster`) declares this field as `uint256`:

```json
{
    "internalType": "uint256",
    "name": "gasPerPubdataByteLimit",
    "type": "uint256"
}
``` [4](#0-3) 

The protocol documentation also confirms the field is `u32` in the Rust implementation: [5](#0-4) 

The downstream consumer of this field in `process_l1_transaction` reads it as `u32` and passes it to `prepare_and_check_resources`: [6](#0-5) [7](#0-6) 

### Impact Explanation

An L1 priority transaction with `gasPerPubdataByteLimit = 2^32` (a valid `uint256` value per the Solidity interface) will be rejected by `try_from_buffer` before any execution logic runs. Since L1 priority transactions must be processed in order and cannot be skipped without halting the chain, a single such transaction in the priority queue can cause a chain-halt. This is a **valid-execution unprovability / state-transition bug**: a transaction that is valid per the L1 contract interface cannot be processed by the ZKsync OS bootloader.

### Likelihood Explanation

The L1 contracts that submit priority transactions accept `gasPerPubdataByteLimit` as a `uint256`. If those contracts do not enforce an upper bound of `u32::MAX`, any user can submit a transaction with a value of `2^32` or higher. The attacker only needs to pay the L1 gas cost for submitting one priority transaction. No privileged access is required.

### Recommendation

Change the type of `gas_per_pubdata_limit` from `u32` to `u64` (or `U256`) in `AbiEncodedTransaction` and update `parse_u32` to `parse_u64` (or `parse_u256`) accordingly. Ensure the downstream arithmetic in `prepare_and_check_resources` is updated to match. Alternatively, enforce a hard cap of `u32::MAX` on the L1 contract side and document this constraint explicitly.

### Proof of Concept

1. Construct an ABI-encoded L1→L2 transaction where the `gasPerPubdataByteLimit` word (the 5th 32-byte word) is set to `0x0000...0100000000` (i.e., `2^32 = 4294967296`).
2. Submit this transaction to the L1 priority queue.
3. When ZKsync OS processes the block containing this transaction, `try_from_buffer` calls `parse_u32`, which calls `validate_u32` on the value `4294967296`. Since `4294967296 > u32::MAX`, `validate_u32` returns `Err(())`.
4. `try_from_buffer` returns `Err(())`, the transaction cannot be constructed as an `AbiEncodedTransaction`.
5. The bootloader cannot process the L1 priority transaction, leading to a chain-halt condition.

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L50-51)
```rust
    #[allow(dead_code)]
    pub gas_per_pubdata_limit: ParsedValue<u32>,
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L149-149)
```rust
        let gas_per_pubdata_limit = parser.parse_u32()?;
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L421-431)
```rust
    fn parse_u32(&mut self) -> Result<ParsedValue<u32>, ()> {
        let (v, _) = U256BEPtr::try_from_slice(self.slice())?;
        let v = v.validate_u32()?;
        let value = ParsedValue {
            value: v,
            range: self.offset..self.offset + 32,
        };
        self.offset += 32;

        Ok(value)
    }
```

**File:** tests/contracts_sol/c_aa/out/DefaultAccount.abi.json (L345-350)
```json
                    {
                        "internalType": "uint256",
                        "name": "gasPerPubdataByteLimit",
                        "type": "uint256"
                    },
                    {
```

**File:** docs/bootloader/transaction_format.md (L16-16)
```markdown
| `gas_per_pubdata_limit`   | `u32`        | Maximum gas the user is willing to pay for a byte of [pubdata](https://docs.zksync.io/zksync-protocol/contracts/handling-pubdata).                                                                                               |
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L80-80)
```rust
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L435-446)
```rust
fn prepare_and_check_resources<
    'a,
    S: EthereumLikeTypes + 'a,
    Config: BasicBootloaderExecutionConfig,
>(
    system: &mut System<S>,
    transaction: &AbiEncodedTransaction<S::Allocator>,
    is_priority_op: bool,
    gas_limit: u64,
    gas_price: U256,
    gas_per_pubdata: u32,
    intrinsic_pubdata: u64,
```
