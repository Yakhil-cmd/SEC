### Title
Insufficient Gas Forwarded to `engine_withdraw` / `engine_ft_transfer` / `engine_ft_transfer_call` Promises Causes Silent Fund Freeze - (File: engine/src/contract_methods/connector.rs)

---

### Summary

The `calculate_attached_gas` helper in `engine/src/contract_methods/connector.rs` silently returns `NearGas::new(0)` when the transaction has already consumed all of its prepaid NEAR gas before the outbound promise is scheduled. Every connector method that calls `return_promise` — including `withdraw`, `ft_transfer`, and `ft_transfer_call` — inherits this defect. When the promise is dispatched with 0 gas, the downstream connector call (`engine_withdraw`, `engine_ft_transfer`, `engine_ft_transfer_call`) fails silently on the NEAR side, while the EVM-side state change (balance deduction / token burn) has already been committed. The result is a permanent freeze of the user's funds.

---

### Finding Description

`calculate_attached_gas` computes the gas to forward to the outbound promise as:

```
prepaid_gas - (used_gas + GAS_FOR_PROMISE_CREATION)
```

If `used_gas + GAS_FOR_PROMISE_CREATION >= prepaid_gas`, it returns `NearGas::new(0)` with no error, no revert, and no signal to the caller. [1](#0-0) 

`return_promise` then unconditionally schedules the promise with whatever gas `calculate_attached_gas` returns — including zero: [2](#0-1) 

This `return_promise` helper is used by all three critical fund-movement methods:

- `withdraw` → `"engine_withdraw"` [3](#0-2) 
- `ft_transfer` → `"engine_ft_transfer"` [4](#0-3) 
- `ft_transfer_call` → `"engine_ft_transfer_call"` [5](#0-4) 

The `withdraw` function, for example, reads the user's withdrawal args, serializes them, and calls `return_promise` — but if gas is exhausted, the downstream `engine_withdraw` call on the connector contract receives 0 gas and fails. The EVM-side burn has already occurred (the user's NEP-141 balance was decremented by the connector before calling `withdraw` on the engine), so the funds are permanently lost.

The `TODO` comment in the code itself acknowledges this is unresolved:

```rust
// TODO: Return `Result` with an error about lacking of gas instead.
fn calculate_attached_gas<E: Env>(env: &E) -> NearGas {
``` [1](#0-0) 

---

### Impact Explanation

**Impact: High — Temporary or Permanent Freezing of Funds.**

When `withdraw` is called with insufficient prepaid gas:
1. The NEAR-side `withdraw` method on the engine contract executes and calls `return_promise` with 0 gas.
2. The downstream `engine_withdraw` call on the connector contract fails due to 0 gas.
3. The user's NEP-141 tokens (ETH on NEAR, or bridged ERC-20) have already been debited from their balance in the connector's accounting.
4. No refund mechanism exists in this path — the tokens are frozen.

The same applies to `ft_transfer` and `ft_transfer_call`: if the promise is dispatched with 0 gas, the actual token transfer on the connector never executes, but the engine's internal accounting has already been updated.

---

### Likelihood Explanation

**Likelihood: Medium.**

The NEAR maximum gas per transaction is 300 TGas. The `withdraw`, `ft_transfer`, and `ft_transfer_call` methods are relatively lightweight before the promise dispatch, so under normal conditions there is sufficient gas. However:

- A user or contract can call these methods with a low `prepaid_gas` value (e.g., just enough to pass the NEAR runtime's minimum but not enough to leave gas for the promise).
- Any future increase in NEAR runtime costs for storage reads (analogous to EIP-2929 in Ethereum) could push `used_gas` past the threshold.
- The code itself has a `TODO` acknowledging this is a known gap with no guard. [1](#0-0) 

---

### Recommendation

Replace the silent fallback to 0 with an explicit error. The `TODO` comment already identifies this:

```rust
fn calculate_attached_gas<E: Env>(env: &E) -> Result<NearGas, ContractError> {
    let required_gas = env.used_gas().saturating_add(GAS_FOR_PROMISE_CREATION);
    if required_gas >= env.prepaid_gas() {
        Err(/* ERR_INSUFFICIENT_GAS */)
    } else {
        Ok(env.prepaid_gas() - required_gas)
    }
}
```

Propagate this `Result` through `return_promise` and all callers (`withdraw`, `ft_transfer`, `ft_transfer_call`). This ensures the transaction reverts before any state change is committed when gas is insufficient, rather than silently dispatching a doomed promise.

---

### Proof of Concept

1. Deploy Aurora Engine on NEAR testnet.
2. Deposit ETH to NEAR (bridge ETH → NEP-141 on Aurora).
3. Call `withdraw` on the Aurora engine contract with `prepaid_gas` set to a value just above the NEAR minimum (e.g., 5 TGas), which is less than what `calculate_attached_gas` needs to leave a non-zero remainder after `used_gas + GAS_FOR_PROMISE_CREATION`.
4. Observe: `calculate_attached_gas` returns `NearGas::new(0)`.
5. The promise to `engine_withdraw` is scheduled with 0 gas and fails on the connector side.
6. The user's NEP-141 balance has been decremented; no refund is issued.
7. Funds are frozen.

The `GAS_FOR_PROMISE_CREATION` constant is only 2 TGas: [6](#0-5) 

Any caller who provides less than `used_gas + 2 TGas` of prepaid gas triggers the zero-gas path. Since NEAR allows callers to specify prepaid gas, an unprivileged user can deliberately or accidentally trigger this condition.

### Citations

**File:** engine/src/contract_methods/connector.rs (L40-41)
```rust
/// Amount of gas required for the promise creation.
const GAS_FOR_PROMISE_CREATION: NearGas = NearGas::new(2_000_000_000_000);
```

**File:** engine/src/contract_methods/connector.rs (L43-59)
```rust
pub fn withdraw<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    env.assert_one_yocto()?;

    let args: WithdrawCallArgs = io.read_input_borsh()?;
    let args = borsh::to_vec(&EngineWithdrawCallArgs {
        sender_id: env.predecessor_account_id(),
        recipient_address: args.recipient_address,
        amount: args.amount,
    })
    .unwrap();

    return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)
}
```

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

**File:** engine/src/contract_methods/connector.rs (L267-285)
```rust
pub fn ft_transfer_call<I: IO + Copy + PromiseHandler, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    require_running(&state::get_state(&io)?)?;
    // Check is payable
    env.assert_one_yocto()?;
    let args = read_json_args(&io).and_then(|args: FtTransferCallArgs| {
        serde_json::to_vec(&(
            env.predecessor_account_id(),
            args.receiver_id,
            args.amount,
            args.memo,
            args.msg,
        ))
        .map_err(Into::<ParseArgsError>::into)
    })?;

    return_promise(io, env, "engine_ft_transfer_call", args, ONE_YOCTO)
```

**File:** engine/src/contract_methods/connector.rs (L587-596)
```rust
// TODO: Return `Result` with an error about lacking of gas instead.
fn calculate_attached_gas<E: Env>(env: &E) -> NearGas {
    let required_gas = env.used_gas().saturating_add(GAS_FOR_PROMISE_CREATION);

    if required_gas >= env.prepaid_gas() {
        NearGas::new(0)
    } else {
        env.prepaid_gas() - required_gas
    }
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
