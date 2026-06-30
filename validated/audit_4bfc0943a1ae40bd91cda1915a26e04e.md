### Title
Silo Whitelists Disabled by Default Allows Any User to Bypass Access Control in Silo Mode - (File: `engine/src/contract_methods/silo/whitelist.rs`, `engine/src/contract_methods/silo/mod.rs`)

---

### Summary

Aurora Engine's Silo mode provides four access-control whitelists (`Admin`, `EvmAdmin`, `Account`, `Address`) intended to restrict who can submit EVM transactions, deploy EVM code, and receive ERC-20 tokens. All four whitelists are **disabled by default** and must be explicitly enabled by the operator. The access-check helper functions are written so that a disabled whitelist unconditionally grants access to every caller. When an operator configures Silo mode via `set_silo_params` but does not separately call `set_whitelist_status` to enable the whitelists, any unprivileged NEAR account or EVM address can submit transactions, deploy contracts, and receive bridged ERC-20 tokens — completely bypassing the intended Silo access control.

---

### Finding Description

`Whitelist::is_enabled()` explicitly documents and implements the "disabled by default" behavior:

```rust
pub fn is_enabled(&self) -> bool {
    // White list is disabled by default. So return `false` if the key doesn't exist.
    let key = self.key(STATUS);
    self.io
        .read_storage(&key)
        .is_some_and(|value| value.to_vec() == [1])
}
``` [1](#0-0) 

Every access-check helper in `silo/mod.rs` uses the pattern `!list.is_enabled() || list.is_exist(...)`, meaning a disabled whitelist unconditionally returns `true` (access granted) for every caller:

```rust
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
``` [2](#0-1) 

These helpers feed into the two public gate functions:

```rust
pub fn is_allow_deploy<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_account_allowed_deploy(io, account) && is_address_allowed_deploy(io, address)
}

pub fn is_allow_submit<I: IO + Copy>(io: &I, account: &AccountId, address: &Address) -> bool {
    is_address_allowed(io, address) && is_account_allowed(io, account)
}

pub fn is_allow_receive_erc20_tokens<I: IO + Copy>(io: &I, address: &Address) -> bool {
    is_address_allowed(io, address)
}
``` [3](#0-2) 

`assert_access` in `engine.rs` is the sole enforcement point for EVM transaction submission and deployment:

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
``` [4](#0-3) 

`set_silo_params` only writes `fixed_gas` and `erc20_fallback_address`; it does **not** enable any whitelist: [5](#0-4) 

The ERC-20 token fallback redirection in `receive_erc20_tokens` is also silently bypassed when the `Address` whitelist is disabled, because `is_allow_receive_erc20_tokens` returns `true` for every address, so the `if` branch that redirects to the fallback address is never taken:

```rust
if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
    && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
{
    recipient = fallback_address;
}
``` [6](#0-5) 

---

### Impact Explanation

In a Silo deployment where the operator calls `set_silo_params` (setting a fixed gas cost and ERC-20 fallback address) but does not separately call `set_whitelist_status` to enable the whitelists, **all four whitelists remain disabled**. Any unprivileged NEAR account can:

1. Submit arbitrary EVM transactions (bypassing the `Account` and `Address` whitelists).
2. Deploy arbitrary EVM bytecode (bypassing the `Admin` and `EvmAdmin` whitelists).
3. Receive bridged ERC-20 tokens directly at any EVM address (bypassing the fallback-redirection mechanism that is supposed to protect non-whitelisted addresses).

In a Silo deployment that holds bridged NEP-141/ERC-20 token balances, an attacker who can submit unrestricted EVM transactions can interact with those token contracts and drain balances they control, or exploit any contract deployed in the Silo. The ERC-20 fallback mechanism — the primary fund-protection feature of Silo mode — is rendered inoperative because `is_allow_receive_erc20_tokens` always returns `true` when the `Address` whitelist is disabled.

**Impact**: High — theft of bridged ERC-20 token balances held in the Silo EVM; complete bypass of the Silo access-control model.

---

### Likelihood Explanation

The Silo feature requires two independent configuration steps: (1) `set_silo_params` to activate fixed gas and the fallback address, and (2) one or more `set_whitelist_status` calls to actually enable the whitelists. Nothing in the API, the `set_silo_params` call, or the contract state couples these two steps. An operator who configures Silo mode and observes that the fallback address is set may reasonably believe access is now restricted, when in fact all whitelists remain disabled. The existing test suite explicitly demonstrates that whitelists must be enabled separately (`enable_all_whitelists` is always called as a distinct step in tests). [7](#0-6) 

**Likelihood**: Medium — any operator who deploys Silo mode without reading the full whitelist-activation documentation is exposed.

---

### Recommendation

**Short term**: In `set_silo_params`, automatically enable all four whitelists when Silo parameters are set (non-`None`), and disable them when Silo parameters are cleared (`None`). This couples the Silo activation to its access-control enforcement, matching operator intent.

**Long term**: Add a runtime invariant check or a clear on-chain assertion that rejects `ft_on_transfer` and `submit` calls when Silo params are set but no whitelist is enabled, to prevent silent misconfiguration. Document the two-step activation requirement prominently.

---

### Proof of Concept

1. Deploy Aurora Engine.
2. Call `set_silo_params` with a valid `fixed_gas` and `erc20_fallback_address` (Silo mode activated).
3. Do **not** call `set_whitelist_status` for any whitelist kind.
4. From any arbitrary NEAR account (`attacker.near`), call `submit` with a signed EVM transaction targeting any ERC-20 token contract in the Silo.
5. Observe that `assert_access` calls `is_allow_submit`, which calls `is_account_allowed` and `is_address_allowed`; both return `true` because `Whitelist::is_enabled()` returns `false` (no storage key exists), so `!list.is_enabled()` is `true`.
6. The transaction executes successfully — the attacker has bypassed the Silo whitelist entirely.
7. Similarly, call `ft_on_transfer` with any recipient EVM address; `is_allow_receive_erc20_tokens` returns `true`, so the fallback redirection never fires and tokens are minted directly to the attacker-controlled address. [1](#0-0) [8](#0-7) [4](#0-3) [6](#0-5)

### Citations

**File:** engine/src/contract_methods/silo/whitelist.rs (L28-32)
```rust
    /// Enable a whitelist. (A whitelist is disabled after creation).
    pub fn enable(&mut self) {
        let key = self.key(STATUS);
        self.io.write_storage(&key, &[1]);
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

**File:** engine/src/contract_methods/silo/mod.rs (L31-38)
```rust
pub fn set_silo_params<I: IO>(io: &mut I, args: Option<SiloParamsArgs>) {
    let (cost, address) = args.map_or((None, None), |params| {
        (Some(params.fixed_gas), Some(params.erc20_fallback_address))
    });

    set_fixed_gas(io, cost);
    set_erc20_fallback_address(io, address);
}
```

**File:** engine/src/contract_methods/silo/mod.rs (L130-143)
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
```

**File:** engine/src/contract_methods/silo/mod.rs (L145-163)
```rust
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
