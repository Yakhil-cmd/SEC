### Title
Owner Can Redirect All Bridge Fund Flows to a Malicious Contract via `set_eth_connector_contract_account` — (`engine/src/contract_methods/connector.rs`)

---

### Summary

The Aurora Engine owner can call `set_eth_connector_contract_account` to replace the ETH connector contract address with any arbitrary NEAR account. Because every user-facing bridge operation (`withdraw`, `ft_transfer`, `ft_transfer_call`, `storage_deposit`, `storage_unregister`, `storage_withdraw`) and both exit precompiles (`ExitToNear`, `ExitToEthereum`) unconditionally route their NEAR cross-contract calls to the stored connector account ID, a malicious owner can silently redirect all in-flight user funds to an attacker-controlled contract.

---

### Finding Description

`set_eth_connector_contract_account` writes an arbitrary NEAR `AccountId` into engine storage with no validation beyond an owner-or-private-call check: [1](#0-0) 

The stored value is then read by `get_connector_account_id`, which is the sole source of the target account for every connector promise: [2](#0-1) 

Every user-facing bridge method delegates to `return_promise`, which calls `get_connector_account_id` to determine the target: [3](#0-2) 

The same stored account is consumed by both exit precompiles. `ExitToNear` reads it at line 431: [4](#0-3) 

`ExitToEthereum` reads it at line 901 and uses it as the `target_account_id` of the withdrawal promise: [5](#0-4) 

The `ft_on_transfer` entry point also compares `predecessor_account_id` against the stored connector ID to decide whether an incoming transfer is a base-token deposit or an ERC-20 deposit: [6](#0-5) 

Replacing the connector account breaks this check: the real connector's `ft_on_transfer` calls are no longer recognized as base-token deposits, corrupting deposit accounting for all users.

---

### Impact Explanation

**Critical — Direct theft of user funds.**

When a user calls `withdraw` to bridge ETH from Aurora back to NEAR, the engine forwards the call (including the user's token amount) to whatever account is stored as the connector. A malicious connector can accept `engine_withdraw` without burning the user's NEP-141 tokens or initiating the bridge withdrawal, permanently retaining the funds. The same applies to `ft_transfer` and `ft_transfer_call`. For the exit precompiles, the withdrawal NEAR promise is sent to the malicious account, so the user's EVM balance is debited but the NEAR-side release never occurs.

---

### Likelihood Explanation

The owner account is a single privileged NEAR account with no on-chain time-lock or multi-sig enforced at the contract level. The function is callable at any time while the engine is running (`require_running` passes). There is no delay, no guardian veto, and no event that would allow users to exit before the change takes effect. Any compromise or malicious act by the owner key is sufficient.

---

### Recommendation

1. **Remove or time-lock the setter.** Apply a mandatory delay (e.g., `upgrade_delay_blocks`) between the announcement and the activation of a new connector account, giving users time to withdraw.
2. **Emit an observable event** when the connector account changes so off-chain monitors can alert users immediately.
3. **Validate the new account** by performing a view call to confirm it implements the expected interface before committing the change.
4. **Consider immutability.** Following the Convex model cited in the original report, the connector address could be set once at initialization and made immutable, requiring a full redeployment to change.

---

### Proof of Concept

1. Owner calls `set_eth_connector_contract_account` with `account = attacker.near`. [7](#0-6) 

2. User calls `withdraw` on the engine with their bridged ETH amount. [8](#0-7) 

3. `return_promise` resolves `get_connector_account_id` → `attacker.near` and schedules `engine_withdraw` on that account. [9](#0-8) 

4. `attacker.near` receives the call, keeps the NEP-141 tokens, and returns success. The user's funds are permanently lost.

5. Simultaneously, any EVM user calling the `ExitToEthereum` precompile has their withdrawal promise routed to `attacker.near` instead of the real connector: [10](#0-9)

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

**File:** engine-precompiles/src/native.rs (L289-293)
```rust
impl<I: IO> EthConnector for ExitToNear<I> {
    fn get_eth_connector_contract_account(&self) -> Result<AccountId, ExitError> {
        get_eth_connector_contract_account(&self.io)
    }
}
```

**File:** engine-precompiles/src/native.rs (L901-904)
```rust
                let eth_connector_account_id = self.get_eth_connector_contract_account()?;

                (
                    eth_connector_account_id,
```

**File:** engine-precompiles/src/native.rs (L977-983)
```rust
        let withdraw_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method: "withdraw".to_string(),
            args: serialized_args,
            attached_balance: Yocto::new(1),
            attached_gas: costs::WITHDRAWAL_GAS,
        };
```
