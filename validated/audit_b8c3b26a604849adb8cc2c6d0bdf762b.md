### Title
ERC-20 Token Receipt Whitelist Bypass When Fallback Address Is Not Set — (`engine/src/engine.rs`)

### Summary

In `receive_erc20_tokens`, the silo Address whitelist check is gated behind the existence of the ERC-20 fallback address. When the whitelist is enabled but no fallback address is configured, the check is silently skipped and any address — including non-whitelisted ones — can receive ERC-20 tokens. Because the same Address whitelist also blocks transaction submission, those tokens become inaccessible to the recipient, causing a temporary freeze of funds.

### Finding Description

`Engine::receive_erc20_tokens` in `engine/src/engine.rs` contains the following guard:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

This is a Rust `if let … && …` expression. The whitelist predicate `is_allow_receive_erc20_tokens` is only evaluated when `get_erc20_fallback_address` returns `Some`. When the fallback address is absent (`None`), the entire branch is skipped and `recipient` is never changed — regardless of whether the Address whitelist is enabled and regardless of whether the recipient is in it.

`is_allow_receive_erc20_tokens` delegates to `is_address_allowed`, which reads the `WhitelistKind::Address` list:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}

fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
``` [2](#0-1) 

The same `WhitelistKind::Address` list is checked by `assert_access` before every `submit` / `deploy` call:

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
    ...
}
``` [3](#0-2) 

The two controls — "who may receive ERC-20 tokens" and "who may submit transactions" — share the same whitelist, but the receipt check is only enforced when the fallback address is present. The submission check is always enforced.

The `set_whitelist_status` and `set_erc20_fallback_address` entrypoints are independent:

```rust
pub extern "C" fn set_whitelist_status() { … }
pub extern "C" fn set_erc20_fallback_address() { … }
``` [4](#0-3) 

A silo operator can therefore enable the Address whitelist without ever setting a fallback address, believing the whitelist alone is sufficient to restrict ERC-20 token receipt.

### Impact Explanation

When the Address whitelist is enabled and no fallback address is set:

1. Any caller can invoke `ft_on_transfer` targeting a non-whitelisted EVM address.
2. `receive_erc20_tokens` mints the ERC-20 tokens to that address without consulting the whitelist.
3. The recipient address cannot submit any EVM transaction (`assert_access` rejects it as `NotAllowed`).
4. The minted tokens are inaccessible — temporarily frozen — until the silo operator explicitly whitelists the address.

**Impact: High — Temporary freezing of funds.**

### Likelihood Explanation

The `set_whitelist_status` and `set_erc20_fallback_address` / `set_silo_params` calls are separate administrative operations. A silo operator who wants to restrict ERC-20 token receipt to whitelisted addresses may enable the whitelist without realising that the enforcement is conditional on the fallback address being set. The comment in `SiloParamsArgs` ("the logic described above works only if the fallback address is set") is easy to miss. The likelihood is medium. [5](#0-4) 

### Recommendation

Decouple the whitelist check from the fallback address. Enforce the whitelist first; only then decide whether to redirect or reject:

```rust
if !silo::is_allow_receive_erc20_tokens(&self.io, &recipient) {
    match silo::get_erc20_fallback_address(&self.io) {
        Some(fallback) => recipient = fallback,
        None => return Err(/* appropriate error */),
    }
}
```

This ensures that a non-whitelisted recipient is always handled (redirected or rejected) whenever the whitelist is active, regardless of whether a fallback address has been configured.

### Proof of Concept

1. Deploy Aurora Engine in silo mode.
2. Enable the `Address` whitelist via `set_whitelist_status { kind: Address, active: true }` — do **not** call `set_silo_params` or `set_erc20_fallback_address`.
3. Deploy a NEP-141 token and register its ERC-20 mirror on Aurora.
4. Call `ft_transfer_call` on the NEP-141 contract, targeting the Aurora Engine, with `msg` set to the hex-encoded address of a non-whitelisted EVM account.
5. Aurora's `ft_on_transfer` → `receive_erc20_tokens` is invoked. `get_erc20_fallback_address` returns `None`; the `if let` branch is skipped entirely; the ERC-20 tokens are minted to the non-whitelisted address.
6. Attempt any EVM transaction from that address — `assert_access` returns `NotAllowed`.
7. The minted tokens are frozen in the non-whitelisted address. [6](#0-5)

### Citations

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

**File:** engine/src/lib.rs (L841-815)
```rust

```

**File:** engine-types/src/parameters/silo.rs (L19-23)
```rust
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
```
