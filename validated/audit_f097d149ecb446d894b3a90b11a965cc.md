Based on my analysis of the code, this is a **valid vulnerability**. Here is the full finding:

---

### Title
Silo Admin/EvmAdmin Whitelist Bypassed for Internal CREATE/CREATE2 Deployments via Call Transactions — (`engine/src/engine.rs`, `engine/src/contract_methods/silo/mod.rs`)

### Summary

The `assert_access` gate in `engine/src/engine.rs` only inspects the **outer transaction's `to` field** to decide which whitelist to enforce. A call transaction (`to = Some(...)`) is checked against the permissive Account+Address whitelists, while a deploy transaction (`to = None`) is checked against the restrictive Admin+EvmAdmin whitelists. Because the EVM's internal CREATE/CREATE2 opcode execution is never re-checked against the Admin/EvmAdmin whitelists, any address in the Account+Address whitelists can deploy new EVM contracts by calling an existing factory contract — completely bypassing the deployment restriction.

### Finding Description

The sole access-control gate before EVM execution is `assert_access`: [1](#0-0) 

The branch logic is:

```
if transaction.to.is_some()  →  is_allow_submit  (Account + Address whitelists)
if transaction.to.is_none()  →  is_allow_deploy  (Admin  + EvmAdmin whitelists)
```

`is_allow_deploy` checks the Admin and EvmAdmin whitelists: [2](#0-1) 

`is_allow_submit` checks only the Account and Address whitelists: [3](#0-2) 

After `assert_access` passes, the EVM (SputnikVM) executes the transaction. When the called contract executes a CREATE or CREATE2 opcode internally, SputnikVM invokes the `Backend` trait methods on `Engine` to apply state changes. The `ApplyBackend::apply` implementation writes the new contract code to storage unconditionally: [4](#0-3) 

There is **no whitelist re-check** at this point. The Admin/EvmAdmin restriction is never consulted for internally-triggered contract deployments.

The `WhitelistKind` documentation confirms the intended invariant that is broken: [5](#0-4) 

### Impact Explanation

A user in the Account+Address whitelists but **not** in the Admin+EvmAdmin whitelists can deploy arbitrary EVM contracts by calling a pre-existing factory contract. The deployed contracts can implement logic that locks or redirects user funds (e.g., a malicious vault or token contract), causing temporary freezing of funds for users who interact with them. In a silo environment where the operator intends to tightly control which contracts exist, this undermines the entire deployment access model.

### Likelihood Explanation

- The precondition (an existing factory contract) is realistic: factory contracts are common in DeFi (Uniswap V2/V3 factories, CREATE2 deployers, etc.) and may have been deployed by an admin before the whitelist was tightened, or may be a legitimate protocol component.
- The attacker only needs to be in the Account+Address whitelists, which is the lower-privilege tier.
- The attack requires no special tooling — a standard EVM transaction suffices.

### Recommendation

Add a whitelist check inside the EVM execution path for CREATE/CREATE2 operations. One approach is to intercept contract creation in the `Backend` implementation (or a custom SputnikVM handler) and verify the originating address against the Admin/EvmAdmin whitelists before allowing the new contract code to be stored. Alternatively, the silo operator documentation should explicitly state that the Admin/EvmAdmin whitelist does **not** gate internal CREATE/CREATE2 calls, and operators should avoid deploying factory contracts in restricted silos.

### Proof of Concept

1. Enable all four whitelists.
2. Add `caller_account` to Account whitelist; add `sender_address` to Address whitelist.
3. Do **not** add `caller_account` or `sender_address` to Admin or EvmAdmin whitelists.
4. Deploy a factory contract (as an admin) that exposes a `deploy(bytes)` function executing `CREATE` internally.
5. Submit a call transaction from `sender_address` to the factory's `deploy` function with arbitrary bytecode.
6. `assert_access` calls `is_allow_submit` → passes (Account+Address whitelists satisfied).
7. EVM executes the factory's `CREATE` opcode → `ApplyBackend::apply` writes the new contract code with no whitelist check.
8. Confirm the new contract exists at the expected address — the Admin/EvmAdmin restriction was bypassed. [6](#0-5) [7](#0-6)

### Citations

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

**File:** engine/src/engine.rs (L2011-2014)
```rust
                    if let Some(code) = code {
                        set_code(&mut self.io, &address, &code);
                        code_bytes_written = code.len();
                        sdk::log!("code_write_at_address {:?} {}", address, code_bytes_written);
```

**File:** engine/src/contract_methods/silo/mod.rs (L131-153)
```rust
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
```

**File:** engine-types/src/parameters/silo.rs (L65-79)
```rust
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
```
