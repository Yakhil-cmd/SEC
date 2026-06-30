### Title
Silo `WhitelistKind::Address` Conflates Transaction-Submission and ERC-20 Token-Receipt Permissions — (`engine/src/contract_methods/silo/mod.rs`)

---

### Summary

In Silo mode, the single `WhitelistKind::Address` whitelist is used to gate two entirely distinct operations: submitting EVM transactions (`is_allow_submit`) and receiving ERC-20 tokens (`is_allow_receive_erc20_tokens`). Because both checks resolve to the same underlying list, an operator cannot grant one permission without granting the other, and cannot revoke one without revoking the other. This is the direct structural analog of the HubPool route-whitelist conflation.

---

### Finding Description

`engine/src/contract_methods/silo/mod.rs` defines two public access-control predicates:

```rust
// line 136
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

// line 141
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
```

Both delegate to the same private helper:

```rust
// line 155
fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
``` [1](#0-0) [2](#0-1) 

The `WhitelistKind::Address` enum variant is documented as controlling addresses that "can submit transactions," with no mention of token receipt: [3](#0-2) 

Yet `is_allow_receive_erc20_tokens` silently reuses the same list. The two permissions are inseparable at the storage level.

---

### Impact Explanation

**High — Temporary freezing of funds / whitelist bypass.**

Two concrete failure modes arise in any Silo deployment where the `Address` whitelist is enabled:

1. **Bypass of transaction-submission restriction.** An operator adds an EVM address to `WhitelistKind::Address` solely to allow it to receive ERC-20 tokens (e.g., a bridge escrow contract or a recipient contract that should never originate calls). Because `is_allow_submit` reads the same list, that address immediately gains the ability to submit arbitrary EVM transactions, bypassing the Silo's core access-control invariant.

2. **Forced freezing of in-flight tokens.** An operator removes an address from `WhitelistKind::Address` to revoke its transaction-submission rights (e.g., after detecting abuse). Because `is_allow_receive_erc20_tokens` reads the same list, any ERC-20 tokens subsequently sent to that address are redirected to the `erc20_fallback_address` instead of the intended recipient. Funds that were legitimately in transit to that address are effectively frozen or misdirected with no independent remedy. [4](#0-3) 

---

### Likelihood Explanation

Any Silo deployment that (a) enables the `Address` whitelist and (b) has a legitimate need to allow token receipt for an address that should not submit transactions — or vice versa — will encounter this issue. Both scenarios are routine in permissioned bridge or DEX deployments. The operator has no API to express the distinction; the conflation is structural and unavoidable with the current whitelist design.

---

### Recommendation

Introduce a separate whitelist kind (e.g., `WhitelistKind::TokenRecipient`) dedicated to ERC-20 token receipt, and update `is_allow_receive_erc20_tokens` to consult only that list. This mirrors the fix applied to the HubPool: differentiate the two routes so each can be enabled or disabled independently. The `WhitelistKind` enum and `is_allow_receive_erc20_tokens` in `silo/mod.rs` are the only two sites that need to change. [5](#0-4) [6](#0-5) 

---

### Proof of Concept

1. Operator enables the `Address` whitelist via `set_whitelists_statuses`.
2. Operator calls `add_entry_to_whitelist` with `WhitelistKind::Address` and address `0xABCD` — intending only to allow `0xABCD` to receive ERC-20 tokens (e.g., as a refund destination).
3. `is_allow_submit` is now `true` for `0xABCD` because `is_address_allowed` returns `true`.
4. `0xABCD` submits an EVM transaction via `submit`; the Silo's transaction-submission gate passes, bypassing the operator's intent.

Conversely:

1. Operator removes `0xABCD` from `WhitelistKind::Address` to revoke its transaction rights.
2. A NEP-141 `ft_on_transfer` arrives with `0xABCD` as the EVM recipient.
3. `is_allow_receive_erc20_tokens` returns `false`; tokens are redirected to `erc20_fallback_address`.
4. The legitimate recipient never receives their tokens. [7](#0-6)

### Citations

**File:** engine/src/contract_methods/silo/mod.rs (L130-158)
```rust
/// Check if a user has the right to deploy EVM code.
pub fn is_allow_deploy<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_account_allowed_deploy(io, account) && is_address_allowed_deploy(io, address)
}

/// Check if a user has the right to submit transactions.
pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

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

**File:** engine-types/src/parameters/silo.rs (L19-23)
```rust
    /// EVM address, which is used for withdrawing ERC-20 base tokens in case
    /// a recipient of the tokens is not in the silo white list.
    /// Note: the logic described above works only if the fallback address
    /// is set by `set_silo_params` function. In other words, in Silo mode.
    pub erc20_fallback_address: Address,
```

**File:** engine-types/src/parameters/silo.rs (L62-80)
```rust
#[derive(Debug, Copy, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
#[cfg_attr(feature = "impl-serde", derive(serde::Serialize, serde::Deserialize))]
#[borsh(use_discriminant = false)]
pub enum WhitelistKind {
    /// The whitelist of this type is for storing NEAR accounts. Accounts stored in this whitelist
    /// have an admin role. The admin role allows to add new admins and add new entities
    /// (`AccountId` and `Address`) to whitelists. Also, this role allows to deploy of EVM code
    /// and submit transactions.
    Admin = 0x0,
    /// The whitelist of this type is for storing EVM addresses. Addresses included in this
    /// whitelist can deploy EVM code.
    EvmAdmin = 0x1,
    /// The whitelist of this type is for storing NEAR accounts. Accounts included in this
    /// whitelist can submit transactions.
    Account = 0x2,
    /// The whitelist of this type is for storing EVM addresses. Addresses included in this
    /// whitelist can submit transactions.
    Address = 0x3,
}
```
