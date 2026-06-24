Audit Report

## Title
DoS via `update_balance` Guard Slot Exhaustion in ckBTC Minter - (File: `rs/bitcoin/ckbtc/minter/src/guard.rs`)

## Summary
The ckBTC minter enforces a global cap of `MAX_CONCURRENT = 100` concurrent `update_balance` calls via a `Guard<PendingBalanceUpdates>` keyed on `Account` (owner + subaccount). Because `update_balance` accepts a caller-supplied `args.owner` and `args.subaccount`, an unprivileged attacker can generate 100 distinct `Account` values, fill all 100 guard slots, and hold them for the duration of the `get_utxos` Bitcoin-canister round-trip. Every subsequent `update_balance` call from a legitimate user is rejected with `TooManyConcurrentRequests` / `TemporarilyUnavailable`, blocking all ckBTC minting for the duration of the attack.

## Finding Description
`Guard::<PendingBalanceUpdates>::new` in `rs/bitcoin/ckbtc/minter/src/guard.rs` (L45–60) inserts the caller's `Account` into `state.update_balance_accounts` and rejects any new call once the set reaches `MAX_CONCURRENT = 100`:

```rust
// guard.rs L6, L51-53
const MAX_CONCURRENT: usize = 100;
if accounts.len() >= MAX_CONCURRENT {
    return Err(GuardError::TooManyConcurrentRequests);
}
accounts.insert(account);
```

The guard is acquired in `update_balance` (`rs/bitcoin/ckbtc/minter/src/updates/update_balance.rs` L168) **after** `init_ecdsa_public_key().await` (L162) but **before** the `get_utxos` inter-canister call (L175–183):

```rust
let _guard = balance_update_guard(caller_account)?;   // L168 – slot acquired
// ...
let utxos = get_utxos(btc_network, &address, min_confirmations,
                      CallSource::Client, runtime).await?;  // L175 – slot held here
```

`init_ecdsa_public_key` returns immediately once the key is cached (`get_btc_address.rs` L53–55), so it does not protect against slot exhaustion. The `Drop` implementation (`guard.rs` L63–67) releases the slot only when `update_balance` returns — there is no timeout or expiry.

`UpdateBalanceArgs` exposes both `owner: Option<Principal>` and `subaccount: Option<Subaccount>` (`update_balance.rs` L38–41). The `caller_account` is constructed directly from these fields (`update_balance.rs` L164–167), so a single attacker principal can trivially produce 100 distinct `Account` values by varying `subaccount`, each occupying a separate guard slot.

No per-principal quota, no pre-guard UTXO existence check, and no slot timeout exist anywhere in the guard or `update_balance` logic.

## Impact Explanation
`update_balance` is the sole user-facing mechanism for minting ckBTC after a BTC deposit. Exhausting all 100 guard slots prevents any new `update_balance` call from succeeding, halting all ckBTC minting for the duration of the attack. This is a concrete **application/platform-level DoS on a financial integration (ckBTC)** with direct user harm: depositors cannot advance their UTXO state and receive `TemporarilyUnavailable` errors. This matches the **High ($2,000–$10,000)** allowed impact: *"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS"* and *"Significant Chain Fusion, ck-token … security impact with concrete user or protocol harm."*

## Likelihood Explanation
The attack requires no BTC, no ckBTC, and no governance power — only cycles sufficient to sustain 100 concurrent ingress calls. The Bitcoin canister's `get_utxos` round-trip spans multiple IC execution rounds (several seconds), giving the attacker a comfortable window to re-fill slots as old calls complete. The attack is repeatable indefinitely and reachable by any unprivileged ingress sender.

## Recommendation
- **Short term**: Before acquiring the guard, perform a cheap synchronous check (e.g., verify the derived address has at least one known UTXO in local state, or enforce a per-principal slot quota) so that accounts with no on-chain activity are rejected without holding a slot.
- **Long term**: Introduce a guard-slot timeout so that slots held beyond a configurable threshold are automatically released. Alternatively, raise `MAX_CONCURRENT` and add per-principal (not per-account) quotas to increase the cost of exhaustion proportionally.

## Proof of Concept
1. Attacker generates 100 accounts: `Account { owner: attacker_principal, subaccount: Some([i; 32]) }` for `i` in `0..100`.
2. Attacker sends 100 concurrent `update_balance` calls, each with `args.owner = Some(attacker_principal)` and `args.subaccount = Some([i; 32])`.
3. Each call passes the mode check, calls `init_ecdsa_public_key` (returns immediately after first call), acquires a guard slot, then suspends at `get_utxos(...).await`.
4. `update_balance_accounts.len()` reaches 100.
5. Any new `update_balance` call hits `accounts.len() >= MAX_CONCURRENT` → `GuardError::TooManyConcurrentRequests` → `UpdateBalanceError::TemporarilyUnavailable`.
6. As old calls complete, the attacker immediately re-submits to refill slots, sustaining the blockade.

A deterministic unit test can reproduce this using the existing `guard_prevents_more_than_max_concurrent_accounts` test pattern (`guard.rs` L141–162), extended to use subaccounts of a single principal and verify that a 101st call from a distinct legitimate account is rejected.