### Title
Governance-Controlled `retrieve_btc_min_amount` Lacks Upper-Bound Validation, Enabling Implicit Lock of All ckBTC Withdrawals - (`rs/bitcoin/ckbtc/minter/src/state.rs`)

### Summary

The ckBTC minter's `retrieve_btc_min_amount` parameter, which sets the minimum satoshi amount for ckBTC-to-BTC conversions, can be set to an arbitrarily large value (including `u64::MAX`) via NNS governance upgrade proposals. No upper-bound validation exists on this field. If set to an extreme value, every call to `retrieve_btc` and `retrieve_btc_with_approval` will fail with `AmountTooLow`, implicitly locking all ckBTC withdrawals. This is a silent, implicit lock that bypasses the minter's explicit `Mode`-based withdrawal-pause mechanism.

### Finding Description

`CkBtcMinterState` stores two related fields:

```rust
pub retrieve_btc_min_amount: u64,
pub fee_based_retrieve_btc_min_amount: u64,
``` [1](#0-0) 

The `upgrade()` method applies a new value from `UpgradeArgs` with no upper-bound check:

```rust
if let Some(retrieve_btc_min_amount) = retrieve_btc_min_amount {
    self.retrieve_btc_min_amount = retrieve_btc_min_amount;
    self.fee_based_retrieve_btc_min_amount = retrieve_btc_min_amount;
}
``` [2](#0-1) 

The only post-upgrade validation is `validate_config()`, which only checks that `check_fee <= retrieve_btc_min_amount` — a condition that trivially holds when `retrieve_btc_min_amount = u64::MAX`:

```rust
pub fn validate_config(&self) {
    if self.check_fee > self.retrieve_btc_min_amount {
        ic_cdk::trap("check_fee cannot be greater than retrieve_btc_min_amount");
    }
    ...
}
``` [3](#0-2) 

Both `retrieve_btc` and `retrieve_btc_with_approval` read `fee_based_retrieve_btc_min_amount` and reject any request below it:

```rust
let (min_retrieve_amount, btc_network) =
    read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

if args.amount < min_retrieve_amount {
    return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
}
``` [4](#0-3) [5](#0-4) 

Since no user can hold `u64::MAX` satoshis of ckBTC, every withdrawal call would be rejected. The fee-estimation timer also sets `fee_based_retrieve_btc_min_amount` to `retrieve_btc_min_amount + fee_estimate`, which saturates at or above `u64::MAX`, so the lock persists across timer ticks.

The minter already has an **explicit** withdrawal-pause mechanism via the `Mode` field (checked before the amount check):

```rust
state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
    .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
``` [6](#0-5) 

Setting `retrieve_btc_min_amount` to `u64::MAX` is an **implicit** lock that bypasses this explicit mechanism, making the lock semantically opaque to users and operators.

The `UpgradeArgs` struct exposes `retrieve_btc_min_amount` as an optional field with no documented maximum: [7](#0-6) 

### Impact Explanation

All new `retrieve_btc` and `retrieve_btc_with_approval` calls would return `AmountTooLow(u64::MAX)`, permanently blocking ckBTC-to-BTC withdrawals until a corrective governance proposal is passed. Users with ckBTC balances cannot redeem them for BTC. The lock is not surfaced as a deliberate pause (unlike `Mode::ReadOnly`), so users and monitoring systems receive a misleading error rather than a clear "withdrawals paused" signal.

### Likelihood Explanation

This requires an NNS governance proposal to pass — a high bar. However, the scenario is realistic: a governance actor intending to temporarily raise the minimum withdrawal amount (e.g., during high-fee periods) could accidentally set the value to an extreme number. The absence of an upper-bound guard means there is no on-chain protection against this mistake. The FiRM report's analogous finding was accepted under the same rationale: governance acting in good faith can trigger an implicit lock with no explicit semantic.

### Recommendation

1. Add an upper-bound check in `validate_config()` and in `CkBtcMinterState::upgrade()` — e.g., reject any `retrieve_btc_min_amount` above a documented maximum (e.g., `MAX_BTC_SUPPLY_SATOSHI = 2_100_000_000_000_000`).
2. Document that `Mode` is the canonical mechanism for pausing withdrawals, and that `retrieve_btc_min_amount` must not be used as a substitute.
3. Emit a warning log or metric when `retrieve_btc_min_amount` is set above a threshold that would effectively block all users.

### Proof of Concept

1. NNS governance passes an upgrade proposal: `retrieve_btc_min_amount = opt (18_446_744_073_709_551_615 : nat64)`.
2. `CkBtcMinterState::upgrade()` sets `self.retrieve_btc_min_amount = u64::MAX` and `self.fee_based_retrieve_btc_min_amount = u64::MAX`.
3. `validate_config()` passes: `check_fee (1_000) <= u64::MAX` ✓.
4. Any user calling `retrieve_btc { amount = X, address = "..." }` receives `Err(AmountTooLow(18446744073709551615))` regardless of their ckBTC balance.
5. The fee-estimation timer recalculates `fee_based_retrieve_btc_min_amount = u64::MAX + fee_estimate`, which saturates at `u64::MAX`, so the lock persists.
6. Reverting requires a second NNS governance upgrade proposal, subject to the full NNS proposal lifecycle delay.

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L453-457)
```rust
    /// Minimum amount of bitcoin that can be retrieved
    pub retrieve_btc_min_amount: u64,

    /// Minimum amount of bitcoin that can be retrieved based on recent fees
    pub fee_based_retrieve_btc_min_amount: u64,
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L708-711)
```rust
        if let Some(retrieve_btc_min_amount) = retrieve_btc_min_amount {
            self.retrieve_btc_min_amount = retrieve_btc_min_amount;
            self.fee_based_retrieve_btc_min_amount = retrieve_btc_min_amount;
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

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L152-153)
```rust
    state::read_state(|s| s.mode.is_withdrawal_available_for(&caller))
        .map_err(RetrieveBtcError::TemporarilyUnavailable)?;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L166-171)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));

    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs (L264-270)
```rust
    let (min_retrieve_amount, btc_network) =
        read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));
    if args.amount < min_retrieve_amount {
        return Err(RetrieveBtcWithApprovalError::AmountTooLow(
            min_retrieve_amount,
        ));
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L18-21)
```rust
    /// Minimum amount of bitcoin that can be retrieved.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retrieve_btc_min_amount: Option<u64>,

```
