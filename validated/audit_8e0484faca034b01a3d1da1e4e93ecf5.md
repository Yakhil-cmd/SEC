Audit Report

## Title
Single-Principal Multi-Subaccount Guard Exhaustion DoS in `retrieve_doge_with_approval` / `retrieve_btc_with_approval` — (`rs/bitcoin/ckbtc/minter/src/guard.rs`, `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, `rs/dogecoin/ckdoge/minter/src/main.rs`)

## Summary

`retrieve_doge_with_approval` in the ckDOGE minter delegates directly to `retrieve_btc_with_approval` in the shared ckBTC minter library. That function keys its concurrency guard on the full `Account{owner, subaccount}` rather than on the principal alone. Because the shared `BTreeSet<Account>` guard has a hard cap of `MAX_CONCURRENT = 100` slots across all callers, a single unprivileged principal can exhaust all 100 slots using 100 distinct `from_subaccount` values, causing every other user's withdrawal call to return `TemporarilyUnavailable("too many concurrent requests")` — a complete DoS on the ckDOGE withdrawal path.

## Finding Description

`retrieve_doge_with_approval` in `rs/dogecoin/ckdoge/minter/src/main.rs` (lines 131–143) calls `ic_ckbtc_minter::updates::retrieve_btc::retrieve_btc_with_approval` directly:

```rust
// rs/dogecoin/ckdoge/minter/src/main.rs:135-141
let result = ic_ckbtc_minter::updates::retrieve_btc::retrieve_btc_with_approval(
    args.into(),
    &DOGECOIN_CANISTER_RUNTIME,
)
.await
.map(RetrieveDogeOk::from)
.map_err(RetrieveDogeWithApprovalError::from);
```

Inside `retrieve_btc_with_approval` (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`, lines 259–263), the guard is keyed on the full account including the caller-supplied `from_subaccount`:

```rust
let caller_account = Account {
    owner: caller,
    subaccount: args.from_subaccount,
};
let _guard = retrieve_btc_guard(caller_account)?;
```

The guard implementation (`rs/bitcoin/ckbtc/minter/src/guard.rs`, lines 45–60) inserts the account into a global `BTreeSet<Account>` and rejects new entries once `len() >= MAX_CONCURRENT` (100):

```rust
if accounts.len() >= MAX_CONCURRENT {
    return Err(GuardError::TooManyConcurrentRequests);
}
accounts.insert(account);
```

The guard is acquired at line 263 and is held across two subsequent `await` points: `check_address(...).await` (lines 283–307) and `burn_ckbtcs_icrc2(...).await` (lines 314–319). Because the IC yields at every `await`, an attacker can have 100 concurrent in-flight calls — each with a distinct `from_subaccount` — all holding a guard slot simultaneously.

No ckDOGE balance is required to hold the guard. `InsufficientAllowance`/`InsufficientFunds` errors only arise from `burn_ckbtcs_icrc2` (line 314), which executes *after* the guard is already held. The attacker only needs syntactically valid requests (valid DOGE address, `amount >= min_retrieve_amount`).

The existing `MAX_CONCURRENT_PENDING_REQUESTS = 5000` check (line 274) is a separate guard on the completed withdrawal queue and does not mitigate this issue.

The existing unit test `guard_prevents_more_than_max_concurrent_accounts` (`rs/bitcoin/ckbtc/minter/src/guard.rs`, lines 141–162) explicitly demonstrates that a single principal with 50 distinct subaccounts can hold 50 guard slots simultaneously, confirming the attack surface.

## Impact Explanation

Once all 100 guard slots are occupied, every other user calling `retrieve_doge_with_approval` receives `TemporarilyUnavailable("too many concurrent requests")`. This is a complete, sustained denial-of-service on the ckDOGE withdrawal path — a financial integration (Chain Fusion / ck-token) that is explicitly in scope. The attacker can sustain the attack by continuously re-submitting calls as old ones complete. This matches the **High ($2,000–$10,000)** impact: "Application/platform-level DoS … not based on raw volumetric DDoS" and "Significant Chain Fusion, ck-token … security impact with concrete user or protocol harm."

## Likelihood Explanation

- Requires no privileged access, no ckDOGE balance, and no governance majority.
- Requires only 100 concurrent update calls with distinct `from_subaccount` byte arrays — trivially scriptable.
- The IC's per-canister message queue accommodates 100 concurrent in-flight update calls.
- The attack is free: calls fail at `burn_ckbtcs_icrc2` with `InsufficientAllowance`, but the guard is already held at that point.
- Sustained DoS is achievable by looping the attack.

## Recommendation

Key the guard on `owner` (principal) only, not on the full `Account{owner, subaccount}`, restoring the original one-pending-withdrawal-per-principal intent:

```rust
let _guard = retrieve_btc_guard(Account {
    owner: caller,
    subaccount: None, // normalize to principal-only
})?;
```

Alternatively, enforce a per-principal cap on the number of concurrent guard slots regardless of subaccount, or increase `MAX_CONCURRENT` while adding per-principal rate limiting. Note that the legacy `retrieve_btc` path already uses `subaccount: None` for its guard (line 162–165), so the fix is consistent with existing practice.

## Proof of Concept

State-machine test (no mainnet required):

1. Initialize the ckDOGE minter with default settings (`MAX_CONCURRENT = 100`).
2. From a single attacker principal, send 100 concurrent `retrieve_doge_with_approval` calls, each with a distinct `from_subaccount` byte array (`[i; 32]` for `i` in `0..100`), a valid DOGE address, and `amount = min_retrieve_amount`. No `icrc2_approve` needed.
3. Each call passes the guard check (distinct `Account` keys), acquires a slot, and suspends at `check_address(...).await`.
4. Assert: `retrieve_btc_accounts.len() == 100`.
5. From a different principal (victim), call `retrieve_doge_with_approval`.
6. Assert: response is `Err(TemporarilyUnavailable("too many concurrent requests"))`.
7. Assert: victim's call never reaches the ledger.

This maps directly to `GuardError::TooManyConcurrentRequests` → `RetrieveBtcWithApprovalError::TemporarilyUnavailable` → `RetrieveDogeWithApprovalError` at `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs` lines 123–131.