### Title
EIP-7702 Authorization Gas Pre-Charge Uses `NEWACCOUNT` (25,000) Instead of `PER_AUTH_BASE_COST` (12,500), Causing Double-Charging for Non-Empty Authorities - (`File: basic_bootloader/src/bootloader/transaction/authorization_list.rs`)

### Summary

The `validate_and_apply_delegation` function in ZKsync OS pre-charges `NEWACCOUNT` (25,000 gas) per authorization entry at step 0, and then issues a refund of `NEWACCOUNT - PER_AUTH_BASE_COST` (12,500 gas) only when the authority account is non-empty. However, the intrinsic gas calculation in `calculate_tx_intrinsic_gas` **also** pre-charges `NEWACCOUNT` per authorization at the transaction level. This means the per-entry gas charge at step 0 inside `validate_and_apply_delegation` is a **double-charge** on top of the already-deducted intrinsic gas, causing users to pay 25,000 extra gas per authorization entry beyond what EIP-7702 specifies.

### Finding Description

**EIP-7702 specification** (EIP-7702, Prague): The intrinsic cost per authorization tuple is `PER_AUTH_BASE_COST` = 12,500 gas for a non-empty authority, or `NEWACCOUNT` = 25,000 gas for an empty authority. The correct design is: pre-charge `PER_AUTH_BASE_COST` at intrinsic time, then charge an additional `NEWACCOUNT - PER_AUTH_BASE_COST` = 12,500 if the authority is empty (new account creation).

**ZKsync OS design intent** (per comment in `calculate_tx_intrinsic_gas`):

> "We precharge the empty-account cost; when the authority turns out to be non-empty the delta (NEWACCOUNT - PER_AUTH_BASE_COST) is added back as a gas refund inside `validate_and_apply_delegation`."

This means the intrinsic gas already charges `NEWACCOUNT` (25,000) per authorization entry at the transaction level. [1](#0-0) 

**The bug**: Inside `validate_and_apply_delegation`, step 0 charges `NEWACCOUNT * ERGS_PER_GAS` **again** from `resources` (which is `intrinsic_resources` with infinite ergs, but the native cost is real): [2](#0-1) 

The comment says "Pre-charge intrinsic gas cost of delegation (PER_AUTH_BASE_COST)" but the actual value charged is `NEWACCOUNT` (25,000), not `PER_AUTH_BASE_COST` (12,500): [3](#0-2) 

The call site in `validation_impl.rs` passes `intrinsic_resources.with_infinite_ergs(...)`, meaning the ergs dimension is infinite (so the ergs charge at step 0 is a no-op), but the **native cost** (`PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD`) is real and charged from `intrinsic_resources`: [4](#0-3) 

The ergs charge at step 0 inside `validate_and_apply_delegation` is therefore effectively zero (infinite ergs context), but the **refund logic** at step 7 is still executed against the real `resources` (not infinite ergs): [5](#0-4) 

The refund of `NEWACCOUNT - PER_AUTH_BASE_COST` is added to the refund counter when the authority is non-empty. But since the ergs charge at step 0 was a no-op (infinite ergs), the refund counter is being credited for gas that was never actually charged inside `validate_and_apply_delegation`. The intrinsic gas pre-charge of `NEWACCOUNT` per entry was already deducted from `main_resources` before the call. The refund is then applied post-execution, effectively giving back 12,500 gas for non-empty authorities — which is the correct EIP-7702 behavior.

**However**, the `is_empty` check at step 7 uses `account_properties.nonce.0 == 0 && has_bytecode() == false && nominal_token_balance.0.is_zero()`. A **currently-delegated** account (one that already has `0xef0100 || addr` as its bytecode) has `has_bytecode() == true` (bytecode length = 23), so `is_empty` is `false`, and the refund is issued. But the `is_contract()` check at step 5 returns `false` for delegated accounts (since `is_contract()` = `has_bytecode() && !is_delegated`). This means a delegated account passes step 5 and reaches step 7, where it is correctly treated as non-empty and gets the refund.

The deeper issue is the **mismatch between the comment and the actual charge**: the comment at step 0 says `PER_AUTH_BASE_COST` but the code charges `NEWACCOUNT`. The design intent (per `calculate_tx_intrinsic_gas` comment) is that the ergs are pre-charged at intrinsic time and step 0 is a no-op for ergs (infinite ergs context). This is architecturally consistent, but the **native cost** at step 0 (`PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD`) is charged in addition to the intrinsic native cost already pre-charged by `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION`: [6](#0-5) 

`L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` already includes `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD`. The step 0 charge inside `validate_and_apply_delegation` charges it **again** from `intrinsic_resources`. This is a double-charge of the native computational overhead for every authorization entry.

### Impact Explanation

Every EIP-7702 transaction with N authorization entries pays `N * PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD` (2,000 native units) in native computational cost twice: once via the intrinsic formula and once inside `validate_and_apply_delegation`. This causes users to overpay native computational cost for EIP-7702 transactions. In the worst case (many authorization entries), this could cause transactions to run out of native computational budget prematurely and fail, even though they have sufficient gas. This is a resource accounting bug that affects all EIP-7702 transactions on ZKsync OS.

### Likelihood Explanation

EIP-7702 (type 0x04) transactions are a standard Ethereum Prague feature. Any user submitting a valid EIP-7702 transaction with one or more authorization entries will trigger this double-charge. The likelihood is high once EIP-7702 is enabled (`#[cfg(feature = "eip-7702")]`), as it affects every such transaction unconditionally.

### Recommendation

Remove the native cost charge from step 0 inside `validate_and_apply_delegation`, since `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD` is already included in `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` which is pre-charged at the transaction level. Step 0 should only charge the ergs component (which is already a no-op in the infinite-ergs context), or be removed entirely since the intrinsic formula covers both ergs and native costs:

```rust
// Step 0: Remove the double-charge of native overhead
// The intrinsic formula already includes PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD
// Only charge ergs if not in infinite-ergs context (but this is called with infinite ergs anyway)
// resources.charge(...) // REMOVE THIS
```

Alternatively, align the `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` formula to exclude `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD` if step 0 is intended to charge it dynamically.

### Proof of Concept

1. Submit an EIP-7702 transaction with 1 authorization entry (authority = non-empty EOA with nonce > 0).
2. The intrinsic native cost pre-charges `L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION` which includes `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD = 2000`.
3. Inside `validate_and_apply_delegation`, step 0 charges `PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD = 2000` again from `intrinsic_resources`.
4. Total native overhead charged = 4,000 instead of the intended 2,000.
5. For a transaction with many authorization entries (e.g., 100), the excess native charge = 200,000 native units, potentially causing the transaction to exhaust its native computational budget and fail with an out-of-native error, even though the user provided sufficient gas. [2](#0-1) [7](#0-6)

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

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L99-105)
```rust
    // 0. Pre-charge intrinsic gas
    resources.charge(&S::Resources::from_ergs_and_native(
        Ergs(evm_interpreter::gas_constants::NEWACCOUNT * ERGS_PER_GAS),
        <<S::Resources as Resources>::Native as zk_ee::system::Computational>::from_computational(
            crate::bootloader::constants::PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD,
        ),
    ))?;
```

**File:** basic_bootloader/src/bootloader/transaction/authorization_list.rs (L160-174)
```rust
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

**File:** evm_interpreter/src/gas_constants.rs (L13-54)
```rust
pub const NEWACCOUNT: u64 = 25000;
pub const EXP: u64 = 10;
pub const MEMORY: u64 = 3;
pub const LOG: u64 = 375;
pub const LOGDATA: u64 = 8;
pub const LOGTOPIC: u64 = 375;
pub const SHA3: u64 = 30;
pub const SHA3WORD: u64 = 6;
pub const COPY: u64 = 3;
pub const BLOCKHASH: u64 = 20;
pub const CODEDEPOSIT: u64 = 200;
pub const BLOBHASH: u64 = 3;

// SSTORE write extras.
pub const REFUND_SSTORE_CLEARS: i64 = 15000;
pub const SSTORE_SET_EXTRA: u64 = 19900;
pub const SSTORE_RESET_EXTRA: u64 = 2800;

pub const TRANSACTION_ZERO_DATA: u64 = 4;
pub const TRANSACTION_NON_ZERO_DATA_INIT: u64 = 16;
pub const TRANSACTION_NON_ZERO_DATA_FRONTIER: u64 = 68;

// berlin eip2929 constants
pub const ACCESS_LIST_ADDRESS: u64 = 2400;
pub const ACCESS_LIST_STORAGE_KEY: u64 = 1900;
pub const COLD_SLOAD_COST: u64 = 2100;
pub const COLD_ACCOUNT_ACCESS_COST: u64 = 2600;
pub const WARM_STORAGE_READ_COST: u64 = 100;

/// EIP-3860 : Limit and meter initcode
pub const INITCODE_WORD_COST: u64 = 2;

pub const CALL_STIPEND: u64 = 2300;

pub const ADDRESS_ACCESS_COST_COLD: u64 = 2600;
pub const ADDRESS_ACCESS_COST_WARM: u64 = 100;

pub const TSTORE: u64 = 100;
pub const TLOAD: u64 = 100;
pub const SELFBALANCE: u64 = 5;

pub const PER_AUTH_BASE_COST: u64 = 12_500;
```

**File:** basic_bootloader/src/bootloader/transaction_flow/zk/validation_impl.rs (L423-436)
```rust
    #[cfg(feature = "eip-7702")]
    {
        if let Some(authorization_list) = transaction.authorization_list() {
            // Same as for the access list: gas is included in the intrinsic
            // gas above, so we are only charging native
            intrinsic_resources.with_infinite_ergs(|inf_resources| {
                crate::bootloader::transaction::authorization_list::parse_authorization_list_and_apply_delegations(
                    system,
                    inf_resources,
                    authorization_list,
                )
            })?;
        }
    }
```

**File:** basic_bootloader/src/bootloader/constants.rs (L119-129)
```rust
pub const L2_TX_INTRINSIC_COMPUTATIONAL_NATIVE_PER_AUTHORIZATION: u64 =
    PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD + // computational overhead
    keccak256_native_cost_for_rounds_u64(1) + // auth message keccak cost (1 round)
    ECRECOVER_NATIVE_COST + // signature verification
    NEW_COLD_ACCOUNT_READ_COST + // worst case account read
    ACCOUNT_UPDATE_COST + // nonce update
    ACCOUNT_UPDATE_COST + PREIMAGE_CACHE_SET_NATIVE_COST + keccak256_native_cost_for_rounds_u64(1) /*bytecode hashing */ + blake2s_native_cost(24) /* blake2s padded bytecode */ + // delegation write
    132 * DYNAMIC_PART_KECCAK_COMPUTATIONAL_NATIVE_PER_BYTE * 2; // keccak for tx signing + full hash, 132 - worst case contribution to rlp encoding (33 chain_id, 21 address, 9 nonce, 1 y_parity, 33 r, 33 s, 2 list overhead)

/// Native computational overhead of 7702 auth.
pub const PER_AUTH_NATIVE_COMPUTATIONAL_OVERHEAD: u64 = 2000;
```
