### Title
Unvalidated Zero `refund_recipient` in L1→L2 Transactions Causes Permanent Base-Token Loss - (File: `basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

---

### Summary

In L1→L2 priority transactions, the `refund_recipient` address is read directly from `transaction.reserved[1]` without any zero-address check. When this field is zero (the default for unset `ZKsyncL1Tx::refund_recipient`), the bootloader mints the unused-gas refund to `address(0)`, permanently burning those base tokens.

---

### Finding Description

In `process_l1_transaction.rs`, after computing the refund amount, the code guards only on the *amount* being nonzero before minting:

```rust
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
    mint_base_token::<S, Config>(
        system,
        ...
        &to_refund_recipient,
        &refund_recipient,   // ← can be B160::ZERO
        ...
    )
``` [1](#0-0) 

There is no check that `refund_recipient != B160::ZERO`. The transaction structure validation in `abi_encoded/mod.rs` explicitly skips this check with a `TODO`:

```rust
// reserved[1] = refund recipient for l1 to l2 and upgrade txs
match tx_type {
    Self::L1_L2_TX_TYPE | Self::UPGRADE_TX_TYPE => {
        // TODO: validate address?
    }
``` [2](#0-1) 

The `ZKsyncL1Tx` struct derives `Default`, making `refund_recipient: Address` default to `Address::ZERO`: [3](#0-2) 

The `refund_recipient` is encoded into `reserved[1]` as a `U256`: [4](#0-3) 

An existing test explicitly confirms that a zero `refund_recipient` is accepted and the refund is credited to `address(0)` without error: [5](#0-4) 

---

### Impact Explanation

When an L1→L2 transaction is submitted with `refund_recipient = address(0)` (either intentionally or by omission), the unused-gas refund — computed as `(gas_limit - gas_used) * gas_price` — is minted to `B160::ZERO`. These base tokens are permanently unrecoverable. For transactions with large gas limits and low actual consumption, the burned amount can be substantial. The loss is irreversible because no contract controls `address(0)`.

---

### Likelihood Explanation

**High.** The `ZKsyncL1Tx` struct's `refund_recipient` field defaults to `Address::ZERO` via `#[derive(Default)]`. Any L1 bridge integration that constructs an `L1Tx` without explicitly setting `refund_recipient` will silently burn the refund. Additionally, a malicious L1 sender can deliberately set `refund_recipient = address(0)` to burn the refund of any L1→L2 transaction they submit. No privileged access is required — any account that can submit an L1 priority transaction can trigger this path.

---

### Recommendation

Add a zero-address guard before calling `mint_base_token` for the refund:

```rust
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
    if refund_recipient == B160::ZERO {
        return Err(internal_error!("refund_recipient is zero address").into());
    }
    mint_base_token::<S, Config>(..., &refund_recipient, ...)?;
}
```

Alternatively, enforce the check in `validate_structure` in `abi_encoded/mod.rs` by resolving the `TODO: validate address?` comment and rejecting transactions where `reserved[1]` decodes to the zero address. [2](#0-1) 

---

### Proof of Concept

1. Construct an L1→L2 priority transaction using `ZKsyncL1Tx::default()` (or any `L1TxBuilder` that omits `.refund_recipient(...)`). The `refund_recipient` field defaults to `Address::ZERO`, which encodes as `reserved[1] = U256::ZERO`.
2. Set `gas_limit` significantly higher than the actual execution cost (e.g., `gas_limit = 100_000`, actual usage ~21,000).
3. Submit the transaction. The bootloader computes `to_refund_recipient = (100_000 - 21_000) * gas_price > 0`.
4. Because `to_refund_recipient > U256::ZERO`, the guard passes and `mint_base_token` is called with `refund_recipient = B160::ZERO`.
5. The refund tokens are credited to `address(0)` and permanently lost.

This is directly confirmed by the existing test `test_treasury_based_token_distribution_regression` which uses `refund_recipient = address("0000...0000")` and asserts the refund is successfully sent there: [5](#0-4) [6](#0-5)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L336-344)
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
```

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

**File:** tests/common/src/zksync_tx/l1_tx.rs (L13-32)
```rust
#[derive(Debug, Default, Clone)]
pub struct ZKsyncL1Tx {
    pub from: Address,
    pub to: Address,
    pub gas_limit: u128,
    pub gas_per_pubdata_byte_limit: u128,
    pub max_fee_per_gas: u128,
    pub max_priority_fee_per_gas: u128,
    pub nonce: u128,
    pub value: U256,
    /// The amount of base token that should be minted on L2 as the result of this transaction.
    pub to_mint: U256,
    /// The recipient of the refund for the transaction on L2. If the transaction fails, then this
    /// address will receive the `value` of this transaction.
    pub refund_recipient: Address,
    /// data: An unlimited size byte array specifying the input data of the message call.
    pub input: Bytes,
    /// The set of L2 bytecode hashes whose preimages were shown on L1.
    pub factory_deps: Vec<B256>,
}
```

**File:** tests/common/src/zksync_tx/l1_tx.rs (L60-66)
```rust
        let refund_recipient: U160 = self.refund_recipient.into();
        let reserved = [
            self.to_mint,
            U256::from(refund_recipient),
            U256::ZERO,
            U256::ZERO,
        ];
```

**File:** tests/instances/transactions/src/lib.rs (L1843-1843)
```rust
    let refund_recipient = address!("0000000000000000000000000000000000000000"); // refund recipient (zero address)
```

**File:** tests/instances/transactions/src/lib.rs (L1937-1943)
```rust
    // Verify refund recipient received value from treasury (not minted)
    let refund_recipient_increase =
        refund_recipient_final_balance - refund_recipient_initial_balance;
    assert_eq!(
        refund_recipient_increase, total_to_refund_recipient,
        "Refund recipient should receive correct refund amount from treasury"
    );
```
