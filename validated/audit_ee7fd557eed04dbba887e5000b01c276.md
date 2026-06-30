### Title
Cross-Chain Signature Replay via Accepted Pre-EIP-155 Legacy Transactions Without Chain ID - (File: engine-transactions/src/legacy.rs)

---

### Summary

Aurora Engine deliberately re-allows legacy Ethereum transactions signed without a chain ID (EIP-155 replay protection disabled, `v = 27` or `v = 28`). The engine's chain ID validation is conditional on the chain ID being present in the signature. When it is absent, the check is skipped entirely, allowing any such transaction to be replayed verbatim across all Aurora deployments (mainnet chain ID `1313161554`, testnet `1313161555`, local `1313161556`) by any unprivileged actor who observes the original transaction.

---

### Finding Description

`LegacyEthSignedTransaction::sender()` in `engine-transactions/src/legacy.rs` decodes the `v` field of a signed transaction. When `v = 27` or `v = 28`, it returns `chain_id = None`, meaning the transaction was signed without EIP-155 replay protection: [1](#0-0) 

The resulting `NormalizedEthTransaction` carries `chain_id: None` for this case: [2](#0-1) 

In `engine/src/engine.rs`, the chain ID validation inside `submit_with_alt_modexp` is guarded by `if let Some(chain_id) = transaction.chain_id`. When `chain_id` is `None`, the entire guard is skipped and the transaction is accepted unconditionally: [3](#0-2) 

The engine's own changelog confirms this is a deliberate re-allowance: v2.4.0 blocked pre-EIP-155 transactions, but v2.6.0 re-enabled them for EIP-1820 compatibility: [4](#0-3) 

Aurora Engine is deployed on multiple NEAR networks with distinct chain IDs. A legacy transaction signed without a chain ID is cryptographically identical across all of them. Any observer of such a transaction on one network can submit the same raw bytes to any other Aurora deployment.

---

### Impact Explanation

**Critical — Direct theft of user funds.**

If a user submits a pre-EIP-155 legacy transaction (e.g., a value transfer or ERC-20 approval) on Aurora mainnet, an attacker who observes the transaction bytes can replay the identical bytes on Aurora testnet (or any other Aurora deployment) via the public `submit()` entry point. If the sender's account on the target network has the same nonce and sufficient balance, the transaction executes identically, draining the sender's funds on that network without their consent.

The nonce mechanism does not prevent this: a user who has performed the same number of transactions on both networks will have the same nonce on both, making replay directly applicable.

---

### Likelihood Explanation

**Medium.**

- Pre-EIP-155 transactions are produced by older Ethereum tooling, hardware wallets in legacy mode, and contracts that use `ecrecover` with raw signatures (e.g., EIP-1820 factory deployments). The engine explicitly accepts them.
- Aurora mainnet and testnet share the same contract ID (`aurora`) and are both publicly accessible. Users who test on testnet before mainnet frequently have matching nonce sequences.
- The attacker entry path requires only observing a submitted transaction (public on-chain data) and re-submitting it to a different Aurora NEAR account — no special privileges are needed.
- The `submit()` function is callable by any NEAR account: [5](#0-4) 

---

### Recommendation

1. **Reject pre-EIP-155 transactions at the engine level.** Remove the `27..=28` arm from `LegacyEthSignedTransaction::sender()` or add an explicit check in `submit_with_alt_modexp` that rejects transactions where `transaction.chain_id` is `None`.
2. **If EIP-1820 compatibility must be preserved**, handle it as a narrow special case (e.g., only for the known EIP-1820 deployer address and transaction hash) rather than accepting all chain-ID-free transactions.
3. Alternatively, enforce that `transaction.chain_id` must always be `Some(engine_chain_id)` — matching the behavior that was in place in v2.4.0 before the regression in v2.6.0.

---

### Proof of Concept

1. On Aurora testnet, generate a legacy transaction with `chain_id = None` (v=27 or v=28) transferring ETH from address `A` to address `B`. Submit it. It succeeds.
2. Observe the raw RLP-encoded transaction bytes from the NEAR receipt.
3. Submit the identical bytes to Aurora mainnet's `submit()` function (callable by any NEAR account as `relay.aurora` or similar).
4. If address `A` on mainnet has the same nonce and sufficient balance, the transfer executes on mainnet — draining `A`'s mainnet funds without `A`'s authorization.

The root cause is confirmed at:
- `engine-transactions/src/legacy.rs` lines 70–72: `v = 27|28` → `chain_id = None`
- `engine/src/engine.rs` lines 1055–1059: `if let Some(chain_id)` skips validation when `chain_id` is `None` [6](#0-5) [7](#0-6)

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

**File:** CHANGES.md (L585-587)
```markdown
- If the `v` byte of secp256k1 is incorrect, it now returns correctly an empty vector by [@RomanHodulak]. ([#513])
- Original ETH transactions which do not contain a Chain ID are allowed again to allow for use of [EIP-1820] by [@joshuajbouw]. ([#520])
- Ecrecover didn't reject `r`, `s` values larger than curve order by [@RomanHodulak]. ([#515])
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
