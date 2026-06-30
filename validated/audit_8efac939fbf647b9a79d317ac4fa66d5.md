### Title
Missing Whitelist/Fallback Check in `receive_base_tokens` Allows Permanent ETH Freeze in Silo Mode - (`engine/src/engine.rs`)

### Summary

In Aurora Engine's Silo mode, when the `Address` whitelist is enabled, bridged ERC-20 tokens are correctly redirected to a fallback address if the recipient is not whitelisted. However, the analogous path for bridging ETH (base tokens) via `receive_base_tokens` performs **no whitelist check and has no fallback mechanism**. ETH deposited to a non-whitelisted EVM address is permanently frozen because the `submit` entrypoint enforces the whitelist and blocks all EVM transactions from that address.

### Finding Description

The `ft_on_transfer` connector entrypoint dispatches to one of two paths depending on whether the predecessor is the ETH connector or a NEP-141 token contract: [1](#0-0) 

For NEP-141 ERC-20 tokens, `receive_erc20_tokens` applies a whitelist check and redirects to the fallback address when the recipient is not whitelisted: [2](#0-1) 

For ETH (base tokens), `receive_base_tokens` unconditionally credits the balance to whatever address is specified in the message, with **no whitelist check and no fallback redirection**: [3](#0-2) 

The `is_allow_receive_erc20_tokens` function and the fallback address mechanism exist precisely to handle the Silo mode case: [4](#0-3) 

But they are never called from `receive_base_tokens`. Once ETH is credited to a non-whitelisted EVM address, the `assert_access` guard in the `submit` path blocks all EVM transactions from that address: [5](#0-4) 

The `is_allow_submit` check requires the sender's EVM address to be in the `Address` whitelist: [6](#0-5) 

### Impact Explanation

ETH deposited to a non-whitelisted EVM address in Silo mode is permanently frozen. The user cannot submit any EVM transaction (including calling the `ExitToNear` precompile) because `submit` enforces the `Address` whitelist. The `call` NEAR entrypoint uses `predecessor_address` as the EVM origin, so it only helps users whose EVM address is deterministically derived from their NEAR account ID — users with independently generated EVM key pairs have no recovery path. The funds are irrecoverably locked without admin intervention to whitelist each affected address individually.

**Impact: Permanent freezing of funds (Critical).**

### Likelihood Explanation

Silo mode with the `Address` whitelist enabled is an explicitly supported and documented production configuration. Any user who bridges ETH to Aurora and specifies a non-whitelisted EVM address (e.g., a hardware wallet address or any address not derived from a NEAR account) triggers this freeze. The `ft_transfer_call` call on the ETH connector is a standard, publicly accessible NEAR method. No special privileges are required.

### Recommendation

Apply the same whitelist/fallback guard to `receive_base_tokens` that `receive_erc20_tokens` already uses. Before crediting the balance, check `silo::is_allow_receive_erc20_tokens` and redirect to the configured fallback address if the recipient is not whitelisted:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &receipient)
{
    receipient = fallback_address;
}
```

This mirrors the existing pattern in `receive_erc20_tokens` and ensures consistent behavior across both bridging paths.

### Proof of Concept

1. Deploy Aurora Engine in Silo mode: call `set_silo_params` with a `fixed_gas` and `erc20_fallback_address`, then call `set_whitelist_status` to enable the `Address` whitelist.
2. Do **not** add the victim's EVM address to the `Address` whitelist.
3. Call `ft_transfer_call` on the ETH connector contract with `receiver_id = aurora`, `amount = N`, and `msg = <victim_evm_address_hex>`.
4. Aurora's `ft_on_transfer` is called; `predecessor_account_id == connector_account_id`, so `receive_base_tokens` is invoked.
5. `receive_base_tokens` credits `N` ETH to the victim's EVM address with no whitelist check.
6. The victim attempts to call `submit` with a signed EVM transaction to call `ExitToNear` — `assert_access` returns `EngineErrorKind::NotAllowed` because the address is not whitelisted.
7. ETH is permanently frozen. The ERC-20 path would have redirected to the fallback address; the ETH path does not. [7](#0-6) [3](#0-2)

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

**File:** engine/src/engine.rs (L796-822)
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
