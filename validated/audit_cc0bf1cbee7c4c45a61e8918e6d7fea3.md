### Title
`gas_per_pubdata_limit` Parsed as `u32` Causes L1→L2 Transaction Rejection When Value Exceeds `u32::MAX` - (File: `basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs`)

---

### Summary

The `gas_per_pubdata_limit` field in `AbiEncodedTransaction` is typed as `u32` (max ~4.29 billion), while the canonical Solidity interface defines `gasPerPubdataByteLimit` as `uint256`. Any L1→L2 transaction whose `gasPerPubdataByteLimit` exceeds `u32::MAX` is rejected by the bootloader with `InvalidEncoding`, potentially locking user funds already committed on L1.

---

### Finding Description

`AbiEncodedTransaction` declares `gas_per_pubdata_limit` as `ParsedValue<u32>`: [1](#0-0) 

During parsing, `try_from_buffer` calls `parser.parse_u32()` for this field: [2](#0-1) 

`parse_u32` reads a full 256-bit ABI word and validates it fits in `u32`. If the encoded value exceeds `u32::MAX` (4,294,967,295), parsing returns `Err(())`, which propagates as: [3](#0-2) 

The Solidity interface, however, defines `gasPerPubdataByteLimit` as `uint256`: [4](#0-3) 

The transaction format documentation also specifies the field as `u32` in the Rust encoding, but the on-chain ABI slot is a full 256-bit word: [5](#0-4) 

When the parsed value is used downstream in `prepare_and_check_resources`, it is cast to `u64` for the `native_per_pubdata` calculation: [6](#0-5) 

The function signature accepts `gas_per_pubdata: u32`, confirming the narrowed type propagates throughout the L1 transaction processing path: [7](#0-6) 

---

### Impact Explanation

An L1→L2 transaction with `gasPerPubdataByteLimit > u32::MAX` (i.e., any value in the range `[4,294,967,296, 2^256-1]`) will fail to parse and be rejected with `InvalidTransaction::InvalidEncoding` before the bootloader can apply its "L1 transactions cannot be invalidated" resilience logic. The user's deposit is already committed on L1 at this point. Depending on L1 contract refund mechanics, this can result in locked or lost user funds. The type mismatch also means the bootloader silently enforces a tighter constraint than the Solidity interface advertises, creating a divergence between what L1 accepts and what L2 processes.

---

### Likelihood Explanation

The L1 contracts accept `gasPerPubdataByteLimit` as a full `uint256`. If the L1 bridge does not enforce an upper bound of `u32::MAX` on this field, any user or contract that sets `gasPerPubdataByteLimit > 4,294,967,295` will have their L1→L2 transaction silently rejected by the bootloader. This is reachable by any unprivileged transaction sender on L1 who sets an unusually large pubdata gas limit (e.g., `type(uint256).max` as a "no limit" sentinel, a pattern used in some DeFi protocols).

---

### Recommendation

Change the type of `gas_per_pubdata_limit` in `AbiEncodedTransaction` from `ParsedValue<u32>` to `ParsedValue<u64>` (or `ParsedValue<U256>` if the full range must be supported), update `parse_u32` to `parse_u64` (or `parse_u256`) at the corresponding parsing site, and propagate the wider type through `prepare_and_check_resources`. Alternatively, add an explicit upper-bound check on the L1 contract side to reject transactions with `gasPerPubdataByteLimit > u32::MAX` before they are committed to the priority queue.

---

### Proof of Concept

1. Construct an L1→L2 transaction with `gasPerPubdataByteLimit = 2^32` (i.e., `4,294,967,296`).
2. Submit it to the L1 bridge; the deposit is committed on L1.
3. The bootloader receives the transaction and calls `AbiEncodedTransaction::try_from_buffer`.
4. `parser.parse_u32()` reads the 256-bit word `0x0000...0100000000` and finds it exceeds `u32::MAX`, returning `Err(())`.
5. `Transaction::try_from_buffer` maps this to `TxError::Validation(InvalidTransaction::InvalidEncoding)`.
6. The transaction is rejected before the L1-resilience path (`prepare_and_check_resources` with saturating arithmetic) is ever reached.
7. User funds committed on L1 are not credited on L2. [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L50-51)
```rust
    #[allow(dead_code)]
    pub gas_per_pubdata_limit: ParsedValue<u32>,
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L148-149)
```rust
        let gas_limit = parser.parse_u64()?;
        let gas_per_pubdata_limit = parser.parse_u32()?;
```

**File:** basic_bootloader/src/bootloader/transaction/mod.rs (L87-90)
```rust
            TxEncodingFormat::Abi => {
                let tx = AbiEncodedTransaction::try_from_buffer(buffer)
                    .map_err(|_| TxError::Validation(InvalidTransaction::InvalidEncoding))?;
                Ok(Self::Abi(tx))
```

**File:** tests/contracts_sol/c_aa/contracts/IAccount.sol (L31-31)
```text
    uint256 gasPerPubdataByteLimit;
```

**File:** docs/bootloader/transaction_format.md (L16-16)
```markdown
| `gas_per_pubdata_limit`   | `u32`        | Maximum gas the user is willing to pay for a byte of [pubdata](https://docs.zksync.io/zksync-protocol/contracts/handling-pubdata).                                                                                               |
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L80-80)
```rust
    let gas_per_pubdata = transaction.gas_per_pubdata_limit.read();
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L443-445)
```rust
    gas_limit: u64,
    gas_price: U256,
    gas_per_pubdata: u32,
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L481-488)
```rust
    let native_per_pubdata = (gas_per_pubdata as u64)
        .checked_mul(native_per_gas)
        .unwrap_or_else(|| {
            system_log!(
                system,
                "Native per pubdata calculation for L1 tx overflows, using saturated arithmetic instead");
                u64::MAX
        });
```
