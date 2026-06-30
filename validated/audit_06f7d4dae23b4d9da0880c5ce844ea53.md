### Title
Silo `WhitelistKind::Address` Check for ERC-20 Token Reception Is Silently Bypassed When No Fallback Address Is Configured - (`engine/src/engine.rs`)

---

### Summary

In `Engine::receive_erc20_tokens`, the `WhitelistKind::Address` whitelist check is gated inside a combined `if let … && !…` condition that only executes when a Silo fallback address is configured. If no fallback address is set, the whitelist check is never reached, and any EVM address — including those not in the whitelist — can receive bridged ERC-20 tokens.

---

### Finding Description

`Engine::receive_erc20_tokens` in `engine/src/engine.rs` contains the following guard:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

The short-circuit semantics of Rust's `&&` mean that `is_allow_receive_erc20_tokens` is only evaluated when `get_erc20_fallback_address` returns `Some`. When no fallback address is stored, the entire whitelist check is skipped and execution continues with the original `recipient` unchanged.

`is_allow_receive_erc20_tokens` itself is correct — it checks the `WhitelistKind::Address` whitelist:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
``` [2](#0-1) 

The problem is that the call site makes the whitelist check conditional on the existence of a fallback address, rather than treating them as independent concerns. The whitelist should be enforced regardless of whether a fallback address is configured.

The entry path is `ft_on_transfer` in `engine/src/contract_methods/connector.rs`, which calls `engine.receive_erc20_tokens` whenever a non-ETH NEP-141 token is transferred to the Aurora Engine contract:

```rust
engine.receive_erc20_tokens(
    &predecessor_account_id,
    &args,
    &current_account_id,
    handler,
)
``` [3](#0-2) 

The `recipient` address is attacker-controlled — it is parsed directly from `args.msg`, which is the message field supplied by the caller of `ft_transfer_call` on the NEP-141 token contract.

---

### Impact Explanation

In a Silo deployment where the operator has enabled the `WhitelistKind::Address` whitelist (to restrict which EVM addresses may receive bridged ERC-20 tokens) but has not configured a fallback address, the whitelist is entirely inoperative for ERC-20 token reception. Any EVM address — including addresses the Silo operator explicitly excluded — can receive minted ERC-20 tokens. This permanently undermines the access control guarantee of the Silo whitelist for the ERC-20 bridge path, and results in ERC-20 tokens being minted to unauthorized addresses, constituting a misrouting of bridged funds.

---

### Likelihood Explanation

The condition is triggered whenever a Silo operator enables the `WhitelistKind::Address` whitelist without also setting a fallback address. These are independent administrative actions exposed through separate entrypoints (`set_whitelist_status` and `set_erc20_fallback_address` / `set_silo_params`). An operator who enables the whitelist expecting it to protect ERC-20 reception — but who has not yet configured a fallback — is fully exposed. Any external NEAR account can exploit this by calling `ft_transfer_call` on any registered NEP-141 token and specifying a non-whitelisted EVM address in the message field.

---

### Recommendation

Separate the whitelist enforcement from the fallback redirect. The whitelist check should gate the transfer independently of whether a fallback address exists:

```rust
if !silo::is_allow_receive_erc20_tokens(&self.io, &recipient) {
    if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io) {
        recipient = fallback_address;
    } else {
        return Err(/* ERR_NOT_ALLOWED or similar */);
    }
}
```

This ensures that when the `WhitelistKind::Address` whitelist is enabled, it is always enforced for ERC-20 token reception, regardless of fallback address configuration.

---

### Proof of Concept

1. Deploy Aurora Engine in Silo mode.
2. Enable the `WhitelistKind::Address` whitelist via `set_whitelist_status`.
3. Do **not** call `set_silo_params` or `set_erc20_fallback_address` (leave fallback unset).
4. Register a NEP-141 token and deploy its ERC-20 mirror.
5. From any NEAR account, call `ft_transfer_call` on the NEP-141 contract, targeting the Aurora Engine, with `msg` set to the hex-encoded address of a non-whitelisted EVM address.
6. Aurora Engine's `ft_on_transfer` is invoked; `receive_erc20_tokens` is called; `get_erc20_fallback_address` returns `None`; the `if let Some(…) && …` condition is false; the whitelist check is never executed; ERC-20 tokens are minted to the non-whitelisted address. [1](#0-0) [4](#0-3)

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
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

**File:** engine/src/contract_methods/connector.rs (L84-90)
```rust
            engine.receive_erc20_tokens(
                &predecessor_account_id,
                &args,
                &current_account_id,
                handler,
            )
        };
```
