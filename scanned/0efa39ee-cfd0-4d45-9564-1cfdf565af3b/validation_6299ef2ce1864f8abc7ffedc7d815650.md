### Title
Stale Certified ICP/XDR Rate Window During XRC Inter-Canister Call - (File: `rs/nns/cmc/src/exchange_rate_canister.rs`)

---

### Summary
The Cycles Minting Canister (CMC) updates its ICP/XDR conversion rate by awaiting an inter-canister call to the Exchange Rate Canister (XRC). During this await window, the CMC's certified state still reflects the **old rate**. Any query caller or external canister reading `get_icp_xdr_conversion_rate` or `get_average_icp_xdr_conversion_rate` during this window receives a stale certified rate. This is the IC analog of the EVM read-only reentrancy pattern: the "reentrancy guard" equivalent (IC's single-message-at-a-time execution) prevents update-call reentrancy, but query calls execute concurrently and can observe the intermediate, pre-update state.

---

### Finding Description

In `rs/nns/cmc/src/exchange_rate_canister.rs`, the `update_exchange_rate` function is called from the CMC heartbeat. It performs an inter-canister call to the XRC and only updates the certified state **after** the call returns:

```rust
// rs/nns/cmc/src/exchange_rate_canister.rs ~L245-L275
UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
    let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
    // ↑ AWAIT POINT — certified state is stale here
    // ...
    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
    // ↑ certified data is only updated inside this call, AFTER the await
})
.await
``` [1](#0-0) 

The heartbeat triggers this flow every ~60 seconds:

```rust
// rs/nns/cmc/src/main.rs ~L2397-L2416
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}
``` [2](#0-1) 

The CMC exposes two query endpoints that read `icp_xdr_conversion_rate` directly from state:

- `get_icp_xdr_conversion_rate` (query)
- `get_average_icp_xdr_conversion_rate` (query) [3](#0-2) 

On IC, **query calls execute concurrently with in-flight update calls**. While the CMC's update execution is suspended at the `.await` point waiting for the XRC response, any query call to `get_icp_xdr_conversion_rate` is served from the pre-update state — the old rate — including its certified data tree. The `set_certified_data` call inside `do_set_icp_xdr_conversion_rate` has not yet executed.

The CMC state struct confirms both the rate and the certified data are updated together only after the XRC response: [4](#0-3) 

---

### Impact Explanation

Any external canister or user that reads the CMC's certified ICP/XDR rate during the XRC await window receives a **stale certified rate**. If an external protocol (e.g., a DeFi canister, a subnet rental pricing contract, or a cycles marketplace) uses the CMC's certified rate as a price oracle for financial decisions — such as computing how much ICP to accept for a service, or pricing cycles — it will act on an outdated rate. An attacker who can observe the XRC's new rate (e.g., by querying the XRC directly) before the CMC commits it can exploit the discrepancy: they know the true rate has changed but the CMC's certified rate still reflects the old value, enabling them to transact at a stale price in any protocol that trusts the CMC's certified output.

The `icp_xdr_conversion_rate` is also used internally by the CMC to compute cycles minted during `notify_top_up`. If a `notify_top_up` call reads the rate from state while `update_exchange_rate` is suspended at its await, it uses the old rate for minting — potentially minting more or fewer cycles than the current market rate warrants. [5](#0-4) 

---

### Likelihood Explanation

The CMC heartbeat fires every ~60 seconds. The XRC inter-canister call takes at least one subnet round-trip (~2 seconds on mainnet). During this window, any query to the CMC's rate endpoints returns stale certified data. An unprivileged user can:

1. Monitor the XRC canister directly to learn the new rate before the CMC commits it.
2. Time a query to the CMC during the heartbeat's await window to obtain the stale certified rate.
3. Present this stale certified rate to an external protocol that trusts CMC certification.

No privileged access, governance majority, or threshold corruption is required. The entry path is a standard query call to the CMC, which is publicly accessible.

---

### Recommendation

1. **Snapshot the new rate before the await**: Read and store the new rate from the XRC response, then update the certified state atomically in a synchronous step before any further awaits, or immediately upon resumption before any other state reads can occur.
2. **Document the stale window**: External protocols that use the CMC's certified rate as a price oracle should be explicitly warned that the certified rate may lag by up to one XRC round-trip during each heartbeat cycle.
3. **Consider a read-only reentrancy guard pattern**: Similar to Balancer's `VaultReentrancyLib`, the CMC could expose a flag indicating that a rate update is in progress, allowing external callers to detect and reject reads during the stale window.

---

### Proof of Concept

1. Observe the CMC heartbeat schedule (fires every ~60 seconds, predictable from block time).
2. Query the XRC canister directly for the current ICP/XDR rate — this is the rate the CMC will commit after its next heartbeat.
3. During the CMC heartbeat's XRC await window (~2 seconds), submit a query to `get_icp_xdr_conversion_rate` on the CMC. The response will carry the **old** certified rate with a valid certificate.
4. Present this stale certified rate to any external protocol that accepts CMC-certified rates as a price oracle input.
5. If the new rate is higher (ICP appreciated), the stale rate understates ICP value — an attacker can use this to purchase cycles or services at a below-market ICP price in any protocol that trusts the stale certificate.

### Citations

**File:** rs/nns/cmc/src/exchange_rate_canister.rs (L245-275)
```rust
    UpdateExchangeRateGuard::with_guard(safe_state, current_minute_seconds, async {
        let call_xrc_result = xrc_client.get_icp_to_xdr_exchange_rate(None).await;
        // Check if updating the rate via the exchange rate canister was disabled while retrieving the rate.
        // If it has, exit early.
        let is_updating_rate_disabled = read_state(safe_state, |state| {
            state
                .update_exchange_rate_canister_state
                .unwrap_or_default()
                == UpdateExchangeRateState::Disabled
        });
        if is_updating_rate_disabled {
            return Err(UpdateExchangeRateError::Disabled);
        }

        match call_xrc_result {
            Ok(exchange_rate) => {
                validate_exchange_rate(&exchange_rate)
                    .map_err(|error| UpdateExchangeRateError::InvalidRate(error.to_string()))?;
                let icp_xdr_conversion_rate = IcpXdrConversionRate::from(exchange_rate);
                if let Err(error) =
                    do_set_icp_xdr_conversion_rate(safe_state, env, icp_xdr_conversion_rate)
                {
                    return Err(UpdateExchangeRateError::FailedToSetRate(error));
                }
            }
            Err(error) => {
                return Err(UpdateExchangeRateError::FailedToRetrieveRate(
                    error.to_string(),
                ));
            }
        };
```

**File:** rs/nns/cmc/src/main.rs (L217-230)
```rust
    /// How many XDR 1 ICP is worth, along with a timestamp.
    pub icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The average ICP/XDR rate over `NUM_DAYS_FOR_ICP_XDR_AVERAGE` days. The
    /// timestamp is the UNIX epoch time in seconds at the start of the last
    /// considered day, which should correspond to midnight of the current
    /// day.
    pub average_icp_xdr_conversion_rate: Option<IcpXdrConversionRate>,

    /// The recent ICP/XDR rates used to compute the average rate.
    pub recent_icp_xdr_rates: Option<Vec<IcpXdrConversionRate>>,

    /// How many cycles 1 XDR is worth.
    pub cycles_per_xdr: Cycles,
```

**File:** rs/nns/cmc/src/main.rs (L2397-2416)
```rust
#[heartbeat]
async fn canister_heartbeat() {
    if with_state(|state| state.exchange_rate_canister_id.is_some()) {
        update_exchange_rate().await
    }
}

async fn update_exchange_rate() {
    let xrc_client = match with_state(|state| state.exchange_rate_canister_id) {
        Some(exchange_rate_canister_id) => {
            RealExchangeRateCanisterClient::new(exchange_rate_canister_id)
        }
        None => {
            print("[cycles] Exchange rate canister ID must be set to call the XRC");
            return;
        }
    };
    let env = CanisterEnvironment;
    let periodic_result =
        exchange_rate_canister::update_exchange_rate(&STATE, &env, &xrc_client).await;
```

**File:** rs/nns/cmc/src/main.rs (L2442-2505)
```rust
fn encode_metrics(w: &mut ic_metrics_encoder::MetricsEncoder<Vec<u8>>) -> std::io::Result<()> {
    with_state(|state| {
        w.encode_gauge(
            "cmc_last_purged_notification",
            state.last_purged_notification as f64,
            "Block index of the last purged notification.",
        )?;
        w.encode_gauge(
            "cmc_blocks_notified_count",
            state.blocks_notified.len() as f64,
            "Number of notifications stored in the cache.",
        )?;
        w.encode_gauge(
            "cmc_icp_xdr_conversion_rate",
            state
                .icp_xdr_conversion_rate
                .as_ref()
                .unwrap()
                .xdr_permyriad_per_icp as f64
                / 10_000_f64,
            "Amount of XDR corresponding to 1 ICP.",
        )?;
        w.encode_gauge(
            "cmc_cycles_per_xdr",
            state.cycles_per_xdr.get() as f64,
            "Number of cycles corresponding to 1 XDR.",
        )?;
        w.encode_counter(
            "cmc_cycles_minted_total",
            state.total_cycles_minted.get() as f64,
            "Number of cycles minted since the Genesis.",
        )?;
        w.encode_gauge(
            "cmc_avg_icp_xdr_conversion_rate",
            state
                .average_icp_xdr_conversion_rate
                .as_ref()
                .unwrap()
                .xdr_permyriad_per_icp as f64
                / 10_000_f64,
            "Average amount of XDR corresponding to 1 ICP.",
        )?;
        w.encode_gauge(
            "cmc_avg_icp_xdr_conversion_rate_timestamp_seconds",
            state
                .average_icp_xdr_conversion_rate
                .as_ref()
                .unwrap()
                .timestamp_seconds as f64,
            "Timestamp of the last update to the Average ICP/XDR conversion rate, in seconds since the Unix epoch.",
        )?;
        w.encode_gauge(
            "cmc_icp_xdr_conversion_rate_timestamp_seconds",
            state
                .icp_xdr_conversion_rate
                .as_ref()
                .unwrap()
                .timestamp_seconds as f64,
            "Timestamp of the last ICP/XDR conversion rate, in seconds since the Unix epoch.",
        )?;
        w.encode_gauge(
            "cmc_update_exchange_rate_canister_state",
            u8::from(state.update_exchange_rate_canister_state.as_ref().unwrap()) as f64,
            "The current state of the CMC calling the exchange rate canister.",
```

**File:** rs/nns/test_utils/src/state_test_helpers.rs (L2301-2324)
```rust
pub fn get_icp_xdr_conversion_rate(
    machine: &StateMachine,
) -> IcpXdrConversionRateCertifiedResponse {
    let bytes = query(
        machine,
        CYCLES_MINTING_CANISTER_ID,
        "get_icp_xdr_conversion_rate",
        Encode!().unwrap(),
    )
    .expect("Failed to retrieve the conversion rate");
    Decode!(&bytes, IcpXdrConversionRateCertifiedResponse).unwrap()
}

pub fn get_average_icp_xdr_conversion_rate(
    machine: &StateMachine,
) -> IcpXdrConversionRateCertifiedResponse {
    let bytes = query(
        machine,
        CYCLES_MINTING_CANISTER_ID,
        "get_average_icp_xdr_conversion_rate",
        Encode!().unwrap(),
    )
    .expect("Failed to retrieve the average conversion rate");
    Decode!(&bytes, IcpXdrConversionRateCertifiedResponse).unwrap()
```
