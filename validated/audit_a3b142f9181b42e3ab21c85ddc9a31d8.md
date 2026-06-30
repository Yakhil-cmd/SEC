### Title
Owner Can Atomically Swap `EthConnector` Account Mid-Flight, Freezing User ETH Deposits — (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`set_eth_connector_contract_account` lets the owner replace the live `EthConnector` account ID at any time with no balance-continuity guard. Because every withdrawal and transfer promise is routed to whatever account ID is stored at call time, a connector swap that lands between a user's deposit and their withdrawal silently redirects the withdrawal to a contract that holds none of the user's ETH, permanently trapping the user's EVM balance.

---

### Finding Description

**Mutable connector account with no safety invariant**

`set_eth_connector_contract_account` writes a new `AccountId` into storage unconditionally:

```rust
// engine/src/contract_methods/connector.rs  lines 418-438
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();
        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }
        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;
        set_connector_account_id(io, &args.account);          // ← no balance check
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
        Ok(())
    })
}
``` [1](#0-0) 

**All outbound calls use the live connector ID**

`return_promise` — called by `withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_deposit`, `storage_unregister`, and `storage_withdraw` — reads the connector account ID at execution time:

```rust
// engine/src/contract_methods/connector.rs  lines 598-617
fn return_promise<I: IO + PromiseHandler, E: Env>(...) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,   // ← live read
        method: method.to_string(),
        ...
    };
    ...
}
``` [2](#0-1) 

**Deposit routing also uses the live connector ID**

`ft_on_transfer` identifies base-ETH deposits by comparing the predecessor to the stored connector account:

```rust
// engine/src/contract_methods/connector.rs  line 81
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)
} else {
    engine.receive_erc20_tokens(...)
};
``` [3](#0-2) 

**EVM balance is credited on deposit but deducted only inside the connector**

`receive_base_tokens` increases the EVM balance when the old connector calls `ft_on_transfer`. The engine's `withdraw` function does **not** deduct the EVM balance itself — it only schedules a promise to `engine_withdraw` on the connector contract. If that connector contract changes, the promise targets a contract that holds no ETH for the user, the call fails, and the EVM balance is never deducted — leaving the user's ETH permanently unwithdrawable. [4](#0-3) 

---

### Impact Explanation

A user who deposited ETH through the old connector has an EVM balance credited in Aurora's state. After the connector swap, every call to `withdraw` routes the `engine_withdraw` promise to the new connector, which holds none of that ETH. The promise fails; the EVM balance is never decremented; the ETH is permanently unwithdrawable from Aurora to NEAR/Ethereum. The user retains an EVM balance they can spend inside Aurora but can never bridge out — a **permanent freezing of bridged funds**.

---

### Likelihood Explanation

`set_eth_connector_contract_account` is an explicitly supported owner operation (it appears in the standalone-storage sync types, workspace contract bindings, and integration tests). Any legitimate connector migration — a routine operational event — triggers the race window. No attacker capability is required; the owner performing a normal upgrade is sufficient. [5](#0-4) 

---

### Recommendation

Mirror the mitigation proposed in the original report: add a balance-continuity assertion inside `set_eth_connector_contract_account`. Before committing the new connector ID, verify that the new connector account holds at least as much ETH (NEP-141 supply) as the old one. Alternatively, make the connector account immutable after initialization and require a full migration path (drain old → fund new → swap) as an atomic operation.

```rust
// Pseudocode guard
let old_balance = query_connector_supply(&old_account_id);
set_connector_account_id(io, &args.account);
let new_balance = query_connector_supply(&args.account);
require!(new_balance >= old_balance, ERR_CONNECTOR_BALANCE_MISMATCH);
```

---

### Proof of Concept

1. Alice calls `ft_transfer_call` on the old connector, transferring 1 ETH to Aurora. `ft_on_transfer` fires with `predecessor == old_connector == get_connector_account_id()`, so `receive_base_tokens` is called and Alice's EVM balance is credited with 1 ETH.
2. Owner calls `set_eth_connector_contract_account` with `new_connector` as the argument. The stored connector ID is now `new_connector`.
3. Alice calls `withdraw` on the Aurora Engine with `amount = 1 ETH`. The engine constructs `EngineWithdrawCallArgs` and calls `return_promise(..., "engine_withdraw", ...)`. `return_promise` reads `get_connector_account_id()` — now `new_connector` — and schedules the promise there.
4. `new_connector` has no ETH balance for Alice. Its `engine_withdraw` handler fails (or does not exist). The promise result is a failure.
5. The engine's `withdraw` function has no failure callback; it never deducted Alice's EVM balance. Alice's 1 ETH EVM balance remains in Aurora state but is permanently unwithdrawable: every future `withdraw` call repeats step 3–4 against `new_connector`.

Alice has lost access to 1 ETH worth of bridged value with no recovery path unless the owner manually reverts the connector account.

### Citations

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

**File:** engine/src/contract_methods/connector.rs (L81-90)
```rust
        let result = if predecessor_account_id == get_connector_account_id(&io)? {
            engine.receive_base_tokens(&args)
        } else {
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```

**File:** engine/src/contract_methods/connector.rs (L418-438)
```rust
pub fn set_eth_connector_contract_account<I: IO + Copy, E: Env>(
    io: I,
    env: &E,
) -> Result<(), ContractError> {
    with_hashchain(io, env, function_name!(), |io| {
        let state = state::get_state(&io)?;
        require_running(&state)?;
        let is_private = env.assert_private_call();

        if is_private.is_err() {
            require_owner_only(&state, &env.predecessor_account_id())?;
        }

        let args: SetEthConnectorContractAccountArgs = io.read_input_borsh()?;

        set_connector_account_id(io, &args.account);
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);

        Ok(())
    })
}
```

**File:** engine/src/contract_methods/connector.rs (L562-567)
```rust
pub fn set_connector_account_id<I: IO + Copy>(mut io: I, account_id: &AccountId) {
    io.write_borsh(
        &construct_contract_key(EthConnectorStorageId::EthConnectorAccount),
        account_id,
    );
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
