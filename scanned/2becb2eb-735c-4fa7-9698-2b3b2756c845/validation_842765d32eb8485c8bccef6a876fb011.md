### Title
Governance Authorization Bug: `min_confirmations` Cannot Be Increased via Upgrade Args in ckBTC Minter — (File: `rs/bitcoin/ckbtc/minter/src/state.rs`)

---

### Summary

The ckBTC minter's `upgrade` function silently ignores any attempt to **increase** `min_confirmations` via NNS upgrade args, despite the Candid interface advertising it as a freely settable parameter. This creates an undocumented one-way ratchet: the security threshold can only be weakened (decreased) but never strengthened (increased) through the standard governance upgrade path.

---

### Finding Description

In `rs/bitcoin/ckbtc/minter/src/state.rs`, the `CkBtcMinterState::upgrade` function applies a new `min_confirmations` value only when it is **strictly less than** the current value:

```rust
if let Some(min_conf) = min_confirmations {
    if min_conf < self.min_confirmations {
        self.min_confirmations = min_conf;
    } else {
        log!(
            Priority::Info,
            "Didn't increase min_confirmations to {} (current value: {})",
            min_conf,
            self.min_confirmations
        );
    }
}
``` [1](#0-0) 

If the NNS passes a proposal to raise `min_confirmations` (e.g., from 6 to 12), the upgrade call succeeds without error, the event is recorded in the event log, but the state field is left unchanged. The only signal is an `Info`-level log line that is invisible to governance voters and users.

The Candid interface documents `min_confirmations` in `UpgradeArgs` as a plain optional `nat32` with no mention of this restriction:

```
/// The minimum number of confirmations required for the minter to
/// accept a Bitcoin transaction.
min_confirmations : opt nat32;
``` [2](#0-1) 

The same asymmetry is present in the ckDOGE minter, which delegates to the same ckBTC minter `upgrade` path. [3](#0-2) 

The `reinit` path (used during event-log replay) does **not** carry this restriction and sets `min_confirmations` directly, confirming the asymmetry is specific to the live upgrade path and not an intentional invariant of the state machine.

---

### Impact Explanation

`min_confirmations` is the primary on-chain finality guard for the ckBTC bridge. If the NNS governance passes an emergency proposal to raise this threshold (e.g., in response to a Bitcoin mining-power concentration event or a discovered reorg vulnerability), the upgrade will silently leave the minter at the old, lower value.

Any unprivileged user who calls `update_balance` with a UTXO that has between `old_min_conf` and `new_min_conf` confirmations will have ckBTC minted against a transaction that the governance intended to treat as unconfirmed. In a reorg scenario this constitutes a double-spend: the attacker receives ckBTC while the underlying BTC transaction is later rolled back.

The impact is a **chain-fusion mint/burn conservation bug**: ckBTC is minted for BTC that the governance has explicitly decided is not yet final.

---

### Likelihood Explanation

Low under normal conditions — the NNS has not needed to raise `min_confirmations` since mainnet launch. However, the likelihood becomes non-negligible precisely when it matters most: during an active Bitcoin security incident. At that point the silent failure converts a governance emergency response into a no-op, and the window of exploitability is the entire period between the upgrade and the next canister reinstall.

---

### Recommendation

Remove the one-sided guard. Allow `min_confirmations` to be raised as well as lowered via upgrade args. If backward-compatibility concerns exist (e.g., not wanting to invalidate already-queued UTXOs), document the restriction explicitly in the Candid interface and emit a canister-level error (not merely an `Info` log) when the requested value is rejected.

---

### Proof of Concept

1. Current live state: `min_confirmations = 6`.
2. NNS passes proposal: upgrade ckBTC minter with `UpgradeArgs { min_confirmations: Some(12), .. }`.
3. `post_upgrade` is called; the event `EventType::Upgrade(args)` is recorded in the event log.
4. Inside `CkBtcMinterState::upgrade`, the branch `12 < 6` is false → the field is not updated; an `Info` log is emitted.
5. `min_confirmations` remains `6` in live state and after any subsequent event-log replay.
6. Attacker observes a BTC transaction with 8 confirmations (≥ 6, < 12).
7. Attacker calls `update_balance`; the minter accepts the UTXO and mints ckBTC.
8. If the underlying BTC transaction is later reorganized out of the chain, the attacker retains ckBTC backed by no BTC. [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L687-757)
```rust
    #[allow(deprecated)]
    pub fn upgrade(
        &mut self,
        UpgradeArgs {
            deposit_btc_min_amount,
            retrieve_btc_min_amount,
            max_time_in_queue_nanos,
            min_confirmations,
            mode,
            check_fee,
            btc_checker_principal,
            kyt_principal: _,
            kyt_fee,
            get_utxos_cache_expiration_seconds,
            utxo_consolidation_threshold,
            max_num_inputs_in_transaction,
        }: UpgradeArgs,
    ) {
        if let Some(deposit_btc_min_amount) = deposit_btc_min_amount {
            self.deposit_btc_min_amount = deposit_btc_min_amount;
        }
        if let Some(retrieve_btc_min_amount) = retrieve_btc_min_amount {
            self.retrieve_btc_min_amount = retrieve_btc_min_amount;
            self.fee_based_retrieve_btc_min_amount = retrieve_btc_min_amount;
        }
        if let Some(max_time_in_queue_nanos) = max_time_in_queue_nanos {
            self.max_time_in_queue_nanos = max_time_in_queue_nanos;
        }
        if let Some(min_conf) = min_confirmations {
            if min_conf < self.min_confirmations {
                self.min_confirmations = min_conf;
            } else {
                log!(
                    Priority::Info,
                    "Didn't increase min_confirmations to {} (current value: {})",
                    min_conf,
                    self.min_confirmations
                );
            }
        }
        if let Some(mode) = mode {
            self.mode = mode;
        }
        if let Some(btc_checker_principal) = btc_checker_principal {
            self.btc_checker_principal = Some(btc_checker_principal);
        }
        if let Some(check_fee) = check_fee {
            self.check_fee = check_fee;
        } else if let Some(kyt_fee) = kyt_fee {
            self.check_fee = kyt_fee;
        }
        if let Some(expiration) = get_utxos_cache_expiration_seconds {
            self.get_utxos_cache
                .set_expiration(Duration::from_secs(expiration));
        }
        if let Some(max) = max_num_inputs_in_transaction {
            self.max_num_inputs_in_transaction = max as usize;
        }
        if let Some(threshold) = utxo_consolidation_threshold {
            if threshold > self.max_num_inputs_in_transaction as u64 {
                self.utxo_consolidation_threshold = threshold as usize;
            } else {
                log!(
                    Priority::Info,
                    "Didn't set utxo_consolidation_threshold to {} (current value: {})",
                    threshold,
                    self.utxo_consolidation_threshold
                );
            }
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/ckbtc_minter.did (L247-287)
```text
// The upgrade parameters of the minter canister.
type UpgradeArgs = record {
    // The minimal amount of BTC that can be converted to ckBTC.
    // UTXOs with lower values will be ignored.
    deposit_btc_min_amount : opt nat64;

    // The minimal amount of ckBTC that the minter converts to BTC.
    retrieve_btc_min_amount : opt nat64;

    /// Maximum time in nanoseconds that a transaction should spend in the queue
    /// before being sent.
    max_time_in_queue_nanos : opt nat64;

    /// The minimum number of confirmations required for the minter to
    /// accept a Bitcoin transaction.
    min_confirmations : opt nat32;

    /// If set, overrides the current minter's operation mode.
    mode : opt Mode;

    /// The fee per Bitcoin check.
    check_fee : opt nat64;

    /// The fee paid per check by the KYT canister (deprecated, use check_fee instead).
    kyt_fee : opt nat64;

    /// The principal of the Bitcoin checker canister.
    btc_checker_principal : opt principal;

    /// The canister id of the KYT canister (deprecated, use btc_checker_principal instead).
    kyt_principal: opt principal;

    /// The expiration duration (in seconds) for cached entries in the get_utxos cache.
    get_utxos_cache_expiration_seconds: opt nat64;

    /// The minimum number of available UTXOs to trigger a consolidation.
    utxo_consolidation_threshold: opt nat64;

    /// The maximum number of input UTXOs allowed in a transaction.
    max_num_inputs_in_transaction: opt nat64;
};
```

**File:** rs/dogecoin/ckdoge/minter/src/lifecycle/upgrade.rs (L1-42)
```rust
use candid::{CandidType, Deserialize};
use ic_ckbtc_minter::lifecycle::upgrade::UpgradeArgs as CkbtcMinterUpgradeArgs;
use ic_ckbtc_minter::state::Mode;
use serde::Serialize;

#[derive(Clone, Eq, PartialEq, Debug, Default, CandidType, Deserialize, Serialize)]
pub struct UpgradeArgs {
    /// Minimum amount of doge that can be deposited.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deposit_doge_min_amount: Option<u64>,

    /// Minimum amount of doge that can be retrieved.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retrieve_doge_min_amount: Option<u64>,

    /// Specifies the minimum number of confirmations on the Dogecoin network
    /// required for the minter to accept a transaction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min_confirmations: Option<u32>,

    /// Maximum time in nanoseconds that a transaction should spend in the queue
    /// before being sent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_time_in_queue_nanos: Option<u64>,

    /// The mode in which the minter is running.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<Mode>,

    /// The expiration duration (in seconds) for cached entries in
    /// the get_utxos cache.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub get_utxos_cache_expiration_seconds: Option<u64>,

    /// The minimum number of available UTXOs required to trigger a consolidation.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub utxo_consolidation_threshold: Option<u64>,

    /// The maximum number of input UTXOs allowed in a transaction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_num_inputs_in_transaction: Option<u64>,
}
```

**File:** rs/bitcoin/ckbtc/minter/src/lifecycle/upgrade.rs (L66-110)
```rust
pub fn post_upgrade<R: CanisterRuntime>(upgrade_args: Option<UpgradeArgs>, runtime: &R) {
    if let Some(upgrade_args) = upgrade_args {
        log!(
            Priority::Info,
            "[upgrade]: updating configuration with {:?}",
            upgrade_args
        );
        record_event(EventType::Upgrade(upgrade_args), runtime);
    };

    let start = ic_cdk::api::instruction_counter();

    if let Some(removed) = migrate_old_events_if_not_empty() {
        log!(
            Priority::Info,
            "[upgrade]: {} empty events removed",
            removed
        )
    }
    log!(
        Priority::Info,
        "[upgrade]: replaying {} events",
        count_events()
    );

    let event_logger = runtime.event_logger();

    let state = event_logger
        .replay::<CheckInvariantsImpl>(event_logger.events_iter())
        .unwrap_or_else(|e| {
            ic_cdk::trap(format!("[upgrade]: failed to replay the event log: {e:?}"))
        });

    runtime.validate_config(&state);

    replace_state(state);

    let end = ic_cdk::api::instruction_counter();

    log!(
        Priority::Info,
        "[upgrade]: replaying events consumed {} instructions",
        end - start
    );
}
```
