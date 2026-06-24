### Title
HTTP Outcall Legacy Pricing Charges Fees Based on Theoretical Maximum Response Size, Not Actual Bytes Received - (`rs/cycles_account_manager/src/cycles_account_manager.rs`, `rs/execution_environment/src/execution_environment.rs`, `rs/https_outcalls/pricing/src/legacy.rs`)

---

### Summary

Under the default (and only user-accessible) `PricingVersion::Legacy` for canister HTTP outcalls, the cycles fee is computed using the caller-specified `max_response_bytes` as the response size, not the actual bytes received. The full fee is deducted upfront and is never refunded based on actual response size. Any canister making HTTP outcalls with a `max_response_bytes` larger than the actual response permanently loses the difference in cycles.

---

### Finding Description

`http_request_fee` in `rs/cycles_account_manager/src/cycles_account_manager.rs` computes the legacy fee as:

```rust
let amount = (self.config.http_request_linear_baseline_fee
    + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
    + self.config.http_request_per_byte_fee * request_size.get()
    + self.config.http_response_per_byte_fee * response_size)   // ← max_response_bytes
    * (subnet_size as u64);
```

where `response_size` is `max_response_bytes` (or `MAX_CANISTER_HTTP_RESPONSE_BYTES = 2 MiB` if unset), not the actual response payload size. [1](#0-0) 

In `try_add_http_context_to_replicated_state` (execution environment), under `PricingVersion::Legacy`, the full legacy fee is deducted from the payment immediately:

```rust
PricingVersion::Legacy => {
    // Legacy pricing deducts the full request fee from the payment.
    // The remaining payment is refunded when the response is delivered.
    canister_http_request_context.request.payment -= legacy_fee.real();
}
```

The comment says "refunded when the response is delivered," but the code explicitly sets `refundable_cycles` to `payment - base_fee.real()` (not `payment - legacy_fee.real()`), and then states: **"nothing will actually be refunded for legacy pricing."** [2](#0-1) 

At the adapter layer, `LegacyTracker::create_payment_receipt` always returns a zero-refund receipt:

```rust
fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
    // Legacy pricing does not perform cycles accounting, so no cycles
    // are ever refunded.
    CanisterHttpPaymentReceipt::default()
}
``` [3](#0-2) 

`PricingFactory::new_tracker` always returns a `LegacyTracker` regardless of the context's `pricing_version` field (with a TODO comment acknowledging this is incomplete):

```rust
impl PricingFactory {
    pub fn new_tracker(context: &CanisterHttpRequestContext) -> Box<dyn BudgetTracker> {
        // TODO(IC-1937): This should take into account context.pricing_version and a replica config.
        // Currently, we only support the legacy pricing version.
        Box::new(LegacyTracker::new(context.max_response_bytes))
    }
}
``` [4](#0-3) 

The `PayAsYouGo` pricing version, which would charge only the base fee upfront and refund the remainder based on actual usage, is not accessible to users: `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS` contains only `PRICING_VERSION_LEGACY`, and the adapter client immediately rejects any `PayAsYouGo` request with `SysFatal`. [5](#0-4) 

---

### Impact Explanation

Any canister making HTTP outcalls with legacy pricing (the only available mode) is charged for `max_response_bytes` worth of response transmission, regardless of how many bytes the server actually returns. The overcharge per request is:

```
overcharge = (max_response_bytes - actual_response_bytes)
             × http_response_per_byte_fee
             × subnet_size²
```

On a 13-node subnet with `max_response_bytes = 2 MiB` and an actual 1 KB response, this is approximately `(2,000,000 - 1,000) × 50 × 169 ≈ 16.9 billion cycles` per request — permanently lost. Canisters that set `max_response_bytes = None` default to the 2 MiB maximum and are charged for the full 2 MiB on every call regardless of actual response size.

---

### Likelihood Explanation

This affects every canister HTTP outcall on every subnet using the default (legacy) pricing. Any unprivileged canister developer calling `ic00::HttpRequest` triggers this path. The overcollection is deterministic and proportional to the gap between `max_response_bytes` and actual response size. Since most real-world HTTP responses are far smaller than the declared maximum, overcollection is the norm rather than the exception.

---

### Recommendation

1. Implement the `PayAsYouGo` tracker in `PricingFactory::new_tracker` (resolve `TODO(IC-1937)`) so that the adapter returns a per-replica payment receipt reflecting actual bytes consumed.
2. Add `PRICING_VERSION_PAY_AS_YOU_GO` to `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS` once the adapter-side implementation is complete, allowing callers to opt in.
3. As an interim measure for legacy pricing, compute the refund at response delivery time using `(max_response_bytes - actual_response_bytes) × http_response_per_byte_fee × subnet_size²` and credit it back to the calling canister, mirroring the existing `refund_for_response_transmission` pattern used for inter-canister calls.

---

### Proof of Concept

1. Canister A calls `ic00::HttpRequest` with `max_response_bytes = Some(2_000_000)` and attaches sufficient cycles.
2. The execution environment computes `legacy_fee` using `response_size = 2_000_000` and deducts it from `request.payment`.
3. The HTTP adapter fetches the URL; the server returns a 500-byte response.
4. `LegacyTracker::create_payment_receipt` returns `CanisterHttpPaymentReceipt::default()` (zero refund).
5. The response is delivered to canister A; the remaining `request.payment` (after the full legacy fee deduction) is refunded, but the `(2,000,000 - 500) × fee_per_byte × subnet_size²` cycles charged for the unused response capacity are permanently consumed.
6. Canister A has paid for 2 MB of response transmission but received 500 bytes — the difference is irrecoverable under legacy pricing.

### Citations

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1229-1239)
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
```

**File:** rs/execution_environment/src/execution_environment.rs (L2184-2211)
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
        };
        let node_count = match &canister_http_request_context.replication {
            Replication::Flexible { committee, .. } => committee.len().max(1),
            Replication::NonReplicated(_) => 1,
            Replication::FullyReplicated => cycles_config.subnet_size.max(1),
        };
        canister_http_request_context.refund_status = RefundStatus {
            refundable_cycles,
            per_replica_allowance: refundable_cycles / node_count,
            refunded_cycles: Cycles::new(0),
            refunding_nodes: BTreeSet::new(),
        };

        // The payment deduction differs per pricing version.
        match canister_http_request_context.pricing_version {
            PricingVersion::Legacy => {
                // Legacy pricing deducts the full request fee from the payment.
                // The remaining payment is refunded when the response is delivered.
                canister_http_request_context.request.payment -= legacy_fee.real();
```

**File:** rs/https_outcalls/pricing/src/legacy.rs (L48-52)
```rust
    fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
        // Legacy pricing does not perform cycles accounting, so no cycles
        // are ever refunded.
        CanisterHttpPaymentReceipt::default()
    }
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

**File:** rs/types/management_canister_types/src/http.rs (L67-72)
```rust
pub const DEFAULT_HTTP_OUTCALLS_PRICING_VERSION: u32 = PRICING_VERSION_LEGACY;

/// A set of all allowed pricing versions for HTTP outcalls.
///
/// If the pricing version provided in the request is not in this set, the request will use the default pricing version.
pub const ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS: &[u32] = &[PRICING_VERSION_LEGACY];
```
