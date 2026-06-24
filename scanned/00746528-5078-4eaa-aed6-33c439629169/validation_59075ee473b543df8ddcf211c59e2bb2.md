### Title
HTTP Outcall Cycles Fee Charged on `max_response_bytes` Not Actual Response Size — (`rs/cycles_account_manager/src/cycles_account_manager.rs`)

---

### Summary

Under the legacy HTTP outcalls pricing model (the current default), the cycles fee for a canister HTTP outcall is computed using the caller-supplied `max_response_bytes` ceiling, not the actual bytes returned by the remote server. No refund is issued for the unused response budget. This is a direct structural analog to the Seaport partial-fill overcharge: both systems charge a fee proportional to a declared maximum rather than the value actually consumed.

---

### Finding Description

`http_request_fee()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` computes the legacy fee as:

```
fee = (linear_baseline + quadratic_baseline * n + per_byte_request * req_size
       + per_byte_response * max_response_bytes) * n
``` [1](#0-0) 

The `response_size` term is taken from `max_response_bytes` (the canister-declared ceiling), defaulting to `MAX_CANISTER_HTTP_RESPONSE_BYTES` (2 MB) when the caller omits the field. The actual bytes delivered by the remote server play no role.

In `rs/execution_environment/src/execution_environment.rs`, when `PricingVersion::Legacy` is active, the full `legacy_fee` is deducted from the canister's payment immediately:

```rust
PricingVersion::Legacy => {
    canister_http_request_context.request.payment -= legacy_fee.real();
}
``` [2](#0-1) 

The code explicitly notes that nothing is refunded for legacy pricing:

> "However, nothing will actually be refunded for legacy pricing." [3](#0-2) 

This is confirmed at the adapter layer. `PricingFactory::new_tracker` unconditionally constructs a `LegacyTracker` regardless of the `pricing_version` field stored in the context:

```rust
// TODO(IC-1937): This should take into account context.pricing_version and a replica config.
// Currently, we only support the legacy pricing version.
Box::new(LegacyTracker::new(context.max_response_bytes))
``` [4](#0-3) 

`LegacyTracker::create_payment_receipt()` returns `CanisterHttpPaymentReceipt::default()`, meaning zero cycles are ever credited back: [5](#0-4) 

The default pricing version falls back to `PricingVersion::Legacy` for any request that does not explicitly opt into a newer version: [6](#0-5) 

---

### Impact Explanation

A canister that declares `max_response_bytes = 2_000_000` (or omits the field, triggering the 2 MB default) but receives a 100-byte response is charged for 2 MB of response bandwidth. On a 13-node application subnet the per-byte response fee is `400_000` cycles/byte, so the overcharge for a 2 MB ceiling vs. a 100-byte actual response is approximately:

```
400_000 * (2_000_000 - 100) * 13 ≈ 10.4 trillion cycles (~$0.013 per call)
```

Multiplied across high-frequency outcall patterns (e.g., price oracles, monitoring canisters), this constitutes a systematic, non-recoverable cycles drain. The canister's balance is depleted faster than its developer expects, potentially causing it to run out of cycles and be frozen or deleted. [7](#0-6) 

---

### Likelihood Explanation

- Legacy pricing is the **default** for every HTTP outcall that does not explicitly set `pricing_version`.
- The `PricingFactory::new_tracker` TODO confirms the pay-as-you-go path is not yet wired end-to-end in the adapter, so even canisters that opt into `PayAsYouGo` receive no actual refund today.
- Any canister developer issuing HTTP outcalls is affected without any special configuration or adversarial action. [8](#0-7) 

---

### Recommendation

1. In `http_request_fee()`, replace the `max_response_bytes` term with the **actual response size** once the response is available, and issue a refund for the difference — mirroring the existing `refund_for_response_transmission` pattern used for inter-canister calls.
2. Complete the `TODO(IC-1937)` in `PricingFactory::new_tracker` so that `PayAsYouGo` contexts use a tracker that records actual network usage and produces a non-empty `CanisterHttpPaymentReceipt`.
3. Until (2) is done, document clearly that `PayAsYouGo` does not yet produce refunds, so canister developers are not misled by the API field. [9](#0-8) 

---

### Proof of Concept

1. Canister A calls `ic00::HttpRequest` with `max_response_bytes = Some(2_000_000)` and attaches sufficient cycles.
2. The remote server returns a 50-byte body.
3. `http_request_fee()` computes the fee using `response_size = 2_000_000`.
4. The full fee is deducted at context creation time (`payment -= legacy_fee.real()`).
5. `LegacyTracker::create_payment_receipt()` returns `CanisterHttpPaymentReceipt::default()`.
6. No refund path is triggered; the canister has permanently paid for 2 MB of response data while consuming 50 bytes.

The gap between charged and consumed response bytes is `(2_000_000 - 50) * http_response_per_byte_fee * subnet_size²` cycles, lost on every such call. [10](#0-9) [11](#0-10)

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1229-1242)
```rust
        let response_size = match response_size_limit {
            Some(response_size) => response_size.get(),
            // Defaults to maximum response size.
            None => MAX_CANISTER_HTTP_RESPONSE_BYTES,
        };

        let amount = (self.config.http_request_linear_baseline_fee
            + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
            + self.config.http_request_per_byte_fee * request_size.get()
            + self.config.http_response_per_byte_fee * response_size)
            * (subnet_size as u64);

        CompoundCycles::new(amount, subnet_cycles_config.cost_schedule)
    }
```

**File:** rs/execution_environment/src/execution_environment.rs (L2184-2192)
```rust
        // The refundable cycles are everything the payment covers beyond the
        // base fee; on a free cost schedule nothing is charged, so nothing is
        // refundable. We set the refund status even for legacy pricing in order
        // to enable observability during the dark launch. However, nothing will
        // actually be refunded for legacy pricing.
        let refundable_cycles = if cost_schedule == CanisterCyclesCostSchedule::Free {
            Cycles::new(0)
        } else {
            canister_http_request_context.request.payment - base_fee.real()
```

**File:** rs/execution_environment/src/execution_environment.rs (L2207-2211)
```rust
        match canister_http_request_context.pricing_version {
            PricingVersion::Legacy => {
                // Legacy pricing deducts the full request fee from the payment.
                // The remaining payment is refunded when the response is delivered.
                canister_http_request_context.request.payment -= legacy_fee.real();
```

**File:** rs/https_outcalls/pricing/src/lib.rs (L55-60)
```rust
impl PricingFactory {
    pub fn new_tracker(context: &CanisterHttpRequestContext) -> Box<dyn BudgetTracker> {
        // TODO(IC-1937): This should take into account context.pricing_version and a replica config.
        // Currently, we only support the legacy pricing version.
        Box::new(LegacyTracker::new(context.max_response_bytes))
    }
```

**File:** rs/https_outcalls/pricing/src/legacy.rs (L34-52)
```rust
    fn subtract_network_usage(&mut self, _network_usage: NetworkUsage) -> Result<(), PricingError> {
        // Note: currently the client enforces the timeout limit, while the adapter enforces the response size limit.
        // So there is no need to do anything here.
        Ok(())
    }

    fn get_transform_limit(&self) -> NumInstructions {
        MAX_INSTRUCTIONS_PER_QUERY_MESSAGE
    }

    fn subtract_transform_usage(&mut self, _usage: NumInstructions) -> Result<(), PricingError> {
        Ok(())
    }

    fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
        // Legacy pricing does not perform cycles accounting, so no cycles
        // are ever refunded.
        CanisterHttpPaymentReceipt::default()
    }
```

**File:** rs/types/types/src/canister_http.rs (L581-587)
```rust
            pricing_version: {
                let final_version_u32 = args
                    .pricing_version
                    .filter(|v| ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS.contains(v))
                    .unwrap_or(DEFAULT_HTTP_OUTCALLS_PRICING_VERSION);
                PricingVersion::from_repr(final_version_u32).unwrap_or(PricingVersion::Legacy)
            },
```

**File:** rs/execution_environment/src/execution/response.rs (L167-181)
```rust
        // The canister that sends a request must also pay the fee for
        // the transmission of the response. As we do not know how big
        // the response might be, we reserve cycles for the largest
        // possible response when the request is being sent. Now that we
        // have received the response, we can refund the cycles based on
        // the actual size of the response.
        let refund_for_response_transmission = round
            .cycles_account_manager
            .refund_for_response_transmission(
                round.log,
                round.counters.response_cycles_refund_error,
                &response.response_payload,
                original.callback.prepayment_for_response_transmission,
                original.subnet_cycles_config,
            );
```
