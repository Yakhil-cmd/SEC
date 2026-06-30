### Title
Missing Silo Address-Whitelist Enforcement in `receive_base_tokens` Allows Minting ETH to Non-Whitelisted Addresses, Freezing Funds - (File: engine/src/engine.rs)

---

### Summary

In silo mode with the `Address` whitelist enabled, `receive_erc20_tokens` enforces the whitelist and redirects tokens to a configured fallback address when the recipient is not whitelisted. The parallel function `receive_base_tokens`, which mints the native ETH-equivalent balance, performs no such check. Any user bridging ETH to Aurora can specify an arbitrary EVM address as the recipient; if that address is not whitelisted, the minted ETH is permanently inaccessible to the holder because all EVM transaction submission from non-whitelisted addresses is blocked.

---

### Finding Description

`ft_on_transfer` in `engine/src/contract_methods/connector.rs` dispatches to one of two minting paths depending on whether the predecessor is the registered connector account:

```
if predecessor_account_id == get_connector_account_id(&io)? {
    engine.receive_base_tokens(&args)          // ← base ETH path
} else {
    engine.receive_erc20_tokens(...)           // ← ERC-20 path
}
```

`receive_erc20_tokens` (engine/src/engine.rs lines 818-822) applies the silo whitelist before crediting the recipient:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;   // redirect to safe fallback
}
```

`receive_base_tokens` (engine/src/engine.rs lines 773-790) contains no equivalent guard:

```rust
pub fn receive_base_tokens(&mut self, args: &FtOnTransferArgs)
    -> Result<Option<SubmitResult>, ContractError>
{
    let message_data = FtTransferMessageData::try_from(args.msg.as_str())?;
    let amount = Wei::new_u128(args.amount.as_u128());
    let receipient = message_data.recipient;          // fully user-controlled
    // ← no whitelist check, no fallback redirect
    set_balance(&mut self.io, &receipient, &new_balance);
    Ok(None)
}
```

`is_allow_receive_erc20_tokens` and `is_allow_submit` both delegate to the same `is_address_allowed` helper (engine/src/contract_methods/silo/mod.rs lines 136-143), which gates on the `WhitelistKind::Address` list. Because `receive_base_tokens` skips this check entirely, ETH is credited to whatever address the caller encodes in `args.msg`, regardless of whitelist status.

Once credited, the ETH is unreachable: `assert_access` (engine/src/engine.rs lines 1756-1774) calls `silo::is_allow_submit` before executing any EVM transaction, so the non-whitelisted address cannot call the exit precompile or transfer the balance to a whitelisted address. There is no out-of-band recovery path for base-token balances.

---

### Impact Explanation

**High — Temporary freezing of funds.**

ETH bridged to a non-whitelisted address in silo mode is frozen in the EVM state. The holder cannot submit any EVM transaction (blocked by `is_allow_submit`), cannot call the exit precompile, and cannot transfer the balance. Recovery requires the silo operator to whitelist the address, which is an out-of-band administrative action with no protocol guarantee. Until that action is taken the funds are inaccessible.

---

### Likelihood Explanation

**Medium.**

The condition requires silo mode to be active with the `Address` whitelist enabled — a deliberate operator configuration. Within that configuration, any user who bridges ETH and supplies a non-whitelisted address (their own address before it is whitelisted, a mistyped address, or an address later removed from the whitelist) triggers the freeze. No special privilege is required; the entry point is the standard NEAR `ft_transfer_call` → `ft_on_transfer` bridge flow available to every token holder.

---

### Recommendation

Apply the same silo guard to `receive_base_tokens` that already exists in `receive_erc20_tokens`. When the `Address` whitelist is enabled and the parsed recipient is not whitelisted, either reject the transfer (returning the full amount to the sender) or redirect the balance to the configured fallback address, mirroring the ERC-20 behavior.

---

### Proof of Concept

1. Deploy Aurora Engine in silo mode: call `set_silo_params` with a non-zero `fixed_gas` and a `erc20_fallback_address`, then call `set_whitelist_status` to enable `WhitelistKind::Address`.
2. Do **not** add address `0xDEAD…` to the whitelist.
3. From any NEAR account, call `ft_transfer_call` on the NEAR ETH connector with `receiver_id = aurora`, `amount = 1_000_000`, `msg = "dead…"` (hex of `0xDEAD…`).
4. The NEAR ETH connector calls `ft_on_transfer` on Aurora; `predecessor == connector_account_id`, so `receive_base_tokens` is invoked.
5. `receive_base_tokens` credits `0xDEAD…` with 1 000 000 wei and returns `Ok(None)` — no whitelist check fires.
6. Attempt to submit any EVM transaction from `0xDEAD…` (e.g., a self-transfer or exit precompile call): `assert_access` returns `EngineErrorKind::NotAllowed` because `is_allow_submit` fails for a non-whitelisted address.
7. The 1 000 000 wei is frozen. Contrast with step 3 repeated for an ERC-20 token: `receive_erc20_tokens` redirects the tokens to the fallback address, preserving recoverability.

**Relevant code locations:**

- `engine/src/contract_methods/connector.rs` lines 81–90 — dispatch to `receive_base_tokens` [1](#0-0) 
- `engine/src/engine.rs` lines 773–790 — `receive_base_tokens`, no whitelist check [2](#0-1) 
- `engine/src/engine.rs` lines 818–822 — `receive_erc20_tokens`, whitelist check present [3](#0-2) 
- `engine/src/contract_methods/silo/mod.rs` lines 136–143 — `is_allow_submit` and `is_allow_receive_erc20_tokens` both use `is_address_allowed` [4](#0-3) 
- `engine/src/engine.rs` lines 1756–1774 — `assert_access` blocks non-whitelisted addresses from submitting transactions [5](#0-4)

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

**File:** engine/src/engine.rs (L1756-1774)
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
