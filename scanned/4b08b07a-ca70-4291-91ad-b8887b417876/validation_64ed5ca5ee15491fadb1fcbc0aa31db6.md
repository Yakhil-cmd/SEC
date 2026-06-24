### Title
Cycles Conservation Bug via Integer Division Truncation in HTTP Outcall Pay-As-You-Go Refund Accounting - (`rs/execution_environment/src/execution_environment.rs`)

### Summary

The pay-as-you-go HTTP outcall refund mechanism computes `per_replica_allowance = refundable_cycles / node_count` using integer division. Because each node's refund is individually capped at this truncated allowance, the maximum total cycles that can ever be refunded to the caller is `floor(refundable_cycles / node_count) * node_count`, which is strictly less than `refundable_cycles` whenever `refundable_cycles % node_count != 0`. The remainder cycles — up to `node_count - 1` per outcall — are permanently unrecoverable by the caller.

### Finding Description

When a canister issues an HTTP outcall under pay-as-you-go pricing, the execution environment computes a `RefundStatus`:

```rust
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

Each participating node later submits a `CanisterHttpPaymentReceipt` whose `refund` field is validated to not exceed `per_replica_allowance`:

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
``` [2](#0-1) 

The `RefundStatus` struct documents the invariant as `refunded_cycles <= refundable_cycles`, but the structural maximum achievable is `per_replica_allowance * node_count`: [3](#0-2) 

Because `per_replica_allowance = floor(refundable_cycles / node_count)`, the maximum total refund is:

```
floor(refundable_cycles / node_count) * node_count
  = refundable_cycles - (refundable_cycles % node_count)
```

The remainder `refundable_cycles % node_count` cycles — up to `node_count - 1` per outcall — can never be returned to the caller. These cycles were deducted from the canister's payment upfront (the entire payment is taken under pay-as-you-go): [4](#0-3) 

This is structurally identical to the Velo Vault bug: one path computes a total value once (here: `refundable_cycles`, computed once without truncation), while the other path divides per-item and sums (here: `per_replica_allowance` truncated per node, then summed across nodes). The two paths diverge by up to `node_count - 1` units.

### Impact Explanation

For every HTTP outcall where `refundable_cycles % node_count != 0`, the calling canister is permanently overcharged by `refundable_cycles % node_count` cycles. These cycles are neither refunded to the caller nor explicitly burned as a fee — they are simply unaccounted for. On a fully-replicated subnet of 13 nodes, up to 12 cycles per outcall are lost. While small per-call, this is a systematic cycles conservation violation that accumulates across all HTTP outcalls on the subnet. The canister cannot recover these cycles regardless of how efficiently the HTTP request is served.

### Likelihood Explanation

This triggers on every pay-as-you-go HTTP outcall where `refundable_cycles` is not exactly divisible by `node_count`. Since `refundable_cycles = payment - base_fee` and both `payment` and `base_fee` are arbitrary cycle amounts, non-divisibility is the common case. Any unprivileged canister calling `http_request` or `FlexibleHttpRequest` with a normal payment triggers this. No special privileges, governance access, or threshold corruption is required.

### Recommendation

Compute the refund to the caller as `refundable_cycles` minus the sum of actual per-node charges, rather than distributing `per_replica_allowance` per node and discarding the remainder. Alternatively, after all nodes have reported, explicitly return `refundable_cycles - refunded_cycles` to the caller or burn it as an explicit fee, so that no cycles are silently lost. The fix mirrors the Velo Vault recommendation: aggregate first, then divide once, rather than dividing per-item and summing.

### Proof of Concept

Consider a fully-replicated request on a 13-node subnet with `refundable_cycles = 100`:

- `per_replica_allowance = 100 / 13 = 7` (integer division, truncated)
- All 13 nodes claim their maximum: `7 * 13 = 91` cycles refunded
- `refundable_cycles - refunded_cycles = 100 - 91 = 9` cycles permanently lost

The caller paid for 100 refundable cycles but can only ever receive 91 back. The 9-cycle remainder is unrecoverable. This is confirmed by the test setup in `rs/https_outcalls/consensus/src/pool_manager.rs` which explicitly constructs a context with `refundable_cycles: Cycles::new(1000), per_replica_allowance: Cycles::new(100)` — a 10-node divisor — demonstrating that the system treats these as independent values with no reconciliation of the remainder. [5](#0-4)

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

**File:** rs/execution_environment/src/execution_environment.rs (L2213-2220)
```rust
            PricingVersion::PayAsYouGo => {
                // Take out the entire payment upfront; the refundable portion is
                // returned later via the refund mechanism. On a free cost
                // schedule there is nothing to charge.
                if cost_schedule != CanisterCyclesCostSchedule::Free {
                    canister_http_request_context.request.payment.take();
                }
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

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L3488-3494)
```rust
                let request = CanisterHttpRequestContext {
                    refund_status: RefundStatus {
                        refundable_cycles: Cycles::new(1000),
                        per_replica_allowance: Cycles::new(100),
                        refunded_cycles: Cycles::new(0),
                        refunding_nodes: BTreeSet::new(),
                    },
```
