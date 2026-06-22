### Title
Stale ICP/XDR Exchange Rate Used Without Freshness Check in Cycle Minting — (`rs/nns/cmc/src/main.rs`)

---

### Summary

The Cycles Minting Canister (CMC) converts ICP to cycles using `state.icp_xdr_conversion_rate` in `tokens_to_cycles()` without any staleness check on the rate's timestamp. If the Exchange Rate Canister (XRC) is unavailable for an extended period, the CMC silently continues minting cycles at an outdated ICP/XDR rate. An unprivileged user who observes that the stored rate is stale and higher than the current market rate can buy ICP cheaply and convert it to cycles at the inflated old rate, extracting more cycles than the current ICP value justifies — directly analogous to the Cooler's hardcoded `maxLTC` allowing under-collateralized loans when the gOHM price drops.

---

### Finding Description

The CMC initializes with a hardcoded default rate:

```
DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS = 1_620_633_600  // 10 May 2021
DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE     = 1_000_000      // 1 ICP = 100 XDR
``` [1](#0-0) 

This default is loaded into live state at canister initialization: [2](#0-1) 

The rate is subsequently updated via the XRC heartbeat (every 5 minutes) or governance proposals. However, the function that converts ICP to cycles for all three minting operations (`notify_top_up`, `notify_create_canister`, `notify_mint_cycles`) reads the stored rate with **no check on its age**: [3](#0-2) 

All three minting paths call `tokens_to_cycles()` unconditionally: [4](#0-3) [5](#0-4) 

The `do_set_icp_xdr_conversion_rate()` function only validates that a new rate has a strictly greater timestamp than the current one — it does not enforce any maximum age on the rate at the point of use: [6](#0-5) 

By contrast, the Governance canister's `should_refresh_xdr_rate()` **does** enforce a 1-day maximum age before using the rate for node-provider reward calculations: [7](#0-6) 

This asymmetry means the CMC has no equivalent protection for cycle minting.

The XRC update loop can be silently disabled in two ways:
1. A governance proposal with `DivergedRate` reason sets `UpdateExchangeRateState::Disabled`, after which only governance proposals can update the rate.
2. The XRC canister fails persistently; the CMC retries every minute but the stored rate is never refreshed. [8](#0-7) 

---

### Impact Explanation

If ICP's market price drops significantly while the CMC's stored rate remains stale at a higher value, any unprivileged user can:

1. Buy ICP at the current low market price.
2. Call `notify_top_up` or `notify_mint_cycles` to convert that ICP to cycles at the stale (inflated) rate.
3. Receive more cycles than the current ICP value justifies.

This over-mints cycles relative to the ICP burned, diluting the cycles economy. The default rate of 100 XDR/ICP (from May 2021) is already ~20–30× higher than the current ICP/XDR rate (~3–5 XDR/ICP), meaning a fresh CMC deployment with no rate update would allow massive over-minting until governance intervenes. [1](#0-0) 

---

### Likelihood Explanation

The XRC is a system canister that could be unavailable due to bugs, upgrades, or network partitions. The CMC's heartbeat-based update mechanism retries every minute on failure but does not alert or block minting. The `UpdateExchangeRateState::Disabled` path is reachable via a governance proposal with `DivergedRate` reason, after which the rate is frozen until another governance proposal re-enables it. During any such period, if ICP price moves materially, the stale rate is exploitable by any unprivileged user who can call `notify_top_up` or `notify_mint_cycles`. [9](#0-8) [10](#0-9) 

---

### Recommendation

Add a maximum-age check in `tokens_to_cycles()` (analogous to `should_refresh_xdr_rate()` in Governance) that rejects cycle minting with a clear error if `state.icp_xdr_conversion_rate.timestamp_seconds` is older than a defined threshold (e.g., 1 hour or 1 day) relative to the current canister time. This mirrors the protection already present in the Governance canister and prevents the CMC from silently minting cycles at a stale rate. [3](#0-2) 

---

### Proof of Concept

1. Deploy or observe a CMC instance where the XRC is disabled (`exchange_rate_canister_id = None`) or the XRC canister is unavailable.
2. The stored `icp_xdr_conversion_rate` remains at its last-set value (e.g., 50,000 permyriad = 5 XDR/ICP from a period when ICP was worth $7).
3. ICP market price drops to $3 (≈ 2.1 XDR/ICP at current XDR rate), but the CMC rate is not updated.
4. An unprivileged user sends 1 ICP to the CMC subaccount and calls `notify_top_up`.
5. `tokens_to_cycles()` reads `xdr_permyriad_per_icp = 50_000` (stale) and mints `50_000 / 10_000 * 1_000_000_000_000 = 5_000_000_000_000` cycles (5T cycles).
6. At the correct current rate of 21,000 permyriad, the user should only receive 2.1T cycles.
7. The user has extracted ~2.4T extra cycles per ICP, exploiting the stale rate — directly analogous to the Cooler borrower taking a loan at the stale `maxLTC` and profiting from the price difference. [3](#0-2) [11](#0-10)

### Citations

**File:** rs/nns/cmc/src/lib.rs (L33-34)
```rust
pub const DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS: u64 = 1_620_633_600; // 10 May 2021 10:00:00 AM CEST
pub const DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE: u64 = 1_000_000; // 1 ICP = 100 XDR
```

**File:** rs/nns/cmc/src/main.rs (L360-374)
```rust
        let initial_icp_xdr_conversion_rate = IcpXdrConversionRate {
            timestamp_seconds: DEFAULT_ICP_XDR_CONVERSION_RATE_TIMESTAMP_SECONDS,
            xdr_permyriad_per_icp: DEFAULT_XDR_PERMYRIAD_PER_ICP_CONVERSION_RATE,
        };

        Self {
            ledger_canister_id: CanisterId::ic_00(),
            governance_canister_id: CanisterId::ic_00(),
            exchange_rate_canister_id: None,
            cycles_ledger_canister_id: None,
            minting_account_id: None,
            authorized_subnets: BTreeMap::new(),
            default_subnets: vec![],
            icp_xdr_conversion_rate: Some(initial_icp_xdr_conversion_rate.clone()),
            average_icp_xdr_conversion_rate: Some(initial_icp_xdr_conversion_rate.clone()),
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

**File:** rs/nns/cmc/src/main.rs (L1958-1966)
```rust
async fn process_mint_cycles(
    to_account: Account,
    amount: Tokens,
    deposit_memo: Option<Vec<u8>>,
    from: AccountIdentifier,
    sub: Subaccount,
) -> NotifyMintCyclesResult {
    let cycles = tokens_to_cycles(amount)?;
    match do_mint_cycles(to_account, cycles, deposit_memo).await {
```

**File:** rs/nns/cmc/src/main.rs (L1985-2012)
```rust
async fn process_top_up(
    canister_id: CanisterId,
    from: AccountIdentifier,
    amount: Tokens,
    limiter_to_use: CyclesMintingLimiterSelector,
) -> Result<Cycles, NotifyError> {
    let cycles = tokens_to_cycles(amount)?;

    let sub = Subaccount::from(&canister_id);

    print(format!(
        "Topping up canister {canister_id} by {cycles} cycles."
    ));

    match deposit_cycles(canister_id, cycles, true, limiter_to_use).await {
        Ok(()) => {
            burn_and_log(sub, amount).await;
            Ok(cycles)
        }
        Err(err) => {
            let refund_block = refund_icp(sub, from, amount, TOP_UP_CANISTER_REFUND_FEE).await?;
            Err(NotifyError::Refunded {
                reason: err.to_string(),
                block_index: refund_block,
            })
        }
    }
}
```

**File:** rs/nns/governance/src/governance.rs (L6336-6348)
```rust
    fn should_refresh_xdr_rate(&self) -> bool {
        let xdr_conversion_rate = &self.heap_data.xdr_conversion_rate;

        let now_seconds = self.env.now();

        let seconds_since_last_conversion_rate_refresh =
            now_seconds.saturating_sub(xdr_conversion_rate.timestamp_seconds);

        // Return `true` if more than 1 day has passed since the last `xdr_conversion_rate` was
        // updated. This assumes that `xdr_conversion_rate.timestamp_seconds` is rounded down to
        // the nearest day's beginning.
        seconds_since_last_conversion_rate_refresh > ONE_DAY_SECONDS
    }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L15-16)
```rust
/// If the rate is older than this value, the CMC should ask for a new rate.
const REFRESH_RATE_INTERVAL_SECONDS: u64 = 5 * ONE_MINUTE_SECONDS;
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L98-112)
```rust
        if current_call_state == UpdateExchangeRateState::Disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        if current_call_state == UpdateExchangeRateState::InProgress {
            return Err(UpdateExchangeRateError::UpdateAlreadyInProgress);
        }

        if let UpdateExchangeRateState::GetRateAt(next_attempt_seconds) = current_call_state
            && current_minute_in_seconds < next_attempt_seconds
        {
            return Err(UpdateExchangeRateError::NotReadyToGetRate(
                next_attempt_seconds,
            ));
        }
```

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L311-315)
```rust
                UpdateIcpXdrConversionRatePayloadReason::DivergedRate => {
                    state
                        .update_exchange_rate_canister_state
                        .replace(UpdateExchangeRateState::Disabled);
                }
```
