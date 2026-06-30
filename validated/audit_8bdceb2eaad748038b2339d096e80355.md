### Title
`set_eth_connector_contract_account` Does Not Verify Zero Outstanding Balance Before Switching Connector — (`engine/src/contract_methods/connector.rs`)

---

### Summary

`set_eth_connector_contract_account` atomically overwrites the stored eth-connector account ID without verifying that the old connector holds zero user balances. Every subsequent connector operation (`withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_unregister`, etc.) routes its promise to the **new** connector. Any funds that users held in the old connector become permanently inaccessible through the engine's interface.

---

### Finding Description

`set_eth_connector_contract_account` is the engine-side function that records which NEAR account acts as the eth-connector (the NEP-141 custodian for bridged ETH and ERC-20 tokens). [1](#0-0) 

The function performs only an ownership/running check and then unconditionally overwrites the stored connector account ID: [2](#0-1) 

Every user-facing connector operation — `withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_unregister`, `storage_withdraw`, `ft_balance_of`, etc. — resolves the target account at call time via `get_connector_account_id`, which reads the value just written: [3](#0-2) 

There is no check that the old connector's total supply is zero, no migration step, and no drain-first requirement. The moment the owner calls `set_eth_connector_contract_account(new_connector)`, all future operations are silently redirected to `new_connector`, and the old connector's balances become unreachable through any engine entrypoint.

---

### Impact Explanation

**Permanent freezing of funds (Critical).**

All ETH and NEP-141 token balances that users held in the old connector contract are permanently frozen. The engine provides no alternative path to recover them: every withdrawal, transfer, or balance query is forwarded to the new connector, which has no record of the old balances. The old connector contract still holds the underlying tokens, but no engine method can reach them.

---

### Likelihood Explanation

The owner legitimately needs to call `set_eth_connector_contract_account` during connector upgrades (the test harness itself calls it during setup). [4](#0-3) 

A connector upgrade is a routine operational event. Without an enforced pre-condition, any upgrade that does not manually drain all user balances first will permanently freeze those funds. The missing guard is not obvious from the function signature or documentation, making accidental omission highly plausible.

---

### Recommendation

Before overwriting the connector account ID, the engine should verify that the old connector reports a zero total supply. Concretely:

1. Issue a cross-contract view call to `ft_total_supply` on the old connector before committing the change.
2. Only proceed with the account ID update if the returned supply is zero.
3. Alternatively, expose a dedicated migration method that atomically migrates all balances from the old connector to the new one before switching the pointer.

---

### Proof of Concept

1. User A deposits 1 ETH into Aurora. The eth-connector (old_connector) mints 1 NEP-141 unit to User A's account.
2. Owner calls `set_eth_connector_contract_account(new_connector)`. The engine writes `new_connector` to storage. [5](#0-4) 
3. User A calls `withdraw(recipient_eth_address, 1)`. The engine's `withdraw` function calls `return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)`. [6](#0-5) 
4. `return_promise` resolves `get_connector_account_id` → returns `new_connector`. The promise is dispatched to `new_connector`, which has no record of User A's balance and rejects the call. [7](#0-6) 
5. User A's 1 ETH remains locked in `old_connector` with no engine entrypoint able to reach it. The funds are permanently frozen.

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

**File:** engine-tests/src/utils/workspace.rs (L91-95)
```rust
    let result = aurora
        .set_eth_connector_contract_account(contract_account.id(), WithdrawSerializeType::Borsh)
        .transact()
        .await?;
    assert!(result.is_success());
```
