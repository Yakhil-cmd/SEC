### Title
Canister HTTP Outcall Transform Instruction Cost Not Charged Under Legacy Pricing — (`rs/https_outcalls/pricing/src/legacy.rs`)

### Summary

The IC's canister HTTP outcall feature allows canisters to supply a user-defined `transform` query function that is executed on every replica to normalize the raw HTTP response before consensus. Under the legacy pricing model (the only currently deployed model), the cycles cost of executing this user-controlled transform function is never deducted from the canister's payment. The `LegacyTracker::subtract_transform_usage` implementation is a no-op that always returns `Ok(())`, meaning a canister can supply an arbitrarily expensive transform function and consume up to `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE` (5 billion instructions) of subnet compute on every replica, per request, at no additional cycles cost beyond the flat legacy fee.

### Finding Description

When a canister calls `http_request` on the management canister, it may optionally supply a `TransformContext` containing a user-chosen query method name and opaque context blob. After the HTTP adapter fetches the remote response, each replica executes this transform function via `transform_adapter_response` in `rs/https_outcalls/client/src/client.rs`.

The `BudgetTracker` trait (`rs/https_outcalls/pricing/src/lib.rs`) defines `get_transform_limit()` and `subtract_transform_usage()` to control and account for transform instruction consumption. The `LegacyTracker` implementation (`rs/https_outcalls/pricing/src/legacy.rs`) sets the transform limit to `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE` (5 billion instructions, from `rs/config/src/subnet_config.rs`) but its `subtract_transform_usage` is a no-op:

```rust
fn subtract_transform_usage(&mut self, _usage: NumInstructions) -> Result<(), PricingError> {
    Ok(())  // Never charges, never enforces
}
```

The `PricingFactory::new_tracker` always returns a `LegacyTracker` regardless of the request's `pricing_version` field, because the TODO comment at line 57 of `rs/https_outcalls/pricing/src/lib.rs` acknowledges that pay-as-you-go is not yet implemented:

```rust
pub fn new_tracker(context: &CanisterHttpRequestContext) -> Box<dyn BudgetTracker> {
    // TODO(IC-1937): This should take into account context.pricing_version and a replica config.
    // Currently, we only support the legacy pricing version.
    Box::new(LegacyTracker::new(context.max_response_bytes))
}
```

The legacy fee formula in `CyclesAccountManager::http_request_fee` (`rs/cycles_account_manager/src/cycles_account_manager.rs`) charges only for request bytes, response bytes, and subnet size — it contains no term for transform instruction cost:

```rust
let amount = (self.config.http_request_linear_baseline_fee
    + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
    + self.config.http_request_per_byte_fee * request_size.get()
    + self.config.http_response_per_byte_fee * response_size)
    * (subnet_size as u64);
```

The transform function is executed as a query call with up to `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE` = 5 billion instructions per replica. On a 13-node subnet, this is 65 billion instructions of free compute per HTTP outcall.

### Impact Explanation

An unprivileged canister can craft an HTTP outcall with a maximally expensive transform function (e.g., a tight computation loop up to the 5B instruction limit) and pay only the flat legacy fee. Since the transform runs on every replica independently, the attacker gets `subnet_size × 5B` instructions of free compute per request. With `CANISTER_HTTP_MAX_RESPONSES_PER_BLOCK = 500` responses per block and a 13-node subnet, this is up to `500 × 13 × 5B = 32.5 trillion` free instructions per block. This constitutes a **cycles/resource accounting bug**: the canister pays far less than the actual compute consumed, subsidized by the subnet. At scale, this can be used to degrade subnet throughput for other canisters by saturating the query execution capacity used for transform processing.

### Likelihood Explanation

The entry path is fully reachable by any unprivileged canister that can call `http_request` on the management canister with sufficient cycles to cover the flat legacy fee. The transform function name and context are user-supplied fields in `CanisterHttpRequestArgs`. The bug is structural and present in all currently deployed replicas since `PricingFactory::new_tracker` always returns `LegacyTracker`. The TODO comment confirms this is a known gap, not an oversight in a single path.

### Recommendation

1. **Charge for transform instructions in the legacy fee**: Add a term to `http_request_fee` that accounts for the maximum possible transform cost (e.g., proportional to `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE × ten_update_instructions_execution_fee`), or cap the transform instruction limit to a value already covered by the existing fee.
2. **Enforce `subtract_transform_usage` in `LegacyTracker`**: Rather than a no-op, track consumed instructions and return `Err(PricingError::InsufficientCycles)` if the transform exceeds a budget derived from the cycles already paid.
3. **Reduce the transform instruction limit**: Lower `get_transform_limit()` in `LegacyTracker` to a value commensurate with what the legacy fee actually covers, rather than the full `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE`.

### Proof of Concept

1. Deploy a canister on an application subnet with a query method `expensive_transform` that loops for ~5 billion instructions before returning the response unchanged.
2. Call `http_request` on the management canister with `transform = Some(TransformContext { function: expensive_transform, context: [] })` and attach only the minimum legacy fee (e.g., ~1.6T cycles for a 13-node subnet with 2MB response limit).
3. Observe that the request is accepted and the transform executes on all 13 replicas, consuming ~65 billion instructions of subnet compute, while the canister is charged only the flat legacy fee with no additional deduction for transform execution.

**Root cause chain:**
- `CanisterHttpRequestArgs.transform` → user-controlled method name
- `transform_adapter_response` in [1](#0-0)  calls `budget.get_transform_limit()` and then `budget.subtract_transform_usage()`
- `PricingFactory::new_tracker` in [2](#0-1)  always returns `LegacyTracker`
- `LegacyTracker::subtract_transform_usage` in [3](#0-2)  is a no-op returning `Ok(())`
- `LegacyTracker::get_transform_limit` in [4](#0-3)  returns the full `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE` = 5B instructions
- `http_request_fee` in [5](#0-4)  contains no transform instruction cost term
- `MAX_INSTRUCTIONS_PER_QUERY_MESSAGE` is defined as [6](#0-5)  5 billion instructions

### Citations

**File:** rs/https_outcalls/client/src/client.rs (L435-442)
```rust
async fn transform_adapter_response(
    budget: &mut dyn BudgetTracker,
    query_handler: TransformExecutionService,
    canister_http_response: CanisterHttpResponsePayload,
    transform_canister: CanisterId,
    transform: &Transform,
) -> (Result<Vec<u8>, CanisterHttpReject>, u64) {
    let transform_limit = budget.get_transform_limit();
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

**File:** rs/https_outcalls/pricing/src/legacy.rs (L40-42)
```rust
    fn get_transform_limit(&self) -> NumInstructions {
        MAX_INSTRUCTIONS_PER_QUERY_MESSAGE
    }
```

**File:** rs/https_outcalls/pricing/src/legacy.rs (L44-46)
```rust
    fn subtract_transform_usage(&mut self, _usage: NumInstructions) -> Result<(), PricingError> {
        Ok(())
    }
```

**File:** rs/cycles_account_manager/src/cycles_account_manager.rs (L1235-1239)
```rust
        let amount = (self.config.http_request_linear_baseline_fee
            + self.config.http_request_quadratic_baseline_fee * (subnet_size as u64)
            + self.config.http_request_per_byte_fee * request_size.get()
            + self.config.http_response_per_byte_fee * response_size)
            * (subnet_size as u64);
```

**File:** rs/config/src/subnet_config.rs (L41-41)
```rust
pub const MAX_INSTRUCTIONS_PER_QUERY_MESSAGE: NumInstructions = NumInstructions::new(5 * B);
```
