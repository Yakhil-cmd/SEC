### Title
Silo Mode `Address` Whitelist Bypass for ERC-20 Token Receipt When No Fallback Address Is Configured — (File: `engine/src/engine.rs`)

---

### Summary

In Aurora Engine's Silo mode, the `Address` whitelist check that is supposed to gate which EVM addresses may receive bridged ERC-20 tokens is only enforced when a fallback address is also configured. When no fallback address is set, the guard is silently skipped and any EVM address — whitelisted or not — can receive ERC-20 tokens via `ft_on_transfer`. This is a direct analog to the Neptune Mutual "unenforced staking requirement" class: a gating requirement exists in code but is structurally bypassed under a reachable configuration.

---

### Finding Description

`receive_erc20_tokens` in `engine/src/engine.rs` contains the following guard:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

This is a Rust `if let … && …` chain. The entire branch — including the whitelist check — is only evaluated when `get_erc20_fallback_address` returns `Some`. When no fallback address is stored, the outer `if let` short-circuits to `false` and the inner `!silo::is_allow_receive_erc20_tokens(…)` call is **never reached**. Tokens are then minted to whatever EVM address the caller supplied, regardless of whitelist membership.

The whitelist check itself is:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
``` [2](#0-1) 

```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
``` [3](#0-2) 

When the `Address` whitelist is enabled and the recipient is absent from it, `is_allow_receive_erc20_tokens` returns `false`. The intent is to block the mint. But because the entire branch is gated on `fallback_address` being `Some`, a Silo operator who enables the whitelist without configuring a fallback address gets **no enforcement at all**.

The `ft_on_transfer` entry point that drives this path performs no `Account` or `Address` whitelist check of its own:

```rust
pub fn ft_on_transfer<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I, env: &E, handler: &mut H,
) -> Result<Option<SubmitResult>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        …
        engine.receive_erc20_tokens(…)
```

<cite repo="Jaredbentat/aurora-engine--009" path="engine/src/contract_methods/connector.rs" start="62" end

### Citations

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

**File:** engine/src/contract_methods/silo/mod.rs (L155-158)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
```
