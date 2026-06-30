### Title
Silo Whitelist Bypass via `call` and `deploy_code` Entrypoints - (`engine/src/contract_methods/evm_transactions.rs`)

### Summary

The Aurora Engine Silo mode enforces access control via four whitelists (`Admin`, `EvmAdmin`, `Account`, `Address`). These whitelists are checked inside `engine::submit` (the standalone function) through `assert_access`. However, the `call` and `deploy_code` NEAR-level entrypoints in `evm_transactions.rs` bypass this check entirely, allowing any unprivileged NEAR account to execute EVM calls or deploy EVM code in a Silo even when whitelists are enabled.

### Finding Description

**The `assert_access` guard is only enforced in `engine::submit`, not in `call` or `deploy_code`.**

`engine/src/engine.rs` defines `assert_access`, which enforces the silo whitelist:

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
        return Err(EngineError { kind: EngineErrorKind::NotAllowed, gas_used: 0 });
    }
    Ok(())
}
``` [1](#0-0) 

This function is called only in the `engine::submit` standalone function path (reached via the `submit` and `submit_with_args` NEAR entrypoints). The `call` entrypoint, however, only checks `require_running` and then directly calls `engine.call_with_args()`:

```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(...) {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;   // ← only pause check, NO silo whitelist check
        // ...
        let result = engine.call_with_args(args, handler)?;  // ← bypasses assert_access
``` [2](#0-1) 

Similarly, `deploy_code` only checks `require_running` and calls `engine.deploy_code_with_input()` directly: [3](#0-2) 

The silo whitelist helper functions confirm the intended restriction:

```rust
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
pub fn is_allow_deploy<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_account_allowed_deploy(io, account) && is_address_allowed_deploy(io, address)
}
``` [4](#0-3) 

The whitelist enforcement pattern (`!list.is_enabled() || list.is_exist(account_id)`) means that when a whitelist is enabled, only listed entries are allowed — but this check is never reached from `call` or `deploy_code`: [5](#0-4) 

### Impact Explanation

In a Silo deployment with `Account`/`Address` whitelists enabled, any unprivileged NEAR account can:
1. Call the `call` entrypoint to execute arbitrary EVM transactions against any EVM contract in the Silo, with `msg.sender` derived from their NEAR account ID.
2. Call the `deploy_code` entrypoint to deploy arbitrary EVM bytecode, bypassing the `Admin`/`EvmAdmin` whitelist.

If EVM contracts in the Silo hold user funds and rely on the Silo's access control as a perimeter defense, an unauthorized caller can interact with those contracts and steal funds. This is a direct whitelist bypass that undermines the core security guarantee of Silo mode.

**Impact: High — Theft of user funds / temporary freezing of funds via unauthorized EVM execution in a restricted Silo.**

### Likelihood Explanation

Any NEAR account can call the `call` or `deploy_code` entrypoints on the Aurora Engine contract. No special privileges, leaked keys, or governance capture are required. The attacker only needs to know the Silo is deployed and that these entrypoints exist. Likelihood is **High** for any Silo operator who relies on whitelists to restrict EVM access.

### Recommendation

Add silo whitelist enforcement to both `call` and `deploy_code` entrypoints, mirroring the check performed in `engine::submit`. For `call`, check `silo::is_allow_submit`; for `deploy_code`, check `silo::is_allow_deploy`. Both checks should use `env.predecessor_account_id()` as the NEAR account and the address derived from it as the EVM address.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode with `Account` and `Address` whitelists enabled (as done in `test_submit_access_right`).
2. Confirm that calling `submit` with an unsigned NEAR account is blocked with `NotAllowed`.
3. Call the `call` entrypoint directly from the same unauthorized NEAR account with a `CallArgs` targeting any EVM contract.
4. Observe that the call succeeds — the silo whitelist is not checked, and the EVM call executes with `msg.sender` derived from the unauthorized NEAR account. [6](#0-5)

### Citations

**File:** engine/src/engine.rs (L1756-1774)
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
```

**File:** engine/src/contract_methods/evm_transactions.rs (L21-43)
```rust
pub fn deploy_code<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let input = io.read_input().to_vec();
        let current_account_id = env.current_account_id();
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&env.predecessor_account_id()),
            current_account_id,
            io,
            env,
        );
        let result = engine.deploy_code_with_input(input, None, handler)?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);
        Ok(result)
    })
}
```

**File:** engine/src/contract_methods/evm_transactions.rs (L46-71)
```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes).ok_or(errors::ERR_BORSH_DESERIALIZE)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();

        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id),
            current_account_id,
            io,
            env,
        );
        let result = engine.call_with_args(args, handler)?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);
        Ok(result)
    })
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L130-153)
```rust
/// Check if a user has the right to deploy EVM code.
pub fn is_allow_deploy<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_account_allowed_deploy(io, account) && is_address_allowed_deploy(io, address)
}

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
```

**File:** engine/src/contract_methods/silo/mod.rs (L155-163)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}

fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```

**File:** engine-tests/src/tests/silo.rs (L462-514)
```rust
#[test]
fn test_submit_access_right() {
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

    validate_address_balance_and_nonce(&runner, sender, INITIAL_BALANCE, INITIAL_NONCE.into())
        .unwrap();
    validate_address_balance_and_nonce(&runner, receiver, ZERO_BALANCE, INITIAL_NONCE.into())
        .unwrap();

    // perform transfer
    let err = runner
        .submit_transaction(&signer.secret_key, transaction.clone())
        .unwrap_err();
    assert_eq!(err.kind, EngineErrorKind::NotAllowed);

    // validate post-state
    validate_address_balance_and_nonce(&runner, sender, INITIAL_BALANCE, INITIAL_NONCE.into())
        .unwrap();
    validate_address_balance_and_nonce(&runner, receiver, ZERO_BALANCE, INITIAL_NONCE.into())
        .unwrap();

    // Add caller and signer to whitelists.
    add_account_to_whitelist(&mut runner, caller);
    add_address_to_whitelist(&mut runner, sender);

    // perform transfer
    let result = runner
        .submit_transaction(&signer.secret_key, transaction)
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
}
```
