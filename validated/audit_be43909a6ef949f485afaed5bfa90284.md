### Title
Missing Silo Address-Whitelist Enforcement in `receive_base_tokens` Allows Funds to Be Frozen in Non-Whitelisted EVM Addresses - (File: `engine/src/engine.rs`)

---

### Summary

In Silo mode, the `receive_erc20_tokens` path enforces the `Address` whitelist before minting bridged ERC-20 tokens, redirecting to a fallback address when the recipient is not whitelisted. The `receive_base_tokens` path — triggered when the eth-connector bridges base ETH into Aurora — performs **no whitelist check at all**. Any unprivileged NEAR account can therefore deposit base ETH to any EVM address, including non-whitelisted ones. Because non-whitelisted addresses cannot submit EVM transactions in Silo mode, the deposited ETH becomes permanently inaccessible to the recipient, constituting a fund freeze.

---

### Finding Description

`ft_on_transfer` dispatches to one of two handlers depending on whether the predecessor is the eth-connector account: [1](#0-0) 

`receive_erc20_tokens` applies the Silo whitelist before minting: [2](#0-1) 

`receive_base_tokens` performs **no equivalent check**: [3](#0-2) 

The Silo whitelist helper `is_allow_receive_erc20_tokens` simply checks the `Address` whitelist: [4](#0-3) 

And `is_allow_submit` — which gates all EVM transaction submission — also checks the same `Address` whitelist: [5](#0-4) 

Because `receive_base_tokens` skips the whitelist, ETH can be minted to any EVM address. Once minted to a non-whitelisted address, the owner of that address cannot call `submit` (blocked by the `Address` whitelist), cannot call the `ExitToNear` or `ExitToEthereum` precompiles (which require EVM execution via `submit`), and has no other on-chain path to recover the funds.

---

### Impact Explanation

**High — Temporary (potentially permanent) freezing of funds.**

ETH deposited to a non-whitelisted EVM address in Silo mode is inaccessible: the address cannot submit transactions, cannot invoke exit precompiles, and cannot transfer the balance. Recovery requires the Silo operator to whitelist the address, which is an out-of-band action with no protocol guarantee. If the operator does not act, the freeze is permanent.

---

### Likelihood Explanation

**Medium.** The eth-connector `ft_transfer_call` interface is a standard, publicly documented NEAR cross-contract call. Any NEAR account holding bridgeable ETH can trigger this path by specifying an arbitrary EVM address in the `msg` field. No special privilege is required. The condition is reachable whenever a Silo deployment has the `Address` whitelist enabled, which is the intended production configuration for Silo mode.

---

### Recommendation

Apply the same whitelist-and-fallback logic in `receive_base_tokens` that exists in `receive_erc20_tokens`. Before crediting the balance, check `silo::is_allow_receive_erc20_tokens`; if the recipient is not whitelisted and a fallback address is configured, redirect the deposit to the fallback address. If no fallback is configured and the recipient is not whitelisted, return an error so the tokens are refunded to the sender.

---

### Proof of Concept

1. Deploy Aurora in Silo mode: call `set_silo_params` with a `fixed_gas` and `erc20_fallback_address`, then call `set_whitelist_status` to enable `WhitelistKind::Address`.
2. Do **not** add victim address `V` to the `Address` whitelist.
3. From any NEAR account, call `ft_transfer_call` on the eth-connector contract with `receiver_id = aurora`, `amount = X`, `msg = hex(V)`.
4. The eth-connector calls `ft_on_transfer` on Aurora; `predecessor_account_id == eth_connector`, so `receive_base_tokens` is invoked.
5. `receive_base_tokens` parses `V` from `msg` and calls `set_balance(&mut self.io, &V, &new_balance)` with no whitelist check.
6. Confirm `V`'s EVM balance is now `X` ETH.
7. Attempt to submit any EVM transaction from `V` — it is rejected with `NotAllowed` because `V` is not in the `Address` whitelist.
8. Attempt to call `ExitToNear` or `ExitToEthereum` — both require EVM execution via `submit`, which is also blocked.
9. The `X` ETH is frozen in `V` with no protocol-level recovery path. [6](#0-5) [7](#0-6)

### Citations

**File:** engine/src/contract_methods/connector.rs (L62-109)
```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let current_account_id = env.current_account_id();
        let predecessor_account_id = env.predecessor_account_id();
        let mut engine: Engine<_, _> = Engine::new(
            predecessor_address(&predecessor_account_id),
            current_account_id.clone(),
            io,
            env,
        )?;

        sdk::log!("Call ft_on_transfer");

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

        #[allow(clippy::used_underscore_binding)]
        let amount_to_return = if let Err(_err) = &result {
            sdk::log!("Error in ft_on_transfer: {_err:?}");
            // An error occurred, so we need to return the amount of tokens to the sender.
            args.amount.as_u128()
        } else {
            // Everything is ok, so return 0.
            0
        };

        let output = crate::prelude::format!("\"{amount_to_return}\"");
        io.return_output(output.as_bytes());

        // In case of an error, we just return Ok(None) to avoid a panic in the contract. It's ok
        // because in case of an error, we already returned the amount of tokens to the sender.
        Ok(result.unwrap_or(None))
    })
}
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

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L140-143)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```
