### Title
SNS Swap Deadline Advances During Subnet Downtime, Causing Unfair Swap Abortion — (`rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister's `swap_due_timestamp_seconds` is a fixed wall-clock deadline with no mechanism to account for application subnet downtime. If the subnet hosting the swap canister experiences an outage during the swap's open period, participants cannot contribute ICP, but the deadline continues to advance. When the subnet recovers, `run_periodic_tasks` immediately calls `try_abort`, which aborts the swap because the deadline has passed and sufficient participation was not reached — even though the participation shortfall was caused entirely by the outage, not by lack of interest.

### Finding Description

The `can_abort` function evaluates:

```rust
(self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
    && !self.sufficient_participation()
``` [1](#0-0) 

`swap_due` compares `now_seconds` against the fixed `swap_due_timestamp_seconds` field: [2](#0-1) 

There is no downtime-awareness anywhere in this path. The canister does not track how long it was unavailable, does not extend the deadline after recovery, and does not distinguish between "deadline passed with full participation opportunity" and "deadline passed while the subnet was offline."

The `run_periodic_tasks` heartbeat calls `try_abort` unconditionally on every tick: [3](#0-2) 

The first heartbeat after subnet recovery will therefore abort any swap whose deadline elapsed during the outage, regardless of whether the participation shortfall was caused by the outage. The `try_abort` transition is irreversible: [4](#0-3) 

The swap duration is validated to be between `MIN_SALE_DURATION_SECONDS` (1 day) and `MAX_SALE_DURATION_SECONDS` (14 days): [5](#0-4) 

No grace period or downtime-compensation logic exists anywhere in the swap lifecycle.

### Impact Explanation

1. **SNS decentralization fails** due to subnet downtime rather than genuine lack of community interest. The SNS project must restart the entire `CreateServiceNervousSystem` NNS proposal process.
2. **Participants' ICP is locked** in the swap canister's per-user subaccounts for the duration of the outage and cannot be withdrawn until `finalize` is called post-abort.
3. **Neurons' Fund maturity** reserved for the swap is locked during the outage period and is only released after the abort is finalized via `settle_neurons_fund_participation` back to NNS governance. [6](#0-5) 

4. **Reputational and economic harm** to the SNS project: a failed decentralization swap signals failure to the market even when the root cause was infrastructure, not community rejection.

### Likelihood Explanation

Application subnets have experienced outages on the IC mainnet. SNS swap canisters are deployed on ordinary application subnets with no special availability guarantees — unlike the NNS subnet. The swap duration window is 1–14 days, so even a multi-hour outage near the end of a swap period is sufficient to abort a swap that was on track to succeed. The `swap_due_timestamp_seconds` is set at proposal execution time and is immutable thereafter: [7](#0-6) 

There is no on-chain mechanism to extend it after the fact.

### Recommendation

The SNS swap canister should either:

1. **Track effective downtime** by recording the last heartbeat timestamp and extending `swap_due_timestamp_seconds` by the gap between expected and actual heartbeat intervals when the canister resumes; or
2. **Implement a post-recovery grace period** before allowing `try_abort` to execute, giving participants time to contribute ICP they could not contribute during the outage — analogous to the sequencer grace period recommended in the Arcadia fix.

### Proof of Concept

1. An SNS swap is opened with a 7-day duration (`swap_due_timestamp_seconds = T + 7 days`).
2. After 6 days, the swap has 80% of the required ICP — close to `sufficient_participation()` but not yet there.
3. The application subnet hosting the swap canister goes offline for 30 hours (a realistic outage duration based on historical IC subnet incidents).
4. During the outage, participants who intended to contribute the remaining 20% cannot reach the canister. No ingress messages are processed; no heartbeats fire.
5. The subnet recovers at `now = T + 7 days + 6 hours` (past the deadline).
6. The first heartbeat calls `run_periodic_tasks(now_fn)` → `try_abort(now)`.
7. `can_abort` returns `true`: `swap_due(now)` is `true` (deadline passed), `sufficient_participation()` is `false` (80% < threshold).
8. The swap transitions irreversibly to `Lifecycle::Aborted`. All ICP is refunded. The SNS decentralization fails.
9. Had the subnet been online, the swap would have committed within the final 24 hours. [8](#0-7)

### Citations

**File:** rs/sns/swap/src/swap.rs (L991-1002)
```rust
    /// Tries to transition the Swap Lifecycle to `Lifecycle::Aborted`.
    /// Returns true if a transition was made, and false otherwise.
    pub fn try_abort(&mut self, now_seconds: u64) -> bool {
        if !self.can_abort(now_seconds) {
            return false;
        }

        self.set_lifecycle(Lifecycle::Aborted);
        self.decentralization_swap_termination_timestamp_seconds = Some(now_seconds);

        true
    }
```

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

**File:** rs/sns/swap/src/swap.rs (L2906-2914)
```rust
    pub fn can_abort(&self, now_seconds: u64) -> bool {
        if self.lifecycle() != Lifecycle::Open {
            return false;
        }

        // if the swap is due or the ICP target is reached without sufficient participation, we can abort
        (self.swap_due(now_seconds) || self.icp_target_progress().is_reached_or_exceeded())
            && !self.sufficient_participation()
    }
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L98-103)
```rust
/// Step 3b. (State ABORTED). If the parameters of the swap have not
/// been satisfied before the due date/time, the swap is aborted and
/// the ICP tokens transferred back to their respective owners. The
/// swap can also be aborted early if it is determined that the
/// swap cannot possibly succeed, e.g., because the ICP ceiling has
/// been reached and the minimum number of participants has not been.
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L650-654)
```rust
    /// The date/time when the swap is due, i.e., it will automatically
    /// end and commit or abort depending on whether the parameters have
    /// been fulfilled.
    #[prost(uint64, tag = "6")]
    pub swap_due_timestamp_seconds: u64,
```

**File:** rs/sns/swap/src/types.rs (L456-485)
```rust
    pub fn is_valid_if_initiated_at(&self, now_seconds: u64) -> Result<(), String> {
        let sale_delay_seconds = self.sale_delay_seconds.unwrap_or(0);

        let open_timestamp_seconds = now_seconds.saturating_add(sale_delay_seconds);
        let duration_seconds = self
            .swap_due_timestamp_seconds
            .saturating_sub(open_timestamp_seconds);

        if duration_seconds < Self::MIN_SALE_DURATION_SECONDS {
            return Err(format!(
                "If the swap were initiated at the requested time ({}), its duration would be \
                    {} seconds, but MIN_SALE_DURATION_SECONDS = {}.",
                now_seconds,
                duration_seconds,
                Self::MIN_SALE_DURATION_SECONDS,
            ));
        }
        // Swap can be at most MAX_SALE_DURATION_SECONDS long
        if duration_seconds > Self::MAX_SALE_DURATION_SECONDS {
            return Err(format!(
                "If the swap were initiated at the requested time ({}), its duration would be \
                    {} seconds, but MAX_SALE_DURATION_SECONDS = {}.",
                now_seconds,
                duration_seconds,
                Self::MAX_SALE_DURATION_SECONDS,
            ));
        }

        Ok(())
    }
```

**File:** rs/nns/governance/src/governance.rs (L4456-4484)
```rust
        let (swap_start_timestamp_seconds, swap_due_timestamp_seconds) = {
            let start_time = create_service_nervous_system
                .swap_parameters
                .as_ref()
                .and_then(|swap_parameters| swap_parameters.start_time);

            let duration = create_service_nervous_system
                .swap_parameters
                .as_ref()
                .and_then(|swap_parameters| swap_parameters.duration);

            CreateServiceNervousSystem::swap_start_and_due_timestamps(
                start_time.unwrap_or(random_swap_start_time),
                duration.unwrap_or_default(),
                current_timestamp_seconds,
            )
        }?;

        let sns_init_payload = SnsInitPayload::try_from(ApiCreateServiceNervousSystem::from(
            create_service_nervous_system,
        ))?;

        Ok(SnsInitPayload {
            neurons_fund_participation_constraints,
            nns_proposal_id: Some(proposal_id.id),
            swap_start_timestamp_seconds: Some(swap_start_timestamp_seconds),
            swap_due_timestamp_seconds: Some(swap_due_timestamp_seconds),
            ..sns_init_payload
        })
```
