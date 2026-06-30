### Title
`receive_base_tokens` Lacks Silo Whitelist Check, Permanently Freezing ETH Deposited to Non-Whitelisted Addresses — (`engine/src/engine.rs`)

### Summary

In Silo mode, `receive_erc20_tokens` redirects bridged ERC-20 tokens to a configured fallback address when the intended recipient is not in the `Address` whitelist. The analogous ETH deposit path, `receive_base_tokens`, performs no such check and mints ETH directly to any recipient address. Because non-whitelisted addresses cannot submit EVM transactions in Silo mode, ETH minted to a non-whitelisted address is permanently frozen with no recovery path.

### Finding Description

Aurora Engine's Silo mode enforces an `Address` whitelist that gates both transaction submission and ERC-20 token receipt. When a NEP-141 token is transferred to Aurora via `ft_on_transfer`, the engine branches on whether the caller is the ETH connector: [1](#0-0) 

For ERC-20 tokens, `receive_erc20_tokens` applies the whitelist and redirects to the fallback address when the recipient is not whitelisted: [2](#0-1) 

For ETH (base tokens), `receive_base_tokens` performs no whitelist check and no fallback redirect — it unconditionally mints ETH to the address parsed from `args.msg`: [3](#0-2) 

The `Address` whitelist is enforced at transaction submission time via `assert_access`: [4](#0-3) 

`is_allow_receive_erc20_tokens` and `is_allow_submit` both delegate to the same `Address` whitelist: [5](#0-4) 

A non-whitelisted address that receives ETH via `receive_base_tokens` cannot submit any transaction (including `ExitToNear` to bridge back to NEAR), because every `submit` call goes through `assert_access`. There is no admin-free recovery path.

### Impact Explanation

**Critical — Permanent freezing of funds.**

ETH bridged to a non-whitelisted EVM address in a Silo deployment is irrecoverable without the Silo owner manually whitelisting the address and then coordinating with the user to withdraw. The ERC-20 path avoids this by redirecting to the fallback address; the ETH path has no equivalent protection, so the funds are silently frozen at the recipient address.

### Likelihood Explanation

**Medium.** This requires a Silo deployment with the `Address` whitelist enabled and an `erc20_fallback_address` configured (i.e., the operator has opted into the full Silo access-control model). Within such a deployment, any user who bridges ETH to an address that is not yet whitelisted — including their own address before it has been added — triggers the freeze. This is a realistic operational scenario (e.g., funding a new address before whitelisting it, or a user who is unaware their address is not whitelisted).

### Recommendation

Apply the same whitelist-and-fallback logic to `receive_base_tokens` that `receive_erc20_tokens` already applies. Specifically, after parsing the recipient from `args.msg`, check `silo::is_allow_receive_erc20_tokens` and, if the recipient is not whitelisted and a fallback address is configured, redirect the ETH mint to the fallback address instead of the intended recipient. This mirrors the existing ERC-20 protection:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &receipient)
{
    receipient = fallback_address;
}
```

### Proof of Concept

1. Deploy Aurora Engine in Silo mode: enable the `Address` whitelist and set an `erc20_fallback_address`.
2. Do **not** add Alice's EVM address to the `Address` whitelist.
3. Alice bridges ETH from NEAR to Aurora, specifying her EVM address in the `msg` field of the NEP-141 transfer to the ETH connector.
4. The ETH connector calls `ft_on_transfer` on Aurora; `receive_base_tokens` mints ETH to Alice's address with no whitelist check.
5. Alice attempts to submit any EVM transaction (e.g., `ExitToNear` to recover her ETH). `assert_access` rejects the transaction with `NotAllowed` because her address is not in the `Address` whitelist.
6. Alice's ETH is permanently frozen. The ERC-20 fallback address receives nothing (the fallback only applies to ERC-20 tokens). No recovery is possible without owner intervention.

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

**File:** engine/src/engine.rs (L1756-1775)
```rust
fn assert_access<I: IO + Copy, E: Env>(
    io: &I,
    env: &E,
    transaction: &NormalizedEthTransaction,
) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };

    if !allowed {
        return Err(EngineError {
            kind: EngineErrorKind::NotAllowed,
            gas_used: 0,
        });
    }

    Ok(())
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L136-143)
```rust
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```
