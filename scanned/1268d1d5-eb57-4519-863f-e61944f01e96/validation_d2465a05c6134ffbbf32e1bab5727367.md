### Title
Unvalidated `None` `btc_checker_principal` Causes Canister Trap on Unprivileged `update_balance`/`retrieve_btc` Calls — (File: `rs/bitcoin/ckbtc/minter/src/lib.rs`)

---

### Summary

The ckBTC minter stores `btc_checker_principal` as `Option<CanisterId>` in `CkBtcMinterState`. This field is explicitly marked optional for backward compatibility with pre-checker deployments. When a user calls `update_balance` or `retrieve_btc`, the minter reads this field as `Option<Principal>` and passes it through the call chain to `IcCanisterRuntime::check_transaction` and `IcCanisterRuntime::check_address`, both of which call `.expect()` on the `Option`. If the field is `None` — a reachable state after upgrading from an old minter version without supplying `btc_checker_principal` in `UpgradeArgs` — the canister traps on every user-triggered deposit or withdrawal, permanently DoS-ing ckBTC minting and burning until an operator upgrade corrects the state.

---

### Finding Description

`CkBtcMinterState` declares:

```rust
pub btc_checker_principal: Option<CanisterId>,
``` [1](#0-0) 

`InitArgs` marks the field optional for backward compatibility:

```rust
/// NOTE: this field is optional for backward compatibility.
pub btc_checker_principal: Option<CanisterId>,
``` [2](#0-1) 

`validate_config()` traps if the field is `None`, but it is only called inside `init()`, not inside the upgrade path:

```rust
if self.btc_checker_principal.is_none() {
    ic_cdk::trap("Bitcoin checker principal is not set");
}
``` [3](#0-2) 

The `upgrade()` method on `CkBtcMinterState` only sets `btc_checker_principal` when the upgrade argument is `Some(...)`:

```rust
if let Some(btc_checker_principal) = btc_checker_principal {
    self.btc_checker_principal = Some(btc_checker_principal);
}
``` [4](#0-3) 

So a minter that was initialized before the Bitcoin checker field existed, then upgraded to the current version without supplying `btc_checker_principal` in `UpgradeArgs`, will have `btc_checker_principal = None` in its live state with no guard at the upgrade boundary.

**User-triggered path 1 — `update_balance`:**

`check_utxo` reads the field as `Option<Principal>` and passes it directly to `runtime.check_transaction`:

```rust
let btc_checker_principal = read_state(|s| s.btc_checker_principal.map(Principal::from));
...
runtime.check_transaction(btc_checker_principal, utxo, CHECK_TRANSACTION_CYCLES_REQUIRED)
``` [5](#0-4) 

`IcCanisterRuntime::check_transaction` then calls `.expect()` on the `Option`:

```rust
let btc_checker_principal = btc_checker_principal
    .expect("BUG: upgrade procedure must ensure that the Bitcoin checker principal is set");
``` [6](#0-5) 

**User-triggered path 2 — `retrieve_btc` / `retrieve_btc_with_approval`:**

Both functions read the field and pass it to `check_address`:

```rust
let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
``` [7](#0-6) 

`IcCanisterRuntime::check_address` also calls `.expect()`:

```rust
let btc_checker_principal = btc_checker_principal
    .expect("BUG: upgrade procedure must ensure that the Bitcoin checker principal is set");
``` [8](#0-7) 

The `.expect()` comment itself — *"BUG: upgrade procedure must ensure that the Bitcoin checker principal is set"* — is an in-code acknowledgment that the invariant is not enforced by the type system or by the upgrade lifecycle, only by an out-of-band operational convention.

---

### Impact Explanation

If `btc_checker_principal` is `None` in the live minter state, every call to `update_balance` (BTC deposit → ckBTC mint) and every call to `retrieve_btc` / `retrieve_btc_with_approval` (ckBTC burn → BTC withdrawal) will cause the canister to trap. The IC runtime converts the trap into a reject response to the caller, but the minter's own state is rolled back and the operation is never completed. No ckBTC can be minted or burned until an NNS-governed upgrade corrects the state. This is a complete, user-triggerable DoS of the chain-fusion mint/burn path.

---

### Likelihood Explanation

The mainnet ckBTC minter predates the Bitcoin checker canister. The field is explicitly marked optional for backward compatibility, and the upgrade argument `btc_checker_principal` is also optional. Any upgrade that omits `btc_checker_principal` from `UpgradeArgs` silently preserves `None`. Because `validate_config()` is not called in the upgrade hook, there is no on-chain enforcement that the field is populated before the minter resumes serving user calls. The risk is low in steady-state (the field is presumably set on mainnet today), but the code pattern leaves a latent trap reachable by any unprivileged user if the invariant is ever violated by an incomplete upgrade.

---

### Recommendation

1. **Call `validate_config()` in the upgrade lifecycle** (`canister_post_upgrade`) so that an upgrade that leaves `btc_checker_principal = None` traps immediately at upgrade time rather than at the first user call.
2. **Validate at entry points**: in `update_balance` and `retrieve_btc`, check `btc_checker_principal.is_some()` and return a structured `TemporarilyUnavailable` error before proceeding, rather than relying on `.expect()` deep in the call chain.
3. **Replace `Option<CanisterId>` with a required field** now that backward compatibility with pre-checker minters is no longer needed, eliminating the `None` state entirely.

---

### Proof of Concept

1. Deploy a ckBTC minter with `btc_checker_principal = None` (simulating an old-version state) by initializing with `InitArgs { btc_checker_principal: None, ... }` — note that `validate_config()` would trap this at init, so the realistic path is to load old stable memory that predates the field.
2. Upgrade the minter to the current version with `UpgradeArgs { btc_checker_principal: None, ... }` (omitting the field). `validate_config()` is not called; the state retains `btc_checker_principal = None`.
3. As any anonymous user, call `update_balance` with a valid `UpdateBalanceArgs`.
4. Observe the call is rejected with a trap: *"BUG: upgrade procedure must ensure that the Bitcoin checker principal is set"* — triggered at `rs/bitcoin/ckbtc/minter/src/lib.rs:1712`.
5. Repeat for `retrieve_btc` — same trap at line 1819.
6. No ckBTC can be minted or burned until an operator upgrade sets `btc_checker_principal`. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L512-513)
```rust
    /// The principal of the Bitcoin checker canister.
    pub btc_checker_principal: Option<CanisterId>,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L730-732)
```rust
        if let Some(btc_checker_principal) = btc_checker_principal {
            self.btc_checker_principal = Some(btc_checker_principal);
        }
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L759-769)
```rust
    pub fn validate_config(&self) {
        if self.check_fee > self.retrieve_btc_min_amount {
            ic_cdk::trap("check_fee cannot be greater than retrieve_btc_min_amount");
        }
        if self.ecdsa_key_name.is_empty() {
            ic_cdk::trap("ecdsa_key_name is not set");
        }
        if self.btc_checker_principal.is_none() {
            ic_cdk::trap("Bitcoin checker principal is not set");
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/init.rs (L59-62)
```rust
    /// The principal of the Bitcoin checker canister.
    /// NOTE: this field is optional for backward compatibility.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub btc_checker_principal: Option<CanisterId>,
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L395-406)
```rust
    let btc_checker_principal = read_state(|s| s.btc_checker_principal.map(Principal::from));

    if let Some(checked_utxo) = read_state(|s| s.checked_utxos.get(utxo).cloned()) {
        return Ok(checked_utxo.status);
    }
    for i in 0..MAX_CHECK_TRANSACTION_RETRY {
        match runtime
            .check_transaction(
                btc_checker_principal,
                utxo,
                CHECK_TRANSACTION_CYCLES_REQUIRED,
            )
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1711-1712)
```rust
        let btc_checker_principal = btc_checker_principal
            .expect("BUG: upgrade procedure must ensure that the Bitcoin checker principal is set");
```

**File:** rs/bitcoin/ckbtc/minter/src/lib.rs (L1818-1819)
```rust
        let btc_checker_principal = btc_checker_principal
            .expect("BUG: upgrade procedure must ensure that the Bitcoin checker principal is set");
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L186-187)
```rust
    let btc_checker_principal = read_state(|s| s.btc_checker_principal).map(|id| id.get().into());
    let status = check_address(btc_checker_principal, args.address.clone(), runtime).await?;
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L688-757)
```rust

```
