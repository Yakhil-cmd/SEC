### Title
Unvalidated `refund_recipient` in L1→L2 Transactions Causes Permanent Loss of Refund Funds — (`File: basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs`)

---

### Summary

The `refund_recipient` field (`reserved[1]`) of L1→L2 priority transactions is never validated in ZKsync OS. The bootloader unconditionally mints the unused-gas refund to whatever address the L1 transaction submitter provides, including `address(0)` or other permanently inaccessible addresses. This causes the refund portion of the deposit to be permanently lost.

---

### Finding Description

L1→L2 transactions carry a `refund_recipient` field encoded in `reserved[1]`. After execution, the bootloader computes the unused-gas refund and mints it to this address via `mint_base_token` → `transfer_from_treasury`.

In `validate_structure()`, the field is explicitly left unvalidated with a developer TODO:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
    _ => unreachable!(),
}
``` [1](#0-0) 

At refund time, the address is read from the raw transaction bytes with only a 160-bit range check (`u256_to_b160_checked`), and the refund is minted unconditionally:

```rust
let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
mint_base_token::<S, Config>(
    system, system_functions, memories.reborrow(),
    &to_refund_recipient, &refund_recipient, ...
)?;
``` [2](#0-1) 

`u256_to_b160_checked` only asserts the value fits in 160 bits — it does not reject `address(0)` or system-contract addresses:

```rust
pub fn u256_to_b160_checked(src: U256) -> B160 {
    assert!(src.as_limbs()[3] == 0 && src.as_limbs()[2] < (1u64 << 32));
    ...
}
``` [3](#0-2) 

The `ZKsyncL1Tx` struct documents `refund_recipient` as a plain caller-supplied field with no constraints: [4](#0-3) 

The test builder defaults `refund_recipient` to `address(0)` via `unwrap_or_default()`: [5](#0-4) 

---

### Impact Explanation

When `refund_recipient` is `address(0)` (or any permanently inaccessible address), the refund amount — `(gas_limit - gas_used) * gas_price` — is transferred from the treasury to `address(0)` and is permanently unrecoverable. These are real base tokens that were locked on L1 and bridged to L2. The loss is proportional to the unused gas in the transaction.

For a transaction with `gas_limit = 100,000`, `gas_used = 21,000`, and `gas_price = 1,000`, the lost refund is `79,000,000` wei of base token per transaction.

---

### Likelihood Explanation

The default value of `refund_recipient` in the test builder is `address(0)`: [6](#0-5) 

Bridge contracts or integrators that do not explicitly set `refund_recipient` will silently burn all gas refunds. The `// TODO: validate address?` comment confirms the developers are aware this validation is missing but have not yet implemented it. [7](#0-6) 

---

### Recommendation

In `validate_structure()`, reject `reserved[1]` values that decode to `address(0)`. Alternatively, when `refund_recipient` is `address(0)`, fall back to `transaction.from` as the refund destination, matching the behavior of the L2 transaction flow: [8](#0-7) 

---

### Proof of Concept

1. Submit an L1→L2 priority transaction with:
   - `gas_limit = 100_000`
   - `gas_price = 1_000`
   - `to_mint = gas_limit * gas_price` (= 100,000,000 wei)
   - `refund_recipient = address(0)` (the default)
   - A simple ETH transfer as the body (uses ~21,000 gas)

2. The bootloader executes the transaction, computes `gas_used ≈ 21,000`, and calculates `to_refund_recipient = (100,000 - 21,000) * 1,000 = 79,000,000 wei`.

3. At line 337 of `process_l1_transaction.rs`, `refund_recipient` resolves to `B160::ZERO` (address(0)). [9](#0-8) 

4. `transfer_from_treasury` credits 79,000,000 wei to `address(0)`: [10](#0-9) 

5. No one controls `address(0)`. The 79,000,000 wei is permanently inaccessible. The treasury has been debited for the full `to_mint` amount, but 79,000,000 wei of that is unrecoverable.

### Citations

**File:** basic_bootloader/src/bootloader/transaction/abi_encoded/mod.rs (L267-273)
```rust
        // reserved[1] = refund recipient for l1 to l2 and upgrade txs
        match tx_type {
            Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
                // TODO: validate address?
            }
            _ => unreachable!(),
        }
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-348)
```rust
    if to_refund_recipient > U256::ZERO {
        let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
        mint_base_token::<S, Config>(
            system,
            system_functions,
            memories.reborrow(),
            &to_refund_recipient,
            &refund_recipient,
            l1_chain_id,
            &mut inf_resources,
            tracer,
            validator,
        )
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L813-821)
```rust
    let _ = system
        .io
        .update_account_nominal_token_balance(
            zk_ee::execution_environment_type::ExecutionEnvironmentType::EVM,
            resources,
            to,
            nominal_token_value,
            false, // false = add to balance
            fee_payment_in_simulation,
```

**File:** zk_ee/src/utils/integer_utils.rs (L133-143)
```rust
pub fn u256_to_b160_checked(src: U256) -> B160 {
    assert!(src.as_limbs()[3] == 0 && src.as_limbs()[2] < (1u64 << 32));
    let mut result = B160::ZERO;
    unsafe {
        result.as_limbs_mut()[0] = src.as_limbs()[0];
        result.as_limbs_mut()[1] = src.as_limbs()[1];
        result.as_limbs_mut()[2] = src.as_limbs()[2];
    }

    result
}
```

**File:** tests/common/src/zksync_tx/l1_tx.rs (L25-27)
```rust
    /// The recipient of the refund for the transaction on L2. If the transaction fails, then this
    /// address will receive the `value` of this transaction.
    pub refund_recipient: Address,
```

**File:** tests/rig/src/utils/mod.rs (L403-414)
```rust
            to_mint: self.to_mint.unwrap_or_else(|| {
                alloy::primitives::U256::from(self.gas_limit)
                    * alloy::primitives::U256::from(self.gas_price)
            }),
            input: self.input.into(),
            nonce: self.nonce,
            refund_recipient: self.refund_recipient.unwrap_or_default(),
            factory_deps: self.factory_deps,
            gas_per_pubdata_byte_limit: self.gas_per_pubdata_byte_limit,
            value: self.value,
        }
        .into()
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L456-456)
```rust
            let refund_recipient = transaction.from();
```
