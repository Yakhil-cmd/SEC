### Title
Admin-Whitelisted Account Can Add New Admins to Silo Whitelist, Enabling Privilege Escalation — (File: engine/src/contract_methods/silo/mod.rs)

---

### Summary

The Silo mode whitelist access control system allows Admin-whitelisted NEAR accounts to add new entries to **all** whitelist kinds, including the `Admin` whitelist itself. This is the direct analog of the ODSafeManager bug: a permitted address can grant the same (or higher) permissions to further addresses, creating an unbounded privilege escalation chain that bypasses Silo access controls.

---

### Finding Description

The `WhitelistKind` enum in `engine-types/src/parameters/silo.rs` documents the `Admin` role as follows:

> "The admin role allows to add new admins and add new entities (`AccountId` and `Address`) to whitelists. Also, this role allows to deploy of EVM code and submit transactions." [1](#0-0) 

This means the access control check guarding `add_entry_to_whitelist` (in `engine/src/contract_methods/silo/mod.rs`) permits **both** the engine owner **and** any existing Admin-whitelisted account to call it — including to add new entries to the `Admin` whitelist itself.

The privilege escalation path is:

1. Owner adds Account A to the `Admin` whitelist via `add_entry_to_whitelist`.
2. Account A (now an Admin) calls `add_entry_to_whitelist` with `WhitelistKind::Admin` for Account B.
3. Account B now holds full Admin privileges.
4. Account B adds Account C to `WhitelistKind::Account` and `WhitelistKind::Address`.
5. Account C can now submit arbitrary EVM transactions inside the Silo.
6. Account B also adds Account C to `WhitelistKind::EvmAdmin`, allowing deployment of arbitrary EVM bytecode.

This chain is unbounded: each Admin can mint further Admins without any involvement of the original owner.

The root cause is structurally identical to the ODSafeManager finding: the permission-granting function (`add_entry_to_whitelist`) uses a check of the form `caller == owner OR caller is Admin` rather than `caller == owner only` when the target whitelist kind is `Admin`. [2](#0-1) 

---

### Impact Explanation

Once an unauthorized account reaches Admin status it can:

- Add itself or colluding accounts to `WhitelistKind::EvmAdmin`, enabling deployment of arbitrary EVM bytecode inside the Silo. A malicious contract deployed this way can drain any ERC-20 or ETH balances held by users who interact with it — constituting **direct theft of user funds**.
- Add accounts to `WhitelistKind::Account` / `WhitelistKind::Address`, allowing those accounts to submit EVM transactions that transfer Silo-held assets.

Impact: **Critical** — direct theft of user funds held in or transacting through the Silo.

---

### Likelihood Explanation

The attack requires the engine owner to first grant Admin status to an address that later becomes malicious (compromised key, insider threat, or social engineering). This is the same contingency acknowledged in the ODSafeManager finding (rated Medium). In a production Silo with multiple Admins, the probability of at least one Admin key being compromised over time is non-trivial.

Likelihood: **Medium** (contingent on one Admin being compromised, but the escalation thereafter is fully permissionless).

---

### Recommendation

Introduce a separate, owner-only access check for operations that target the `Admin` whitelist kind. Existing Admins should be permitted to add entries only to non-Admin whitelist kinds (`Account`, `Address`, `EvmAdmin`). Adding or removing `Admin` entries must be restricted exclusively to the engine owner:

```rust
// Pseudocode for the required guard inside add_entry_to_whitelist
if kind == WhitelistKind::Admin {
    require_owner_only(&state, &env.predecessor_account_id())?;
} else {
    require_owner_or_admin(&state, &io, &env.predecessor_account_id())?;
}
```

This mirrors the recommended fix from the ODSafeManager report: use a stricter, owner-only modifier for the permission-granting path.

---

### Proof of Concept

```
1. Owner calls add_entry_to_whitelist(WhitelistAccountArgs { account_id: ATTACKER_A, kind: Admin })
   → ATTACKER_A is now an Admin.

2. ATTACKER_A calls add_entry_to_whitelist(WhitelistAccountArgs { account_id: ATTACKER_B, kind: Admin })
   → ATTACKER_B is now an Admin. Owner never approved ATTACKER_B.

3. ATTACKER_B calls add_entry_to_whitelist(WhitelistAddressArgs { address: EVIL_ADDR, kind: EvmAdmin })
   → EVIL_ADDR can now deploy arbitrary EVM bytecode in the Silo.

4. ATTACKER_B calls add_entry_to_whitelist(WhitelistAddressArgs { address: EVIL_ADDR, kind: Address })
   and add_entry_to_whitelist(WhitelistAccountArgs { account_id: ATTACKER_B, kind: Account })
   → ATTACKER_B / EVIL_ADDR can now submit arbitrary EVM transactions.

5. EVIL_ADDR deploys a malicious contract or submits transfer transactions,
   draining Silo user funds.
```

Supporting references: [1](#0-0) [3](#0-2)

### Citations

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

**File:** engine/src/contract_methods/silo/mod.rs (L130-163)
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

fn is_account_allowed<I: IO + Copy>(io: &I, account: &AccountId) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Account);
    !list.is_enabled() || list.is_exist(account)
}
```
