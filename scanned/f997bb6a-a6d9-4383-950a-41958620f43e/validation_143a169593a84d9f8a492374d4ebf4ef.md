### Title
Unprivileged Callers Can Drain ckBTC Minter Cycles via `update_balance` with Arbitrary Owner Principals - (File: `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs`)

---

### Summary

The ckBTC minter's `update_balance` endpoint allows any non-anonymous ingress caller to trigger expensive Bitcoin canister `get_utxos` calls paid entirely from the minter's own cycle balance, for any arbitrary `owner` principal. With no per-caller rate limiting and a global concurrency cap of only 100 simultaneous requests, a malicious actor can continuously exhaust the minter's cycles, eventually causing the minter to be uninstalled and permanently disrupting ckBTC deposit and withdrawal operations.

---

### Finding Description

The `update_balance` function in `rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` is a public `#[update]` endpoint that accepts an optional `owner: Option<Principal>` argument. When `owner` is set, the minter derives the Bitcoin deposit address for that arbitrary principal and calls the Bitcoin canister's `get_utxos` on its behalf — paying the cycle cost from the minter's own balance. [1](#0-0) 

The guard protecting this endpoint is `balance_update_guard(caller_account)`, which is keyed on the resolved `caller_account` (i.e., `args.owner.unwrap_or(caller)`). It prevents the same account from being processed concurrently and caps total concurrent requests at `MAX_CONCURRENT = 100`. [2](#0-1) 

A single attacker principal can call `update_balance` with 100 distinct `owner` values simultaneously. Each call resolves to a different Bitcoin address, producing a different `GetUtxosRequest`, and therefore a cache miss in the `get_utxos_cache`. Each cache miss results in a real call to the Bitcoin canister, paid by the minter. [3](#0-2) 

Critically, when no new UTXOs are found (the common case for any fresh or random address), the function makes a **second** `get_utxos` call with `min_confirmations=0` to report pending UTXOs to the caller. This doubles the cycle cost per invocation. [4](#0-3) 

After the 100 concurrent slots are released, the attacker can immediately submit another 100. There is no cooldown, no per-caller quota, and no cycle fee charged to the caller. The minter's cycle balance is the only resource being consumed.

The `check_anonymous_caller()` guard in `main.rs` is the only access control — any non-anonymous principal (trivially obtained) can call this endpoint. [5](#0-4) 

---

### Impact Explanation

If the minter's cycle balance is exhausted, the IC runtime will uninstall the canister. This permanently destroys the minter's state, including all pending `retrieve_btc` requests and the mapping of known UTXOs. Consequences include:

- **Loss of BTC**: Users who have sent BTC to their deposit address but have not yet had it minted as ckBTC lose access to those funds.
- **Loss of pending withdrawals**: In-flight `retrieve_btc` requests that have not yet been submitted to the Bitcoin network are lost.
- **Protocol-wide DOS**: All ckBTC deposit and withdrawal operations cease until the minter is redeployed via NNS governance, which takes days.

This matches the external report's impact class: a shared resource (cycles, analogous to ETH) held by a protocol canister is drained by an unprivileged user triggering many small operations, causing core protocol functions to fail and resulting in asset loss.

---

### Likelihood Explanation

The attack requires only a non-anonymous IC principal (free to create) and the ability to send ingress messages. No tokens, no stake, no privileged access. The attacker pays only the ingress induction fee (charged to the minter, not the caller, on the IC). The attack is repeatable in a tight loop. The minter's cycle balance is finite and must be topped up manually; if the drain rate exceeds the top-up rate, the minter is eventually uninstalled.

---

### Recommendation

1. **Charge the caller**: Require callers to attach cycles to `update_balance` calls sufficient to cover the Bitcoin canister `get_utxos` cost. Refund any excess on success.
2. **Per-caller rate limiting**: Track the last call time per caller principal and enforce a minimum interval (e.g., 60 seconds) between calls from the same principal.
3. **Restrict arbitrary `owner`**: Require that `args.owner`, if set, equals the caller's principal, or require a separate privileged role to call on behalf of others.
4. **Eliminate the second `get_utxos` call**: When no new UTXOs are found, return `NoNewUtxos` without making the second zero-confirmation query, or serve it from cache.

---

### Proof of Concept

```
Attacker principal P calls update_balance 100 times concurrently with:
  owner = [P_1, P_2, ..., P_100]  (100 distinct fresh principals)

Each call:
  1. Derives a unique Bitcoin address for P_i
  2. Calls bitcoin_get_utxos(address_i, min_confirmations=6) → cache miss → minter pays cycles
  3. Finds 0 UTXOs → calls bitcoin_get_utxos(address_i, min_confirmations=0) → cache miss → minter pays cycles
  4. Returns NoNewUtxos

After all 100 complete, attacker immediately repeats with 100 new principals.
Each round costs the minter 200 Bitcoin canister calls worth of cycles.
Repeat until minter cycle balance reaches 0 → minter uninstalled.
``` [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L143-183)
```rust
/// Notifies the ckBTC minter to update the balance of the user subaccount.
pub async fn update_balance<R: CanisterRuntime>(
    args: UpdateBalanceArgs,
    runtime: &R,
) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    let caller = runtime.caller();
    if args.owner.unwrap_or(caller) == runtime.id() {
        ic_cdk::trap("cannot update minter's balance");
    }

    // Record start time of method execution for metrics
    let start_time = runtime.time();

    // When the minter is in the mode using a whitelist we only want a certain
    // set of principal to be able to mint. But we also want those principals
    // to mint at any desired address. Therefore, the check below is on "caller".
    state::read_state(|s| s.mode.is_deposit_available_for(&caller))
        .map_err(UpdateBalanceError::TemporarilyUnavailable)?;

    init_ecdsa_public_key().await;

    let caller_account = Account {
        owner: args.owner.unwrap_or(caller),
        subaccount: args.subaccount,
    };
    let _guard = balance_update_guard(caller_account)?;

    let address = state::read_state(|s| runtime.derive_user_address(s, &caller_account));

    let (btc_network, min_confirmations) =
        state::read_state(|s| (s.btc_network, s.min_confirmations));

    let utxos = get_utxos(
        btc_network,
        &address,
        min_confirmations,
        CallSource::Client,
        runtime,
    )
    .await?
    .utxos;
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs (L217-265)
```rust
    if satoshis_to_mint == 0 {
        // We bail out early if there are no UTXOs to avoid creating a new entry
        // in the UTXOs map. If we allowed empty entries, malicious callers
        // could exhaust the canister memory.

        // We get the entire list of UTXOs again with a zero
        // confirmation limit so that we can indicate the approximate
        // wait time to the caller.
        let GetUtxosResponse {
            tip_height,
            mut utxos,
            ..
        } = get_utxos(
            btc_network,
            &address,
            /*min_confirmations=*/ 0,
            CallSource::Client,
            runtime,
        )
        .await?;

        utxos.retain(|u| {
            tip_height
                < u.height
                    .checked_add(min_confirmations)
                    .expect("bug: this shouldn't overflow")
                    .checked_sub(1)
                    .expect("bug: this shouldn't underflow")
        });
        let pending_utxos: Vec<PendingUtxo> = utxos
            .iter()
            .map(|u| PendingUtxo {
                outpoint: u.outpoint.clone(),
                value: u.value,
                confirmations: tip_height - u.height + 1,
            })
            .collect();

        let current_confirmations = pending_utxos.iter().map(|u| u.confirmations).max();

        observe_update_call_latency(0, start_time, runtime.time());

        return Err(UpdateBalanceError::NoNewUtxos {
            current_confirmations,
            required_confirmations: min_confirmations,
            pending_utxos: Some(pending_utxos),
            suspended_utxos: Some(suspended_utxos),
        });
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/guard.rs (L1-60)
```rust
use crate::state::{CkBtcMinterState, mutate_state};
use icrc_ledger_types::icrc1::account::Account;
use std::collections::BTreeSet;
use std::marker::PhantomData;

const MAX_CONCURRENT: usize = 100;

#[derive(Eq, PartialEq, Debug)]
pub enum GuardError {
    AlreadyProcessing,
    TooManyConcurrentRequests,
}

pub trait PendingRequests {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account>;
}

pub struct PendingBalanceUpdates;

impl PendingRequests for PendingBalanceUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.update_balance_accounts
    }
}
pub struct RetrieveBtcUpdates;

impl PendingRequests for RetrieveBtcUpdates {
    fn pending_requests(state: &mut CkBtcMinterState) -> &mut BTreeSet<Account> {
        &mut state.retrieve_btc_accounts
    }
}

/// Guards a block from executing twice when called by the same user and from being
/// executed [MAX_CONCURRENT] or more times in parallel.
#[must_use]
pub struct Guard<PR: PendingRequests> {
    account: Account,
    _marker: PhantomData<PR>,
}

impl<PR: PendingRequests> Guard<PR> {
    /// Attempts to create a new guard for the current block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    pub fn new(account: Account) -> Result<Self, GuardError> {
        mutate_state(|s| {
            let accounts = PR::pending_requests(s);
            if accounts.contains(&account) {
                return Err(GuardError::AlreadyProcessing);
            }
            if accounts.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            accounts.insert(account);
            Ok(Self {
                account,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/management.rs (L129-157)
```rust
    async fn bitcoin_get_utxos<R: CanisterRuntime>(
        now: &mut u64,
        req: GetUtxosRequest,
        source: CallSource,
        runtime: &R,
    ) -> Result<GetUtxosResponse, CallError> {
        match source {
            CallSource::Client => &crate::metrics::GET_UTXOS_CLIENT_CALLS,
            CallSource::Minter => &crate::metrics::GET_UTXOS_MINTER_CALLS,
        }
        .with(|cell| cell.set(cell.get() + 1));
        if let Some(res) = crate::state::read_state(|s| s.get_utxos_cache.get(&req, *now).cloned())
        {
            crate::metrics::GET_UTXOS_CACHE_HITS.with(|cell| cell.set(cell.get() + 1));
            Ok(res)
        } else {
            crate::metrics::GET_UTXOS_CACHE_MISSES.with(|cell| cell.set(cell.get() + 1));
            runtime.get_utxos(&req).await.inspect(|res| {
                *now = runtime.time();
                crate::state::mutate_state(|s| {
                    if s.last_get_utxos_tip_height != Some(res.tip_height) {
                        s.get_utxos_cache.clear();
                        s.last_get_utxos_tip_height = Some(res.tip_height)
                    }
                    s.get_utxos_cache.insert(req, res.clone(), *now)
                })
            })
        }
    }
```

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L196-200)
```rust
#[update]
async fn update_balance(args: UpdateBalanceArgs) -> Result<Vec<UtxoStatus>, UpdateBalanceError> {
    check_anonymous_caller();
    check_postcondition(updates::update_balance::update_balance(args, &IC_CANISTER_RUNTIME).await)
}
```
