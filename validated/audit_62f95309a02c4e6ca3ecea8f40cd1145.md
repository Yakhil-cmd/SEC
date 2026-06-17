### Title
EIP-7702 Failed Authorization Entries Overcharged by `NEWACCOUNT - PER_AUTH_BASE_COST` Gas — (`File: basic_bootloader/src/bootloader/transaction/authorization_list.rs`, `basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs`)

---

### Summary

ZKsync OS pre-charges `NEWACCOUNT` (25,000) gas for every EIP-7702 authorization entry and issues a partial refund only for **successful, non-empty** authorities. Authorization entries that **fail** (wrong chain ID, nonce mismatch, bad signature) never receive the refund, so they are charged 25,000 gas instead of the EIP-7702-mandated `PER_AUTH_BASE_COST` (12,500) gas. This is an EVM semantic mismatch: a transaction that would succeed on Ethereum with a correctly-sized gas limit can run out of gas on ZKsync OS.

---

### Finding Description

`calculate_tx_intrinsic_gas` pre-charges `NEWACCOUNT` (25,000) gas per authorization entry: [1](#0-0) 

The design intent (stated in the comment) is that a refund of `NEWACCOUNT - PER_AUTH_BASE_COST` is issued inside `validate_and_apply_delegation` when the authority is **non-empty**, reducing the effective cost to `PER_AUTH_BASE_COST` (12,500): [2](#0-1) 

However, `validate_and_apply_delegation` returns `false` (without issuing any refund) at multiple early-exit points — wrong chain ID (line 108), nonce overflow (line 112), bad `s` value (line 120), failed `ecrecover` (line 134), authority is a contract (line 153), or **nonce mismatch** (line 157): [3](#0-2) 

For every such failed entry the full 25,000-gas pre-charge stands. Per EIP-7702, a failed tuple must cost exactly `PER_AUTH_BASE_COST` = 12,500 gas. ZKsync OS overcharges by 12,500 gas per failed entry.

The `PER_AUTH_BASE_COST` constant confirms the intended per-entry base cost: [4](#0-3) 

The codebase itself acknowledges the asymmetry for native resources (failed auths consume far less native than the formula budgets) but does not address the gas overcharge: [5](#0-4) 

---

### Impact Explanation

**EVM semantic mismatch / resource accounting bug.**

A user constructs an EIP-7702 transaction with `N` authorization entries and sets `gasLimit` according to Ethereum's gas model (`N × 12,500` for the authorization list). If any entry fails at execution time (e.g., the authority's nonce advanced between signing and inclusion), ZKsync OS charges `N × 25,000` instead of `N × 12,500` for those entries. The transaction can revert with out-of-gas on ZKsync OS while the identical transaction would succeed on Ethereum. This breaks EVM equivalence and can cause permanent loss of user funds (gas fees paid for a reverted transaction).

**Impact: Medium** — EVM equivalence violation; legitimate transactions fail; users overpay fees.

---

### Likelihood Explanation

EIP-7702 is a live feature (feature-gated `eip-7702`). The nonce-mismatch path is the most realistic trigger: an authority sends a transaction between the time the EIP-7702 transaction is signed and the time it is included in a block. This is a normal race condition in any mempool. Bad-signature entries (e.g., cross-chain replays with `chain_id = 0` that are invalidated by a chain-ID check) are another common path.

**Likelihood: Low-Medium** — requires EIP-7702 transactions with at least one failing authorization entry, which is a realistic mempool race condition.

---

### Recommendation

Issue the `NEWACCOUNT - PER_AUTH_BASE_COST` refund for **every** authorization entry that passes at least the chain-ID and nonce-overflow checks (i.e., reaches the ecrecover step), regardless of whether the entry ultimately succeeds. Alternatively, change the intrinsic pre-charge to `PER_AUTH_BASE_COST` per entry and charge the additional `NEWACCOUNT - PER_AUTH_BASE_COST` only for entries that succeed with an empty authority. This matches the EIP-7702 specification exactly.

---

### Proof of Concept

1. Deploy ZKsync OS with `eip-7702` feature enabled.
2. Create account `A` with nonce `0`.
3. Sign an EIP-7702 authorization tuple: `(chain_id=current, address=delegate, nonce=0)` with `A`'s key.
4. Before submitting the EIP-7702 transaction, send a plain ETH transfer from `A` to advance its nonce to `1`.
5. Submit the EIP-7702 transaction with `gasLimit = 21000 + PER_AUTH_BASE_COST = 21000 + 12500 = 33500` (sufficient per EIP-7702 spec).
6. **On Ethereum**: transaction succeeds (failed auth costs 12,500 gas; total ≤ 33,500).
7. **On ZKsync OS**: transaction reverts with out-of-gas (failed auth costs 25,000 gas; total > 33,500).

The root cause is confirmed at: [6](#0-5) [7](#0-6)

### Citations

**File:** basic_bootloader/src/bootloader/transaction_flow/gas_helpers.rs (L291-297)
```rust
    // EIP-7702 authorization list: per-authorization. We precharge the
    // empty-account cost; when the authority turns out to be non-empty the
    // delta (NEWACCOUNT - PER_AUTH_BASE_COST) is added back as a gas refund
    // inside `validate_and_apply_delegation`.
    intrinsic_gas = intrinsic_gas.saturating_add(
        authorization_list_num.saturating_mul(evm_interpreter::gas_constants::NEWACCOUNT),
    );
```

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L107-174)
```rust
    // 1. Check chain id
    if !auth_chain_id.is_zero() && auth_chain_id != &U256::from(chain_id) {
        return Ok(false);
    }
    // 2. Check for nonce overflow
    if auth_nonce == u64::MAX {
        return Ok(false);
    }
    // 3. Signature
    // EIP-2 check
    let (_, _, auth_s) = auth_sig_data;
    let s = U256::try_from_be_slice(auth_s)
        .ok_or::<TxError>(InvalidTransaction::InvalidStructure.into())?;
    if s > crypto::secp256k1::SECP256K1N_HALF_U256 {
        return Ok(false);
    }
    let msg = resources.with_infinite_ergs(|inf_ergs| {
        compute_auth_message_signed_hash::<S>(
            inf_ergs,
            auth_chain_id,
            auth_nonce,
            delegation_address,
            hasher,
        )
    })?;
    let Some(authority) = resources
        .with_infinite_ergs(|inf_ergs| recover_authority(system, inf_ergs, auth_sig_data, &msg))?
    else {
        return Ok(false);
    };

    // 4. Read authority account
    // Gas already charged in intrinsic
    let account_properties = resources.with_infinite_ergs(|inf_ergs| {
        system.io.read_account_properties(
            ExecutionEnvironmentType::NoEE,
            inf_ergs,
            &authority,
            AccountDataRequest::empty()
                .with_nonce()
                .with_nominal_token_balance()
                .with_is_delegated()
                .with_has_bytecode(),
        )
    })?;
    // 5. Check authority is not a contract
    if account_properties.is_contract() {
        return Ok(false);
    }
    // 6. Check nonce
    if account_properties.nonce.0 != auth_nonce {
        return Ok(false);
    }
    // 7. Add refund if authority is not empty.
    let is_empty = account_properties.nonce.0 == 0
        && account_properties.has_bytecode() == false
        && account_properties.nominal_token_balance.0.is_zero();

    if !is_empty {
        let ergs = Ergs(
            (evm_interpreter::gas_constants::NEWACCOUNT
                - evm_interpreter::gas_constants::PER_AUTH_BASE_COST)
                * ERGS_PER_GAS,
        );
        system
            .io
            .add_to_refund_counter(S::Resources::from_ergs(ergs))?
    }
```

**File:** evm_interpreter/src/gas_constants.rs (L54-54)
```rust
pub const PER_AUTH_BASE_COST: u64 = 12_500;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/mod.rs (L935-946)
```rust
        // Skip the overcharging check when authorization-list entries are
        // present: failed auths (bad sig, wrong chain id, nonce overflow)
        // consume only PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD while the
        // formula budgets worst-case success cost per entry.
        if context.authorization_list_num == 0 {
            assert!(
                formula <= actual_used * 2,
                "intrinsic computational native formula ({}) is overcharging more than twice compared to actual consumption ({})",
                formula,
                actual_used
            );
        }
```
