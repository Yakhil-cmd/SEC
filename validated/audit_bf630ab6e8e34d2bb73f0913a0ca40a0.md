### Title
Missing Freshness Check on `icp_xdr_conversion_rate` in `tokens_to_cycles()` Enables Stale-Rate Arbitrage During XRC Outage - (File: `rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) function `tokens_to_cycles()` converts ICP to cycles using `state.icp_xdr_conversion_rate` without validating the timestamp of that rate. If the Exchange Rate Canister (XRC) becomes unavailable or persistently returns errors, the CMC continues to mint cycles at an arbitrarily stale ICP/XDR rate indefinitely. Any unprivileged user can exploit this by timing `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` calls against the outdated rate.

---

### Finding Description

The CMC refreshes its `icp_xdr_conversion_rate` every 5 minutes via heartbeat by calling the XRC. The refresh interval is defined as: [1](#0-0) 

When the XRC call fails (e.g., XRC canister is being upgraded, is unavailable, or returns persistent errors), the CMC schedules a retry at the next minute but **does not invalidate or age-gate the existing stored rate**: [2](#0-1) 

The stored rate is then consumed by `tokens_to_cycles()`, which reads only `xdr_permyriad_per_icp` and performs no check on `timestamp_seconds`: [3](#0-2) 

This function is called unconditionally by all three public ICP-to-cycles conversion paths: [4](#0-3) [5](#0-4) [6](#0-5) 

The `do_set_icp_xdr_conversion_rate` function only enforces monotonicity (new timestamp > current timestamp) but imposes no maximum age on the stored rate: [7](#0-6) 

There is no point in the `notify_top_up`, `notify_mint_cycles`, or `notify_create_canister` call paths where the age of `icp_xdr_conversion_rate` is compared against `now()` before the conversion is executed.

---

### Impact Explanation

**Vulnerability class:** cycles/resource accounting bug (stale oracle rate used for ICP→cycles minting without freshness check).

If the XRC is unavailable for an extended period (e.g., during a canister upgrade, a subnet stall, or persistent XRC errors), the CMC's `icp_xdr_conversion_rate` becomes stale. During this window:

- If ICP's market price **drops** while the stale rate is **higher** than the current market rate, an attacker can buy ICP cheaply on the open market and convert it to cycles at the inflated stale rate, receiving more cycles per ICP than the protocol should issue. This extracts value from the protocol's cycle economy (cycles are backed by XDR value; issuing excess cycles dilutes that backing).
- If ICP's market price **rises** while the stale rate is **lower**, users are disadvantaged (receive fewer cycles per ICP), but this is a user-harm rather than a protocol-extraction scenario.

The impact is a **ledger conservation / cycles accounting bug**: the total cycles minted per unit of ICP burned diverges from the intended XDR-pegged rate, allowing arbitrageurs to extract excess cycles from the system at the expense of the protocol's economic integrity.

---

### Likelihood Explanation

The XRC is a live canister on the NNS subnet. It undergoes upgrades, can experience transient failures, and the CMC's own error-handling explicitly anticipates XRC unavailability (scheduling retries at 1-minute intervals). The CMC's initial default rate is hardcoded to `May 10, 2021` — demonstrating that the system is designed to operate with a potentially very stale rate at startup. Any period of XRC unavailability lasting more than a few minutes creates a window for exploitation. The entry path requires only a valid ICP transfer to the CMC's subaccount, which is a standard, permissionless operation available to any principal.

---

### Recommendation

In `tokens_to_cycles()`, compare `rate.timestamp_seconds` against `ic_cdk::api::time() / 1_000_000_000` (current time in seconds) and reject conversions if the rate is older than a defined maximum staleness threshold (e.g., `REFRESH_RATE_INTERVAL_SECONDS * N`). Return a `NotifyError` indicating the rate is stale, allowing callers to retry once the rate is refreshed.

---

### Proof of Concept

1. The CMC's `icp_xdr_conversion_rate` is last updated at time `T` with rate `R` (e.g., 1 ICP = 5 XDR).
2. The XRC becomes unavailable at time `T`. The CMC retries every minute but all calls fail.
3. At time `T + 1 hour`, ICP's market price has dropped to 1 ICP = 2 XDR.
4. An attacker purchases ICP at the current market price (cheap).
5. The attacker transfers ICP to the CMC's subaccount with `MEMO_MINT_CYCLES` and calls `notify_mint_cycles`.
6. `tokens_to_cycles()` reads `state.icp_xdr_conversion_rate` — still `R = 5 XDR/ICP` from time `T` — with no timestamp check: [8](#0-7) 
7. The attacker receives cycles computed at 5 XDR/ICP instead of the correct 2 XDR/ICP — a **2.5× excess** in cycles minted per ICP burned.
8. The attacker repeats until the XRC recovers and the rate is updated.

### Citations

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L149-163)
```rust
                Err(error) => match error {
                    UpdateExchangeRateError::UpdateAlreadyInProgress => {}
                    UpdateExchangeRateError::Disabled => {}
                    UpdateExchangeRateError::NotReadyToGetRate(_) => {}
                    UpdateExchangeRateError::FailedToRetrieveRate(_)
                    | UpdateExchangeRateError::FailedToSetRate(_)
                    | UpdateExchangeRateError::InvalidRate(_) => {
                        state.update_exchange_rate_canister_state.replace(
                            UpdateExchangeRateState::get_rate_at_next_minute(
                                self.current_minute_in_seconds,
                            ),
                        );
                    }
                },
            }
```

**File:** rs/nns/cmc/src/main.rs (L1022-1030)
```rust
    mutate_state(safe_state, |state| {
        if let Some(current_conversion_rate) = state.icp_xdr_conversion_rate.as_ref()
            && proposed_conversion_rate.timestamp_seconds
                <= current_conversion_rate.timestamp_seconds
        {
            return Err(
                "Proposed conversion rate must have greater timestamp than current one".to_string(),
            );
        }
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

**File:** rs/nns/cmc/src/main.rs (L1931-1932)
```rust
) -> Result<CanisterId, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1964-1965)
```rust
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
```

**File:** rs/nns/cmc/src/main.rs (L1990-1991)
```rust
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;
```
