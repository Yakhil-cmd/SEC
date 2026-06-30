### Title
Pre-EIP-155 Legacy Transaction Replay Bypass Allows Cross-Chain Fund Theft - (File: `engine/src/engine.rs`)

---

### Summary

The `submit_with_alt_modexp` function in `engine/src/engine.rs` validates the chain ID only when it is present in the transaction (`if let Some(chain_id) = transaction.chain_id`). Pre-EIP-155 legacy transactions (signed with `v=27` or `v=28`) produce `chain_id: None` and therefore bypass this check entirely. Any pre-EIP-155 transaction signed on any other EVM chain (e.g., Ethereum mainnet) can be replayed on Aurora by an unprivileged attacker, draining the victim's Aurora ETH balance.

---

### Finding Description

`LegacyEthSignedTransaction::chain_id()` in `engine-transactions/src/legacy.rs` returns `Option<u64>`:

```rust
pub const fn chain_id(&self) -> Option<u64> {
    match self.v {
        0..=34 => None,          // v=27 or v=28: pre-EIP-155, no chain ID
        _ => Some((self.v - 35) / 2),
    }
}
```

When a legacy transaction uses `v=27` or `v=28`, `chain_id()` returns `None`. This value is propagated directly into `NormalizedEthTransaction.chain_id` in `engine-transactions/src/lib.rs`:

```rust
Legacy(tx) => Self {
    chain_id: tx.chain_id(),   // None for pre-EIP-155
    ...
}
```

In `engine/src/engine.rs`, the chain ID guard is:

```rust
// Validate the chain ID, if provided inside the signature:
if let Some(chain_id) = transaction.chain_id
    && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
{
    return Err(EngineErrorKind::InvalidChainId.into());
}
```

Because the guard is `if let Some(...)`, when `chain_id` is `None` the entire block is skipped. The transaction proceeds to nonce check and execution with no chain binding. The `submit` entrypoint's own documentation states: *"Must match `CHAIN_ID` to make sure it's signed for given chain vs replayed from another chain"* — but this guarantee does not hold for pre-EIP-155 transactions.

---

### Impact Explanation

An attacker who observes a pre-EIP-155 legacy transaction broadcast on Ethereum mainnet (or any other EVM chain) can replay the identical RLP-encoded bytes on Aurora via the `submit` entrypoint. If the victim holds ETH on Aurora at the same address and the nonce matches, the transaction executes on Aurora, transferring the victim's Aurora ETH to the attacker-controlled destination. This constitutes direct theft of user funds at rest.

---

### Likelihood Explanation

Pre-EIP-155 transactions (`v=27`/`v=28`) are still produced by hardware wallets in certain modes, older tooling, and chain-agnostic signing libraries. Aurora users who also hold ETH on Ethereum mainnet under the same key are directly exposed whenever their nonce on Aurora matches a pre-EIP-155 transaction they have broadcast elsewhere. The attacker requires no special privileges — only the ability to call the public `submit` NEAR function with the replayed transaction bytes.

---

### Recommendation

Reject pre-EIP-155 legacy transactions outright in `submit_with_alt_modexp`. Change the conditional guard from an optional check to a mandatory one:

```rust
// Require chain ID to be present and correct:
let tx_chain_id = transaction.chain_id
    .ok_or(EngineErrorKind::InvalidChainId)?;
if U256::from(tx_chain_id) != U256::from_big_endian(&state.chain_id) {
    return Err(EngineErrorKind::InvalidChainId.into());
}
```

This aligns with the stated intent of the `submit` entrypoint and eliminates the replay surface entirely.

---

### Proof of Concept

1. Alice holds 10 ETH on Aurora at address `0xAlice` with nonce `5`.
2. Alice previously broadcast a pre-EIP-155 transaction on Ethereum mainnet: send 1 ETH to `0xAttacker`, nonce=5, `v=27`.
3. Attacker captures the RLP-encoded bytes of that transaction.
4. Attacker calls `submit` on the Aurora NEAR contract with those bytes.
5. In `submit_with_alt_modexp`:
   - `EthTransactionKind::try_from` parses the legacy transaction successfully.
   - `NormalizedEthTransaction::try_from` sets `chain_id: None` because `v=27`.
   - The `if let Some(chain_id)` guard at line 1055 is **not entered** — no chain ID rejection.
   - `check_nonce` passes because Alice's Aurora nonce is also `5`.
   - The EVM executes the transfer: 1 ETH moves from `0xAlice` to `0xAttacker` on Aurora.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** engine-transactions/src/legacy.rs (L86-93)
```rust
    /// Returns chain id encoded in `v` parameter of the signature if that was done, otherwise None.
    #[must_use]
    pub const fn chain_id(&self) -> Option<u64> {
        match self.v {
            0..=34 => None,
            _ => Some((self.v - 35) / 2),
        }
    }
```

**File:** engine-transactions/src/lib.rs (L106-118)
```rust
            Legacy(tx) => Self {
                address: tx.sender()?,
                chain_id: tx.chain_id(),
                nonce: tx.transaction.nonce,
                gas_limit: tx.transaction.gas_limit,
                max_priority_fee_per_gas: tx.transaction.gas_price,
                max_fee_per_gas: tx.transaction.gas_price,
                to: tx.transaction.to,
                value: tx.transaction.value,
                data: tx.transaction.data,
                access_list: vec![],
                authorization_list: vec![],
            },
```

**File:** engine/src/engine.rs (L1054-1059)
```rust
    // Validate the chain ID, if provided inside the signature:
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }
```

**File:** engine/src/lib.rs (L272-282)
```rust
    /// Process signed Ethereum transaction.
    /// Must match `CHAIN_ID` to make sure it's signed for given chain vs replayed from another chain.
    #[unsafe(no_mangle)]
    pub extern "C" fn submit() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::evm_transactions::submit(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
