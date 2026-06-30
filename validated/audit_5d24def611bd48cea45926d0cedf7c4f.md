### Title
User's NEAR Overpayment in `storage_deposit()` is Permanently Lost to the Engine Contract - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `storage_deposit` function in the Aurora Engine contract forwards the caller's **entire** attached NEAR deposit to the eth-connector without any validation or minimum-amount check. Because the eth-connector's storage deposit amount is intentionally set to zero (any deposit is excess), the eth-connector refunds the deposit back to the engine contract — not to the original user. No callback is registered to forward the refund to the user. Any NEAR attached by the user is permanently lost to the engine contract's balance.

---

### Finding Description

In `engine/src/contract_methods/connector.rs`, the `storage_deposit` function is defined as:

```rust
pub fn storage_deposit<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    let args = read_json_args(&io).and_then(|args: StorageDepositArgs| {
        serde_json::to_vec(&(
            env.predecessor_account_id(),
            args.account_id,
            args.registration_only,
        ))
        .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(
        io,
        env,
        "engine_storage_deposit",
        args,
        Yocto::new(env.attached_deposit()),   // <-- entire user deposit forwarded
    )
}
``` [1](#0-0) 

Unlike every other payable method in the same file — `ft_transfer`, `ft_transfer_call`, `storage_unregister`, `storage_withdraw` — which all call `env.assert_one_yocto()` before forwarding exactly `ONE_YOCTO`, `storage_deposit` performs **no deposit validation** and blindly forwards `env.attached_deposit()` in full. [2](#0-1) [3](#0-2) 

The `return_promise` helper creates a single cross-contract call with no callback:

```rust
fn return_promise<I: IO + PromiseHandler, E: Env>(
    mut io: I,
    env: &E,
    method: &str,
    args: Vec<u8>,
    deposit: Yocto,
) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,
        method: method.to_string(),
        args,
        attached_balance: deposit,
        attached_gas: calculate_attached_gas(env),
    };
    let promise_id = io.promise_create_call(&promise_args);
    io.promise_return(promise_id);
    Ok(())
}
``` [4](#0-3) 

There is no callback registered to intercept and forward any refund from the eth-connector back to the original user.

The eth-connector's storage deposit amount is explicitly zero. The integration test confirms this:

```
// The NEP-141 implementation of ETH intentionally set the storage deposit amount equal to 0
// so any non-zero deposit amount is automatically returned to the user, leaving 0 storage
// balance behind.
``` [5](#0-4) 

In NEAR Protocol, when the eth-connector refunds the excess deposit, the refund is sent to the **predecessor of the cross-contract call** — which is the engine contract itself, not the original user. The engine contract's balance silently absorbs the user's NEAR. The user receives nothing back.

---

### Impact Explanation

**Critical — Direct theft of user funds (NEAR tokens).**

Any user who attaches NEAR to a `storage_deposit` call on the engine contract permanently loses that NEAR. Since the required storage amount is zero, 100% of any attached deposit is excess. The funds accumulate in the engine contract's balance with no recovery path for the user.

---

### Likelihood Explanation

**Medium.** The NEP-145 storage standard (`storage_deposit`) is a well-known interface that users and wallets routinely call with a non-zero deposit (e.g., the standard minimum of 1.25 mNEAR). Any user or dApp following the standard pattern of attaching NEAR to `storage_deposit` will silently lose those funds. A malicious frontend could amplify this by prompting users to attach large amounts.

---

### Recommendation

Add `env.assert_one_yocto()` (or an equivalent exact-amount check) at the top of `storage_deposit`, and forward only `ONE_YOCTO` to the eth-connector — consistent with every other payable method in the same file:

```rust
pub fn storage_deposit<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;   // <-- add this
    let args = read_json_args(&io).and_then(|args: StorageDepositArgs| {
        serde_json::to_vec(&(
            env.predecessor_account_id(),
            args.account_id,
            args.registration_only,
        ))
        .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(io, env, "engine_storage_deposit", args, ONE_YOCTO)  // <-- ONE_YOCTO
}
```

---

### Proof of Concept

1. User calls `storage_deposit` on the engine contract attaching `1_250_000_000_000_000_000_000` yoctoNEAR (the standard NEP-145 minimum).
2. `storage_deposit` skips any deposit validation and calls `return_promise(..., Yocto::new(env.attached_deposit()))`, forwarding the full `1_250_000_000_000_000_000_000` yoctoNEAR to the eth-connector.
3. The eth-connector processes `engine_storage_deposit`. Because its storage amount is 0, the entire deposit is excess and is refunded — but to the engine contract (the cross-contract call predecessor), not the user.
4. No callback exists in the engine contract to forward the refund to the user.
5. The user's `1_250_000_000_000_000_000_000` yoctoNEAR is permanently absorbed into the engine contract's balance. [6](#0-5) [7](#0-6)

### Citations

**File:** engine/src/contract_methods/connector.rs (L248-265)
```rust
pub fn ft_transfer<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;
    let args = read_json_args(&io).and_then(|args: FtTransferArgs| {
        serde_json::to_vec(&(
            env.predecessor_account_id(),
            args.receiver_id,
            args.amount,
            args.memo,
        ))
        .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(io, env, "engine_ft_transfer", args, ONE_YOCTO)
}
```

**File:** engine/src/contract_methods/connector.rs (L288-309)
```rust
pub fn storage_deposit<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    let args = read_json_args(&io).and_then(|args: StorageDepositArgs| {
        serde_json::to_vec(&(
            env.predecessor_account_id(),
            args.account_id,
            args.registration_only,
        ))
        .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(
        io,
        env,
        "engine_storage_deposit",
        args,
        Yocto::new(env.attached_deposit()),
    )
}
```

**File:** engine/src/contract_methods/connector.rs (L311-324)
```rust
pub fn storage_unregister<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;

    let args = read_json_args(&io).and_then(|args: StorageUnregisterArgs| {
        serde_json::to_vec(&(env.predecessor_account_id(), args.force))
            .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(io, env, "engine_storage_unregister", args, ONE_YOCTO)
}
```

**File:** engine/src/contract_methods/connector.rs (L598-617)
```rust
fn return_promise<I: IO + PromiseHandler, E: Env>(
    mut io: I,
    env: &E,
    method: &str,
    args: Vec<u8>,
    deposit: Yocto,
) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,
        method: method.to_string(),
        args,
        attached_balance: deposit,
        attached_gas: calculate_attached_gas(env),
    };
    let promise_id = io.promise_create_call(&promise_args);

    io.promise_return(promise_id);

    Ok(())
}
```

**File:** engine-tests-connector/src/connector.rs (L814-817)
```rust
    // The NEP-141 implementation of ETH intentionally set the storage deposit amount equal to 0
    // so any non-zero deposit amount is automatically returned to the user, leaving 0 storage
    // balance behind.
    assert_eq!(res, json!({"available": "0", "total": "0"}));
```

**File:** engine/src/lib.rs (L657-664)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn storage_deposit() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::connector::storage_deposit(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
