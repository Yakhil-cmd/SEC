### Title
Unbounded Neuron Iteration in SNS Governance `compute_ballots_for_new_proposal` Causes Instruction-Limit DoS on Proposal Submission — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS governance canister's `compute_ballots_for_new_proposal` function iterates over every neuron in `self.proto.neurons` without any instruction-limit guard. This function is called synchronously inside `make_proposal`, which is an update call reachable by any SNS neuron holder. As the neuron count grows, the per-call instruction cost grows linearly. Beyond a threshold, `make_proposal` traps due to the IC's per-message instruction limit, permanently DoS-ing proposal submission for the entire SNS.

---

### Finding Description

`SNS Governance::compute_ballots_for_new_proposal` iterates over the full `self.proto.neurons` `BTreeMap` to build the electoral roll for every new proposal: [1](#0-0) 

For each neuron the loop calls `v.dissolve_delay_seconds(now_seconds)` and `v.voting_power(now_seconds, ...)`, both of which involve multiple arithmetic operations. There is no `is_message_over_threshold` check, no batching, and no early exit inside the loop.

This function is called unconditionally from `make_proposal`: [2](#0-1) 

and again from `compute_cached_metrics` (the heartbeat/timer path): [3](#0-2) 

By contrast, the NNS governance has already addressed the identical problem by replacing the direct neuron iteration with a pre-computed, snapshotted voting-power map and an instruction-limit guard: [4](#0-3) 

The SNS governance has not received this mitigation.

---

### Impact Explanation

**Vulnerability class**: cycles/resource accounting bug — unbounded synchronous loop over an unbounded collection inside a replicated update call.

The IC enforces a hard per-message instruction limit (5 billion instructions for update calls on application subnets). Once the number of SNS neurons is large enough that a single pass through `self.proto.neurons` exceeds this limit, every call to `make_proposal` traps. Because the loop runs before any state mutation, no proposal can ever be submitted again. The SNS governance canister becomes permanently unable to accept new proposals, which is a complete governance freeze.

The same loop inside `compute_cached_metrics` (called from the timer) would also trap, degrading the canister's ability to maintain cached metrics.

---

### Likelihood Explanation

SNS canisters are deployed by third-party projects and can accumulate neurons organically through token swaps, staking, and airdrops. There is no hard neuron cap enforced in the SNS governance code analogous to the NNS `MAX_NUMBER_OF_NEURONS`. A popular SNS with tens of thousands of neurons is realistic. The NNS governance team already identified this exact pattern as a problem and fixed it for NNS (via voting-power snapshots); the SNS governance has not received the same fix.

Any unprivileged principal who holds SNS tokens can stake a neuron and call `make_proposal`. The attacker does not need to cause the neuron count to grow themselves — they only need to wait until organic growth pushes the SNS past the instruction threshold, or they can accelerate growth by staking many small neurons (if the SNS minimum stake is low).

---

### Recommendation

1. **Cache the electoral roll** using the same snapshot mechanism already used by NNS governance (`compute_voting_power_snapshot_for_standard_proposal` + `VOTING_POWER_SNAPSHOTS`). Compute the snapshot in a periodic timer task with an instruction-limit guard, and read from the snapshot inside `make_proposal`.

2. **Add an instruction-limit guard** inside the loop (analogous to `is_message_over_threshold` / `noop_self_call_if_over_instructions`) so that if the loop cannot complete in one message, it fails gracefully rather than trapping.

3. **Enforce a maximum neuron count** for SNS governance, analogous to the NNS `MAX_NUMBER_OF_NEURONS` limit.

---

### Proof of Concept

**Attacker-controlled entry path**:

1. Any principal stakes SNS tokens to create a neuron with `SubmitProposal` permission.
2. The principal calls `make_proposal` on the SNS governance canister.
3. `make_proposal` calls `compute_ballots_for_new_proposal` synchronously.
4. `compute_ballots_for_new_proposal` iterates all `N` neurons in `self.proto.neurons`, calling `dissolve_delay_seconds` and `voting_power` per neuron.
5. When `N` is large enough that the loop exceeds ~5 billion instructions, the update message traps.
6. Every subsequent `make_proposal` call also traps — the SNS is governance-frozen.

**Relevant code path**:

```
make_proposal (update, any neuron holder)
  └─ compute_ballots_for_new_proposal          [rs/sns/governance/src/governance.rs:5226]
       └─ for (k, v) in self.proto.neurons     [rs/sns/governance/src/governance.rs:5255]
            └─ v.voting_power(...)             [rs/sns/governance/src/neuron.rs:196]
                 └─ arithmetic on stake, dissolve delay, age bonus (no limit check)
``` [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** rs/sns/governance/src/governance.rs (L5225-5227)
```rust
    /// Computes the total potential voting power of the governance canister and ballots.
    fn compute_ballots_for_new_proposal(&self) -> Result<(u64, BTreeMap<String, Ballot>), String> {
        let now_seconds = self.env.now();
```

**File:** rs/sns/governance/src/governance.rs (L5252-5280)
```rust
        let mut electoral_roll = BTreeMap::<String, Ballot>::new();
        let mut total_power: u128 = 0;

        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

**File:** rs/sns/governance/src/governance.rs (L5297-5388)
```rust
    pub(crate) async fn compute_cached_metrics(&mut self) {
        let now_seconds = self.env.now();

        if let Some(GovernanceCachedMetrics {
            timestamp_seconds, ..
        }) = self.proto.metrics
            && now_seconds.saturating_sub(timestamp_seconds) < ONE_HOUR_SECONDS
        {
            return;
        }

        let num_treasury_metrics = self
            .proto
            .metrics
            .as_ref()
            .map(|metrics| metrics.treasury_metrics.len())
            .unwrap_or_default();

        if num_treasury_metrics < 2 {
            // If we don't have too few treasury metrics, initialize them.
            log!(
                INFO,
                "Initializing cached metrics at {} ...",
                format_timestamp_for_humans(now_seconds),
            );
            self.init_cached_metrics().await;
        } else {
            log!(
                INFO,
                "Refreshing cached metrics at {} ...",
                format_timestamp_for_humans(now_seconds),
            );
        }

        let mut metrics = self.proto.metrics.clone().unwrap_or_default();

        metrics.timestamp_seconds = now_seconds;

        let mut treasury_metrics = vec![];

        for TreasuryMetrics {
            // These fields remain unchanged.
            treasury,
            name,
            ledger_canister_id,
            account,
            original_amount_e8s,

            // These fields can change over time.
            amount_e8s: _,
            timestamp_seconds: _,
        } in metrics.treasury_metrics
        {
            let amount_e8s = match self.treasury_valuation_amount_e8s(treasury).await {
                Ok(amount) => amount,
                Err(err) => {
                    log!(ERROR, "Failed to compute_cached_metrics: {}", err);
                    continue;
                }
            };

            treasury_metrics.push(TreasuryMetrics {
                treasury,
                name,
                ledger_canister_id,
                account,
                amount_e8s,
                original_amount_e8s,
                timestamp_seconds: now_seconds,
            });
        }

        metrics.treasury_metrics = treasury_metrics;

        match self.compute_ballots_for_new_proposal() {
            Ok((governance_total_potential_voting_power, _)) => {
                metrics.voting_power_metrics = Some(VotingPowerMetrics {
                    governance_total_potential_voting_power,
                    timestamp_seconds: now_seconds,
                });
            }
            Err(err) => {
                log!(
                    ERROR,
                    "Failed to compute total potential voting power: {}",
                    err
                );
            }
        };

        self.proto.metrics.replace(metrics);
    }
```

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L163-170)
```rust
        self.with_active_neurons_iter_sections(
            |iter| {
                for neuron in iter {
                    process_neuron(&neuron);
                }
            },
            NeuronSections::NONE,
        );
```

**File:** rs/sns/governance/src/neuron.rs (L196-251)
```rust
    pub fn voting_power(
        &self,
        now_seconds: u64,
        max_dissolve_delay_seconds: u64,
        max_neuron_age_for_age_bonus: u64,
        max_dissolve_delay_bonus_percentage: u64,
        max_age_bonus_percentage: u64,
    ) -> u64 {
        // We compute the stake adjustments in u128.
        let stake = self.voting_power_stake_e8s() as u128;
        // Dissolve delay is capped to max_dissolve_delay_seconds, but we cap it
        // again here to make sure, e.g., if this changes in the future.
        let d = std::cmp::min(
            self.dissolve_delay_seconds(now_seconds),
            max_dissolve_delay_seconds,
        ) as u128;
        // 'd_stake' is the stake with bonus for dissolve delay.
        let d_stake = stake
            + if max_dissolve_delay_seconds > 0 {
                (stake * d * max_dissolve_delay_bonus_percentage as u128)
                    / (100 * max_dissolve_delay_seconds as u128)
            } else {
                0
            };
        // Sanity check.
        assert!(d_stake <= stake + (stake * (max_dissolve_delay_bonus_percentage as u128) / 100));
        // The voting power is also a function of the age of the
        // neuron, giving a bonus of up to max_age_bonus_percentage at max_neuron_age_for_age_bonus.
        let a = std::cmp::min(self.age_seconds(now_seconds), max_neuron_age_for_age_bonus) as u128;
        let ad_stake = d_stake
            + if max_neuron_age_for_age_bonus > 0 {
                (d_stake * a * max_age_bonus_percentage as u128)
                    / (100 * max_neuron_age_for_age_bonus as u128)
            } else {
                0
            };
        // Final stake 'ad_stake' has is not more than max_age_bonus_percentage above 'd_stake'.
        assert!(ad_stake <= d_stake + (d_stake * (max_age_bonus_percentage as u128) / 100));

        // Convert the multiplier to u128. The voting_power_percentage_multiplier represents
        // a percent and will always be within the range 0 to 100.
        let v = self.voting_power_percentage_multiplier as u128;

        // Apply the multiplier to 'ad_stake' and divide by 100 to have the same effect as
        // multiplying by a percent.
        let vad_stake = ad_stake
            .checked_mul(v)
            .expect("Overflow detected when calculating voting power")
            .checked_div(100)
            .expect("Underflow detected when calculating voting power");

        // The final voting power is the stake adjusted by both age,
        // dissolve delay, and voting power multiplier. If the stake is is greater than
        // u64::MAX divided by 2.5, the voting power may actually not
        // fit in a u64.
        std::cmp::min(vad_stake, u64::MAX as u128) as u64
```
