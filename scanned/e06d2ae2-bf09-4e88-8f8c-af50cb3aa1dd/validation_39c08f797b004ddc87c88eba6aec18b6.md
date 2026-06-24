### Title
Cycles Permanently Lost Due to Integer Division Rounding in `per_replica_allowance` Computation - (File: `rs/execution_environment/src/execution_environment.rs`)

### Summary
When a canister submits an HTTP outcall under the pay-as-you-go pricing model, the entire payment is taken upfront and the refundable portion is split across participating nodes as `per_replica_allowance = refundable_cycles / node_count`. Because this is integer (floor) division, the remainder `refundable_cycles % node_count` is silently discarded and can never be refunded to the caller. These cycles are permanently lost on every HTTP outcall whose refundable amount is not evenly divisible by the node count.

### Finding Description
In `rs/execution_environment/src/execution_environment.rs`, when processing a canister HTTP request, the execution environment computes the `RefundStatus` for the request:

```rust
let refundable_cycles = canister_http_request_context.request.payment - base_fee.real();
let node_count = match &canister_http_request_context.replication {
    Replication::Flexible { committee, .. } => committee.len().max(1),
    Replication::NonReplicated(_) => 1,
    Replication::FullyReplicated => cycles_config.subnet_size.max(1),
};
canister_http_request_context.refund_status = RefundStatus {
    refundable_cycles,
    per_replica_allowance: refundable_cycles / node_count,  // integer division
    refunded_cycles: Cycles::new(0),
    refunding_nodes: BTreeSet::new(),
};
``` [1](#0-0) 

Under `PricingVersion::PayAsYouGo`, the entire payment is taken from the canister upfront:

```rust
canister_http_request_context.request.payment.take();
``` [2](#0-1) 

The `per_replica_allowance` is the maximum cycles each participating node may claim as a refund. The total maximum refund the canister can ever receive is therefore `per_replica_allowance * node_count`. Due to integer division, this equals `refundable_cycles - (refundable_cycles % node_count)`. The remainder `refundable_cycles % node_count` is never credited back to the canister — it is permanently burned.

The `RefundStatus` struct documents this invariant without accounting for the rounding loss:

```rust
pub struct RefundStatus {
    pub refundable_cycles: Cycles,
    /// per_replica_allowance = refundable_cycles / committee_size
    pub per_replica_allowance: Cycles,
    pub refunded_cycles: Cycles,
    pub refunding_nodes: BTreeSet<NodeId>,
}
``` [3](#0-2) 

The consensus layer enforces that no node may claim more than `per_replica_allowance`:

```rust
pub(crate) fn check_refund_allowance(
    receipt: &CanisterHttpPaymentReceipt,
    per_replica_allowance: Cycles,
) -> Result<(), InvalidCanisterHttpPayloadReason> {
    if receipt.refund > per_replica_allowance {
        return Err(InvalidCanisterHttpPayloadReason::RefundExceedsAllowance { ... });
    }
    Ok(())
}
``` [4](#0-3) 

This means even if every node claims the maximum refund, the total returned to the canister is `per_replica_allowance * node_count < refundable_cycles`, and the remainder is irrecoverable.

### Impact Explanation
Cycles are permanently destroyed on every HTTP outcall where `refundable_cycles % node_count != 0`. For a fully-replicated request on a 34-node subnet, up to 33 cycles are lost per request. While small per-call, the loss is cumulative across all canisters and all subnets. The canister pays the full payment upfront but can never recover the full refundable portion, violating cycles conservation. This is a ledger conservation bug: cycles are debited from the canister but the remainder is never credited anywhere.

### Likelihood Explanation
This affects every pay-as-you-go HTTP outcall where `refundable_cycles` is not a multiple of `node_count`. Since `refundable_cycles = payment - base_fee` and both `payment` (caller-controlled) and `base_fee` (fee schedule) are arbitrary integers, the remainder is non-zero in the vast majority of calls. Any canister making HTTP outcalls is affected without any special conditions.

### Recommendation
Compute `per_replica_allowance` using ceiling division, or explicitly add the remainder to one node's allowance (e.g., the first node), or track the remainder separately and refund it unconditionally when the request is finalized. The fix mirrors the Beefy mitigation: instead of `refundable_cycles / node_count`, use `(refundable_cycles + node_count - 1) / node_count` for the allowance, or after distributing `per_replica_allowance * node_count`, return the remainder directly to the caller canister at finalization time.

### Proof of Concept
Consider a canister on a 34-node subnet making a pay-as-you-go HTTP outcall with `payment = 1_000_000_007` cycles and `base_fee = 500_000_000` cycles:

- `refundable_cycles = 500_000_007`
- `per_replica_allowance = 500_000_007 / 34 = 14_705_882` (floor)
- Maximum total refund = `14_705_882 * 34 = 499_999_988`
- **Permanently lost = `500_000_007 - 499_999_988 = 19` cycles**

Even if all 34 nodes claim their full `per_replica_allowance`, the canister receives back only `499_999_988` cycles instead of `500_000_007`. The 19-cycle remainder is burned with no accounting entry. Across millions of HTTP outcalls on all IC subnets, this constitutes a systematic, cumulative cycles conservation violation reachable by any unprivileged canister caller. [5](#0-4)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L2194-2204)
```rust
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
```

**File:** rs/execution_environment/src/execution_environment.rs (L2213-2219)
```rust
            PricingVersion::PayAsYouGo => {
                // Take out the entire payment upfront; the refundable portion is
                // returned later via the refund mechanism. On a free cost
                // schedule there is nothing to charge.
                if cost_schedule != CanisterCyclesCostSchedule::Free {
                    canister_http_request_context.request.payment.take();
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
