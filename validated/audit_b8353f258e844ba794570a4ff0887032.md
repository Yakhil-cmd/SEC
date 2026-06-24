Audit Report

## Title
`get_allowances` returns allowances for all subaccounts of a principal due to missing subaccount equality check — (`rs/ledger_suite/icrc1/ledger/src/lib.rs`)

## Summary
`get_allowances` iterates a `BTreeMap` range anchored at the queried `from` account but only breaks when `account_spender.account.owner != from.owner`, never when the subaccount differs. Because `AccountSpender` ordering is `(owner, effective_subaccount, spender)`, a query for `{owner: X, subaccount: None}` (effective `[0x00;32]`, the minimum) will traverse and return every stored allowance for every subaccount of owner X. Any unprivileged caller can enumerate all allowances across all subaccounts of any target principal with a single query call.

## Finding Description
`AccountSpender::cmp` delegates to `Account::cmp`, which orders by `(owner, effective_subaccount())`:

```
// packages/icrc-ledger-types/src/icrc1/account.rs L54-60
self.owner.cmp(&other.owner).then_with(|| {
    self.effective_subaccount().cmp(other.effective_subaccount())
})
```

`effective_subaccount()` returns `subaccount.unwrap_or(&[0u8; 32])`, so `None` maps to `[0x00;32]`, the lexicographic minimum.

In `get_allowances` (`rs/ledger_suite/icrc1/ledger/src/lib.rs` L1194–1222), the range `start_account_spender..` starts at `(owner: X, subaccount: [0x00;32], spender: ∅)`. The only loop termination guard on the account is:

```rust
// L1204-1206
if account_spender.account.owner != from.owner {
    break;
}
```

There is no guard checking `account_spender.account != from`. Any stored entry `(owner: X, subaccount: S, spender: *)` where `S >= [0x00;32]` (i.e., every subaccount) satisfies the range and passes the owner check. The loop collects and returns all of them regardless of which subaccount was queried.

The `icrc103_get_allowances` entrypoint (`rs/ledger_suite/icrc1/ledger/src/main.rs` L1214–1231) is a `#[query]` with no access control, callable by any principal.

## Impact Explanation
An attacker can enumerate the complete set of active allowances — including amounts, spenders, and expiry times — for every subaccount of any target principal. Subaccounts are commonly used to isolate financial positions (e.g., DeFi vaults, exchange deposit addresses). This bug collapses that isolation: a single query with `{owner: X, subaccount: None}` reveals all allowances across all of X's subaccounts, violating the intended per-account scoping of `icrc103_get_allowances`. This constitutes a significant ledger information-disclosure impact with concrete user harm, matching the High impact class: "Significant... ledger... security impact with concrete user or protocol harm."

## Likelihood Explanation
The attack requires no tokens, no approvals, no privileged role, and no prior knowledge of the victim's subaccounts. It is a single unauthenticated `#[query]` call. It is repeatable against any principal on any deployed ICRC ledger instance (ICP ledger, ckBTC, ckETH, ckERC20, SNS ledgers). Likelihood is high.

## Recommendation
Add a subaccount equality guard immediately after the owner check in `get_allowances`:

```rust
if account_spender.account.owner != from.owner {
    break;
}
// Add:
if account_spender.account != from {
    break;
}
```

Because `AccountSpender` is ordered by `(owner, subaccount, spender)`, once `account_spender.account > from` (i.e., the subaccount has advanced past `from`'s subaccount), all subsequent entries will also have a larger account, so breaking is correct and complete.

## Proof of Concept
1. Owner X calls `icrc2_approve(from={owner:X, subaccount:[0xFF;32]}, spender:A, amount:1_000_000)`. This stores `AccountSpender{account:{owner:X, subaccount:[0xFF;32]}, spender:A}` in `ALLOWANCES_MEMORY`.
2. Attacker (any principal) calls `icrc103_get_allowances({from_account:{owner:X, subaccount:None}, prev_spender:None, take:None})`.
3. `start_account_spender` is built as `{account:{owner:X, subaccount:None}, spender:{owner:Principal::from_slice(&[]), subaccount:None}}` — the minimum key for owner X.
4. The BTreeMap range `start_account_spender..` includes the stored entry `(X, [0xFF;32], A)` because `[0xFF;32] >= [0x00;32]` and owner matches.
5. The loop does not break (owner check passes), and pushes `Allowance103{from_account:{owner:X, subaccount:[0xFF;32]}, to_spender:A, allowance:1_000_000}` into the result.
6. The response contains an allowance for a subaccount the caller never queried, confirming the invariant `∀ a ∈ response: a.from_account == queried_from_account` is violated.

A deterministic integration test or PocketIC test can reproduce this by: creating a ledger, approving from a non-default subaccount, then asserting that `icrc103_get_allowances` called with the default subaccount returns an empty list (it will not, proving the bug).