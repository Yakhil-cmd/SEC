Audit Report

## Title
ckBTC Minter TOCTOU on `fee_based_retrieve_btc_min_amount` Locks User ckBTC in Minter-Controlled Withdrawal Account - (`rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`)

## Summary
The `retrieve_btc` flow requires users to pre-fund a minter-controlled withdrawal subaccount before calling `retrieve_btc`. Because `fee_based_retrieve_btc_min_amount` is re-read from live state at call time and updated asynchronously by the minter's timer, a Bitcoin fee spike between the user's query and their `retrieve_btc` call causes `AmountTooLow` to be returned without burning any ckBTC and without any automatic refund. The user's ckBTC is then locked in the minter-controlled account — inaccessible until the user acquires additional ckBTC to meet the new minimum or waits for fees to fall.

## Finding Description
The withdrawal account returned by `get_withdrawal_account()` is `Account { owner: ck_btc_principal, subaccount: Some(caller_subaccount) }` — owned by the minter canister, not the user. The user has no ICRC-1 authority over this account and cannot transfer ckBTC back out of it directly.

Inside `retrieve_btc`, the minimum is read from live state at lines 166–171 of `rs/bitcoin/ckbtc/minter/src/updates/retrieve_btc.rs`:

```rust
let (min_retrieve_amount, btc_network) =
    read_state(|s| (s.fee_based_retrieve_btc_min_amount, s.btc_network));
if args.amount < min_retrieve_amount {
    return Err(RetrieveBtcError::AmountTooLow(min_retrieve_amount));
}
```

This check fires before `burn_ckbtcs` is called (line 210), so no burn occurs and no ckBTC leaves the withdrawal account. The `fee_based_retrieve_btc_min_amount` field is updated asynchronously by the timer via `estimate_fee_per_vbyte` at lines 245–249 of `rs/bitcoin/ckbtc/minter/src/lib.rs`:

```rust
mutate_state(|s| {
    s.last_fee_per_vbyte = fees;
    s.last_median_fee_per_vbyte = Some(median_fee);
    s.fee_based_retrieve_btc_min_amount = fee_based_retrieve_btc_min_amount;
});
```

The minimum is computed in discrete 50,000-satoshi increments at lines 137–143 of `rs/bitcoin/ckbtc/minter/src/fees/mod.rs`, meaning a single timer tick can raise the minimum from 100,000 to 150,000 sats. Tests at lines 1831–1834 of `rs/bitcoin/ckbtc/minter/tests/tests.rs` confirm this jump occurs at a fee rate of 116 sat/vbyte, a realistic Bitcoin network condition.

There is no code path in `retrieve_btc` that refunds ckBTC from the withdrawal subaccount back to the user when `AmountTooLow` is returned. The `retrieve_btc_with_approval` path (lines 244–320) does not have this problem because no ckBTC is moved before the call.

## Impact Explanation
This is a moderate user-funds impact on an in-scope ck-token (ckBTC). A user following the documented two-step withdrawal flow can have their ckBTC locked in a minter-controlled account with no direct recovery path. The user is forced to either acquire additional ckBTC to meet the new minimum or wait indefinitely for Bitcoin fees to fall. The funds are not permanently destroyed but are inaccessible to the user without further expenditure. This matches the Medium allowed impact: "moderate user-funds/security impact."

## Likelihood Explanation
No privileged access, malicious actor, or special conditions are required. Any user following the standard `retrieve_btc` flow is exposed to this race window during Bitcoin fee spikes, which are common and unpredictable (e.g., inscription/ordinal activity). The minter's timer fires periodically, and the 50,000-satoshi discrete jump makes it easy for an exact transfer to fall below the new threshold after a single timer cycle.

## Recommendation
1. **Automatic refund on `AmountTooLow`**: When `retrieve_btc` rejects with `AmountTooLow`, issue an ICRC-1 transfer from the withdrawal subaccount back to the caller's principal account before returning the error.
2. **Prefer and document `retrieve_btc_with_approval`**: The ICRC-2 approval path does not require pre-funding a minter-controlled account and is immune to this race condition. It should be the recommended path in documentation.
3. **Snapshot minimum at transfer time**: Record `fee_based_retrieve_btc_min_amount` when ckBTC arrives in the withdrawal account and honor it for at least one timer cycle.

## Proof of Concept
```
1. Call get_minter_info() → retrieve_btc_min_amount = 100_000 sats
2. Call get_withdrawal_account() → withdrawal_account (minter-owned subaccount)
3. Call ckBTC ledger icrc1_transfer(to=withdrawal_account, amount=100_000)
   → ckBTC locked in minter-controlled subaccount
4. Bitcoin fee rate crosses 116 sat/vbyte threshold; minter timer fires:
   fee_based_retrieve_btc_min_amount updated to 150_000
5. Call retrieve_btc(amount=100_000, address="bc1q...")
   → read_state: min_retrieve_amount = 150_000
   → 100_000 < 150_000 → return Err(AmountTooLow(150_000))
   → No burn, no refund
6. User's 100_000 sats ckBTC is locked; user must acquire 50_000 more sats
   and retry with retrieve_btc(150_000, ...) or wait for fees to drop.
```
This is reproducible as a deterministic integration test using `CkBtcSetup`, `set_fee_percentiles`, and `refresh_fee_percentiles` as shown in `rs/bitcoin/ckbtc/minter/tests/tests.rs` lines 1831–1834.