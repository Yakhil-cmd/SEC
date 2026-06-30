### Title
Silo Address Whitelist Bypass via EVM ERC-20 Transfer Leads to Permanent Fund Freeze - (File: `engine/src/engine.rs`, `engine/src/contract_methods/silo/mod.rs`)

### Summary
In Silo mode with the `Address` whitelist enabled, the `receive_erc20_tokens` function redirects bridged ERC-20 tokens to the `erc20_fallback_address` when the intended recipient is not whitelisted. However, a whitelisted EVM address can receive those tokens and then transfer them to any non-whitelisted address via a standard EVM ERC-20 `transfer()` call, since the ERC-20 contracts deployed within Aurora EVM do not enforce the Silo whitelist. Because non-whitelisted addresses are blocked from submitting any transactions by `assert_access`, the tokens transferred to them are permanently frozen with no exit path.

### Finding Description
The Silo mode whitelist check for ERC-20 token receipt is enforced only at the NEAR-to-Aurora bridge entry point inside `receive_erc20_tokens`:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

`is_allow_receive_erc20_tokens` delegates to `is_address_allowed`, which checks the `WhitelistKind::Address` list:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
``` [2](#0-1) 

This check only fires at bridge ingress. Once tokens are minted to a whitelisted address inside the EVM, the ERC-20 contracts deployed by `deploy_erc20_token` are standard contracts with no awareness of the Silo whitelist. A whitelisted address can call ERC-20 `transfer(nonWhitelistedAddress, amount)` freely, moving tokens to any address.

When the non-whitelisted address then attempts to exit those tokens (e.g., by calling the ERC-20 contract's withdraw function to trigger `ExitToNear`), it must submit an EVM transaction. Every submitted transaction passes through `submit_with_alt_modexp`, which calls:

```rust
assert_access(&io, env, &transaction)?;
``` [3](#0-2) 

`assert_access` calls `is_allow_submit`, which checks both the `Account` and `Address` whitelists for the transaction sender:

```rust
fn assert_access<I: IO + Copy, E: Env>(io: &I, env: &E, transaction: &NormalizedEthTransaction) -> Result<(), EngineError> {
    let allowed = if transaction.to.is_some() {
        silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
    } else {
        silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
    };
    if !allowed {
        return Err(EngineError { kind: EngineErrorKind::NotAllowed, gas_used: 0 });
    }
    Ok(())
}
``` [4](#0-3) 

```rust
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}
``` [5](#0-4) 

Since the non-whitelisted address cannot submit any transaction, it has no mechanism to approve a third party, call the ERC-20 contract, or invoke `ExitToNear`. The tokens are permanently locked.

### Impact Explanation
**Critical — Permanent freezing of funds.**

ERC-20 tokens transferred via EVM to a non-whitelisted address in a Silo deployment are irrecoverably frozen. The non-whitelisted address cannot submit any transaction (blocked by `assert_access`), cannot approve a spender, and cannot call `ExitToNear`. There is no administrative recovery path in the engine for this state. The tokens remain in the ERC-20 contract's storage mapped to the non-whitelisted address with no exit.

### Likelihood Explanation
**Medium.** The Silo Address whitelist is an active production feature. Any whitelisted address — including a legitimate user, a DeFi contract, or an attacker who controls a whitelisted address — can trigger this by calling ERC-20 `transfer()` targeting any address not in the whitelist. This can happen accidentally (e.g., a user sends tokens to a fresh address they haven't whitelisted yet) or deliberately (an attacker who is whitelisted drains tokens into a non-whitelisted address to grief other users or lock their own tokens to avoid obligations). No special privileges beyond holding a whitelisted address are required.

### Recommendation
Enforce the Silo `Address` whitelist inside the ERC-20 contracts deployed within Aurora, or add a hook in the EVM `apply` state-change path that rejects balance changes to non-whitelisted addresses. Alternatively, mirror the approach suggested in the reference report: restrict ERC-20 `transfer()` so that a whitelisted sender can only transfer up to the amount that the recipient is permitted to hold (i.e., zero if the recipient is not whitelisted). A simpler mitigation is to add a whitelist check inside `receive_erc20_tokens` that also validates the sender's remaining transferable quota, analogous to the `userWithdrawLimitPerPeriod` fix.

### Proof of Concept

1. Silo mode is active; `Address` whitelist is enabled; `erc20_fallback_address` is set to `F`.
2. Whitelisted address `A` calls `ft_transfer_call` on a NEP-141 contract targeting Aurora with recipient `A`. `receive_erc20_tokens` checks `is_allow_receive_erc20_tokens(A)` → passes → ERC-20 tokens minted to `A`. [1](#0-0) 
3. Address `A` submits an EVM transaction calling `erc20.transfer(B, amount)` where `B` is not whitelisted. `assert_access` passes (sender is `A`, which is whitelisted). The ERC-20 contract transfers tokens to `B` with no whitelist check. [3](#0-2) 
4. Address `B` attempts to submit any transaction (e.g., to call `erc20.withdrawToNear`). `assert_access` → `is_allow_submit(B)` → `is_address_allowed(B)` → `B` not in `WhitelistKind::Address` → `EngineErrorKind::NotAllowed`. [6](#0-5) 
5. Tokens held by `B` are permanently frozen. The fallback address `F` does not receive them (the fallback only applies at bridge ingress, not to EVM-internal transfers).

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```

**File:** engine/src/engine.rs (L1052-1052)
```rust
    assert_access(&io, env, &transaction)?;
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

**File:** engine/src/contract_methods/silo/mod.rs (L136-138)
```rust
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

**File:** engine/src/contract_methods/silo/mod.rs (L155-158)
```rust
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
```
