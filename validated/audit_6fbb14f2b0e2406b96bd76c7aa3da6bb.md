### Title
`receive_base_tokens` Bypasses Silo `Address` Whitelist, Freezing ETH at Non-Whitelisted Addresses - (File: `engine/src/engine.rs`)

### Summary
In Silo mode, `receive_erc20_tokens` correctly checks the `Address` whitelist and redirects tokens to a fallback address when the recipient is not whitelisted. `receive_base_tokens` — the ETH bridging path — performs no such check. Any user bridging ETH to Aurora can specify a non-whitelisted EVM address as the recipient; the ETH is minted directly to that address and is then permanently unspendable because `submit` also enforces the `Address` whitelist.

### Finding Description

The `ft_on_transfer` entry point in `engine/src/contract_methods/connector.rs` dispatches to one of two paths depending on whether the caller is the eth-connector: [1](#0-0) 

When the predecessor is the eth-connector, `receive_base_tokens` is called. That function mints ETH directly to the address parsed from `args.msg` with **no whitelist check**: [2](#0-1) 

By contrast, `receive_erc20_tokens` explicitly checks `silo::is_allow_receive_erc20_tokens` and redirects to the configured fallback address when the recipient is not whitelisted: [3](#0-2) 

`is_allow_receive_erc20_tokens` delegates to `is_address_allowed`, which returns `false` for any address not in the `Address` whitelist when that whitelist is enabled: [4](#0-3) 

The same `Address` whitelist is enforced on `submit` via `is_allow_submit`, so ETH minted to a non-whitelisted address cannot be spent: [5](#0-4) 

### Impact Explanation

When Silo mode is active and the `Address` whitelist is enabled, a user who bridges ETH to Aurora and specifies a non-whitelisted EVM address as the recipient will have their ETH minted to that address. Because `submit` also enforces the `Address` whitelist, the ETH cannot be transferred or used. The funds are frozen until the contract owner either whitelists the address or disables the whitelist. This is a **High — temporary freezing of funds**.

### Likelihood Explanation

Likelihood is **Medium**. The precondition is that the Aurora deployment runs in Silo mode with the `Address` whitelist enabled — a configuration explicitly supported and tested by the codebase. Any user who bridges ETH from Ethereum to Aurora (a normal, permissionless operation) and specifies a non-whitelisted address triggers the freeze. No special privileges are required beyond the ability to call `ft_transfer_call` on the eth-connector.

### Recommendation

Apply the same whitelist check and fallback-redirect logic to `receive_base_tokens` that already exists in `receive_erc20_tokens`:

```rust
pub fn receive_base_tokens(
    &mut self,
    args: &FtOnTransferArgs,
) -> Result<Option<SubmitResult>, ContractError> {
    let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
    let amount = Wei::new_u128(args.amount.as_u128());
    let mut recipient = message_data.recipient;

    // Apply the same Silo Address-whitelist redirect as receive_erc20_tokens
    if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
        && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
    {
        recipient = fallback_address;
    }

    let balance = get_balance(&self.io, &recipient);
    let new_balance = balance
        .checked_add(amount)
        .ok_or(errors::ERR_BALANCE_OVERFLOW)?;
    set_balance(&mut self.io, &recipient, &new_balance);
    sdk::log!("Mint {amount} base tokens for: {}", recipient.encode());
    Ok(None)
}
```

### Proof of Concept

1. Deploy Aurora Engine in Silo mode with `Address` whitelist enabled and a fallback address configured.
2. Create an EVM address `victim` that is **not** in the `Address` whitelist.
3. Call `ft_transfer_call` on the eth-connector with `receiver_id = aurora`, `amount = X`, `msg = victim.encode()`.
4. The eth-connector calls `ft_on_transfer` on Aurora; `predecessor_account_id == connector_account_id` so `receive_base_tokens` is invoked.
5. `receive_base_tokens` mints `X` ETH to `victim` with no whitelist check.
6. Attempt to call `submit` from `victim` — it is rejected with `NotAllowed` because `victim` is not in the `Address` whitelist.
7. The ETH at `victim` is frozen: it cannot be transferred, spent, or recovered without owner intervention.

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

**File:** engine/src/contract_methods/silo/mod.rs (L135-138)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
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
