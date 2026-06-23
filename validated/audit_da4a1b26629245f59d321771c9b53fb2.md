### Title
Stale Cached ICP/XDR Rate in CMC Enables Cycles Over-Minting During Price Spikes — (`rs/nns/cmc/src/main.rs`, `rs/nns/cmc/src/exchange_rate_canister.rs`)

---

### Summary

The Cycles Minting Canister (CMC) caches the ICP/XDR conversion rate and refreshes it at most once every five minutes via a heartbeat-driven poll of the Exchange Rate Canister (XRC). The cached spot rate is used directly and atomically in `tokens_to_cycles()` to determine how many cycles to mint per ICP burned. During the staleness window, if the real ICP market price rises materially, any unprivileged user can call `notify_top_up` or `notify_create_canister` and receive more cycles than the current market rate warrants, constituting a cycles conservation bug analogous to the DSR oracle out-of-sync conversion factor.

---

### Finding Description

**Cached rate and its use in minting**

`tokens_to_cycles()` reads `state.icp_xdr_conversion_rate` — a single cached `IcpXdrConversionRate` struct — and multiplies it by `cycles_per_xdr` to compute the cycles to mint: [1](#0-0) 

The cached rate is the only input; there is no freshness check, no TWAP, and no circuit-breaker.

**Update cadence**

The rate is refreshed by the canister heartbeat, which calls `update_exchange_rate()`. The guard inside `UpdateExchangeRateGuard::new()` enforces a minimum interval of `REFRESH_RATE_INTERVAL_SECONDS = 5 * ONE_MINUTE_SECONDS` (300 seconds) between successful polls: [2](#0-1) [3](#0-2) 

After a successful update the next attempt is scheduled at the next 5-minute boundary: [4](#0-3) 

**The staleness window**

Between two consecutive successful heartbeat polls the CMC holds a fixed snapshot of the ICP/XDR rate. If the real market price of ICP rises during this window, the CMC's cached rate is stale-low: it still reflects the old (lower) price. Any `notify_top_up` or `notify_create_canister` call processed during this window converts ICP at the old rate, minting more cycles than the current market price of ICP warrants.

The heartbeat is triggered by the replica, not by the user, so the attacker cannot force the update. However, the attacker can observe the XRC rate (a public query canister) and time their ICP-to-cycles conversion to land inside the staleness window before the next heartbeat fires.

**State struct confirming the single cached value** [5](#0-4) 

---

### Impact Explanation

Every `notify_top_up` and `notify_create_canister` call during a staleness window where the real ICP price is above the cached rate results in cycles being minted at a below-market ICP price. The excess cycles represent value extracted from the protocol: the ICP is burned at the old rate, but the cycles minted are worth more in XDR terms than the ICP destroyed. The magnitude scales with:

- The percentage price increase of ICP during the window (up to ~5 minutes)
- The volume of ICP converted during the window

For example, a 5 % ICP price spike over 5 minutes with 10 000 ICP converted yields ~500 ICP worth of excess cycles minted. Cycles are consumed by canisters and cannot be directly sold, but they can be used to subsidize computation or transferred to canisters that are then sold, so the value is not entirely illiquid.

---

### Likelihood Explanation

ICP is a liquid, exchange-traded asset. 5 % intra-5-minute moves occur during news events, large trades, or broader crypto market volatility. The XRC is a public query endpoint; any observer can detect a rate change before the next CMC heartbeat fires. The attack requires no privileged access: any principal can call `notify_top_up` with a valid ICP ledger block index. The attack is therefore realistic for a sophisticated market participant who monitors the XRC and the CMC heartbeat schedule.

---

### Recommendation

1. **Reduce the polling interval** or switch to a push model where the XRC notifies the CMC on significant rate changes, reducing the maximum staleness window.
2. **Add a freshness guard in `tokens_to_cycles()`**: reject conversions (or apply a conservative fallback rate) if `now - icp_xdr_conversion_rate.timestamp_seconds` exceeds a threshold (e.g., 10 minutes).
3. **Use the 30-day moving average** (`average_icp_xdr_conversion_rate`) for cycle minting instead of the spot rate. The average is far less susceptible to short-term price spikes and is already computed and certified by the CMC.
4. **Emit a metric** for rate age so operators can detect and alert on prolonged staleness.

---

### Proof of Concept

```
1. Query XRC: observe ICP/XDR spot rate R_real.
2. Query CMC get_icp_xdr_conversion_rate: observe cached rate R_cached.
3. If R_real > R_cached (ICP price rose since last CMC heartbeat):
   a. Transfer N ICP to CMC subaccount with MEMO_TOP_UP_CANISTER.
   b. Call notify_top_up(block_index, target_canister_id).
   c. CMC calls tokens_to_cycles(N) using R_cached (stale-low).
   d. Cycles minted = N * R_cached * cycles_per_xdr / 1e8 / 1e4
      (excess vs. fair value = N * (R_real - R_cached) * cycles_per_xdr / 1e8 / 1e4)
4. Wait for next CMC heartbeat to update R_cached to R_real.
   The cycles already minted at the old rate are not clawed back.
```

The profit per ICP is proportional to `(R_real - R_cached) / R_cached`, which equals the percentage ICP price increase during the staleness window. With a 5 % move and 10 000 ICP, the attacker receives ~500 ICP-equivalent in excess cycles.

### Citations

**File:** rs/nns/cmc/src/main.rs (L217-218)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,
```

**File:** rs/nns/cmc/src/main.rs (L1900-1923)
```rust
fn tokens_to_cycles(amount: Tokens) -> Result<Cycles, NotifyError> {
    with_state(|state| {
        let xdr_permyriad_per_icp = state
            .icp_xdr_conversion_rate
            .as_ref()
            .map(|rate| rate.xdr_permyriad_per_icp);
        match xdr_permyriad_per_icp {
            Some(xdr_permyriad_per_icp) => Ok(TokensToCycles {
                xdr_permyriad_per_icp,
                cycles_per_xdr: state.cycles_per_xdr,
            }
            .to_cycles(amount)),
            None => {
                let error_message =
                    "No conversion rate found in CMC, notification aborted".to_string();
                print(&error_message);
                Err(NotifyError::Other {
                    error_code: NotifyErrorCode::Internal as u64,
                    error_message,
                })
            }
        }
    })
}
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L106-112)
```rust
        if let UpdateExchangeRateState::GetRateAt(next_attempt_seconds) = current_call_state
            && current_minute_in_seconds < next_attempt_seconds
        {
            return Err(UpdateExchangeRateError::NotReadyToGetRate(
                next_attempt_seconds,
            ));
        }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L141-148)
```rust
            match result {
                Ok(_) => {
                    state.update_exchange_rate_canister_state.replace(
                        UpdateExchangeRateState::get_rate_at_next_refresh_rate_interval(
                            self.current_minute_in_seconds,
                        ),
                    );
                }
```
