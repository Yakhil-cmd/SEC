### Title
Narrow `u32` Type for `gas_per_pubdata_limit` in `AbiEncodedTransaction` Causes Silent L1→L2 Transaction Rejection for Values Exceeding `u32::MAX` - (File: `basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs`)

---

### Summary

The `gas_per_pubdata_limit` field in `AbiEncodedTransaction` is parsed and stored as `u32`, while the external-facing API and L1 contract interface both accept `u128`/`uint256` for this field. Any L1→L2 priority transaction submitted with `gas_per_pubdata_limit > u32::MAX` (~4.29 billion) will be rejected at the parsing stage with `InvalidTransaction::InvalidEncoding`, permanently locking the user's deposited funds in the priority queue without recourse.

---

### Finding Description

In `AbiEncodedTransaction`, the `gas_per_pubdata_limit` field is declared as `ParsedValue<u32>` and parsed via `parser.parse_u32()`:

```rust
// basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs, line 51
pub gas_per_pubdata_limit: ParsedValue<u32>,

// line 149
let gas_per_pubdata_limit = parser.parse_u32()?;
```

The on-wire ABI encoding stores every field as a full 256-bit word. `parse_u32` validates that the upper 28 bytes are zero; if the submitted value exceeds `u32::MAX`, parsing returns `Err(())`, which is mapped to `InvalidTransaction::InvalidEncoding` in the transaction dispatch path:

```rust
// basic_bootloader/src/bootloader/transaction/mod.rs, line 88-89
let tx = AbiEncodedTransaction::try_from_buffer(buffer)
    .map_err(|_| TxError::Validation(InvalidTransaction::InvalidEncoding))?;
```

In contrast, every external-facing layer that constructs or accepts this field uses a wider type:

- **Public API** (`api/src/helpers.rs`, line 149): `gas_per_pubdata_byte_limit: Option<u128>`
- **L1 transaction builder** (`tests/common/src/zksync_tx/l1_tx.rs`, line 18): `pub gas_per_pubdata_byte_limit: u128`
- **L1 contract ABI** (`tests/contracts_sol/c_aa/out/IAccount.abi.json`, line 39): `gasPerPubdataByteLimit` typed as `uint256`
- **Upgrade transaction** (`tests/common/src/zksync_tx/upgrade_tx.rs`, line 18): `pub gas_per_pubdata_byte_limit: u128`

The `prepare_and_check_resources` function in the L1 transaction flow receives `gas_per_pubdata: u32`, confirming the narrowing propagates into fee accounting:

```rust
// basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs, line 445
gas_per_pubdata: u32,
```

---

### Impact Explanation

L1→L2 priority transactions are submitted through the L1 priority queue and **cannot be invalidated or skipped** — the bootloader must process them. If parsing fails before the bootloader identifies the transaction as an L1 type, the transaction is rejected as `InvalidEncoding` before the L1-resilience path (saturating arithmetic, no-invalidation policy) is ever reached. The user's deposited ETH/base token (`reserved[0]` = `to_mint`) is locked in the bridge with no mechanism for recovery on L2. The user would need to rely on L1-side refund logic, which may not exist or may be difficult to trigger.

Additionally, the `gas_per_pubdata_limit` value directly controls the `native_per_pubdata` resource budget for the transaction. Silent truncation (if the parser were changed to truncate rather than reject) would cause the transaction to execute with a drastically underestimated pubdata budget, leading to incorrect native resource accounting and potential mid-execution reverts.

---

### Likelihood Explanation

The L1 contract interface exposes `gasPerPubdataByteLimit` as `uint256`. During periods of high L1 data costs (e.g., blob fee spikes), operators or automated bridges may set `gas_per_pubdata_byte_limit` to values exceeding `u32::MAX` (~4.29 billion). A value of `5 * 10^9` (5 billion), which is plausible if gas prices are denominated in wei-scale units, already exceeds `u32::MAX`. Any bridge frontend or SDK that passes a `u128` value without capping at `u32::MAX` will produce a permanently stuck transaction. The mismatch between the `u128` API surface and the `u32` parser is an attacker-controllable input path requiring no privileged access.

---

### Recommendation

Change the `gas_per_pubdata_limit` field type in `AbiEncodedTransaction` from `u32` to `u64` (matching `gas_limit`) or `u128` to align with the external interface:

```rust
// basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs
pub gas_per_pubdata_limit: ParsedValue<u64>,  // or u128
// ...
let gas_per_pubdata_limit = parser.parse_u64()?;  // or parse_u128
```

Update `prepare_and_check_resources` and all downstream callers to accept `u64`/`u128` accordingly. Add a validation check on the L1 side (or in `validate_structure`) to enforce an explicit maximum if a protocol-level cap is desired.

---

### Proof of Concept

1. Construct an L1→L2 priority transaction with `gas_per_pubdata_byte_limit = u32::MAX as u128 + 1` (i.e., `4_294_967_296u128`) using `ZKsyncL1Tx` or `encode_abi_tx`.
2. The value is encoded as a full 256-bit word with the upper bits set.
3. Submit the transaction to the L2 bootloader.
4. `AbiEncodedTransaction::try_from_buffer` calls `parser.parse_u32()`, which detects the non-zero upper bits and returns `Err(())`.
5. The transaction is rejected with `InvalidTransaction::InvalidEncoding` before the L1-resilience path is entered.
6. The user's deposited base token (`to_mint` in `reserved[0]`) is permanently inaccessible on L2. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L49-52)
```rust
    /// The maximum amount of gas the user is willing to pay for a byte of pubdata.
    #[allow(dead_code)]
    pub gas_per_pubdata_limit: ParsedValue<u32>,
    /// The maximum fee per gas that the user is willing to pay.
```

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L147-150)
```rust
        let to = parser.parse_address()?;
        let gas_limit = parser.parse_u64()?;
        let gas_per_pubdata_limit = parser.parse_u32()?;
        let max_fee_per_gas = parser.parse_u256()?;
```

**File:** basic_bootloader/src/bootloader/transaction/mod.rs (L87-90)
```rust
            TxEncodingFormat::Abi => {
                let tx = AbiEncodedTransaction::try_from_buffer(buffer)
                    .map_err(|_| TxError::Validation(InvalidTransaction::InvalidEncoding))?;
                Ok(Self::Abi(tx))
```

**File:** api/src/helpers.rs (L148-150)
```rust
    gas_limit: u128,
    gas_per_pubdata_byte_limit: Option<u128>,
    max_fee_per_gas: u128,
```

**File:** tests/common/src/zksync_tx/l1_tx.rs (L17-19)
```rust
    pub gas_limit: u128,
    pub gas_per_pubdata_byte_limit: u128,
    pub max_fee_per_gas: u128,
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L443-446)
```rust
    gas_limit: u64,
    gas_price: U256,
    gas_per_pubdata: u32,
    intrinsic_pubdata: u64,
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
