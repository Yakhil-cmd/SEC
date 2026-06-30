### Title
`set_eth_connector_contract_account` Switches Connector Without Checking Outstanding Balances, Permanently Freezing User Funds - (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

The `set_eth_connector_contract_account` function in the Aurora Engine immediately overwrites the stored ETH connector account ID with a new one, without verifying that the old connector holds no outstanding user balances. Because every user-facing connector operation (`withdraw`, `ft_transfer`, `ft_transfer_call`, `ft_balance_of`, `ft_total_supply`, `storage_deposit`, `storage_withdraw`) is routed at call-time through `return_promise` → `get_connector_account_id`, switching the connector pointer causes all future calls to target the new contract while all existing user funds remain locked in the old one, with no migration path.

---

### Finding Description

`set_eth_connector_contract_account` is the engine's owner-only method for replacing the external ETH connector contract: [1](#0-0) 

The function performs only an ownership/running check, then unconditionally writes the new account ID and serialization type to storage: [2](#0-1) 

The stored account ID is the single routing pointer used by every connector-facing operation. The private helper `return_promise` reads it fresh on every call: [3](#0-2) 

This means `withdraw`, `ft_transfer`, `ft_transfer_call`, `ft_balance_of`, `ft_total_supply`, `storage_deposit`, and `storage_withdraw` all resolve their target contract dynamically at invocation time: [4](#0-3) [5](#0-4) [6](#0-5) 

Additionally, `ft_on_transfer` uses `get_connector_account_id` to decide whether an incoming NEP-141 transfer is an ETH deposit or an ERC-20 token transfer: [7](#0-6) 

After the connector pointer is switched, transfers arriving from the old connector are no longer recognized as ETH deposits, breaking the deposit flow for any in-flight operations.

The NEAR contract entrypoint exposes this function publicly to the owner: [8](#0-7) 

There is no code anywhere in the engine that migrates NEP-141 balances from the old connector to the new one, and no mechanism for users to withdraw their funds from the old connector through the engine after the pointer has been changed.

---

### Impact Explanation

**Critical — Permanent freezing of funds.**

The ETH connector is the external NEAR contract that holds all bridged ETH as NEP-141 tokens on behalf of Aurora users. Its `ft_total_supply` represents the total ETH locked in the system. When the connector pointer is switched:

1. All user NEP-141 ETH balances recorded in the old connector become unreachable through the engine's normal interface.
2. `withdraw` calls go to the new (empty) connector, which will fail or return zero.
3. `ft_balance_of` queries go to the new connector, returning zero for all users.
4. The old connector still holds the actual locked ETH (backed by the Ethereum-side custodian), but the engine provides no path to access it.
5. There is no migration function in the codebase to transfer balances between connectors.

The entire bridged ETH supply held in the old connector is permanently frozen from the perspective of the Aurora Engine interface.

---

### Likelihood Explanation

The function is owner-only, which limits the attack surface to the protocol owner. However, the scenario is operationally realistic: the owner may legitimately need to upgrade the connector contract (e.g., to patch a bug or add a feature). Without an in-code guard, the owner can switch the connector while users have outstanding balances, with no warning. The original Alchemix report was confirmed at medium severity for the same reason — admin-only does not mean zero probability of operational error, especially for a critical infrastructure upgrade path. The absence of any balance-check requirement makes an accidental fund freeze a realistic operational risk.

---

### Recommendation

Before overwriting the connector account ID, require that the old connector holds no outstanding supply. This can be done by querying `ft_total_supply` on the old connector as a prerequisite cross-contract call, or by adding an explicit on-chain guard that reads the total ETH supply tracked by the engine and requires it to be zero before the switch is permitted.

At minimum, add a check inside `set_eth_connector_contract_account`:

```rust
// Before set_connector_account_id(io, &args.account):
// Require that the old connector's total supply is zero,
// i.e., no user funds remain in the old connector.
// This can be enforced via a synchronous read of the engine's
// internal ETH supply accounting, or via a promise-based
// pre-check against the old connector's ft_total_supply.
```

A safer design would make the connector switch a two-step process: first pause all connector operations, verify the old connector balance is zero (or migrate it), then complete the switch.

---

### Proof of Concept

1. Users deposit ETH into Aurora. The old connector (`connector_v1`) now holds `N` NEP-141 ETH tokens on behalf of users. The engine's `ft_total_supply` (routed to `connector_v1`) returns `N`.

2. The owner deploys a new connector contract (`connector_v2`) and calls `set_eth_connector_contract_account` with `connector_v2` as the argument.

3. `set_eth_connector_contract_account` executes with no balance check:
   ```rust
   set_connector_account_id(io, &args.account);   // pointer now → connector_v2
   set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
   ``` [2](#0-1) 

4. From this point, every call to `return_promise` resolves `get_connector_account_id` to `connector_v2`:
   ```rust
   target_account_id: get_connector_account_id(&io)?  // → connector_v2
   ``` [9](#0-8) 

5. Users call `withdraw` or `ft_balance_of`. These are routed to `connector_v2`, which has zero balances. Users receive zero or errors.

6. The `N` ETH tokens in `connector_v1` are permanently inaccessible through the engine. There is no function in the engine codebase to redirect calls back to `connector_v1` or to migrate balances. All user funds are frozen.

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

**File:** engine/src/contract_methods/connector.rs (L346-355)
```rust
pub fn ft_total_eth_supply_on_near<I: IO + Copy + PromiseHandler + Env>(
    io: I,
) -> Result<(), ContractError> {
    return_promise(io, &io, "ft_total_supply", Vec::new(), ZERO_YOCTO)
}

pub fn ft_balance_of<I: IO + Copy + PromiseHandler + Env>(io: I) -> Result<(), ContractError> {
    let args = io.read_input().to_vec();
    return_promise(io, &io, "ft_balance_of", args, ZERO_YOCTO)
}
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

**File:** engine/src/contract_methods/connector.rs (L598-616)
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
```

**File:** engine/src/lib.rs (L700-707)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn set_eth_connector_contract_account() {
        let io = Runtime;
        let env = Runtime;
        contract_methods::connector::set_eth_connector_contract_account(io, &env)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
