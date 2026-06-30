### Title
Silo Mode Whitelist Bypass via `call` and `deploy_code` — (File: `engine/src/contract_methods/evm_transactions.rs`)

---

### Summary

The Aurora Engine's Silo Mode exposes four NEAR-level EVM entry points: `submit`, `submit_with_args`, `call`, and `deploy_code`. Only `submit` and `submit_with_args` enforce the Silo whitelist via `assert_access`. The `call` and `deploy_code` functions skip this check entirely, allowing any NEAR account to interact with the EVM and deploy contracts regardless of whether the Admin, EvmAdmin, Account, or Address whitelists are enabled and populated.

---

### Finding Description

The `assert_access` function in `engine/src/engine.rs` enforces the Silo whitelist policy: [1](#0-0) 

It gates EVM access by checking `silo::is_allow_submit` (for calls) or `silo::is_allow_deploy` (for deployments) against the caller's NEAR account ID and derived EVM address. [2](#0-1) 

The `submit` path correctly invokes this gate. However, `deploy_code` and `call` in `evm_transactions.rs` only call `require_running` and then proceed directly to EVM execution: [3](#0-2) [4](#0-3) 

Neither function calls `assert_access` or any equivalent silo whitelist check before executing EVM operations. The whitelist logic itself is correct and complete: [5](#0-4) 

The bypass is structural: `submit` routes through `engine::submit()` which calls `assert_access`, while `call` and `deploy_code` call `engine.call_with_args` and `engine.deploy_code_with_input` directly, skipping the gate entirely.

---

### Impact Explanation

In a Silo Mode deployment with whitelists enabled, the operator's intent is to restrict EVM interaction to a curated set of NEAR accounts and EVM addresses. Any NEAR account — including those explicitly excluded from the whitelist — can bypass this restriction by invoking `call` or `deploy_code` directly instead of `submit`. This allows:

- **Unauthorized EVM contract calls**: An excluded NEAR account can call any EVM contract in the Silo, including contracts holding user funds, enabling direct theft of funds at rest.
- **Unauthorized EVM code deployment**: An excluded NEAR account can deploy arbitrary EVM bytecode, bypassing the Admin/EvmAdmin whitelist that is specifically designed to gate deployments.

Impact: **Critical** — direct theft of user funds held in EVM contracts within the Silo, and unauthorized contract deployment that can be used to stage further attacks.

---

### Likelihood Explanation

The entry points `call` and `deploy_code` are standard NEAR contract methods exposed on the Aurora Engine contract. Any NEAR account can call them with no special privileges, no attached deposit requirement beyond what `require_running` checks, and no cryptographic barrier. The bypass requires only knowledge of the method names and the ability to construct valid `CallArgs` or raw EVM bytecode. Likelihood: **High**.

---

### Recommendation

Add a silo whitelist check to both `call` and `deploy_code` before EVM execution, mirroring the `assert_access` pattern used in `engine::submit`. Specifically:

- In `deploy_code`: check `silo::is_allow_deploy(io, &predecessor_account_id, &derived_address)` and return `EngineErrorKind::NotAllowed` if false.
- In `call`: check `silo::is_allow_submit(io, &predecessor_account_id, &derived_address)` and return `EngineErrorKind::NotAllowed` if false.

The check should be conditioned on Silo Mode being active (i.e., `get_silo_params` returning `Some`), consistent with how `assert_access` is structured. [6](#0-5) 

---

### Proof of Concept

1. Deploy Aurora Engine in Silo Mode: call `set_silo_params` with non-`None` params, then call `set_whitelists_statuses` to enable all four whitelists (`Admin`, `EvmAdmin`, `Account`, `Address`).
2. Do **not** add the attacker's NEAR account ID or derived EVM address to any whitelist.
3. Confirm that calling `submit` with a signed Ethereum transaction from the attacker's address returns `EngineErrorKind::NotAllowed` (as demonstrated by the existing silo tests). [7](#0-6) 

4. Now call `call` (NEAR method) directly from the same attacker account with a `CallArgs` targeting any EVM contract. Observe that the call succeeds and EVM state is modified — the whitelist is not consulted.
5. Similarly, call `deploy_code` directly from the attacker account with arbitrary EVM bytecode. Observe that the deployment succeeds despite the EvmAdmin whitelist being enabled and the attacker not being listed. [8](#0-7)

### Citations

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

**File:** engine/src/contract_methods/silo/mod.rs (L130-143)
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
```

**File:** engine/src/contract_methods/silo/mod.rs (L145-163)
```rust
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

**File:** engine/src/contract_methods/evm_transactions.rs (L21-71)
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

#[named]
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
