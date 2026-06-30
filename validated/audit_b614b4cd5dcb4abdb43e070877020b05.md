### Title
Replacing ETH Connector Account Without Functional Validation Causes Temporary Freeze of User Withdrawals — (File: `engine/src/contract_methods/connector.rs`)

---

### Summary

`set_eth_connector_contract_account` immediately overwrites the stored connector account ID with no validation that the new account is live and unpaused. Because the external ETH connector contract is **paused by default** upon deployment, if the owner points the engine at a freshly deployed but not-yet-unpaused connector, every user-facing operation that routes through `return_promise` (`withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_deposit`, `storage_unregister`, `storage_withdraw`) will fail, temporarily freezing user withdrawals and token transfers.

---

### Finding Description

`set_eth_connector_contract_account` writes the new account ID to storage and returns immediately: [1](#0-0) 

Every connector-facing user operation calls `return_promise`, which reads `get_connector_account_id` at call time and dispatches a cross-contract call to whatever account is currently stored: [2](#0-1) 

There is no on-chain check that the new account exists, is initialized, or is unpaused before the pointer is atomically flipped. The test harness for the external ETH connector explicitly documents that the contract ships **paused by default** and must be manually unpaused before use: [3](#0-2) 

If the owner calls `set_eth_connector_contract_account` before calling `pa_unpause_feature` on the new connector, the stored account ID immediately points to a paused contract. All subsequent `return_promise` dispatches target that paused contract; the NEAR promise fails, and the user's transaction fails with it.

Additionally, `ft_on_transfer` uses the stored connector ID to distinguish base-token deposits from ERC-20 deposits: [4](#0-3) 

Any in-flight `ft_on_transfer` call from the **old** connector arriving after the pointer is flipped will be misrouted into `receive_erc20_tokens` instead of `receive_base_tokens`, silently mishandling the deposit.

---

### Impact Explanation

**Temporary freeze of funds (High).** Users holding ETH or NEP-141 tokens on Aurora cannot withdraw or transfer them for as long as the connector pointer targets a paused account. The freeze persists until the owner either unpauses the new connector or reverts to the old one. There is no time-lock or delay on `set_eth_connector_contract_account`, so the freeze takes effect in the same block as the admin call.

---

### Likelihood Explanation

**Moderate.** The ETH connector contract is documented to be paused by default. A routine connector upgrade requires two separate owner transactions: (1) deploy and unpause the new connector, then (2) call `set_eth_connector_contract_account`. If these steps are executed out of order — a realistic operational mistake, exactly as described in the reference report — the engine immediately begins routing all user operations to a paused contract. There is no grace period, no on-chain guard, and no revert if the new account is non-functional.

---

### Recommendation

Before persisting the new connector account ID, add an on-chain cross-contract read (or require a signed attestation) confirming the new account is deployed and unpaused. Alternatively, introduce a time-delayed setter (similar to the `upgrade_delay_blocks` pattern already present in `EngineState`) so that operators have a window to catch misconfiguration before it takes effect. [5](#0-4) 

---

### Proof of Concept

1. **Owner deploys** a new ETH connector contract. The contract initializes in the **paused** state (default behavior, as shown in the test harness).
2. **Owner calls** `set_eth_connector_contract_account(new_connector_id, Borsh)`. The engine's stored connector pointer is immediately updated. No validation occurs.
3. **Owner forgets** (or has not yet submitted) the `pa_unpause_feature` transaction on the new connector.
4. **User calls** `withdraw(recipient, amount)` → `return_promise` dispatches to `new_connector_id.engine_withdraw` → the new connector is paused → the NEAR promise fails → the user's transaction fails. ETH remains locked in Aurora.
5. **User calls** `ft_transfer(receiver, amount, memo)` → same path → same failure.
6. **Old connector** sends an `ft_on_transfer` callback for a deposit that was in flight → `predecessor_account_id != get_connector_account_id()` → routed to `receive_erc20_tokens` instead of `receive_base_tokens` → deposit is mishandled.
7. The freeze and misrouting persist until the owner either unpauses the new connector or resets the pointer — with no on-chain mechanism to alert users or bound the duration.

### Citations

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

**File:** engine-tests/src/utils/workspace.rs (L82-89)
```rust
    // By default, the contract is paused. So we need to unpause it.
    let result = contract
        .call("pa_unpause_feature")
        .args_json(json!({ "key": "ALL" }))
        .max_gas()
        .transact()
        .await?;
    assert!(result.is_success());
```

**File:** engine/src/state.rs (L136-146)
```rust
impl<'a> From<BorshableEngineStateV3<'a>> for EngineState {
    fn from(state: BorshableEngineStateV3<'a>) -> Self {
        Self {
            chain_id: state.chain_id,
            owner_id: state.owner_id.into_owned(),
            upgrade_delay_blocks: state.upgrade_delay_blocks,
            is_paused: state.is_paused,
            key_manager: state.key_manager.map(Cow::into_owned),
        }
    }
}
```
