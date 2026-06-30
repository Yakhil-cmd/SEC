### Title
`set_eth_connector_contract_account` Switches Connector Without Migrating Funds, Permanently Freezing User Balances - (`engine/src/contract_methods/connector.rs`)

### Summary
The `set_eth_connector_contract_account` function allows the owner to redirect the Aurora Engine to a new ETH connector contract without migrating user balances from the old connector. After the switch, all NEP-141 token balances held in the old connector become inaccessible: the engine routes all connector operations to the new connector, while the old connector's privileged `engine_withdraw` method is gated exclusively to the Aurora engine account — which no longer targets the old connector.

### Finding Description
`set_eth_connector_contract_account` in `engine/src/contract_methods/connector.rs` (lines 418–438) performs only two writes:

```rust
set_connector_account_id(io, &args.account);
set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
```

It does not:
- Verify that the old connector has zero total supply before switching
- Migrate user balances from the old connector to the new one
- Provide any engine-level method to withdraw from an arbitrary (non-current) connector

Every connector operation — `withdraw`, `ft_transfer`, `storage_deposit`, `storage_unregister`, `ft_metadata` — is dispatched through `return_promise`, which unconditionally reads the current connector account via `get_connector_account_id`:

```rust
fn return_promise<I: IO + PromiseHandler, E: Env>(...) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,  // always current connector
        ...
    };
```

The ETH connector contract enforces that `engine_withdraw` can only be called by the registered Aurora engine account (`aurora_engine_account_id`). This is confirmed by the integration test `test_access_right` in `engine-tests-connector/src/connector.rs` (lines 406–476):

```
assert!(contract.check_error_message(&res, "Method can be called only by aurora engine")?);
```

After `set_eth_connector_contract_account` is called with a new connector:
- The Aurora engine routes all calls to the new connector (which has zero balances)
- The old connector still holds all user NEP-141 balances representing bridged ETH
- The old connector's `engine_withdraw` can only be called by the Aurora engine account
- The Aurora engine has no method to target `engine_withdraw` at an arbitrary old connector
- User funds are frozen in the old connector

Recovery requires the owner to switch back to the old connector, drain it, then switch to the new connector — the exact "back and forth switches" described in the reference report.

### Impact Explanation
All NEP-141 token balances held in the old ETH connector represent claims on real ETH locked in the Ethereum custodian. After the connector switch, users cannot redeem these claims: calls to `withdraw` on the engine target the new connector (which has no record of their balances), and the old connector's `engine_withdraw` is unreachable through any normal engine path. The funds are frozen until the owner performs a multi-step recovery procedure. This constitutes a **temporary (potentially extended) freezing of all user funds** that were deposited before the connector switch.

### Likelihood Explanation
The scenario is triggered by a legitimate operational action: upgrading the ETH connector contract. The owner calls `set_eth_connector_contract_account` in good faith during a protocol upgrade, without realizing that no balance migration occurs. This is a realistic operational scenario — the function exists precisely to allow connector upgrades — and there is no guard in the code to prevent switching while the old connector holds non-zero balances.

### Recommendation
Before updating the connector account, verify that the old connector's total supply is zero (i.e., all user funds have been withdrawn). If a live migration is needed, implement a two-phase switch: first drain the old connector (by temporarily routing withdrawals to it), then switch to the new connector. At minimum, add a revert condition:

```rust
// Pseudocode
let old_connector = get_connector_account_id(&io)?;
// Cross-contract call to check ft_total_supply on old_connector
// Revert if non-zero
set_connector_account_id(io, &args.account);
```

Alternatively, expose a `yieldStrategyWithdrawAll`-style method that accepts an explicit connector account ID, allowing the owner to drain the old connector even after the switch.

### Proof of Concept
1. Users deposit ETH from Ethereum; the old connector (`old_connector.near`) mints NEP-141 tokens and records user balances.
2. Owner calls `set_eth_connector_contract_account` with `new_connector.near`.
3. Engine state now stores `new_connector.near` as the connector account.
4. User calls `withdraw` on the engine → `return_promise` targets `new_connector.near` → `engine_withdraw` on `new_connector.near` fails (user has no balance there).
5. User's NEP-141 tokens remain in `old_connector.near`.
6. `old_connector.near`'s `engine_withdraw` requires `predecessor == aurora` (the engine account).
7. The engine has no method to call `engine_withdraw` on `old_connector.near` while pointing to `new_connector.near`.
8. All user funds in `old_connector.near` are frozen.

**Root cause**: `set_eth_connector_contract_account` (lines 418–438) unconditionally overwrites the connector pointer with no balance check or migration step. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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

**File:** engine-tests-connector/src/connector.rs (L406-476)
```rust
async fn test_access_right() -> anyhow::Result<()> {
    let contract = TestContract::new_with_owner("owner").await?;
    let user_acc = contract
        .create_sub_account(DEPOSITED_RECIPIENT_NAME)
        .await?;
    let res = contract
        .deposit_eth_to_near(user_acc.id(), DEPOSITED_AMOUNT.into())
        .await?;
    assert!(res.is_success(), "{res:#?}");

    let res = contract
        .eth_connector_contract
        .call("get_aurora_engine_account_id")
        .view()
        .await?
        .json::<AccountId>()?;
    assert_eq!(&res, contract.engine_contract.id());

    let withdraw_amount = NEP141Wei::new(100);
    let res = user_acc
        .call(contract.eth_connector_contract.id(), "engine_withdraw")
        .args_borsh((user_acc.id(), *RECIPIENT_ADDRESS, withdraw_amount))
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_failure());
    assert!(contract.check_error_message(&res, "Method can be called only by aurora engine")?);

    let res = contract
        .owner
        .as_ref()
        .unwrap()
        .call(
            contract.eth_connector_contract.id(),
            "set_aurora_engine_account_id",
        )
        .args_json(json!({
            "new_aurora_engine_account_id": user_acc.id()
        }))
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_success(), "{res:#?}");

    let res = contract
        .eth_connector_contract
        .call("get_aurora_engine_account_id")
        .view()
        .await?
        .json::<AccountId>()?;
    assert_eq!(&res, user_acc.id());

    let res = user_acc
        .call(contract.eth_connector_contract.id(), "engine_withdraw")
        .args_borsh((user_acc.id(), *RECIPIENT_ADDRESS, withdraw_amount))
        .deposit(ONE_YOCTO)
        .transact()
        .await?;
    assert!(res.is_success());

    assert_eq!(
        contract.get_eth_on_near_balance(user_acc.id()).await?.0,
        DEPOSITED_AMOUNT - withdraw_amount.as_u128(),
    );
    assert_eq!(
        contract.total_supply().await?,
        DEPOSITED_AMOUNT - withdraw_amount.as_u128(),
    );

    Ok(())
}
```
