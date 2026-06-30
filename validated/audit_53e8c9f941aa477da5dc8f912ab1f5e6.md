### Title
`set_eth_connector_contract_account` Switches Connector Without State Migration, Freezing User Withdrawals - (File: engine/src/contract_methods/connector.rs)

---

### Summary

`set_eth_connector_contract_account` replaces the stored ETH connector account reference with a new one without migrating NEP-141 token balances from the old connector. All subsequent user withdrawal operations are routed to the new connector, which holds zero NEP-141 ETH balance, causing them to fail. The old connector retains all backing tokens but is no longer reachable through Aurora's normal interface.

---

### Finding Description

`set_eth_connector_contract_account` (lines 417–438) performs only two writes: it overwrites the stored connector account ID and the serialization type. No state migration, balance check, or coordination with the old connector occurs. [1](#0-0) 

Every user-facing withdrawal path — `withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_withdraw`, `storage_balance_of`, `ft_total_eth_supply_on_near`, `ft_balance_of`, `ft_metadata` — is implemented via `return_promise`, which resolves the target contract dynamically by calling `get_connector_account_id` at call time: [2](#0-1) 

After the connector is switched, every one of these calls is dispatched to the **new** connector. The new connector holds zero NEP-141 ETH tokens. Any `engine_withdraw` call against it will fail due to insufficient balance, freezing all ETH that was deposited through the old connector.

The deposit path is equally broken. `ft_on_transfer` uses `get_connector_account_id` to decide whether incoming tokens are base ETH (triggering `receive_base_tokens`) or ERC-20 tokens: [3](#0-2) 

After the switch, any `ft_on_transfer` call originating from the **old** connector is misclassified as an ERC-20 token transfer. The engine will attempt to look up a NEP-141→ERC-20 mapping for the old connector's account ID, fail, and return the full amount to the sender — silently rejecting legitimate deposits.

The old connector retains all NEP-141 ETH tokens but is unreachable through Aurora's public interface. The EVM state (ETH balances) remains intact, creating a permanent divergence between on-chain EVM balances and the NEP-141 backing held by the new connector.

---

### Impact Explanation

**High — Temporary freezing of funds.**

All ETH deposited before the connector switch becomes unwithdrawable. Users' EVM ETH balances are intact but cannot be redeemed because `withdraw` targets the new connector, which has no NEP-141 balance to burn. The freeze is technically reversible by the admin switching back to the old connector, but no automatic recovery exists and users have no recourse in the interim.

---

### Likelihood Explanation

**Medium.** `set_eth_connector_contract_account` is the intended upgrade path for the ETH connector contract. Any legitimate connector upgrade — e.g., deploying a new version of the connector — would trigger this bug unless the admin manually coordinates a full NEP-141 balance migration out-of-band. The function's own comment in the test setup (`set_eth_connector_contract_account` is called during normal initialization) confirms it is a routine operational call. [4](#0-3) 

---

### Recommendation

Before overwriting the connector account ID, `set_eth_connector_contract_account` should:

1. Read the old connector's NEP-141 total supply and verify it equals the sum of all EVM ETH balances, or
2. Schedule a cross-contract call to the old connector to transfer its full NEP-141 balance to the new connector before updating the stored account ID, making the switch atomic.

At minimum, the function should emit an event with the old connector account ID so that operators can manually coordinate the migration.

---

### Proof of Concept

1. Users deposit ETH via `old_connector.near`. The old connector mints NEP-141 ETH tokens and calls `ft_on_transfer` on Aurora, which credits EVM ETH balances via `receive_base_tokens`.
2. The Aurora owner calls `set_eth_connector_contract_account` with `new_connector.near` (a freshly deployed connector with zero NEP-141 balance).
3. A user calls `withdraw` on Aurora to exit their ETH back to NEAR.
4. `withdraw` calls `return_promise(io, env, "engine_withdraw", args, ONE_YOCTO)`.
5. `return_promise` resolves the target via `get_connector_account_id`, which now returns `new_connector.near`.
6. `engine_withdraw` is dispatched to `new_connector.near`, which holds 0 NEP-141 ETH. The call fails with an insufficient-balance error.
7. The user's EVM ETH balance is unchanged (not burned), but the withdrawal is rejected. The user cannot exit.
8. `old_connector.near` still holds all the NEP-141 ETH tokens, but Aurora no longer routes any calls to it. The backing is stranded. [5](#0-4) [6](#0-5)

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

**File:** engine/src/contract_methods/connector.rs (L80-90)
```rust
        let args: FtOnTransferArgs = read_json_args(&io)?;
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

**File:** engine/src/contract_methods/connector.rs (L417-438)
```rust
#[named]
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

**File:** engine/src/contract_methods/connector.rs (L562-566)
```rust
pub fn set_connector_account_id<I: IO + Copy>(mut io: I, account_id: &AccountId) {
    io.write_borsh(
        &construct_contract_key(EthConnectorStorageId::EthConnectorAccount),
        account_id,
    );
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

**File:** engine-tests-connector/src/utils.rs (L170-180)
```rust
        let acc = SetEthConnectorContractAccountArgs {
            account: eth_connector_contract.id().as_str().parse().unwrap(),
            withdraw_serialize_type: WithdrawSerializeType::Borsh,
        };
        let res = engine_contract
            .call("set_eth_connector_contract_account")
            .args_borsh(acc)
            .max_gas()
            .transact()
            .await?;
        assert!(res.is_success());
```
