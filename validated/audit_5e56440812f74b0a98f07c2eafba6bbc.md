### Title
Gas Refund Sent to Zero Address When `refund_recipient` Is Not Set in L1→L2 Priority Transactions - (`File: basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs`)

### Summary

In `process_l1_transaction`, when an L1→L2 priority transaction has an unset (zero) `refund_recipient` field (`reserved[1]`), the unused gas refund is minted directly to the zero address (`0x0000...0000`) rather than falling back to the transaction sender (`from`). This permanently burns the refunded tokens from the treasury without crediting any user.

### Finding Description

The `process_l1_transaction` function handles L1→L2 priority transactions. After execution, it computes the unused gas refund amount (`to_refund_recipient`) and then reads the refund recipient address unconditionally from `transaction.reserved[1]`:

```rust
// Line 336-337
if to_refund_recipient > U256::ZERO {
    let refund_recipient = u256_to_b160_checked(transaction.reserved[1].read());
```

The `reserved[1]` field encodes the `refund_recipient` address for L1 transactions. When a user submits an L1→L2 transaction without explicitly setting a `refund_recipient`, this field defaults to `U256::ZERO`, which decodes to `B160::ZERO` (the zero address). The code then calls `mint_base_token` to transfer the refund to `B160::ZERO`.

There is no fallback: the code does not check whether `reserved[1]` is zero and substitute `transaction.from` as the refund recipient. The `validate_structure` function explicitly notes `// TODO: validate address?` for `reserved[1]`, confirming no validation is enforced.

The L1 contract layer is supposed to enforce that `refund_recipient` is set, but the ZKsync OS bootloader performs no such check. The `L1TxBuilder` test helper also defaults `refund_recipient` to `Address::default()` (zero address) when not explicitly set, confirming this is a reachable state.

The analogous vulnerability in the Axelar report was that the `sender` address was used as the refund address instead of the actual fee payer. Here, the ZKsync OS analog is that the `refund_recipient` field is used verbatim even when it is zero, instead of falling back to `transaction.from`.

### Impact Explanation

When `refund_recipient` is zero (either by user omission or by a bridge contract that does not set it):

1. The treasury (`BASE_TOKEN_HOLDER_ADDRESS`) is debited by `to_refund_recipient` tokens.
2. Those tokens are credited to `address(0)`, which is permanently inaccessible.
3. The actual transaction sender (`from`) receives no refund for unused gas.
4. The loss is proportional to `(gas_limit - gas_used) * gas_price`, which can be substantial for high-gas-limit transactions.

This is a direct, permanent loss of user funds (base tokens) from the treasury to the zero address.

### Likelihood Explanation

The `refund_recipient` field is set by the L1 bridge contract when constructing the priority queue request. If any bridge integration omits this field or sets it to zero (e.g., a simplified bridge, a direct L1 call, or a misconfigured integration), the refund is permanently lost. The `L1TxBuilder` test helper defaults `refund_recipient` to zero when not set, confirming this is a realistic scenario. The `validate_structure` function explicitly skips address validation for `reserved[1]` with a `TODO` comment.

### Recommendation

In `process_l1_transaction`, before minting the refund, check whether `reserved[1]` is zero and substitute `transaction.from` as the fallback refund recipient:

```rust
let refund_recipient = {
    let raw = transaction.reserved[1].read();
    if raw.is_zero() {
        transaction.from.read()
    } else {
        u256_to_b160_checked(raw)
    }
};
```

Additionally, the `validate_structure` function should validate that `reserved[1]` encodes a valid (non-zero) address for L1 and upgrade transaction types, resolving the existing `TODO`.

### Proof of Concept

1. An L1→L2 priority transaction is submitted with `refund_recipient = address(0)` (zero address in `reserved[1]`).
2. The transaction executes successfully but uses less gas than `gas_limit`.
3. `to_refund_recipient = (gas_limit - gas_used) * gas_price > 0`.
4. The guard `if to_refund_recipient > U256::ZERO` passes.
5. `refund_recipient = u256_to_b160_checked(U256::ZERO) = B160::ZERO`.
6. `mint_base_token` is called: treasury balance decreases by `to_refund_recipient`, and `address(0)` balance increases by the same amount.
7. The transaction sender receives no refund; the tokens are permanently burned.

The `L1TxBuilder::build()` helper confirms this is reachable: when `.refund_recipient()` is not called, `refund_recipient: self.refund_recipient.unwrap_or_default()` produces `Address::ZERO`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/process_l1_transaction.rs (L335-360)
```rust
    // Mint refund portion of the deposit to the refund recipient.
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
        .map_err(|e| -> BootloaderSubsystemError {
            match e.root_cause() {
                RootCause::Runtime(RuntimeError::OutOfErgs(_)) => {
                    internal_error!("Out of ergs on infinite ergs").into()
                }
                RootCause::Runtime(RuntimeError::FatalRuntimeError(_)) => {
                    internal_error!("Out of native on infinite").into()
                }
                _ => e,
            }
        })?;
    }
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

**File:** tests/rig/src/utils/mod.rs (L396-415)
```rust
    pub fn build(self) -> ZKsyncTxEnvelope {
        ZKsyncL1Tx {
            from: self.from,
            to: self.to,
            max_fee_per_gas: self.gas_price,
            max_priority_fee_per_gas: self.gas_price,
            gas_limit: self.gas_limit,
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
    }
```
