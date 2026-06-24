### Title
Fractional Division in `per_replica_allowance` Calculation Permanently Burns Cycles - (File: `rs/execution_environment/src/execution_environment.rs`)

### Summary
When computing the per-replica refund allowance for canister HTTP outcalls, integer floor division is used to split `refundable_cycles` across `node_count` replicas. The remainder (`refundable_cycles % node_count`) is permanently burned and never returned to the calling canister, breaking the cycles conservation invariant that the total refundable amount should be fully recoverable.

### Finding Description
In `rs/execution_environment/src/execution_environment.rs`, the `RefundStatus` for a canister HTTP outcall is initialized as:

```rust
per_replica_allowance: refundable_cycles / node_count,
``` [1](#0-0) 

where `node_count` is the committee size: [2](#0-1) 

The `Cycles` division operator uses integer (floor) division: [3](#0-2) 

The `RefundStatus` struct documents the invariant as `refunded_cycles <= refundable_cycles`: [4](#0-3) 

Because `per_replica_allowance = floor(refundable_cycles / node_count)`, the maximum total cycles that can ever be returned to the canister is:

```
node_count × floor(refundable_cycles / node_count)
```

This is strictly less than `refundable_cycles` whenever `refundable_cycles % node_count ≠ 0`. The remainder `refundable_cycles % node_count` cycles are permanently burned — they are neither returned to the canister nor credited anywhere else. The stated invariant (`refunded_cycles <= refundable_cycles`) is satisfied, but the stronger conservation property (that the canister can recover the full `refundable_cycles` it paid beyond the base fee) is broken.

This is structurally identical to the PaymentLib analog: a total is split across N parties using floor division, so the sum of the N parts is less than the total. In PaymentLib the shortfall was in debits (protocol lost funds); here the shortfall is in refunds (canister loses cycles).

### Impact Explanation
Any canister making a `HttpRequest` or `FlexibleHttpRequest` call whose `refundable_cycles` is not evenly divisible by the committee size permanently loses `refundable_cycles % node_count` cycles per call. For a fully replicated request on a 34-node subnet, up to 33 cycles are burned per call. For a `Flexible` HTTP request with a committee of N nodes, up to N−1 cycles are burned. These cycles are not credited to any fee collector or treasury — they are simply destroyed. This is a cycles/resource accounting bug: the sum of all per-replica allowances is strictly less than the total refundable amount the canister paid upfront.

### Likelihood Explanation
This triggers on virtually every HTTP outcall. The `refundable_cycles` value is `payment - base_fee`, where `payment` is caller-controlled and `base_fee` depends on request parameters. There is no mechanism that forces `refundable_cycles` to be divisible by the committee size. Any unprivileged canister making an HTTP outcall with a non-divisible payment amount will trigger this path.

### Recommendation
Replace the floor division with a ceiling division for `per_replica_allowance`, or distribute the remainder to one designated replica (e.g., the first in the committee). Using `div_ceil` ensures that `node_count × per_replica_allowance >= refundable_cycles`, so the canister can always recover the full refundable amount. Alternatively, track the remainder explicitly and add it to one replica's allowance so that the sum of all allowances equals `refundable_cycles` exactly.

### Proof of Concept
Consider a fully replicated HTTP outcall on a 34-node subnet where `refundable_cycles = 35`:

- `per_replica_allowance = floor(35 / 34) = 1`
- Maximum total refund = `34 × 1 = 34`
- Cycles permanently burned = `35 − 34 = 1`

For `refundable_cycles = 33`:

- `per_replica_allowance = floor(33 / 34) = 0`
- Maximum total refund = `34 × 0 = 0`
- Cycles permanently burned = `33`

Even if every replica claims its full `per_replica_allowance`, the canister cannot recover the full `refundable_cycles` it paid. The `refundable_cycles % node_count` remainder is irrecoverably burned on every such call.

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L2194-2198)
```rust
        let node_count = match &canister_http_request_context.replication {
            Replication::Flexible { committee, .. } => committee.len().max(1),
            Replication::NonReplicated(_) => 1,
            Replication::FullyReplicated => cycles_config.subnet_size.max(1),
        };
```

**File:** rs/execution_environment/src/execution_environment.rs (L2199-2204)
```rust
        canister_http_request_context.refund_status = RefundStatus {
            refundable_cycles,
            per_replica_allowance: refundable_cycles / node_count,
            refunded_cycles: Cycles::new(0),
            refunding_nodes: BTreeSet::new(),
        };
```

**File:** rs/types/cycles/src/cycles.rs (L171-177)
```rust
impl Div<u64> for Cycles {
    type Output = Self;

    fn div(self, rhs: u64) -> Self {
        Self(self.0.saturating_div(Cycles::from(rhs).0))
    }
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
