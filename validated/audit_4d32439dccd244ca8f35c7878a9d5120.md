### Title
Cycles Accounting Bug: `PricingFactory::new_tracker` Ignores `pricing_version`, Causing Permanent Loss of Refundable Cycles for `FlexibleHttpRequest` (PayAsYouGo) Callers — (`rs/https_outcalls/pricing/src/lib.rs`)

---

### Summary

`PricingFactory::new_tracker` unconditionally creates a `LegacyTracker` regardless of the request's `pricing_version`. For `FlexibleHttpRequest` calls (which always use `PricingVersion::PayAsYouGo`), the execution environment takes the caller's **entire payment upfront** and records a non-zero `refundable_cycles`. However, because the adapter always uses `LegacyTracker`, every per-replica `CanisterHttpPaymentReceipt` is hardcoded to `refund = 0`. The refundable portion is therefore never returned to the caller, causing permanent cycles loss.

---

### Finding Description

**Step 1 — Execution environment takes full payment upfront for PayAsYouGo.**

In `rs/execution_environment/src/execution_environment.rs` lines 2189–2219, when `pricing_version == PayAsYouGo`:

```rust
let refundable_cycles = if cost_schedule == CanisterCyclesCostSchedule::Free {
    Cycles::new(0)
} else {
    canister_http_request_context.request.payment - base_fee.real()
};
// ...
canister_http_request_context.refund_status = RefundStatus {
    refundable_cycles,
    per_replica_allowance: refundable_cycles / node_count,
    ...
};
// PayAsYouGo: take out the entire payment upfront
if cost_schedule != CanisterCyclesCostSchedule::Free {
    canister_http_request_context.request.payment.take();  // payment → 0
}
``` [1](#0-0) 

So `request.payment` becomes 0, and `refundable_cycles = original_payment - base_fee` is stored in the context, to be returned later via the per-replica refund mechanism.

**Step 2 — `PricingFactory::new_tracker` ignores `pricing_version` and always creates `LegacyTracker`.**

```rust
pub fn new_tracker(context: &CanisterHttpRequestContext) -> Box<dyn BudgetTracker> {
    // TODO(IC-1937): This should take into account context.pricing_version and a replica config.
    // Currently, we only support the legacy pricing version.
    Box::new(LegacyTracker::new(context.max_response_bytes))
}
``` [2](#0-1) 

**Step 3 — `LegacyTracker::create_payment_receipt()` always returns `refund = 0`.**

```rust
fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
    // Legacy pricing does not perform cycles accounting, so no cycles
    // are ever refunded.
    CanisterHttpPaymentReceipt::default()
}
``` [3](#0-2) 

**Step 4 — The adapter client explicitly rejects PayAsYouGo requests with `SysFatal`, using the zero-refund receipt.**

```rust
if request_pricing_version == ic_types::canister_http::PricingVersion::PayAsYouGo {
    warn!(log, "Canister HTTP request with PayAsYouGo pricing is not supported yet: ...");
    let _ = permit.send((
        CanisterHttpResponse { ... SysFatal reject ... },
        budget.create_payment_receipt(),  // refund = 0
    ));
    return;
}
``` [4](#0-3) 

**Step 5 — `check_refund_allowance` passes (0 ≤ per_replica_allowance), so the zero-refund receipt is accepted by consensus.** [5](#0-4) 

**Net result:** The caller paid `P` cycles. `base_fee` was legitimately consumed. `P - base_fee` should be refunded. Instead, `request.payment = 0` is returned and `payment_receipt.refund = 0` is applied. The caller permanently loses `P - base_fee` cycles.

---

### Impact Explanation

Any canister invoking `FlexibleHttpRequest` (which always uses `PayAsYouGo`) loses all cycles above `base_fee` permanently. The request is also rejected with `SysFatal`, so the canister receives neither the service nor its cycles back. This is a **cycles conservation violation**: cycles are destroyed without providing the corresponding service.

The `refundable_cycles` field in `RefundStatus` documents the invariant `refunded_cycles <= refundable_cycles`, but the actual refund mechanism (per-replica receipts) is broken for `PayAsYouGo` because `PricingFactory::new_tracker` never creates a `PayAsYouGoTracker`. [6](#0-5) 

---

### Likelihood Explanation

`FlexibleHttpRequest` is behind a feature flag (`with_flexible_http_requests_enabled()` in tests). If enabled on any application subnet, any canister caller can trigger this bug by calling `FlexibleHttpRequest` with any payment above `base_fee`. The `TODO(IC-1937)` comment in `PricingFactory::new_tracker` confirms this is a known incomplete implementation, not an intentional design choice. The `ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS` constant restricts the regular `HttpRequest` endpoint to legacy only, but `FlexibleHttpRequest` bypasses this restriction. [7](#0-6) 

---

### Recommendation

`PricingFactory::new_tracker` must branch on `context.pricing_version`:

```rust
pub fn new_tracker(context: &CanisterHttpRequestContext) -> Box<dyn BudgetTracker> {
    match context.pricing_version {
        PricingVersion::Legacy => Box::new(LegacyTracker::new(context.max_response_bytes)),
        PricingVersion::PayAsYouGo => Box::new(PayAsYouGoTracker::new(
            context.refund_status.per_replica_allowance,
            // ... other params
        )),
    }
}
```

Until a `PayAsYouGoTracker` is implemented, `FlexibleHttpRequest` should either be disabled or the execution environment should not take the full payment upfront for `PayAsYouGo` requests that will be rejected by the adapter.

---

### Proof of Concept

1. Deploy a canister on a subnet with `FlexibleHttpRequest` enabled.
2. Call `FlexibleHttpRequest` with payment `P = 1_000_000_000` cycles.
3. Execution environment: `request.payment.take()` → 0; `refundable_cycles = P - base_fee`.
4. Adapter: `PricingFactory::new_tracker` creates `LegacyTracker`; returns `SysFatal` reject with `payment_receipt.refund = 0`.
5. Consensus: `check_refund_allowance(0, per_replica_allowance)` passes.
6. Response delivered to canister: `msg_cycles_refunded() = 0`.
7. Canister has lost `P - base_fee` cycles with no service rendered. [2](#0-1) [3](#0-2) [8](#0-7)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L2189-2219)
```rust
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
            }
            PricingVersion::PayAsYouGo => {
                // Take out the entire payment upfront; the refundable portion is
                // returned later via the refund mechanism. On a free cost
                // schedule there is nothing to charge.
                if cost_schedule != CanisterCyclesCostSchedule::Free {
                    canister_http_request_context.request.payment.take();
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

**File:** rs/https_outcalls/pricing/src/legacy.rs (L48-52)
```rust
    fn create_payment_receipt(&self) -> CanisterHttpPaymentReceipt {
        // Legacy pricing does not perform cycles accounting, so no cycles
        // are ever refunded.
        CanisterHttpPaymentReceipt::default()
    }
```

**File:** rs/https_outcalls/client/src/client.rs (L155-178)
```rust
            if request_pricing_version == ic_types::canister_http::PricingVersion::PayAsYouGo {
                warn!(
                    log,
                    "Canister HTTP request with PayAsYouGo pricing is not supported yet: \
                    request_id {}, sender {}, process_id: {}",
                    request_id,
                    request_sender,
                    std::process::id(),
                );
                let _ = permit.send((
                    CanisterHttpResponse {
                        id: request_id,
                        canister_id: request_sender,
                        content: CanisterHttpResponseContent::Reject(CanisterHttpReject {
                            reject_code: RejectCode::SysFatal,
                            message:
                                "Canister HTTP request with PayAsYouGo pricing is not supported"
                                    .to_string(),
                        }),
                    },
                    budget.create_payment_receipt(),
                ));
                return;
            }
```

**File:** rs/https_outcalls/consensus/src/payload_builder/utils.rs (L81-92)
```rust
pub(crate) fn check_refund_allowance(
    receipt: &CanisterHttpPaymentReceipt,
    per_replica_allowance: Cycles,
) -> Result<(), InvalidCanisterHttpPayloadReason> {
    if receipt.refund > per_replica_allowance {
        return Err(InvalidCanisterHttpPayloadReason::RefundExceedsAllowance {
            refund: receipt.refund,
            per_replica_allowance,
        });
    }
    Ok(())
}
```

**File:** rs/types/types/src/canister_http.rs (L144-156)
```rust
#[derive(Clone, Eq, PartialEq, Hash, Debug, Deserialize, Serialize)]
pub struct RefundStatus {
    /// The amount of cycles that are available to be refunded for this request.
    /// The amount is calculated based on the payment of the request.
    pub refundable_cycles: Cycles,
    /// The amount of cycles that are allowed to be refunded for this request.
    /// The allowance is calculated based on the committee size: per_replica_allowance = refundable_cycles / committee_size.
    pub per_replica_allowance: Cycles,
    /// The amount of cycles that have already been refunded for this request.
    /// Invariant: refunded_cycles <= refundable_cycles
    pub refunded_cycles: Cycles,
    pub refunding_nodes: BTreeSet<NodeId>,
}
```

**File:** rs/types/management_canister_types/src/http.rs (L59-72)
```rust
pub const PRICING_VERSION_LEGACY: u32 = 1;
/// The numeric representation for the Pay-As-You-Go pricing version.
pub const PRICING_VERSION_PAY_AS_YOU_GO: u32 = 2;

/// The default pricing version for HTTP outcalls.
///
/// If the field is missing, this is the version that will be assumed by the replica.
/// Described in <https://internetcomputer.org/docs/current/references/ic-interface-spec/#ic-http_request>.
pub const DEFAULT_HTTP_OUTCALLS_PRICING_VERSION: u32 = PRICING_VERSION_LEGACY;

/// A set of all allowed pricing versions for HTTP outcalls.
///
/// If the pricing version provided in the request is not in this set, the request will use the default pricing version.
pub const ALLOWED_HTTP_OUTCALLS_PRICING_VERSIONS: &[u32] = &[PRICING_VERSION_LEGACY];
```
