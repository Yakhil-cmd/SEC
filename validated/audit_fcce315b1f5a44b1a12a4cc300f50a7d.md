### Title
Enabling Silo Whitelist Without Protecting Existing Token Holders Permanently Freezes Their Funds - (`engine/src/engine.rs`, `engine/src/contract_methods/silo/whitelist.rs`)

---

### Summary

In Aurora Engine's Silo mode, the owner can enable the `Address` and/or `Account` whitelists at any time via `set_whitelist_status`. When enabled, `assert_access` in `engine/src/engine.rs` rejects **all** EVM transactions from non-whitelisted senders — including calls to the `exitToNear` and `exitToEthereum` precompiles, which are the only protocol-level paths to withdraw ETH or ERC-20 tokens from Aurora. There is no emergency exit path that bypasses the whitelist check. Any token holder whose address or NEAR account is not on the whitelist at the moment of activation loses the ability to move or recover their funds until the owner intervenes.

---

### Finding Description

The Silo whitelist system is implemented across two files:

`engine/src/contract_methods/silo/whitelist.rs` defines the `Whitelist` struct with `enable()` / `disable()` / `is_enabled()` methods. [1](#0-0) 

`engine/src/contract_methods/silo/mod.rs` exposes `set_whitelist_status` (which calls `whitelist::set_whitelist_status`) and the access-check helpers `is_allow_submit`, `is_address_allowed`, and `is_account_allowed`. [2](#0-1) [3](#0-2) 

The access check is enforced unconditionally inside `submit_with_alt_modexp` in `engine/src/engine.rs`:

```rust
// Check if the sender has rights to submit transactions or deploy code.
assert_access(&io, env, &transaction)?;
``` [4](#0-3) 

`assert_access` calls `silo::is_allow_submit` for every transaction that has a `to` address, and returns `EngineErrorKind::NotAllowed` if the sender's EVM address or NEAR predecessor account is absent from the enabled whitelist: [5](#0-4) 

The `exitToNear` precompile (address `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`) and `exitToEthereum` precompile (address `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`) are the only protocol-level withdrawal paths for ETH and ERC-20 tokens. [6](#0-5) [7](#0-6) 

Both precompiles are reached exclusively through EVM transactions submitted via `submit()`, which routes through `submit_with_alt_modexp` and therefore through `assert_access`. There is no alternative withdrawal entrypoint that skips the whitelist gate.

The owner-callable entrypoint `set_whitelist_status` in `engine/src/lib.rs` activates the whitelist instantly with no grace period, no snapshot of existing balances, and no forced-exit step for current holders: [8](#0-7) 

The `WhitelistKind` enum documents that `Address` and `Account` whitelists gate transaction submission: [9](#0-8) 

---

### Impact Explanation

Any ETH balance or ERC-20 token balance held at an EVM address that is not present in the `Address` whitelist (or whose NEAR predecessor account is absent from the `Account` whitelist) becomes inaccessible the moment the owner enables the respective whitelist. The holder cannot call `exitToNear`, `exitToEthereum`, or any other EVM function. Their funds are frozen for as long as the whitelist remains enabled and their address remains absent. This constitutes **temporary freezing of funds** (High severity); the owner can reverse it by disabling the whitelist or adding the address, but until that remediation occurs the user has zero recourse.

---

### Likelihood Explanation

This is most likely to occur during a Silo migration or initial hardening of a Silo deployment: the operator enables the whitelist to restrict future access but fails to enumerate all addresses that already hold balances. The existing test `test_submit_with_removing_entries` in `engine-tests/src/tests/silo.rs` demonstrates that removing an address from the whitelist immediately blocks all further transactions from that address, confirming the freeze is instantaneous and total. [10](#0-9) 

---

### Recommendation

Before activating any whitelist, the engine should either:

1. **Enumerate and pre-populate** all addresses with non-zero balances into the whitelist atomically in the same transaction that enables it, or
2. **Provide a whitelist-exempt withdrawal path** — a dedicated `emergency_exit` entrypoint (callable by any address, regardless of whitelist status) that only permits `exitToNear` / `exitToEthereum` operations and does not allow arbitrary EVM execution.

Option 2 is the closer analog to the `forceUndelegate` protection in the referenced report.

---

### Proof of Concept

1. A Silo instance is live; address `A` holds 10 ETH on Aurora (deposited before any whitelist was active).
2. The owner calls `set_whitelist_status` with `{ kind: WhitelistKind::Address, active: true }` and `{ kind: WhitelistKind::Account, active: true }`, enabling both whitelists. Address `A` and its NEAR predecessor account are not added.
3. Address `A` constructs an EVM transaction calling `exitToNear` at `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f` to withdraw its 10 ETH.
4. The NEAR runtime routes the call through `submit()` → `submit_with_alt_modexp()` → `assert_access()`.
5. `assert_access` calls `silo::is_allow_submit`, which calls `is_address_allowed` → `Whitelist::is_exist` → returns `false` because `A` is not in the enabled `Address` whitelist.
6. `assert_access` returns `Err(EngineErrorKind::NotAllowed)`. The transaction is rejected before any EVM execution occurs.
7. Address `A` has no other protocol path to recover its 10 ETH. Funds are frozen.

### Citations

**File:** engine/src/contract_methods/silo/whitelist.rs (L28-47)
```rust
    /// Enable a whitelist. (A whitelist is disabled after creation).
    pub fn enable(&mut self) {
        let key = self.key(STATUS);
        self.io.write_storage(&key, &[1]);
    }

    /// Disable a whitelist.
    pub fn disable(&mut self) {
        let key = self.key(STATUS);
        self.io.write_storage(&key, &[0]);
    }

    /// Check if the whitelist is enabled.
    pub fn is_enabled(&self) -> bool {
        // White list is disabled by default. So return `false` if the key doesn't exist.
        let key = self.key(STATUS);
        self.io
            .read_storage(&key)
            .is_some_and(|value| value.to_vec() == [1])
    }
```

**File:** engine/src/contract_methods/silo/mod.rs (L97-100)
```rust
/// Set the given status of the provided whitelist.
pub fn set_whitelist_status<I: IO + Copy>(io: &I, args: &WhitelistStatusArgs) {
    whitelist::set_whitelist_status(io, args);
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-163)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}

fn is_account_allowed_deploy<I: IO + Copy>(io: &I, account_id: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Admin);
    !list.is_enabled() || list.is_exist(account_id)
}

fn is_address_allowed_deploy<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::EvmAdmin);
    !list.is_enabled() || list.is_exist(address)
}

fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}

fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```

**File:** engine/src/engine.rs (L1051-1052)
```rust
    // Check if the sender has rights to submit transactions or deploy code.
    assert_access(&io, env, &transaction)?;
```

**File:** engine/src/engine.rs (L1756-1775)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine-precompiles/src/native.rs (L270-278)
```rust
pub mod exit_to_near {
    use crate::prelude::types::{Address, make_address};

    /// Exit to NEAR precompile address
    ///
    /// Address: `0xe9217bc70b7ed1f598ddd3199e80b093fa71124f`
    /// This address is computed as: `&keccak("exitToNear")[12..]`
    pub const ADDRESS: Address = make_address(0xe9217bc7, 0x0b7ed1f598ddd3199e80b093fa71124f);
}
```

**File:** engine-precompiles/src/native.rs (L821-828)
```rust
pub mod exit_to_ethereum {
    use crate::prelude::types::{Address, make_address};

    /// Exit to Ethereum precompile address
    ///
    /// Address: `0xb0bd02f6a392af548bdf1cfaee5dfa0eefcc8eab`
    /// This address is computed as: `&keccak("exitToEthereum")[12..]`
    pub const ADDRESS: Address = make_address(0xb0bd02f6, 0xa392af548bdf1cfaee5dfa0eefcc8eab);
```

**File:** engine/src/lib.rs (L841-851)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_whitelist_status() {
        let io = Runtime;
        let state = state::get_state(&io).sdk_unwrap();
        require_owner_and_running(&state, &io.predecessor_account_id())
            .map_err(ContractError::msg)
            .sdk_unwrap();

        let args: WhitelistStatusArgs = io.read_input_borsh().sdk_unwrap();
        silo::set_whitelist_status(&io, &args);
    }
```

**File:** engine-types/src/parameters/silo.rs (L65-80)
```rust
pub enum WhitelistKind {
    /// The whitelist of this type is for storing NEAR accounts. Accounts stored in this whitelist
    /// have an admin role. The admin role allows to add new admins and add new entities
    /// (`AccountId` and `Address`) to whitelists. Also, this role allows to deploy of EVM code
    /// and submit transactions.
    Admin = 0x0,
    /// The whitelist of this type is for storing EVM addresses. Addresses included in this
    /// whitelist can deploy EVM code.
    EvmAdmin = 0x1,
    /// The whitelist of this type is for storing NEAR accounts. Accounts included in this
    /// whitelist can submit transactions.
    Account = 0x2,
    /// The whitelist of this type is for storing EVM addresses. Addresses included in this
    /// whitelist can submit transactions.
    Address = 0x3,
}
```

**File:** engine-tests/src/tests/silo.rs (L642-703)
```rust
#[test]
fn test_submit_with_removing_entries() {
    let (mut runner, signer, receiver) = initialize_transfer();
    let sender = utils::address_from_secret_key(&signer.secret_key);
    let caller: AccountId = CALLER_ACCOUNT_ID.parse().unwrap();
    let transaction = utils::transfer_with_price(
        receiver,
        TRANSFER_AMOUNT,
        INITIAL_NONCE.into(),
        ONE_GAS_PRICE.raw(),
    );

    set_silo_params(&mut runner, Some(SILO_PARAMS_ARGS));
    enable_all_whitelists(&mut runner);

    // Allow submitting transactions.
    add_account_to_whitelist(&mut runner, caller.clone());
    add_address_to_whitelist(&mut runner, sender);

    validate_address_balance_and_nonce(&runner, sender, INITIAL_BALANCE, INITIAL_NONCE.into())
        .unwrap();
    validate_address_balance_and_nonce(&runner, receiver, ZERO_BALANCE, INITIAL_NONCE.into())
        .unwrap();

    // perform transfer
    let result = runner
        .submit_transaction(&signer.secret_key, transaction.clone())
        .unwrap();
    assert!(matches!(result.status, TransactionStatus::Succeed(_)));

    // validate post-state
    validate_address_balance_and_nonce(
        &runner,
        sender,
        INITIAL_BALANCE - TRANSFER_AMOUNT - FIXED_GAS * ONE_GAS_PRICE,
        (INITIAL_NONCE + 1).into(),
    )
    .unwrap();
    validate_address_balance_and_nonce(&runner, receiver, TRANSFER_AMOUNT, INITIAL_NONCE.into())
        .unwrap();

    // Remove account id and address from whitelists.
    remove_account_from_whitelist(&mut runner, caller);
    remove_address_from_whitelist(&mut runner, sender);

    // perform transfer
    let err = runner
        .submit_transaction(&signer.secret_key, transaction)
        .unwrap_err();
    assert_eq!(err.kind, EngineErrorKind::NotAllowed);

    // validate post-state
    validate_address_balance_and_nonce(
        &runner,
        sender,
        INITIAL_BALANCE - TRANSFER_AMOUNT - FIXED_GAS * ONE_GAS_PRICE,
        (INITIAL_NONCE + 1).into(),
    )
    .unwrap();
    validate_address_balance_and_nonce(&runner, receiver, TRANSFER_AMOUNT, INITIAL_NONCE.into())
        .unwrap();
}
```
