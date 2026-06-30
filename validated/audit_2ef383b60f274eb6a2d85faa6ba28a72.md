### Title
`Address` Whitelist Not Enforced in `receive_erc20_tokens` When No ERC-20 Fallback Address Is Configured — (`engine/src/engine.rs`)

---

### Summary

In Silo mode, the `WhitelistKind::Address` whitelist is consistently enforced for EVM transaction submission but is silently skipped in the `receive_erc20_tokens` path whenever no ERC-20 fallback address is configured. A non-whitelisted EVM address can therefore receive bridged NEP-141 tokens via `ft_on_transfer`, but cannot subsequently submit any EVM transaction to move or exit those tokens, permanently locking them.

---

### Finding Description

The Silo subsystem exposes `is_allow_receive_erc20_tokens`, which delegates to the same `WhitelistKind::Address` list used by `is_allow_submit`: [1](#0-0) 

For EVM transaction submission the check is unconditional — `assert_access` always calls `is_allow_submit` regardless of any other configuration: [2](#0-1) 

In `receive_erc20_tokens`, however, the whitelist check is wrapped inside a combined `if let … && …` guard that only fires when a fallback address is present: [3](#0-2) 

When `get_erc20_fallback_address` returns `None` the entire block is skipped. `is_allow_receive_erc20_tokens` is never called, and the original `recipient` — even if it is not in the `Address` whitelist — is used directly for minting.

The `ft_on_transfer` entrypoint that drives this path performs only a `require_running` check and no whitelist check of its own: [4](#0-3) 

---

### Impact Explanation

**High — Temporary (potentially permanent) freezing of funds.**

A Silo operator who enables the `Address` whitelist to restrict token recipients but does not configure a fallback address (because the intended behaviour is to reject non-whitelisted transfers and return tokens to the sender) will find the restriction is silently unenforced on the bridge inbound path.

Tokens minted to a non-whitelisted address are immediately inaccessible:

- `assert_access` blocks every EVM transaction from that address (`NotAllowed`), so the address cannot call `transfer`, `approve`, or the `ExitToNear` precompile.
- The `withdraw` entrypoint covers only native ETH, not ERC-20 tokens.
- No other exit path exists for ERC-20 tokens without EVM execution rights.

The tokens remain frozen until the operator explicitly whitelists the address (temporary) or never does (permanent).

---

### Likelihood Explanation

**Medium.** Silo mode is an explicitly supported production configuration. The combination of an enabled `Address` whitelist with no fallback address is a natural operator choice when the intent is to reject rather than redirect non-whitelisted transfers. Any NEAR account can call `ft_on_transfer` on the Aurora engine by sending NEP-141 tokens through the standard NEP-141 receiver interface, specifying any EVM address as the recipient — no special privilege is required.

---

### Recommendation

Decouple the whitelist check from the fallback-address guard. The check should be performed unconditionally; the fallback address should only determine what happens when the check fails:

```rust
pub fn receive_erc20_tokens<P: PromiseHandler>(
    &mut self,
    token: &AccountId,
    args: &FtOnTransferArgs,
    current_account_id: &AccountId,
    handler: &mut P,
) -> Result<Option<SubmitResult>, ContractError> {
    // ... parse recipient ...

-   if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
-       && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
-   {
-       recipient = fallback_address;
-   }

+   if !silo::is_allow_receive_erc20_tokens(&self.io, &recipient) {
+       match silo::get_erc20_fallback_address(&self.io) {
+           Some(fallback) => recipient = fallback,
+           None => return Err(errors::ERR_NOT_ALLOWED.into()),
+       }
+   }

    // ... rest of minting logic ...
}
```

This mirrors the unconditional enforcement already present in `assert_access` for the submit path.

---

### Proof of Concept

1. Deploy Aurora in Silo mode.
2. Enable the `Address` whitelist (`set_whitelists_statuses` with `WhitelistKind::Address, active: true`). Do **not** call `set_erc20_fallback_address`.
3. Ensure address `0xDEAD…` is **not** in the `Address` whitelist.
4. From any NEAR account, call `ft_on_transfer` on the Aurora engine with `msg = "0xDEAD…"` and a non-zero `amount` for a registered NEP-141 token.
5. Observe: `receive_erc20_tokens` succeeds; `0xDEAD…` now holds ERC-20 tokens on Aurora.
6. Attempt any EVM transaction from `0xDEAD…` (e.g., `transfer` or `ExitToNear`): every call is rejected with `NotAllowed`.
7. The tokens are frozen with no exit path. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** engine/src/contract_methods/silo/whitelist.rs (L40-47)
```rust
    /// Check if the whitelist is enabled.
    pub fn is_enabled(&self) -> bool {
        // White list is disabled by default. So return `false` if the key doesn't exist.
        let key = self.key(STATUS);
        self.io
            .read_storage(&key)
            .is_some_and(|value| value.to_vec() == [1])
    }
```
