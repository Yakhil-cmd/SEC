### Title
Silo Address Whitelist Not Enforced for Base ETH Receipts in `receive_base_tokens` - (File: `engine/src/engine.rs`)

### Summary
In Silo mode, the `Address` whitelist is enforced when minting ERC-20 mirror tokens to a recipient (`receive_erc20_tokens`), but is entirely absent when minting base ETH to a recipient (`receive_base_tokens`). This is a direct structural analog to the OpenQ finding: one category of asset (ERC-20) is subject to the whitelist gate, while the other (base ETH) is not, even though both are supposed to be restricted by the same Silo access-control policy.

### Finding Description

`ft_on_transfer` in `engine/src/contract_methods/connector.rs` dispatches to one of two paths depending on the caller: [1](#0-0) 

When the caller is the ETH connector account, `receive_base_tokens` is invoked. When the caller is any other NEP-141 token contract, `receive_erc20_tokens` is invoked.

`receive_erc20_tokens` applies the Silo whitelist gate before crediting the recipient: [2](#0-1) 

If the recipient address is not in the `Address` whitelist and a fallback address is configured, the tokens are silently redirected to the fallback address. This is the intended Silo behavior.

`receive_base_tokens` performs no such check: [3](#0-2) 

It reads the recipient directly from the message and unconditionally credits the ETH balance, with no call to `silo::is_allow_receive_erc20_tokens` or any equivalent. The `is_allow_receive_erc20_tokens` helper itself only checks the `Address` whitelist: [4](#0-3) 

The `Address` whitelist check pattern used in `receive_erc20_tokens` is: [5](#0-4) 

This check is completely absent from the base-ETH path.

### Impact Explanation

In a Silo deployment, the `Address` whitelist also gates transaction submission (`is_allow_submit`): [6](#0-5) 

An address that is not in the `Address` whitelist cannot submit EVM transactions. If ETH is minted to such an address via `receive_base_tokens`, the ETH is immediately frozen: the address holds a balance it cannot spend, and no EVM transaction from that address will be accepted by the Silo. The funds remain frozen until the Silo operator explicitly adds the address to the whitelist — an action the operator may not know is needed, since the deposit succeeded silently.

**Impact: High — Temporary freezing of funds.** (The operator can unfreeze by adding the address to the whitelist, but the freeze is unintended and the operator has no on-chain signal that it occurred.)

### Likelihood Explanation

The attack path is fully reachable by any unprivileged user who holds ETH-on-NEAR (the NEP-141 representation of bridged ETH). The user calls `ft_transfer_call` on the ETH connector contract, specifying any non-whitelisted EVM address as the recipient in the `msg` field. The ETH connector then calls `ft_on_transfer` on Aurora, which routes to `receive_base_tokens`, and the ETH is credited to the non-whitelisted address with no revert or redirection. No admin access, no private key compromise, and no governance action is required.

### Recommendation

Apply the same whitelist-and-fallback logic to `receive_base_tokens` that is already applied in `receive_erc20_tokens`. Specifically, after resolving the recipient from the message, check `silo::is_allow_receive_erc20_tokens` and, if a fallback address is configured and the recipient is not whitelisted, redirect the ETH credit to the fallback address instead of the requested recipient.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode with the `Address` whitelist enabled and a fallback ERC-20 address configured.
2. Ensure address `0xAAAA...` is **not** in the `Address` whitelist.
3. As any NEAR account holding bridged ETH (NEP-141 from the ETH connector), call:
   ```
   ft_transfer_call(receiver_id: "aurora", amount: "1000000", msg: "<relayer>:<fee><0xAAAA...>")
   ```
4. Observe that `ft_on_transfer` routes to `receive_base_tokens` (because `predecessor == connector_account_id`).
5. Observe that `receive_base_tokens` credits `1000000` wei to `0xAAAA...` with no whitelist check.
6. Attempt to submit any EVM transaction from `0xAAAA...` — it is rejected with `NotAllowed` because the address is not in the `Address` whitelist.
7. The ETH balance at `0xAAAA...` is frozen. An equivalent ERC-20 transfer to the same address would have been silently redirected to the fallback address instead. [3](#0-2) [2](#0-1) [1](#0-0)

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

**File:** engine/src/contract_methods/silo/mod.rs (L140-143)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L155-158)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
```
