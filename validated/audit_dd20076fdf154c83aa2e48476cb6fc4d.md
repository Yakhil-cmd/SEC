### Title
Shared `WhitelistKind::Address` Across `is_allow_submit` and `is_allow_receive_erc20_tokens` Bypasses ERC-20 Fallback Mechanism in Silo Mode - (File: `engine/src/contract_methods/silo/mod.rs`)

### Summary

In Aurora Engine's Silo mode, the single `WhitelistKind::Address` list is used without scoping to gate two distinct operations: EVM transaction submission (`is_allow_submit`) and ERC-20 token reception (`is_allow_receive_erc20_tokens`). Any Ethereum address whitelisted for transaction submission is automatically and unconditionally permitted to receive bridged ERC-20 tokens directly, bypassing the operator-configured fallback address. This is the direct analog of M-07: a whitelist entry is not scoped to the specific operation it was intended for, so whitelisting an identifier for one purpose inadvertently authorizes it for a different, unintended purpose.

### Finding Description

`WhitelistKind::Address` is documented as controlling which EVM addresses "can submit transactions." [1](#0-0) 

However, the same list is consumed by two separate access-control functions: [2](#0-1) 

Both `is_allow_submit` and `is_allow_receive_erc20_tokens` delegate to the same private helper: [3](#0-2) 

The ERC-20 fallback mechanism in `Engine::receive_erc20_tokens` redirects bridged tokens to the operator-configured `erc20_fallback_address` when the intended recipient is **not** in the `Address` whitelist: [4](#0-3) 

Because `is_allow_receive_erc20_tokens` checks the same `WhitelistKind::Address` list that is populated for transaction-submission purposes, every address the Silo operator adds to allow transaction submission is simultaneously and silently granted the right to receive ERC-20 tokens directly — bypassing the fallback redirect entirely.

The `SiloParamsArgs` struct documents the fallback address as the destination for recipients "not in the silo white list": [5](#0-4) 

There is no separate whitelist kind for ERC-20 reception, so the operator has no mechanism to express "this address may submit transactions but must not receive tokens directly."

### Impact Explanation

A Silo operator who:
1. Enables the `Address` whitelist to enforce the ERC-20 fallback policy,
2. Configures a `fallback_address` (e.g., a compliance treasury) to collect bridged tokens from non-permitted recipients, and
3. Populates the `Address` whitelist with user addresses to allow transaction submission,

will find that every whitelisted user address bypasses the fallback. Bridged ERC-20 tokens sent to those addresses are minted directly to the users rather than redirected to the fallback/treasury. The fallback address never receives the tokens it was configured to collect. This constitutes diversion of tokens away from the operator-controlled treasury — **theft of yield/tokens that should have accrued to the fallback address**.

The impact maps to: **High — Theft of unclaimed yield** (tokens that should accumulate at the fallback/treasury address are instead minted to individual user addresses).

### Likelihood Explanation

The Silo mode with a fallback address is an explicitly supported and documented production feature. Any compliance-focused or enterprise Silo deployment that:
- Enables the `Address` whitelist (required to activate the fallback redirect), and
- Adds user addresses for transaction submission (the primary purpose of the `Address` whitelist),

is affected by default. No attacker action is required beyond being the recipient of a normal NEP-141 `ft_on_transfer` call. The bypass is automatic and silent.

### Recommendation

Introduce a separate `WhitelistKind` variant (e.g., `WhitelistKind::ReceiveErc20`) and a dedicated storage list for ERC-20 reception authorization. `is_allow_receive_erc20_tokens` should check only that new list, while `is_allow_submit` continues to check `WhitelistKind::Address`. This mirrors the M-07 recommendation: scope each whitelist entry to the specific operation it is intended to authorize, using a `mapping(operation => mapping(identifier => bool))` structure rather than a single shared list.

### Proof of Concept

```
Setup:
  - Silo operator enables WhitelistKind::Address
  - Silo operator sets fallback_address = treasury (operator-controlled)
  - Silo operator calls add_entry_to_whitelist(WhitelistAddressArgs { kind: Address, address: alice })
    (intent: allow Alice to submit transactions)

Attack (no special attacker action needed):
  - Any NEP-141 contract calls ft_on_transfer on the Aurora engine with msg = alice's address

Execution path:
  engine::receive_erc20_tokens()
    -> silo::is_allow_receive_erc20_tokens(&io, &alice)
       -> is_address_allowed(&io, &alice)
          -> Whitelist::init(io, WhitelistKind::Address).is_exist(&alice)
          -> returns TRUE  (alice was added for submit, not for receive)
    -> fallback branch is NOT taken
    -> tokens are minted to alice directly

Expected: tokens redirected to treasury (fallback_address)
Actual:   tokens minted to alice; treasury receives nothing
```

The root cause is that `add_entry_to_whitelist` with `WhitelistKind::Address` — called by the operator to authorize transaction submission — simultaneously and unconditionally authorizes ERC-20 token reception, because both `is_allow_submit` and `is_allow_receive_erc20_tokens` read from the same unscoped `WhitelistKind::Address` storage list. [6](#0-5) [7](#0-6) [4](#0-3)

### Citations

**File:** engine-types/src/parameters/silo.rs (L19-23)
```rust
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
```

**File:** engine-types/src/parameters/silo.rs (L77-79)
```rust
    /// The whitelist of this type is for storing EVM addresses. Addresses included in this
    /// whitelist can submit transactions.
    Address = 0x3,
```

**File:** engine/src/contract_methods/silo/mod.rs (L75-79)
```rust
/// Add an entry to a whitelist depending on a kind of list types in provided arguments.
pub fn add_entry_to_whitelist<I: IO + Copy>(io: &I, args: &WhitelistArgs) {
    let (kind, entry) = get_kind_and_entry(args);
    Whitelist::init(io, kind).add(entry);
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L135-143)
```rust
/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

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

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }
```
