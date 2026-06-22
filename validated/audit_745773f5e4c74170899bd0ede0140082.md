### Title
SNS Treasury Manager Stale Cached Balance Used in Deposit/Withdraw Without Enforced `refresh_balances` Ordering — (`rs/sns/treasury_manager/src/lib.rs`, `rs/sns/treasury_manager/mock/src/main.rs`, `rs/sns/governance/src/extensions.rs`)

---

### Summary

The `TreasuryManager` trait mandates that `refresh_balances` be called only periodically (not before each `deposit`/`withdraw`), while `issue_rewards` can alter the actual managed-asset totals between those periodic refreshes. SNS governance's `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` invoke the treasury manager without first triggering a balance refresh, meaning deposit and withdrawal decisions can be made against a stale cached balance. This is a direct analog to the Brahma-fi "Order of operations: Convex rewards & new depositors profiting at the expense of old depositors' yielded reward tokens" finding.

---

### Finding Description

The `TreasuryManager` trait explicitly documents that `refresh_balances` is a cached-balance updater that "should not be exposed as an API function, but rather called periodically by the canister":

```rust
/// The Treasury Manager needs to have a local cache of these balances to be able to make
/// important decisions, e.g., how much can be refunded / withdrawn. That cache should be
/// regularly updated, and this is the function that should do that.
///
/// Should not be exposed as an API function, but rather called periodically by the canister.
fn refresh_balances(&mut self) -> impl std::future::Future<Output = ()> + Send;
``` [1](#0-0) 

The reference mock implementation schedules `run_periodic_tasks` on a **1-hour timer**, calling `refresh_balances` then `issue_rewards` in sequence:

```rust
async fn run_periodic_tasks() {
    state.refresh_balances().await;
    state.issue_rewards().await;
}
``` [2](#0-1) 

`issue_rewards` is an operation that changes the actual on-ledger balances (distributing rewards to payers / payees). After `issue_rewards` runs, the cached balance held by the treasury manager is immediately stale — it no longer reflects the post-reward state.

SNS governance proposal execution calls `execute_treasury_manager_deposit` and `execute_treasury_manager_withdraw` directly, with no prior call to `refresh_balances`:

```rust
// 1. Transfer funds from treasury to treasury manager
governance.approve_treasury_manager(...).await?;
// 2. Call deposit on treasury manager
let balances = governance.env.call_canister(extension_canister_id, "deposit", arg_blob).await ...
``` [3](#0-2) 

```rust
let balances = governance.env.call_canister(extension_canister_id, "withdraw", arg_blob).await ...
``` [4](#0-3) 

Neither path calls `refresh_balances` before invoking the treasury manager. The `balances` query is a pure query returning cached data tagged with `timestamp_ns`: [5](#0-4) 

The `BalanceBook` invariant documented in the DID states:

```
managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
``` [6](#0-5) 

If `issue_rewards` has run since the last `refresh_balances`, the cached `managed_assets` is incorrect, and any deposit or withdrawal decision made against it violates this invariant.

The DID file itself acknowledges a related but narrower risk (slippage at deposit time), but does not address the stale-balance-after-`issue_rewards` ordering problem for withdrawals: [7](#0-6) 

---

### Impact Explanation

**Scenario A — Withdrawal underestimates available funds:**
1. `run_periodic_tasks` fires: `refresh_balances` caches balance B₀, then `issue_rewards` distributes rewards, increasing the actual balance to B₁ > B₀.
2. A governance proposal to `withdraw` is executed within the next hour.
3. The treasury manager's `withdraw` logic uses cached B₀ to decide how much to pull from the external custodian.
4. The SNS treasury receives less than it is entitled to; the surplus remains stranded in the external custodian.

**Scenario B — Deposit over-allocates:**
1. `issue_rewards` has consumed some of the treasury manager's balance since the last `refresh_balances`, so actual balance is B₁ < B₀ (cached).
2. A governance proposal to `deposit` is executed.
3. The treasury manager believes it has B₀ available and attempts to deposit that amount, potentially exceeding actual holdings and causing a failed or partial transfer that leaves assets in the `suspense` account.

Both scenarios break the `managed_assets` conservation invariant and can result in permanent loss or misallocation of SNS treasury funds.

---

### Likelihood Explanation

The 1-hour periodic interval in the reference mock is the intended cadence. Any governance proposal executed in the window between `issue_rewards` and the next `refresh_balances` — a window that can be up to ~1 hour — triggers the stale-balance path. SNS governance proposals are executable by any token holder once approved, making this window routinely reachable without any privileged access.

---

### Recommendation

1. **Enforce freshness at operation time**: The `TreasuryManager` trait's `deposit` and `withdraw` default implementations (or the governance integration in `execute_treasury_manager_deposit` / `execute_treasury_manager_withdraw`) should call `refresh_balances` before acting, or reject the call if `Balances.timestamp_ns` is older than a defined staleness threshold.
2. **Separate reward accrual from balance cache**: `issue_rewards` should update the cached balance atomically, or `refresh_balances` should be called immediately after `issue_rewards` within `run_periodic_tasks`.
3. **Document the required ordering**: The `TreasuryManager` trait and DID should explicitly state that `refresh_balances` must be called (and its result awaited) before any `deposit` or `withdraw` invocation, and that `issue_rewards` invalidates the cache.

---

### Proof of Concept

```
t=0h:  run_periodic_tasks fires
         → refresh_balances(): cached_balance = 1000 SNS
         → issue_rewards(): 50 SNS distributed to payers; actual_balance = 950 SNS

t=0h+30m: SNS governance proposal "withdraw all" is executed
         → execute_treasury_manager_withdraw() called (no refresh_balances)
         → treasury manager reads cached_balance = 1000 SNS
         → instructs external custodian to return 1000 SNS
         → external custodian only holds 950 SNS → transfer fails or partial
         → 50 SNS remain stranded; SNS treasury receives 950 instead of 950
           (or, if the custodian silently caps, the shortfall is silently absorbed)

t=1h:  run_periodic_tasks fires again
         → refresh_balances(): cached_balance now reflects reality
         → discrepancy is only visible in audit_trail, not corrected automatically
```

The `suspense` field in `BalanceBook` is the only recovery mechanism, and its handling is left entirely to the implementer with no protocol-level guarantee of resolution. [8](#0-7) [9](#0-8)

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L149-153)
```rust
#[derive(CandidType, Clone, Debug, Default, Deserialize, PartialEq)]
pub struct Balances {
    pub timestamp_ns: u64,
    pub asset_to_balances: Option<BTreeMap<Asset, BalanceBook>>,
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L272-278)
```rust
    /// Context: the source of truth for balances are some remote canisters (e.g., the ledgers).
    /// The Treasury Manager needs to have a local cache of these balances to be able to make
    /// important decisions, e.g., how much can be refunded / withdrawn. That cache should be
    /// regularly updated, and this is the function that should do that.
    ///
    /// Should not be exposed as an API function, but rather called periodically by the canister.
    fn refresh_balances(&mut self) -> impl std::future::Future<Output = ()> + Send;
```

**File:** rs/sns/treasury_manager/mock/src/main.rs (L99-107)
```rust
async fn run_periodic_tasks() {
    log("run_periodic_tasks.");

    let mut state = canister_state();

    state.refresh_balances().await;

    state.issue_rewards().await;
}
```

**File:** rs/sns/treasury_manager/mock/src/main.rs (L109-113)
```rust
fn init_periodic_tasks() {
    let _new_timer_id =
        ic_cdk_timers::set_timer_interval(RUN_PERIODIC_TASKS_INTERVAL, async || {
            run_periodic_tasks().await
        });
```

**File:** rs/sns/governance/src/extensions.rs (L1566-1601)
```rust
    // 1. Transfer funds from treasury to treasury manager
    governance
        .approve_treasury_manager(
            extension_canister_id,
            treasury_allocation_sns_e8s,
            treasury_allocation_icp_e8s,
        )
        .await?;

    // 2. Call deposit on treasury manager
    let balances = governance
        .env
        .call_canister(extension_canister_id, "deposit", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.deposit failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!("Error decoding TreasuryManager.deposit response: {err:?}"),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.deposit failed: {err:?}"),
            )
        })?;
```

**File:** rs/sns/governance/src/extensions.rs (L1625-1652)
```rust
    let balances = governance
        .env
        .call_canister(extension_canister_id, "withdraw", arg_blob)
        .await
        .map_err(|(code, err)| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!(
                    "Canister method call {extension_canister_id}.withdraw failed with code {code:?}: {err}"
                ),
            )
        })
        .and_then(|blob| {
            Decode!(&blob, sns_treasury_manager::TreasuryManagerResult).map_err(|err| {
                GovernanceError::new_with_message(
                    ErrorType::External,
                    format!(
                        "Error decoding TreasuryManager.withdraw response: {err:?}"
                    ),
                )
            })
        })?
        .map_err(|err| {
            GovernanceError::new_with_message(
                ErrorType::External,
                format!("TreasuryManager.withdraw failed: {err:?}"),
            )
        })?;
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-41)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.

```

**File:** rs/sns/treasury_manager/treasury_manager.did (L154-160)
```text
/// Current managed assets
/// ----------------------
/// managed_assets[k] == treasury_manager[k] + treasury_owner[k] + external_custodian[k]
///
/// Under "normal operations", the following invariants hold for all k > 0:
/// 1) suspense[k] == 0
/// 2) managed_assets[k] == managed_assets[k-1] + payers[k] - payees[k] - fee_collector[k]
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L168-172)
```text

  // An account in which items are entered temporarily before allocation to the correct
  // or final account, e.g., due to transient errors.
  suspense : opt Balance;
};
```
