### Title
Silo Address Whitelist Bypass via `receive_base_tokens` — (`engine/src/engine.rs`)

---

### Summary

In Silo mode, `receive_erc20_tokens` enforces the address whitelist and redirects tokens to a configured fallback address when the recipient is not whitelisted. The analogous ETH (base-token) deposit path, `receive_base_tokens`, performs no such check. Any unprivileged user can therefore deposit ETH directly to a non-whitelisted EVM address by calling `ft_transfer_call` on the ETH-connector NEP-141 contract, bypassing the Silo access-control restriction entirely.

---

### Finding Description

`ft_on_transfer` in `engine/src/contract_methods/connector.rs` dispatches to one of two functions depending on the predecessor account:

```
if predecessor_account_id == get_connector_account_id(&io) {
    engine.receive_base_tokens(&args)   // ETH deposit — NO whitelist check
} else {
    engine.receive_erc20_tokens(...)    // ERC-20 deposit — whitelist enforced
}
```

`receive_erc20_tokens` (lines 796–844 of `engine/src/engine.rs`) contains the guard:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
```

`receive_base_tokens` (lines 773–790 of the same file) has no equivalent guard. It unconditionally credits the caller-supplied recipient address:

```rust
set_balance(&mut self.io, &receipient, &new_balance);
```

`is_allow_receive_erc20_tokens` delegates to `is_address_allowed`, which checks the `WhitelistKind::Address` list. That same list is what the Silo operator enables to restrict which EVM addresses may interact with the Silo. The check is present for ERC-20 deposits and absent for ETH deposits.

---

### Impact Explanation

When the Silo address whitelist is active:

1. A non-whitelisted EVM address receives ETH on Aurora despite the whitelist, bypassing the operator's access-control intent.
2. The configured fallback address does **not** receive the ETH it was meant to capture, so the operator's token-routing policy is silently violated.
3. ETH credited to a non-whitelisted address is permanently inaccessible: `is_allow_submit` blocks that address from submitting EVM transactions, so it cannot call the `exit_to_near` precompile or any other contract to recover the funds. The ETH is permanently frozen.

Impact: **Permanent freezing of funds** (ETH locked in a non-whitelisted address with no recovery path) and **whitelist bypass** (Silo access-control circumvented).

---

### Likelihood Explanation

- Silo mode with an active address whitelist and a configured `erc20_fallback_address` is a documented, production-supported configuration.
- The attack requires only that the attacker hold ETH as NEP-141 tokens on NEAR and call `ft_transfer_call` on the ETH-connector contract — a standard, permissionless NEAR action available to any token holder.
- No privileged access, leaked keys, or social engineering is required.
- The discrepancy between the two deposit paths is subtle and easy to trigger accidentally (e.g., a user sending ETH to an address they believe is whitelisted but is not).

---

### Recommendation

Apply the same fallback-redirect guard in `receive_base_tokens` that already exists in `receive_erc20_tokens`:

```rust
pub fn receive_base_tokens(
    &mut self,
    args: &FtOnTransferArgs,
) -> Result<Option<SubmitResult>, ContractError> {
    let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
    let amount = Wei::new_u128(args.amount.as_u128());
    let mut receipient = message_data.recipient;

+   if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
+       && !silo::is_allow_receive_erc20_tokens(&self.io, &receipient)
+   {
+       receipient = fallback_address;
+   }

    let balance = get_balance(&self.io, &receipient);
    let new_balance = balance
        .checked_add(amount)
        .ok_or(errors::ERR_BALANCE_OVERFLOW)?;
    set_balance(&mut self.io, &receipient, &new_balance);
    sdk::log!("Mint {amount} base tokens for: {}", receipient.encode());
    Ok(None)
}
```

Alternatively, introduce a dedicated `is_allow_receive_base_tokens` helper (mirroring `is_allow_receive_erc20_tokens`) and apply it symmetrically.

---

### Proof of Concept

**Setup**: Silo mode enabled, `WhitelistKind::Address` whitelist active, `erc20_fallback_address` set to `fallback_addr`, non-whitelisted address `victim_addr`.

**Steps**:

1. Attacker holds ETH as NEP-141 tokens on NEAR (obtained via the standard bridge deposit flow).
2. Attacker calls `ft_transfer_call` on the ETH-connector NEP-141 contract:
   - `receiver_id`: Aurora engine account
   - `amount`: any ETH amount
   - `msg`: hex-encoded `victim_addr`
3. The ETH-connector calls `ft_on_transfer` on Aurora. Because `predecessor_account_id == get_connector_account_id`, the engine dispatches to `receive_base_tokens`.
4. `receive_base_tokens` credits `victim_addr` with the ETH — no whitelist check is performed.

**Observed**: `victim_addr` holds ETH on Aurora despite not being whitelisted. `fallback_addr` receives nothing. `victim_addr` cannot submit EVM transactions (blocked by `is_allow_submit`) and cannot call `exit_to_near`, so the ETH is permanently frozen.

**Expected**: The whitelist check should redirect the ETH to `fallback_addr`, consistent with the behavior of `receive_erc20_tokens`.

---

**Root-cause references**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
