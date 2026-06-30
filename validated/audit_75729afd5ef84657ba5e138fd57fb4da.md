### Title
Whitelist Bypass in `receive_base_tokens` Allows Non-Whitelisted Addresses to Receive Base ETH Tokens in Silo Mode - (File: `engine/src/engine.rs`)

### Summary

In silo mode with the `Address` whitelist enabled, `receive_base_tokens` mints base ETH tokens to any recipient address without performing any whitelist check. This is structurally identical to the reported vulnerability: the restriction enforcement is present in one code path (`receive_erc20_tokens`) but entirely absent in the parallel path (`receive_base_tokens`), allowing a non-whitelisted address to receive tokens it should be blocked from receiving.

### Finding Description

`ft_on_transfer` in `engine/src/contract_methods/connector.rs` routes incoming token transfers to one of two handlers depending on the predecessor account: [1](#0-0) 

When the predecessor is the ETH connector account, `receive_base_tokens` is called. When it is any other NEP-141 token, `receive_erc20_tokens` is called.

`receive_erc20_tokens` correctly enforces the silo `Address` whitelist by checking `silo::is_allow_receive_erc20_tokens` and redirecting to a fallback address when the recipient is not whitelisted: [2](#0-1) 

`receive_base_tokens`, however, performs no such check. It reads the recipient from the message and unconditionally mints base ETH to that address: [3](#0-2) 

`is_allow_receive_erc20_tokens` delegates to `is_address_allowed`, which checks the `Address` whitelist: [4](#0-3) 

This check is never invoked in the `receive_base_tokens` path.

### Impact Explanation

In silo mode with the `Address` whitelist enabled, the protocol intends to restrict which EVM addresses may receive tokens. A non-whitelisted address that receives base ETH via `receive_base_tokens` cannot subsequently submit transactions (`is_allow_submit` enforces the same `Address` whitelist), so the minted ETH is frozen in that address. The tokens are locked in an address that cannot use them, constituting a **permanent or temporary freezing of funds** depending on whether the operator ever whitelists the address.

### Likelihood Explanation

The trigger is straightforward: any NEAR user can send ETH through the ETH connector bridge to Aurora and specify any EVM address as the recipient in the `msg` field. No special privilege is required. The condition is that silo mode is active with the `Address` whitelist enabled, which is the intended production configuration for silo deployments.

### Recommendation

Apply the same whitelist guard in `receive_base_tokens` that exists in `receive_erc20_tokens`. Before calling `set_balance`, check `silo::is_allow_receive_erc20_tokens` (or an equivalent `is_allow_receive_base_tokens` function). If the recipient is not whitelisted and a fallback address is configured, redirect the mint to the fallback address. If no fallback is configured and the recipient is not whitelisted, return an error so the tokens are returned to the sender.

### Proof of Concept

1. Operator deploys Aurora in silo mode, enables the `Address` whitelist, and does **not** add `victim_address` to the whitelist.
2. Attacker calls `ft_transfer_call` on the ETH connector contract, specifying Aurora as the receiver and encoding `victim_address` in the `msg` field.
3. The ETH connector calls `ft_on_transfer` on Aurora with `predecessor = connector_account_id`.
4. `ft_on_transfer` routes to `receive_base_tokens`.
5. `receive_base_tokens` calls `set_balance` for `victim_address` with no whitelist check — the non-whitelisted address now holds base ETH.
6. Any subsequent attempt by `victim_address` to call `submit` is rejected by `is_allow_submit` because the address is not whitelisted.
7. The minted ETH is frozen. [5](#0-4) [1](#0-0)

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

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/contract_methods/silo/mod.rs (L140-158)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}

fn is_account_allowed_deploy<I: IO + Copy>(io: &I, account_id: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Admin);
    !list.is_enabled() || list.is_exist(account_id)
}

fn is_address_allowed_deploy<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::EvmAdmin);
    !list.is_enabled() || list.is_exist(address)
}

fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
```
