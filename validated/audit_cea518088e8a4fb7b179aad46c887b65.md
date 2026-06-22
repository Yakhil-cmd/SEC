### Title
Canister HTTP Outcall Legacy Pricing Charges Fee on Maximum Response Size, Not Actual Response Size - (`rs/execution_environment/src/execution_environment.rs`)

### Summary

The Internet Computer's canister HTTP outcall feature under **legacy pricing** (`PricingVersion::Legacy`) charges the full fee upfront based on the caller-specified `max_response_bytes` (or the global maximum of 2 MB if unspecified), and **never refunds** the difference when the actual HTTP response is smaller. This is the direct IC analog of H-35: a fee is computed on a maximum/declared amount rather than the actual amount consumed.

### Finding Description

In `try_add_http_context_to_replicated_state` in `rs/execution_environment/src/execution_environment.rs`, the legacy fee is computed using `http_request_fee`, which incorporates `max_response_bytes` (the caller-declared ceiling) as the response size:

```rust
let legacy_fee = self.cycles_account_manager.http_request_fee(
    variable_parts_size,
    canister_http_request_context.max_response_bytes,  // declared max, not actual
    cycles_config,
);
```

The fee formula in `http_request_fee` directly multiplies `response_size` (the declared max) into the cost:

```rust
let amount = (self.config.http_request_linear_baseline_fee
    + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
    + self.config.http_request_per_byte_fee * request_size.get()
    + self.config.http_response_per_byte_fee * response_size)   // ← uses declared max
    * (subnet_size as u64);
```

This full legacy fee is then **permanently deducted** from the canister's payment:

```rust
PricingVersion::Legacy => {
    // Legacy pricing deducts the full request fee from the payment.
    // The remaining payment is refunded when the response is delivered.
    canister_http_request_context.request.payment -= legacy_fee.real();
}
```

The `LegacyTracker` in `rs/https_outcalls/pricing/src/legacy.rs` confirms that legacy pricing **never issues a refund**:

```rust
fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
    // Legacy pricing does not perform cycles accounting, so no cycles
    // are ever refunded.
    CanisterHttpPaymentReceipt::default()
}
```

The code comments in `try_add_http_context_to_replicated_state` explicitly acknowledge this:

> "We set the refund status even for legacy pricing in order to enable observability during the dark launch. However, nothing will actually be refunded for legacy pricing."

So a canister that declares `max_response_bytes = 2_000_000` (2 MB) but receives a 100-byte response is charged the full 2 MB fee with zero refund. The `pay-as-you-go` pricing version correctly charges only the base fee upfront and refunds the remainder, but legacy pricing — which is the **current default** (`DEFAULT_HTTP_OUTCALLS_PRICING_VERSION = PRICING_VERSION_LEGACY`) — does not.

### Impact Explanation

Any canister making HTTP outcalls under legacy pricing (the current default) is charged cycles proportional to `max_response_bytes` regardless of the actual response size. If a canister sets a large `max_response_bytes` ceiling (e.g., 2 MB) but the server returns a small response (e.g., 1 KB), the canister overpays by up to ~2000x the response-size component of the fee. On a 13-node subnet with default config, `http_response_per_byte_fee * 2_000_000 * 13` cycles are permanently burned even when only a few bytes were received. This is a systematic overcharge affecting every canister HTTP outcall on mainnet that uses the legacy pricing version.

### Likelihood Explanation

This is the **current production behavior** for all canister HTTP outcalls that do not explicitly opt into pay-as-you-go pricing. Since `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS` only permits legacy pricing (`[PRICING_VERSION_LEGACY]`), every canister HTTP outcall on mainnet today is subject to this overcharge. Any canister developer who sets a conservative `max_response_bytes` ceiling (standard practice) and receives a smaller response is affected on every call. No special attacker action is required — the overcharge is triggered by normal use.

### Recommendation

1. Enable pay-as-you-go pricing as the default or sole allowed pricing version, which already correctly charges only the base fee upfront and refunds the remainder based on actual response size.
2. Until then, document clearly that legacy pricing charges based on `max_response_bytes`, not actual response size, so canister developers can minimize `max_response_bytes` to reduce overpayment.
3. Consider implementing a post-response refund mechanism for legacy pricing that issues a partial refund based on `(max_response_bytes - actual_response_bytes) * http_response_per_byte_fee * subnet_size`.

### Proof of Concept

1. Deploy a canister on a 13-node application subnet.
2. Call `ic00::http_request` with `max_response_bytes = Some(2_000_000)` (2 MB) and a URL that returns a 100-byte JSON response. Use the default pricing version (legacy).
3. Observe cycles balance before and after. The cycles deducted will equal `http_request_fee(variable_parts_size, Some(2_000_000), config)` — the full 2 MB fee — not a fee proportional to the 100 bytes actually received.
4. Repeat with `max_response_bytes = Some(100)`. The fee drops dramatically, confirming the fee is based on the declared maximum, not actual usage.
5. Compare with a pay-as-you-go request (once enabled): the base fee is charged upfront and the response-size component is refunded after delivery.

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L2125-2129)
```rust
        let legacy_fee = self.cycles_account_manager.http_request_fee(
            variable_parts_size,
            canister_http_request_context.max_response_bytes,
            cycles_config,
        );
```

**File:** rs/execution_environment/src/execution_environment.rs (L2207-2211)
```rust
        match canister_http_request_context.pricing_version {
            PricingVersion::Legacy => {
                // Legacy pricing deducts the full request fee from the payment.
                // The remaining payment is refunded when the response is delivered.
                canister_http_request_context.request.payment -= legacy_fee.real();
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1235-1241)
```rust
        let amount = (self.config.http_request_linear_baseline_fee
            + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
            + self.config.http_request_per_byte_fee * request_size.get()
            + self.config.http_response_per_byte_fee * response_size)
            * (subnet_size as u64);

        CompoundCycles::new(amount, subnet_cycles_config.cost_schedule)
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
