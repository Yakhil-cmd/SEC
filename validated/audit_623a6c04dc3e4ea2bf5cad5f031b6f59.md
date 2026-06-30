### Title
Silo Whitelist Bypass via `call` Entry Point Allows Non-Whitelisted Accounts to Execute Arbitrary EVM Transactions - (`engine/src/contract_methods/evm_transactions.rs`)

### Summary

In Aurora Engine's Silo mode, the `submit` and `submit_with_args` entry points enforce the Account and Address whitelists via `assert_access`. However, the `call` entry point performs no silo whitelist check at all, allowing any NEAR account — including those explicitly excluded from the Account whitelist — to execute arbitrary EVM transactions inside the Silo, completely bypassing the intended access control.

### Finding Description

Aurora Engine's Silo mode implements a four-category whitelist system to restrict who can interact with the EVM. The `Account` whitelist controls which NEAR accounts may call `submit`/`submit_with_args`, and the `Address` whitelist controls which EVM addresses (derived from transaction signatures) may be the `msg.sender`.

The enforcement is implemented in `assert_access` in `engine/src/engine.rs`:

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
```

This is called inside `engine::submit_with_alt_modexp` at line 1052, which is the path taken by both `submit` and `submit_with_args`.

However, the `call` entry point in `engine/src/contract_methods/evm_transactions.rs` takes a completely different code path:

```rust
pub fn call<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;   // ← only liveness check, no whitelist check
        let bytes = io.read_input().to_vec();
        let args = CallArgs::deserialize(&bytes).ok_or(errors::ERR_BORSH_DESERIALIZE)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, E, AuroraModExp> = Engine::new_with_state(
            state,
            predecessor_address(&predecessor_account_id),
            current_account_id, io, env,
        );
        let result = engine.call_with_args(args, handler)?;  // ← no assert_access
        ...
    })
}
```

`call` only invokes `require_running` and then directly executes the EVM call. Neither `is_allow_submit` (Account + Address whitelist) nor `is_allow_deploy` (Admin + EvmAdmin whitelist) is consulted. The `call` entry point is exposed as a public NEAR contract method in `engine/src/lib.rs`.

The `call` function sets `msg.sender` to `predecessor_address(&predecessor_account_id)`, meaning the EVM address is deterministically derived from the calling NEAR account. Any NEAR account can therefore call arbitrary EVM contracts inside the Silo with a predictable EVM identity, regardless of whitelist status.

### Impact Explanation

**Critical — Theft of user funds / Permanent whitelist bypass.**

The Silo whitelist is the primary access control mechanism for private/permissioned Aurora deployments. Its purpose is to ensure only authorized NEAR accounts and EVM addresses can interact with the EVM state. By calling `call` instead of `submit`, any NEAR account can:

1. Call ERC-20 contracts to transfer tokens held at the EVM address derived from their NEAR account ID.
2. Interact with any EVM contract in the Silo (DeFi protocols, vaults, etc.) without authorization.
3. Trigger state changes that the Silo operator intended to restrict to whitelisted participants.

This completely undermines the Silo access control model. Any funds held in EVM contracts that can be moved by an arbitrary `msg.sender` are at risk.

### Likelihood Explanation

**High.** The `call` entry point is a standard, publicly documented Aurora Engine method. No special privileges, leaked keys, or governance capture are required. Any NEAR account can call it directly. The only precondition is that the Silo operator has enabled the Account or Address whitelist — which is the entire point of deploying in Silo mode.

### Recommendation

Add a silo whitelist check to the `call` function in `engine/src/contract_methods/evm_transactions.rs`, analogous to the `assert_access` call in `engine::submit_with_alt_modexp`. Since `call` does not parse a signed Ethereum transaction, the check should use the NEAR predecessor account ID and the EVM address derived from it:

```rust
// After require_running(&state)?;
let predecessor_account_id = env.predecessor_account_id();
let evm_address = predecessor_address(&predecessor_account_id);
if !silo::is_allow_submit(&io, &predecessor_account_id, &evm_address) {
    return Err(errors::ERR_NOT_ALLOWED.into());
}
```

Similarly, `deploy_code` should be audited for the same omission and have `is_allow_deploy` applied.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode with the Account and Address whitelists enabled.
2. Do **not** add NEAR account `attacker.near` to the Account whitelist.
3. Confirm that `attacker.near` calling `submit` with a signed Ethereum transaction is rejected with `NotAllowed`.
4. Have `attacker.near` call the `call` entry point directly with `CallArgs` targeting an ERC-20 contract's `transfer` function.
5. Observe that the EVM call executes successfully — the whitelist check is never performed — and the ERC-20 transfer completes, moving tokens from the EVM address `predecessor_address("attacker.near")` to an attacker-controlled address.

**Key code references:**

- `assert_access` (enforced in `submit` path): [1](#0-0) 
- `assert_access` called at line 1052 inside `submit_with_alt_modexp`: [2](#0-1) 
- `call` function — missing whitelist check: [3](#0-2) 
- `submit` function — correctly calls `engine::submit` which enforces `assert_access`: [4](#0-3) 
- `is_allow_submit` — the check that `call` skips: [5](#0-4) 
- `call` exposed as public NEAR entry point: [6](#0-5)

### Citations

**File:** engine/src/engine.rs (L1049-1053)
```rust
    let fixed_gas = silo::get_fixed_gas(&io);

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

**File:** engine/src/contract_methods/evm_transactions.rs (L73-103)
```rust
#[named]
pub fn submit<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<SubmitResult, ContractError> {
    with_logs_hashchain(io, env, function_name!(), |mut io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let tx_data = io.read_input().to_vec();
        let current_account_id = env.current_account_id();
        let relayer_address = predecessor_address(&env.predecessor_account_id());
        let args = SubmitArgs {
            tx_data,
            ..Default::default()
        };
        let result = engine::submit(
            io,
            env,
            &args,
            state,
            current_account_id,
            relayer_address,
            handler,
        )?;
        let result_bytes = borsh::to_vec(&result).map_err(|_| errors::ERR_SERIALIZE)?;
        io.return_output(&result_bytes);

        Ok(result)
    })
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```

**File:** engine/src/lib.rs (L261-270)
```rust
    /// Call method on the EVM contract.
    #[unsafe(no_mangle)]
    pub extern "C" fn call() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::evm_transactions::call(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
