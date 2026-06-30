### Title
Pre-EIP-155 Legacy Transactions Accepted Without Chain ID Validation Enables Cross-Chain Replay Attacks - (File: engine-transactions/src/legacy.rs)

### Summary
Aurora Engine accepts pre-EIP-155 legacy transactions (signature `v = 27` or `v = 28`) that carry no chain ID. The engine's chain ID validation is gated on `if let Some(chain_id)`, so when a legacy transaction omits the chain ID entirely, the check is silently bypassed. An attacker can replay any pre-EIP-155 transaction signed on Ethereum mainnet (or any other EVM chain) directly against Aurora Engine, executing it against the signer's Aurora balance without their consent.

### Finding Description
The `sender()` method of `LegacyEthSignedTransaction` explicitly accepts `v = 27` or `v = 28` as valid recovery IDs, which are the pre-EIP-155 values that carry no chain ID:

```rust
// engine-transactions/src/legacy.rs lines 68-78
let (chain_id, rec_id) = match self.v {
    0..=26 | 29..=34 => return Err(Error::InvalidV),
    27..=28 => (
        None,                                          // ← no chain ID
        u8::try_from(self.v - 27).map_err(|_e| Error::InvalidV)?,
    ),
    _ => (
        Some((self.v - 35) / 2),
        u8::try_from((self.v - 35) % 2).map_err(|_e| Error::InvalidV)?,
    ),
};
``` [1](#0-0) 

The companion `chain_id()` accessor returns `None` for any `v ≤ 34`:

```rust
// engine-transactions/src/legacy.rs lines 88-93
pub const fn chain_id(&self) -> Option<u64> {
    match self.v {
        0..=34 => None,
        _ => Some((self.v - 35) / 2),
    }
}
``` [2](#0-1) 

When the transaction is normalized, this `None` propagates directly into `NormalizedEthTransaction.chain_id`:

```rust
// engine-transactions/src/lib.rs lines 106-118
Legacy(tx) => Self {
    address: tx.sender()?,
    chain_id: tx.chain_id(),   // ← None for pre-EIP-155
    ...
},
``` [3](#0-2) 

The engine's chain ID guard in `submit_transaction` is conditional:

```rust
// engine/src/engine.rs lines 1055-1059
if let Some(chain_id) = transaction.chain_id
    && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
{
    return Err(EngineErrorKind::InvalidChainId.into());
}
``` [4](#0-3) 

When `chain_id` is `None`, the entire `if let` arm is skipped. The transaction proceeds through nonce checking, gas validation, and full EVM execution with no chain binding whatsoever.

### Impact Explanation
**Critical — Direct theft of user funds.**

Any pre-EIP-155 transaction that a user has ever broadcast on Ethereum mainnet, Ethereum Classic, or any other EVM-compatible chain is replayable verbatim on Aurora Engine. The attacker's steps are:

1. Collect any historical pre-EIP-155 signed transaction from a public mempool or block explorer (the victim's nonce on Aurora must match).
2. Submit the raw bytes to Aurora Engine's `submit()` entrypoint.
3. The engine recovers the correct sender address, skips chain ID validation, and executes the transaction — transferring ETH or invoking contracts — against the victim's Aurora balance.

Because the attacker supplies only calldata (the raw signed transaction bytes) and needs no private key or privileged access, this is a fully unprivileged, externally reachable exploit path.

### Likelihood Explanation
**Medium.** Pre-EIP-155 transactions are less common after EIP-155 became the default in 2016, but they remain valid and are still produced by certain hardware wallets, offline signing tools, and legacy dApps. A targeted attacker monitoring public mempools for pre-EIP-155 transactions, or scanning historical chain data, can identify victims whose Aurora nonces happen to align. The attack requires no special capability beyond submitting a NEAR transaction to the Aurora contract.

### Recommendation
Reject legacy transactions that carry no chain ID at the protocol boundary. In `submit_transaction` (or equivalently in `TryFrom<EthTransactionKind> for NormalizedEthTransaction`), treat a `None` chain ID as an error rather than a pass-through:

```rust
// Proposed guard in engine/src/engine.rs
let chain_id = transaction.chain_id
    .ok_or(EngineErrorKind::InvalidChainId)?;
if U256::from(chain_id) != U256::from_big_endian(&state.chain_id) {
    return Err(EngineErrorKind::InvalidChainId.into());
}
```

This mirrors the approach already taken for EIP-2930, EIP-1559, and EIP-7702 transactions, all of which embed a mandatory `chain_id` field and always produce `Some(chain_id)` in `NormalizedEthTransaction`. [5](#0-4) 

### Proof of Concept
1. On Ethereum mainnet (or any EVM chain), construct and sign a legacy transaction with `v = 27` or `v = 28` (pre-EIP-155 mode). Any standard library supports this by omitting the chain ID from the signing payload.
2. Ensure the sender's nonce on Aurora matches the transaction nonce (nonce 0 for a fresh account is trivially satisfied).
3. Submit the raw RLP-encoded bytes to Aurora Engine's `submit()` NEAR method.
4. Observe that `EthTransactionKind::try_from` parses it as `Legacy`, `sender()` succeeds with `chain_id = None`, the `if let Some(chain_id)` guard in `submit_transaction` is skipped, and the EVM executes the transaction — debiting the victim's Aurora ETH balance.

The attacker controls the entire input (the signed transaction bytes) and requires no credentials or elevated permissions beyond the ability to call the public `submit` entrypoint.

### Citations

**File:** engine-transactions/src/legacy.rs (L68-78)
```rust
        let (chain_id, rec_id) = match self.v {
            0..=26 | 29..=34 => return Err(Error::InvalidV),
            27..=28 => (
                None,
                u8::try_from(self.v - 27).map_err(|_e| Error::InvalidV)?,
            ),
            _ => (
                Some((self.v - 35) / 2),
                u8::try_from((self.v - 35) % 2).map_err(|_e| Error::InvalidV)?,
            ),
        };
```

**File:** engine-transactions/src/legacy.rs (L88-93)
```rust
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

**File:** engine-transactions/src/lib.rs (L119-158)
```rust
            Eip2930(tx) => Self {
                address: tx.sender()?,
                chain_id: Some(tx.transaction.chain_id),
                nonce: tx.transaction.nonce,
                gas_limit: tx.transaction.gas_limit,
                max_priority_fee_per_gas: tx.transaction.gas_price,
                max_fee_per_gas: tx.transaction.gas_price,
                to: tx.transaction.to,
                value: tx.transaction.value,
                data: tx.transaction.data,
                access_list: tx.transaction.access_list,
                authorization_list: vec![],
            },
            Eip1559(tx) => Self {
                address: tx.sender()?,
                chain_id: Some(tx.transaction.chain_id),
                nonce: tx.transaction.nonce,
                gas_limit: tx.transaction.gas_limit,
                max_priority_fee_per_gas: tx.transaction.max_priority_fee_per_gas,
                max_fee_per_gas: tx.transaction.max_fee_per_gas,
                to: tx.transaction.to,
                value: tx.transaction.value,
                data: tx.transaction.data,
                access_list: tx.transaction.access_list,
                authorization_list: vec![],
            },
            Eip7702(tx) => Self {
                address: tx.sender()?,
                chain_id: Some(tx.transaction.chain_id),
                nonce: tx.transaction.nonce,
                gas_limit: tx.transaction.gas_limit,
                max_priority_fee_per_gas: tx.transaction.max_priority_fee_per_gas,
                max_fee_per_gas: tx.transaction.max_fee_per_gas,
                to: Some(tx.transaction.to),
                value: tx.transaction.value,
                data: tx.transaction.data.clone(),
                access_list: tx.transaction.access_list.clone(),
                authorization_list: tx.authorization_list()?,
            },
        })
```

**File:** engine/src/engine.rs (L1055-1059)
```rust
    if let Some(chain_id) = transaction.chain_id
        && U256::from(chain_id) != U256::from_big_endian(&state.chain_id)
    {
        return Err(EngineErrorKind::InvalidChainId.into());
    }
```
