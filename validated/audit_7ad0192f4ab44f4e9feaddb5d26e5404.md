### Title
`set_eth_connector_contract_account` Can Be Called While Deposits Are In-Flight, Permanently Trapping NEP-141 Tokens and Freezing User Withdrawals - (File: engine/src/contract_methods/connector.rs)

### Summary

`set_eth_connector_contract_account` can be called at any time by the contract owner with no guard against in-flight bridge operations. Because `ft_on_transfer` and `return_promise` both read the connector account ID dynamically at execution time, changing the connector mid-operation causes in-flight ETH deposits to be misrouted and all future withdrawals to be directed to a new connector that holds no NEP-141 token balances. The NEP-141 tokens backing all existing Aurora ETH balances become permanently inaccessible in the old connector.

### Finding Description

`set_eth_connector_contract_account` unconditionally overwrites the stored connector account ID with no check for outstanding balances or in-flight receipts:

```rust
// engine/src/contract_methods/connector.rs, lines 418-438
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
        set_connector_account_id(io, &args.account);                          // ← overwrites immediately
        set_connector_withdraw_serialization_type(io, &args.withdraw_serialize_type);
        Ok(())
    })
}
``` [1](#0-0) 

Every subsequent read of the connector account ID uses the new value. Two code paths are critically affected:

**Path 1 — `ft_on_transfer` misrouting (in-flight deposits)**

`ft_on_transfer` distinguishes base-token (ETH) deposits from ERC-20 deposits by comparing `predecessor_account_id` against the stored connector account:

```rust
// lines 81-90
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)   // mints ETH on Aurora
} else {
    engine.receive_erc20_tokens(...)    // mints ERC-20 tokens
};
``` [2](#0-1) 

In NEAR, cross-contract calls are asynchronous receipts. If a user initiates an ETH deposit via the old connector (which queues an `ft_on_transfer` receipt), and the owner calls `set_eth_connector_contract_account` before that receipt executes, the receipt arrives with `predecessor_account_id = OLD_CONNECTOR` but `get_connector_account_id` now returns `NEW_CONNECTOR`. The check fails; `receive_erc20_tokens` is called instead of `receive_base_tokens`. Because the old connector's account ID is not registered in the NEP-141→ERC-20 map, `receive_erc20_tokens` returns an error and the tokens are refunded to the sender. The deposit silently fails. [3](#0-2) [4](#0-3) 

**Path 2 — `return_promise` routing (all future withdrawals)**

Every outbound connector call — `withdraw`, `ft_transfer`, `storage_deposit`, `storage_unregister`, `storage_withdraw` — resolves the target account at call time via `return_promise`:

```rust
// lines 598-616
fn return_promise<I: IO + PromiseHandler, E: Env>(...) -> Result<(), ContractError> {
    let promise_args = PromiseCreateArgs {
        target_account_id: get_connector_account_id(&io)?,  // always reads CURRENT connector
        method: method.to_string(),
        ...
    };
``` [5](#0-4) 

After the connector account is changed, all withdrawal calls are routed to the **new** connector. The new connector holds zero NEP-141 tokens (all tokens are held by the old connector). The new connector's `engine_withdraw` will fail because it has no balance to release. Meanwhile, `withdraw` in Aurora does not deduct from the EVM balance before making the cross-contract call, so the user's Aurora ETH balance is preserved — but permanently unwithdrawable. [6](#0-5) 

The NEP-141 tokens in the old connector are also permanently inaccessible: Aurora no longer routes any calls to the old connector, and the old connector's `engine_withdraw` can only be triggered by Aurora.

### Impact Explanation

**Permanent freezing of funds.** All NEP-141 tokens held in the old connector — which back every unit of ETH currently on Aurora — become permanently inaccessible. Users cannot withdraw their ETH from Aurora because all withdrawal calls are routed to the new connector, which has no token balance to release. The old connector's tokens are stranded with no recovery path unless the owner reverts to the old connector account.

**Temporary freezing of funds.** Any ETH deposit that was in-flight (i.e., the `ft_on_transfer` receipt was queued before the connector change) silently fails and the tokens are returned to the sender. This is a temporary DoS on deposits.

### Likelihood Explanation

Low. The trigger is a legitimate administrative action — upgrading or migrating the ETH connector contract — which is a realistic operational event. The owner does not need to be malicious; the damage occurs as an unintended side-effect of a routine upgrade. The likelihood is low because it requires a specific ordering of events (connector change while users have active balances or in-flight deposits), but the impact when it occurs is severe and potentially irreversible.

### Recommendation

Before overwriting the connector account ID, the engine should verify that the old connector holds a zero NEP-141 balance (i.e., no ETH is currently backed by it). If migration is necessary while balances exist, the implementation should:

1. Atomically transfer the full NEP-141 balance from the old connector to the new connector as part of the `set_eth_connector_contract_account` call, or
2. Record the old connector account alongside the new one and continue routing existing-balance withdrawals to the old connector until its balance reaches zero, or
3. Reject `set_eth_connector_contract_account` unless the old connector's NEP-141 balance is zero.

### Proof of Concept

1. User A calls `ft_transfer_call` on the old connector, sending 100 NEP-141 tokens to Aurora. This queues an `ft_on_transfer` receipt on Aurora.
2. Before the receipt executes, the owner calls `set_eth_connector_contract_account` with a new connector account.
3. The queued `ft_on_transfer` receipt executes. `predecessor_account_id` is the old connector; `get_connector_account_id` returns the new connector. The check at line 81 fails; `receive_erc20_tokens` is called, fails (old connector not in ERC-20 map), and 100 tokens are returned to User A. The deposit is lost.
4. User B, who already has 500 ETH on Aurora (deposited before the change), calls `withdraw`. `return_promise` routes to the new connector's `engine_withdraw`. The new connector has no NEP-141 balance; the call fails. User B's 500 ETH on Aurora is permanently unwithdrawable.
5. The 500 NEP-141 tokens backing User B's ETH remain locked in the old connector with no mechanism to retrieve them.

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

**File:** engine/src/engine.rs (L773-790)
```rust
    pub fn receive_base_tokens(
        &mut self,
        args: &FtOnTransferArgs,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
        let amount = Wei::new_u128(args.amount.as_u128());
        let receipient = message_data.recipient;
        let balance = get_balance(&self.io, &receipient);
        let new_balance = balance
            .checked_add(amount)
            .ok_or(errors::ERR_BALANCE_OVERFLOW)?;

        set_balance(&mut self.io, &receipient, &new_balance);

        sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());

        Ok(None)
    }
```

**File:** engine/src/engine.rs (L796-844)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;

        sdk::log!("Mint {amount} ERC-20 tokens for: {}", recipient.encode());

        // Return SubmitResult so that it can be accessed in standalone engine.
        // This is used to help with the indexing of bridge transactions.
        Ok(Some(result))
    }
```
