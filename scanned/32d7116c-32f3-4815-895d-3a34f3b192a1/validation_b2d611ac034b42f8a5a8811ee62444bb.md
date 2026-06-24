### Title
Canister HTTP Outcall Cycles Fee Always Charged on `max_response_bytes`, Never Refunded for Actual Response Size Under Legacy Pricing - (`rs/execution_environment/src/execution_environment.rs`)

### Summary

Under the only currently permitted HTTP outcall pricing version (`PricingVersion::Legacy`), the cycles fee for a canister HTTP outcall is computed using the caller-specified `max_response_bytes` (or the protocol maximum of 2 MB if unset) and is charged in full upfront. No refund is ever issued based on the actual response size received. A canister that specifies `max_response_bytes = 2_000_000` but consistently receives 100-byte responses is overcharged by the full response-byte fee for ~1,999,900 bytes per request.

### Finding Description

`http_request_fee()` in `rs/cycles_account_manager/src/cycles_account_manager.rs` computes the legacy fee using `response_size_limit` (i.e., `max_response_bytes`) as the response size:

```rust
let response_size = match response_size_limit {
    Some(response_size) => response_size.get(),
    None => MAX_CANISTER_HTTP_RESPONSE_BYTES,  // 2 MB default
};
let amount = (... + self.config.http_response_per_byte_fee * response_size) * (subnet_size as u64);
``` [1](#0-0) 

In `execute_canister_http_request_context()`, the pricing version branch for `Legacy` deducts this full fee from the payment and explicitly does **not** refund based on actual response size:

```rust
PricingVersion::Legacy => {
    // Legacy pricing deducts the full request fee from the payment.
    // The remaining payment is refunded when the response is delivered.
    canister_http_request_context.request.payment -= legacy_fee.real();
}
``` [2](#0-1) 

The comment at line 2184–2188 explicitly acknowledges: *"nothing will actually be refunded for legacy pricing."* [3](#0-2) 

The `LegacyTracker` in the adapter pricing layer confirms this: `create_payment_receipt()` always returns a zero-refund receipt, so no per-replica refund is ever issued:

```rust
fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
    // Legacy pricing does not perform cycles accounting, so no cycles
    // are ever refunded.
    CanisterHttpPaymentReceipt::default()
}
``` [4](#0-3) 

The `PayAsYouGo` pricing version was designed to fix this (it charges only the base fee upfront and refunds the remainder based on actual usage), but it is **not yet available to callers**: `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS` contains only `PRICING_VERSION_LEGACY`, and the default is also legacy: [5](#0-4) 

### Impact Explanation

Every canister on the IC that makes HTTP outcalls via `ic00::http_request` is affected. The `http_response_per_byte_fee` is `800 cycles/byte`. On a 13-node application subnet, a canister that sets `max_response_bytes = 2_000_000` but receives a 1 KB response is overcharged by approximately:

```
800 * (2_000_000 - 1_000) * 13 ≈ 20.8 billion cycles per request
```

At scale (e.g., a canister making 1,000 such requests), this represents ~20.8 trillion cycles of excess drain. The overcharge scales linearly with the gap between `max_response_bytes` and the actual response size, and quadratically with subnet size. This is a direct cycles/resource accounting bug causing systematic overcharging of canister developers.

### Likelihood Explanation

This affects **all** canister HTTP outcalls on the IC today, since legacy pricing is the only permitted version. Any canister developer who sets a conservatively large `max_response_bytes` (a common practice to avoid request failures) and receives smaller responses is continuously overcharged. The entry path requires no special privileges — any canister calling `ic00::http_request` triggers this path.

### Recommendation

Enable `PricingVersion::PayAsYouGo` for callers (add `PRICING_VERSION_PAY_AS_YOU_GO` to `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS`) and/or make it the default. The pay-as-you-go infrastructure is already fully implemented: it charges only the base fee upfront and issues per-replica refunds based on actual response bytes consumed via the `RefundStatus` mechanism. [6](#0-5) 

### Proof of Concept

1. Deploy a canister on an application subnet.
2. Call `ic00::http_request` with `max_response_bytes = Some(2_000_000)` and a URL that returns a 100-byte response. Attach sufficient cycles.
3. Observe that the cycles deducted equal the fee for 2,000,000 bytes of response, not 100 bytes.
4. Repeat 1,000 times. The canister is drained of ~20.8 trillion excess cycles compared to what a pay-as-you-go model would charge.

The fee formula is confirmed in `calculate_http_request_cost` in the test suite, which uses `max_response_bytes` (not actual response size) as the billing basis: [7](#0-6)

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

**File:** rs/execution_environment/src/execution_environment.rs (L2207-2212)
```rust
        match canister_http_request_context.pricing_version {
            PricingVersion::Legacy => {
                // Legacy pricing deducts the full request fee from the payment.
                // The remaining payment is refunded when the response is delivered.
                canister_http_request_context.request.payment -= legacy_fee.real();
            }
```

**File:** rs/execution_environment/src/execution_environment.rs (L2213-2221)
```rust
            PricingVersion::PayAsYouGo => {
                // Take out the entire payment upfront; the refundable portion is
                // returned later via the refund mechanism. On a free cost
                // schedule there is nothing to charge.
                if cost_schedule != CanisterCyclesCostSchedule::Free {
                    canister_http_request_context.request.payment.take();
                }
            }
        }
```

**File:** rs/https_outcalls/pricing/src/legacy.rs (L48-52)
```rust
    fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
        // Legacy pricing does not perform cycles accounting, so no cycles
        // are ever refunded.
        CanisterHttpPaymentReceipt::default()
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

**File:** rs/execution_environment/tests/subnet_size_test.rs (L714-730)
```rust
fn calculate_http_request_cost(
    config: &CyclesAccountManagerConfig,
    request_size: NumBytes,
    response_size_limit: Option<NumBytes>,
    subnet_size: usize,
) -> Cycles {
    let response_size = match response_size_limit {
        Some(response_size) => response_size.get(),
        // Defaults to maximum response size.
        None => MAX_CANISTER_HTTP_RESPONSE_BYTES,
    };
    (config.http_request_linear_baseline_fee
        + config.http_request_quadratic_baseline_fee * (subnet_size as u64)
        + config.http_request_per_byte_fee * request_size.get()
        + config.http_response_per_byte_fee * response_size)
        * (subnet_size as u64)
}
```
