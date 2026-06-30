### Title
Silo Whitelist Not Enforced During Base-Token Minting via `receive_base_tokens` — (`engine/src/engine.rs`)

### Summary

In Silo mode, the `WhitelistKind::Address` whitelist is intended to restrict which EVM addresses may receive tokens. The `receive_erc20_tokens` path has a (conditional) whitelist check via `is_allow_receive_erc20_tokens`. However, the `receive_base_tokens` function — which mints ETH (base tokens) to an EVM address when the ETH connector calls `ft_on_transfer` — performs **no whitelist check at all**. Any EVM address, including non-whitelisted ones, can receive minted ETH in a Silo deployment with the `WhitelistKind::Address` whitelist enabled.

### Finding Description

The Silo mode whitelist system defines `is_allow_receive_erc20_tokens` specifically to gate ERC-20 token receipt on the `WhitelistKind::Address` list: [1](#0-0) 

This check is applied (conditionally) inside `receive_erc20_tokens`: [2](#0-1) 

However, `receive_base_tokens`, which mints ETH directly to the recipient's balance, contains **no whitelist check**: [3](#0-2) 

The `ft_on_transfer` entrypoint dispatches to `receive_base_tokens` when the predecessor is the registered ETH connector account: [4](#0-3) 

The public `ft_on_transfer` NEAR entrypoint is callable by any NEAR account that is the registered ETH connector: [5](#0-4) 

The attacker-controlled input is the `msg` field of `FtOnTransferArgs`, which encodes the recipient EVM address. The user calls `ft_transfer_call` on the ETH connector contract, specifying any EVM address as the recipient. The ETH connector then calls `ft_on_transfer` on Aurora, and `receive_base_tokens` mints ETH to that address without consulting the `WhitelistKind::Address` whitelist.

A secondary, related issue exists in `receive_erc20_tokens`: the whitelist check is gated on the existence of a fallback address. If no `erc20_fallback_address` is configured, the entire check is skipped and ERC-20 tokens are minted to any requested address: [2](#0-1) 

### Impact Explanation

In a Silo deployment with `WhitelistKind::Address` enabled, a user can bridge ETH to any EVM address — including addresses the Silo operator explicitly excluded from the whitelist. The minted ETH is credited to the non-whitelisted address. Because `is_allow_submit` blocks that address from submitting EVM transactions, the ETH is inaccessible: the user's bridged ETH is locked in an address that cannot spend it, and the corresponding NEP-141 ETH balance is held by Aurora with no retrieval path unless the operator modifies the whitelist. This constitutes a **High: Temporary freezing of funds** (resolvable only by operator whitelist modification).

### Likelihood Explanation

The precondition is a Silo deployment with the `WhitelistKind::Address` whitelist enabled. This is the documented purpose of Silo mode. Any user who can call `ft_transfer_call` on the ETH connector (an unprivileged NEAR account action) can trigger this path. No special privileges are required beyond holding ETH on the NEAR side of the bridge.

### Recommendation

Add a whitelist check inside `receive_base_tokens` analogous to the one in `receive_erc20_tokens`. If the recipient is not whitelisted and a fallback address is configured, redirect to the fallback; if no fallback is configured and the whitelist is enabled, reject the mint (return the tokens to the sender). Additionally, decouple the whitelist enforcement in `receive_erc20_tokens` from the fallback address: if the whitelist is enabled and the recipient is not allowed, the mint should be rejected regardless of whether a fallback address is set.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode; enable `WhitelistKind::Address` whitelist; do **not** add address `0xDEAD...` to the whitelist.
2. From any NEAR account, call `ft_transfer_call` on the registered ETH connector contract with `receiver_id = aurora`, `amount = 1000`, `msg = "DEAD..."` (the non-whitelisted EVM address).
3. The ETH connector calls `ft_on_transfer` on Aurora; `receive_base_tokens` is invoked.
4. Observe that `set_balance` is called for `0xDEAD...` with no whitelist check — the balance is credited.
5. Attempt to submit any EVM transaction from `0xDEAD...`; it is rejected by `is_allow_submit` because the address is not whitelisted.
6. The 1000 units of ETH are permanently inaccessible: locked in `0xDEAD...` with no retrieval path. [3](#0-2) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/silo/mod.rs (L135-143)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
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

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
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

**File:** engine/src/lib.rs (L602-610)
```rust
    #[unsafe(no_mangle)]
    pub extern "C" fn ft_on_transfer() {
        let io = Runtime;
        let env = Runtime;
        let mut handler = Runtime;
        contract_methods::connector::ft_on_transfer(io, &env, &mut handler)
            .map_err(ContractError::msg)
            .sdk_unwrap();
    }
```
