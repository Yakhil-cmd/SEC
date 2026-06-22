### Title
NNS/SNS Neuron Can Start Dissolving Immediately After Staking, Bypassing Governance Commitment Guarantee - (File: `rs/sns/governance/src/governance.rs`)

### Summary

In both NNS and SNS governance, a neuron owner can call `StartDissolving` immediately after staking/claiming a neuron, with no minimum lock-in period before dissolving is permitted. Once the dissolve timer has been running long enough that the remaining dissolve delay still exceeds `neuron_minimum_dissolve_delay_to_vote_seconds`, the neuron continues to participate in governance (vote, make proposals, earn rewards) while simultaneously counting down toward full liquidity. This is the direct IC analog of the "signal undelegate immediately after delegation" vulnerability: the dissolve delay acts as the waiting period, and `StartDissolving` is the signal.

### Finding Description

The NNS `claim_neuron` function creates a new neuron in `NotDissolving` state with `INITIAL_NEURON_DISSOLVE_DELAY` (7 days). [1](#0-0) 

There is no restriction preventing the neuron owner from immediately calling `StartDissolving` right after claiming. The NNS `start_dissolving` method only checks that the neuron is currently in `NotDissolving` state: [2](#0-1) 

In SNS governance, the same pattern applies. The `check_command_is_valid_if_neuron_is_vesting` guard only blocks `StartDissolving` for neurons that have an explicit `vesting_period_seconds` set — neurons without a vesting period have no such protection: [3](#0-2) 

Once dissolving, the neuron's `dissolve_delay_seconds(now)` decreases over time. The ballot/voting eligibility check in `compute_ballots_for_new_proposal` only requires that the remaining dissolve delay exceeds `min_dissolve_delay_for_vote` at the moment of proposal creation: [4](#0-3) 

The same check applies in NNS via `compute_voting_power_snapshot_for_standard_proposal`: [5](#0-4) 

A neuron that started dissolving immediately after staking with, say, a 2-year dissolve delay will still be eligible to vote for up to ~1.5 years (until the remaining delay drops below 6 months). During this entire window, the neuron participates in governance with full voting power while its owner has already committed to exiting. The owner can monitor pending governance proposals and disburse the moment the neuron dissolves, having participated in governance decisions they have no long-term stake in.

### Impact Explanation

The dissolve delay is the IC's primary mechanism for aligning governance participants with long-term network outcomes. A neuron with a 2-year dissolve delay is supposed to represent a 2-year commitment. However, by starting dissolution immediately, the owner signals their intent to exit while still retaining full voting power and earning voting rewards for up to ~1.5 years. This undermines the economic security assumption of the NNS/SNS: that voters have skin in the game proportional to their dissolve delay. In SNS governance specifically, a large token holder could stake, immediately start dissolving, vote on critical proposals (e.g., treasury transfers, upgrade proposals) that benefit their exit position, and then disburse — all while appearing to be a committed long-term participant.

### Likelihood Explanation

This is trivially exploitable by any neuron owner via a standard `manage_neuron` call with `StartDissolving`. No special access, no threshold corruption, no social engineering is required. The entry path is: stake ICP/SNS tokens → claim neuron → immediately call `StartDissolving` → continue voting while dissolving. This is reachable by any unprivileged governance/ledger user.

### Recommendation

- **Short term**: In `compute_ballots_for_new_proposal` (SNS) and `compute_voting_power_snapshot_for_standard_proposal` (NNS), exclude neurons that are currently in `Dissolving` state from the electoral roll, or reduce their voting power proportionally to their remaining commitment. Alternatively, require a minimum non-dissolving lock period before `StartDissolving` is permitted.
- **Long term**: Review all timestamp-based eligibility checks in governance. Consider whether a neuron's *current* dissolve delay (which decreases while dissolving) is the right metric for voting eligibility, versus requiring the neuron to be in `NotDissolving` state with a sufficient delay.

### Proof of Concept

1. Alice stakes 1000 ICP and claims a neuron with a 2-year dissolve delay (NNS) or the SNS equivalent.
2. Immediately after claiming, Alice calls `manage_neuron` with `StartDissolving`. This succeeds with no restriction. [6](#0-5) 
3. Alice's neuron is now `Dissolving`. Its `dissolve_delay_seconds(now)` returns ~2 years initially, decreasing over time.
4. For the next ~1.5 years, Alice's neuron passes the `dissolve_delay_seconds(now) >= min_dissolve_delay_for_vote` check and is included in every proposal's electoral roll with full voting power. [7](#0-6) 
5. Alice votes on proposals that benefit her exit (e.g., approving a treasury transfer to an address she controls, or blocking a proposal that would lock tokens further).
6. After 2 years, Alice's neuron is fully dissolved. She calls `Disburse` and receives her 1000 ICP back, having participated in governance for 1.5 years with no actual long-term commitment.

### Citations

**File:** rs/nns/governance/src/governance.rs (L6000-6012)
```rust
        let neuron = NeuronBuilder::new(
            nid,
            subaccount,
            controller,
            DissolveStateAndAge::NotDissolving {
                dissolve_delay_seconds: INITIAL_NEURON_DISSOLVE_DELAY,
                aging_since_timestamp_seconds: now,
            },
            now,
        )
        .with_followees(self.heap_data.default_followees.clone())
        .with_kyc_verified(true)
        .build();
```

**File:** rs/nns/governance/src/neuron/types.rs (L622-636)
```rust
    /// If this neuron is not dissolving, start dissolving it.
    ///
    /// If the neuron is dissolving or dissolved, an error is returned.
    fn start_dissolving(&mut self, now_seconds: u64) -> Result<(), GovernanceError> {
        let dissolve_state_and_age = self.dissolve_state_and_age();
        if let DissolveStateAndAge::NotDissolving { .. } = dissolve_state_and_age {
            let new_disolved_dissolve_state_and_age =
                dissolve_state_and_age.start_dissolving(now_seconds);
            self.set_dissolve_state_and_age(new_disolved_dissolve_state_and_age);
            self.eight_year_gang_bonus_base_e8s = 0;
            Ok(())
        } else {
            Err(GovernanceError::new(ErrorType::RequiresNotDissolving))
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L4844-4895)
```rust
    /// Returns an error if the given neuron is vesting and the given command cannot be called by
    /// a vesting neuron
    fn check_command_is_valid_if_neuron_is_vesting(
        &self,
        neuron_id: &NeuronId,
        command: &manage_neuron::Command,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::{Command::*, configure::Operation::*};

        // If this is a "claim" call, the neuron doesn't exist yet, so we return (because no checks
        // can be made). A "refresh" call can be made on a vesting neuron, so in this case also
        // results in returning Ok.
        if let ClaimOrRefresh(_) = command {
            return Ok(());
        }

        let neuron = self.get_neuron_result(neuron_id)?;

        if !neuron.is_vesting(self.env.now()) {
            return Ok(());
        }

        let err = |op: &str| -> Result<(), GovernanceError> {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!("Neuron {neuron_id} is vesting and cannot call {op}"),
            ))
        };

        match command {
            Configure(configure) => match configure.operation {
                Some(IncreaseDissolveDelay(_)) => err("IncreaseDissolveDelay"),
                Some(StartDissolving(_)) => err("StartDissolving"),
                Some(StopDissolving(_)) => err("StopDissolving"),
                Some(SetDissolveTimestamp(_)) => err("SetDissolveTimestamp"),
                Some(ChangeAutoStakeMaturity(_)) => Ok(()),
                None => Ok(()),
            },
            Disburse(_) => err("Disburse"),
            Split(_) => err("Split"),
            Follow(_)
            | SetFollowing(_)
            | MakeProposal(_)
            | RegisterVote(_)
            | ClaimOrRefresh(_)
            | MergeMaturity(_)
            | DisburseMaturity(_)
            | AddNeuronPermissions(_)
            | RemoveNeuronPermissions(_)
            | StakeMaturity(_) => Ok(()),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
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

**File:** rs/nns/governance/src/neuron_store/voting_power.rs (L144-159)
```rust
        let mut process_neuron = |neuron: &Neuron| {
            if neuron.is_inactive(now_seconds)
                || neuron.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_seconds
            {
                return;
            }

            let (potential_voting_power, deciding_voting_power) =
                neuron.potential_and_deciding_voting_power(voting_power_economics, now_seconds);
            // We don't handle overflow here, as in `get_voting_power_as_u64` below,
            // the input arguments bigger than u64::MAX will result in an error.
            total_deciding_voting_power =
                total_deciding_voting_power.saturating_add(deciding_voting_power as u128);
            total_potential_voting_power =
                total_potential_voting_power.saturating_add(potential_voting_power as u128);
            voting_power_map.insert(neuron.id().id, deciding_voting_power);
```
