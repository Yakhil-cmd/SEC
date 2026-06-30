### Title
Non-whitelisted EVM Addresses Can Receive ERC-20 Tokens When No Fallback Address Is Configured, Causing Permanent Fund Freeze - (`engine/src/engine.rs`)

---

### Summary

In Aurora Engine's Silo mode, the `receive_erc20_tokens` function in `engine/src/engine.rs` only enforces the Address whitelist when an `erc20_fallback_address` is configured. When the Address whitelist is enabled but no fallback address is set, the whitelist check is entirely skipped via Rust's `if let` short-circuit. This allows any NEAR user to bridge ERC-20 tokens to a non-whitelisted EVM address. Because `assert_access` blocks all EVM transactions from non-whitelisted addresses, the minted tokens become permanently frozen in the recipient address.

---

### Finding Description

The `receive_erc20_tokens` function in `engine/src/engine.rs` contains the following whitelist enforcement logic:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [1](#0-0) 

This is a compound `if let` + `&&` guard. If `get_erc20_fallback_address` returns `None`, the entire condition short-circuits and `is_allow_receive_erc20_tokens` is **never called**. The recipient address is used as-is, regardless of whether the Address whitelist is enabled and regardless of whether the recipient is whitelisted.

The `is_allow_receive_erc20_tokens` function itself correctly delegates to `is_address_allowed`, which checks the `WhitelistKind::Address` list:

```rust
pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}

fn is_address_allowed<I: IO + Copy>(io: &I, address: &Address) -> bool {
    let list = Whitelist::init(io, WhitelistKind::Address);
    !list.is_enabled() || list.is_exist(address)
}
``` [2](#0-1) 

The fallback address is explicitly optional — `Erc20FallbackAddressArgs` holds `address: Option<Address>`, and `set_erc20_fallback_address` removes the storage key when `None` is passed: [3](#0-2) [4](#0-3) 

This means a valid and reachable configuration exists: Address whitelist enabled, no fallback address set. In this state, the whitelist is completely inoperative for ERC-20 token receipt.

Meanwhile, `assert_access` — which gates all EVM transaction submission — correctly enforces the Address whitelist for the sender:

```rust
let allowed = if transaction.to.is_some() {
    silo::is_allow_submit(io, &env.predecessor_account_id(), &transaction.address)
} else {
    silo::is_allow_deploy(io, &env.predecessor_account_id(), &transaction.address)
};
``` [5](#0-4) 

So a non-whitelisted EVM address can **receive** tokens (via the bridge bypass) but **cannot send** any transaction to move them.

---

### Impact Explanation

**Permanent fund freeze.** When a NEAR user bridges a NEP-141 token to Aurora targeting a non-whitelisted EVM address (with no fallback address configured), `receive_erc20_tokens` mints the ERC-20 tokens to that address without any whitelist check. The non-whitelisted address is then blocked from submitting any EVM transaction by `assert_access`, including calls to transfer or burn the ERC-20 tokens or invoke the `ExitToNear` precompile. The tokens are irrecoverably locked unless an admin later whitelists the address — an out-of-band action that may never occur.

This matches the **Permanent freezing of funds** impact category.

---

### Likelihood Explanation

**Medium.** The scenario requires:
1. Silo mode is active with the Address whitelist enabled — an explicit operator choice.
2. No `erc20_fallback_address` is configured — the default state, since the fallback address is optional and starts absent.
3. A NEAR user calls `ft_transfer_call` on a registered NEP-141 token, targeting Aurora with a non-whitelisted EVM address in the message.

Steps 2 and 3 are the default/normal state. Any NEAR user can trigger this without any special privilege. The operator enabling the whitelist without setting a fallback address is a natural configuration, especially since the CHANGELOG notes that whitelists were decoupled from fixed gas (silo params) in v3.9.0: [6](#0-5) 

This means operators can now enable whitelists independently of silo params, increasing the likelihood of the fallback address being absent.

---

### Recommendation

Decouple the whitelist enforcement from the fallback address existence. The whitelist check should always be performed when the Address whitelist is enabled. If the recipient is not whitelisted, the behavior should be:
- If a fallback address is configured: redirect to the fallback address (current behavior when fallback exists).
- If no fallback address is configured: return an error or refund the tokens to the sender.

Suggested fix in `receive_erc20_tokens`:

```rust
// Always check whitelist if enabled
if !silo::is_allow_receive_erc20_tokens(&self.io, &recipient) {
    if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io) {
        recipient = fallback_address;
    } else {
        return Err(ContractError::msg("ERR_RECIPIENT_NOT_WHITELISTED"));
    }
}
```

---

### Proof of Concept

1. Deploy Aurora Engine in Silo mode.
2. Enable the Address whitelist (`set_whitelist_status` with `WhitelistKind::Address, active: true`).
3. Do **not** set an `erc20_fallback_address` (leave it absent, which is the default).
4. Register a NEP-141 token and its ERC-20 mirror on Aurora.
5. From any NEAR account, call `ft_transfer_call` on the NEP-141 contract, targeting Aurora, with a non-whitelisted EVM address `0xDEAD...` in the message.
6. Aurora's `ft_on_transfer` → `receive_erc20_tokens` is invoked. The `if let Some(fallback_address) = ...` condition is `false` (no fallback), so the whitelist check is skipped entirely.
7. ERC-20 tokens are minted to `0xDEAD...`.
8. Attempt to submit any EVM transaction from `0xDEAD...` — `assert_access` returns `EngineErrorKind::NotAllowed`.
9. The tokens are permanently frozen. [1](#0-0) [7](#0-6) [8](#0-7)

### Citations

**File:** engine/src/engine.rs (L818-822)
```rust
        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
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

**File:** engine/src/contract_methods/silo/mod.rs (L65-73)
```rust
pub fn set_erc20_fallback_address<I: IO>(io: &mut I, address: Option<Address>) {
    let key = erc20_fallback_address_key();

    if let Some(address) = address {
        io.write_storage(&key, address.as_bytes());
    } else {
        io.remove_storage(&key);
    }
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L136-158)
```rust
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

**File:** engine-types/src/parameters/silo.rs (L10-13)
```rust
#[derive(Debug, Clone, PartialEq, Eq, BorshSerialize, BorshDeserialize)]
pub struct Erc20FallbackAddressArgs {
    pub address: Option<Address>,
}
```

**File:** CHANGES.md (L99-99)
```markdown
- The white lists don't require the fixed gas per transaction (silo mode) by [@aleksuss]. ([#1005])
```
