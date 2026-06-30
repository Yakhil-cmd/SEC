### Title
Missing Silo Whitelist Check in `receive_base_tokens` Allows Non-Whitelisted Addresses to Receive Base Tokens - (File: `engine/src/engine.rs`)

### Summary

In Silo mode, Aurora Engine enforces an address whitelist for ERC-20 token receipt via `receive_erc20_tokens`, redirecting tokens to a configured fallback address when the recipient is not whitelisted. However, the analogous base-token (ETH) receipt path in `receive_base_tokens` performs no whitelist check at all, allowing any address — including those explicitly excluded from the Silo whitelist — to receive bridged ETH directly. This is the direct analog of the reported "check at creation, not at execution" class: the whitelist guard is applied in one code path but entirely absent in the parallel path.

### Finding Description

In `engine/src/engine.rs`, the `receive_erc20_tokens` function checks the Silo address whitelist before crediting tokens:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

The `is_allow_receive_erc20_tokens` function itself delegates to the same `Address` whitelist used for transaction submission:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
``` [2](#0-1) 

The parallel function `receive_base_tokens`, called when the predecessor is the ETH connector account, contains no such check:

```rust
pub fn receive_base_tokens(
    &mut self,
    args: &FtOnTransferArgs,
) -> Result<Option<SubmitResult>, ContractError> {
    let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
    let amount = Wei::new_u128(args.amount.as_u128());
    let receipient = message_data.recipient;
    ...
    set_balance(&mut self.io, &receipient, &new_balance);
    Ok(None)
}
``` [3](#0-2) 

The dispatch between the two paths occurs in `ft_on_transfer`:

```rust
let result = if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)
} else {
    engine.receive_erc20_tokens(...)
};
``` [4](#0-3) 

The `ft_on_transfer` entrypoint is publicly callable by the ETH connector contract, which is an external NEAR account: [5](#0-4) 

### Impact Explanation

In a Silo deployment with the `Address` whitelist enabled, the operator's intent is that only whitelisted EVM addresses may receive tokens. For ERC-20 tokens this is enforced: tokens destined for a non-whitelisted address are redirected to the configured `erc20_fallback_address`. For base tokens (ETH), no such redirection occurs. ETH is credited directly to the non-whitelisted recipient address. Because that address is not whitelisted, it cannot submit EVM transactions to move the ETH. The ETH is therefore frozen in an address that has no ability to spend it, and the fallback account — which the Silo operator configured to receive such tokens — receives nothing. This constitutes a **temporary (potentially permanent) freezing of bridged ETH funds** and a **whitelist bypass** in the Silo access-control model.

### Likelihood Explanation

The `ft_on_transfer` function is called by the ETH connector as part of the standard bridge deposit flow. Any user who initiates a bridge deposit and specifies a non-whitelisted EVM address as the recipient (either accidentally or deliberately) will trigger this path. In a Silo environment where the operator has carefully curated the whitelist, this is a realistic scenario — e.g., a user who was removed from the whitelist after initiating a bridge deposit, or a user who simply specifies an address that was never whitelisted. No special privileges are required beyond the ability to initiate a bridge deposit.

### Recommendation

Apply the same whitelist-and-fallback logic to `receive_base_tokens` that is already applied in `receive_erc20_tokens`. Specifically, after resolving the recipient address, check `silo::is_allow_receive_erc20_tokens` (or an equivalent `is_allow_receive_base_tokens` helper) and, if the recipient is not whitelisted and a fallback address is configured, redirect the credit to the fallback address. This mirrors the existing guard at: [1](#0-0) 

### Proof of Concept

1. Deploy Aurora Engine in Silo mode: call `set_silo_params` with a non-zero `fixed_gas` and a valid `erc20_fallback_address`, then call `set_whitelist_status` to enable the `Address` whitelist.
2. Do **not** add address `0xAAAA...` to the `Address` whitelist.
3. Initiate a bridge deposit of ETH from Ethereum, specifying `0xAAAA...` as the EVM recipient. The ETH connector calls `ft_on_transfer` on Aurora with `predecessor == connector_account_id`.
4. `ft_on_transfer` routes to `receive_base_tokens`. No whitelist check is performed. `set_balance` credits the ETH to `0xAAAA...`.
5. Observe that `0xAAAA...` now holds ETH but cannot call `submit` (blocked by `is_allow_submit`), so the ETH is frozen.
6. Observe that the `erc20_fallback_address` received nothing, contrary to the Silo operator's intent.
7. Repeat with an ERC-20 token: the token is correctly redirected to the fallback address, confirming the asymmetry.

### Citations

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

**File:** engine/src/contract_methods/silo/mod.rs (L140-143)
```rust
/// Check if a user has the right to receive erc20 tokens.
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
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
