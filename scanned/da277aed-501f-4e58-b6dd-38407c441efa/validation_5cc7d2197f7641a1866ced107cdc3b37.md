### Title
Single Buyer Can Abort SNS Swap Immediately When `max_participant_icp_e8s == max_direct_participation_icp_e8s` and `min_participants > 1` - (File: rs/sns/init/src/lib.rs)

### Summary

The SNS swap canister's `validate_participation_constraints` function does not reject configurations where a single buyer can fill the entire ICP target while `min_participants > 1`. When `max_participant_icp_e8s == max_direct_participation_icp_e8s` (the per-buyer cap equals the total swap ceiling), one buyer contributing the full amount triggers `icp_target_reached` without `sufficient_participation`, causing the swap to abort immediately — before the due date and before other participants can join.

### Finding Description

The SNS swap lifecycle is governed by two conditions:

- `sufficient_participation`: `min_participants` reached **and** `min_direct_participation_icp_e8s` reached
- `icp_target_reached`: total direct ICP == `max_direct_participation_icp_e8s`

The abort path fires when `(swap_due || icp_target_reached) && !sufficient_participation`. [1](#0-0) 

The validation in `validate_participation_constraints` enforces:

- `max_direct_participation_icp_e8s >= min_direct_participation_icp_e8s`
- `max_participant_icp_e8s >= min_participant_icp_e8s`
- `min_participants * min_participant_icp_e8s <= max_direct_participation_icp_e8s` [2](#0-1) 

But there is **no check** that prevents `max_participant_icp_e8s == max_direct_participation_icp_e8s` when `min_participants > 1`. The proto documentation even explicitly endorses this as a way to "disable" the per-participant cap:

> "Can effectively be disabled by setting it to `max_icp_e8s`." [3](#0-2) 

The swap's own test suite confirms the abort-on-max-ICP behavior is intentional and reachable: [4](#0-3) 

### Impact Explanation

When `max_participant_icp_e8s == max_direct_participation_icp_e8s` and `min_participants > 1`:

1. A single buyer calls `refresh_buyer_token_e8s` with `max_direct_participation_icp_e8s` ICP.
2. `icp_target_progress().is_reached_or_exceeded()` becomes `true`.
3. `min_participation_reached()` is `false` (only 1 buyer, but `min_participants > 1`).
4. `can_abort()` returns `true` immediately — **before the swap due date**.
5. The swap transitions to `LIFECYCLE_ABORTED` on the next heartbeat.

The SNS fails to decentralize. All ICP is refunded, but the SNS governance tokens remain locked in the swap canister until `finalize` is called. The SNS project loses the decentralization window and must restart the entire NNS proposal process. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

The proto comment explicitly documents `max_participant_icp_e8s = max_direct_participation_icp_e8s` as a valid configuration to "disable" the per-participant cap. An SNS creator who wants no per-participant limit but still requires multiple participants (`min_participants > 1`) would naturally set this combination — exactly the dangerous configuration. The NNS governance validation passes it without error. Once the swap is open, any unprivileged buyer with sufficient ICP can trigger the abort by contributing the full target amount in a single `refresh_buyer_token_e8s` call. [7](#0-6) 

### Recommendation

Add a validation in `validate_participation_constraints` that rejects configurations where a single buyer can fill the entire swap when `min_participants > 1`:

```rust
if min_participants > 1 && max_participant_icp_e8s >= max_direct_participation_icp_e8s {
    return Err(format!(
        "Error: when min_participants ({min_participants}) > 1, \
         max_participant_icp_e8s ({max_participant_icp_e8s}) must be strictly less than \
         max_direct_participation_icp_e8s ({max_direct_participation_icp_e8s}), \
         otherwise a single buyer can abort the swap immediately by filling the ICP target."
    ));
}
``` [8](#0-7) 

### Proof of Concept

Using the existing `SwapBuilder` test harness:

```rust
// Configuration: min_participants=2, but max_participant == max_direct (single buyer can fill)
let mut swap = SwapBuilder::new()
    .with_lifecycle(Lifecycle::Open)
    .with_swap_start_due(None, Some(1000))
    .with_min_participants(2)
    .with_min_max_participant_icp(10, 100)   // max_participant = 100
    .with_min_max_direct_participation(50, 100) // max_direct = 100 == max_participant
    .build();

// Single buyer contributes the full max_direct amount
let buyers = btreemap! {
    PrincipalId::new_user_test_id(0).to_string() => BuyerState::new(100),
};
swap.buyers = buyers;
swap.update_derived_fields();

// icp_target_reached = true, sufficient_participation = false (only 1 buyer, need 2)
// Swap aborts immediately, well before due date (now=50, due=1000)
assert!(swap.try_abort(50));
assert_eq!(swap.lifecycle, Lifecycle::Aborted as i32);
```

This mirrors the M-03 pattern exactly: two thresholds (`max_participant_icp_e8s` and `max_direct_participation_icp_e8s`) being equal allows a single participant to trigger an unintended termination of the protocol. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1014-1054)
```rust
    pub async fn run_periodic_tasks(&mut self, now_fn: fn(bool) -> u64) {
        let periodic_task_start_seconds = now_fn(false);

        // Purge old tickets
        const NUMBER_OF_TICKETS_THRESHOLD: u64 = 100_000_000; // 100M * ~size(ticket) = ~25GB
        const TWO_DAYS_IN_NANOSECONDS: u64 = 60 * 60 * 24 * 2 * 1_000_000_000;
        const MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT: u64 = 100_000;

        self.try_purge_old_tickets(
            ic_cdk::api::time,
            NUMBER_OF_TICKETS_THRESHOLD,
            TWO_DAYS_IN_NANOSECONDS,
            MAX_NUMBER_OF_PRINCIPALS_TO_INSPECT,
        );

        // Automatically transition the state. Only one state transition per periodic task.

        // Auto-open the swap
        if self.try_open(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap opened at timestamp {}",
                periodic_task_start_seconds
            );
        }
        // Auto-commit the swap
        else if self.try_commit(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap committed at timestamp {}",
                periodic_task_start_seconds
            );
        }
        // Auto-abort the swap
        else if self.try_abort(periodic_task_start_seconds) {
            log!(
                INFO,
                "Swap aborted at timestamp {}",
                periodic_task_start_seconds
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L2794-2797)
```rust
    /// The minimum number of participants have been achieved, and the
    /// minimal total amount of direct participation has been reached.
    pub fn sufficient_participation(&self) -> bool {
        self.min_participation_reached() && self.min_direct_participation_icp_e8s_reached()
```

**File:** rs/sns/swap/src/swap.rs (L2838-2854)
```rust
    /// Returns the `IcpTargetProgress`, a structure summarizing the current progress in reaching
    /// the target total ICP amount (both direct and NF contributions).
    pub fn icp_target_progress(&self) -> IcpTargetProgress {
        if self.params.is_some() {
            let current_direct_participation_e8s = self.current_direct_participation_e8s();
            let max_direct_participation_e8s = self.max_direct_participation_e8s();
            match current_direct_participation_e8s.cmp(&max_direct_participation_e8s) {
                Ordering::Less => IcpTargetProgress::NotReached {
                    current_direct_participation_e8s,
                    max_direct_participation_e8s,
                },
                Ordering::Greater => IcpTargetProgress::Exceeded {
                    current_direct_participation_e8s,
                    max_direct_participation_e8s,
                },
                Ordering::Equal => IcpTargetProgress::Reached(max_direct_participation_e8s),
            }
```

**File:** rs/sns/swap/src/swap.rs (L2899-2914)
```rust
    /// Returns true if the Swap can be aborted at the specified
    /// timestamp, and false otherwise.
    ///
    /// Conditions:
    /// 1. The lifecycle of Swap is `Lifecycle::Open`
    /// 2. The Swap has ended (either the Swap is due or the maximum ICP target was reached) and there
    ///    has not been sufficient participation reached.
    pub fn can_abort(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Open {
            return false;
        }

        // if the swap is due or the ICP target is reached without sufficient participation, we can abort
        (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
            && !self.sufficient_participation()
    }
```

**File:** rs/sns/swap/src/swap.rs (L4672-4709)
```rust
    #[test]
    fn test_try_commit_or_abort_insufficient_participation_with_max_icp() {
        let sale_duration = 100;
        let time_remaining = 50;
        let now = sale_duration - time_remaining;
        let buyers = btreemap! {
            PrincipalId::new_user_test_id(0).to_string() => BuyerState::new(20),
        };
        let mut swap = SwapBuilder::new()
            .with_lifecycle(Lifecycle::Open)
            .with_buyers(buyers)
            .with_swap_start_due(None, Some(sale_duration))
            .with_min_participants(2)
            .with_min_max_participant_icp(10, 20)
            .with_min_max_direct_participation(10, 20)
            .build();
        swap.update_derived_fields();

        // test try_commit
        {
            let mut swap = swap.clone();
            let result = swap.try_commit(now);
            // swap should not commit because we have reached the max icp but
            // have not reached the minimum number of participants

            assert!(!result);
            assert_eq!(swap.lifecycle, Lifecycle::Open as i32);
        }
        // test try_abort
        {
            let result = swap.try_abort(now);
            // swap should abort because we have reached the max icp but
            // have not reached the minimum number of participants

            assert!(result);
            assert_eq!(swap.lifecycle, Lifecycle::Aborted as i32);
        }
    }
```

**File:** rs/sns/init/src/lib.rs (L1507-1580)
```rust
    ///     participants.
    /// (6) If the minimum required number of participants participate each with the minimum
    ///     required amount of ICP, the maximum ICP amount that the swap can obtain is not exceeded.
    /// (7) Determines the smallest SNS neuron size is greater than the SNS ledger transaction fee.
    /// (8) Required ICP participation amount is big enough to ensure that all participants will
    ///     end up with enough SNS tokens to form the right number of SNS neurons (after paying for
    ///     the SNS ledger transaction fee to create each such SNS neuron).
    ///
    /// (9) min_participant_icp_e8s is at least as big as `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`.
    ///     This ensures, that users upon calling `swap.refresh_buyer_token()` must participate
    ///     at least `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S` Hence, no malicious user can overflow
    ///     node's memory by participating with very low amounts.\
    ///
    /// * -- In the context of this function, swap participation-related parameters include:
    /// - `min_direct_participation_icp_e8s` - Required ICP amount for the swap to succeed.
    /// - `max_direct_participation_icp_e8s` - Maximum ICP amount that the swap can obtain.
    /// - `min_participant_icp_e8s`          - Required ICP participation amount.
    /// - `max_participant_icp_e8s`          - Maximum ICP amount from one participant.
    /// - `min_participants`                 - Required number of *direct* participants for the swap to succeed. This does not restrict the number of *Neurons' Fund* participants.
    /// - `initial_token_distribution.swap_distribution.initial_swap_amount_e8s` - How many SNS tokens will be distributed amoung all the swap participants if the swap succeeds.
    /// - `neuron_basket_construction_parameters` - How many SNS neurons will be created per participant.
    /// - `neuron_minimum_stake_e8s`         - Determines the smallest SNS neuron size.
    /// - `sns_transaction_fee_e8s`          - SNS ledger transaction fee, in particular, charged for SNS neuron creation at swap finalization.
    fn validate_participation_constraints(&self) -> Result<(), String> {
        // (1)
        let min_direct_participation_icp_e8s = self
            .min_direct_participation_icp_e8s
            .ok_or("Error: min_direct_participation_icp_e8s must be specified")?;

        let max_direct_participation_icp_e8s = self
            .max_direct_participation_icp_e8s
            .ok_or("Error: max_direct_participation_icp_e8s must be specified")?;

        let min_participant_icp_e8s = self
            .min_participant_icp_e8s
            .ok_or("Error: min_participant_icp_e8s must be specified")?;

        let max_participant_icp_e8s = self
            .max_participant_icp_e8s
            .ok_or("Error: max_participant_icp_e8s must be specified")?;

        let min_participants = self
            .min_participants
            .ok_or("Error: min_participants must be specified")?;

        let initial_swap_amount_e8s = self
            .get_swap_distribution()
            .map_err(|_| "Error: the SwapDistribution must be specified")?
            .initial_swap_amount_e8s;

        let neuron_basket_construction_parameters_count = self
            .neuron_basket_construction_parameters
            .as_ref()
            .ok_or("Error: neuron_basket_construction_parameters must be specified")?
            .count;

        let neuron_minimum_stake_e8s = self
            .neuron_minimum_stake_e8s
            .ok_or("Error: neuron_minimum_stake_e8s must be specified")?;

        let sns_transaction_fee_e8s = self
            .transaction_fee_e8s
            .ok_or("Error: transaction_fee_e8s must be specified")?;

        // (2)
        if min_direct_participation_icp_e8s == 0 {
            return Err("Error: min_direct_participation_icp_e8s must be > 0".to_string());
        }
        if min_participant_icp_e8s == 0 {
            return Err("Error: min_participant_icp_e8s must be > 0".to_string());
        }
        if min_participants == 0 {
            return Err("Error: min_participants must be > 0".to_string());
        }
```

**File:** rs/sns/init/src/lib.rs (L1589-1596)
```rust
        // (3)
        if max_direct_participation_icp_e8s < min_direct_participation_icp_e8s {
            return Err(format!(
                "Error: max_direct_participation_icp_e8s ({max_direct_participation_icp_e8s}) \
                 must be >= min_direct_participation_icp_e8s ({min_direct_participation_icp_e8s})"
            ));
        }
        if max_participant_icp_e8s < min_participant_icp_e8s {
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L600-604)
```text
  // The maximum amount of ICP that each buyer can contribute. Must be
  // greater than or equal to `min_participant_icp_e8s` and less than
  // or equal to `max_icp_e8s`. Can effectively be disabled by
  // setting it to `max_icp_e8s`.
  uint64 max_participant_icp_e8s = 5;
```
