### Title
NNS Neurons' Fund Members Can Sandwich-Exit SNS Swap Participation to Avoid Maturity Draw - (File: rs/nns/governance/src/governance.rs)

### Summary

When a `CreateServiceNervousSystem` proposal is submitted and enters its voting period, any NNS neuron currently in the Neurons' Fund can call `LeaveCommunityFund` to exit before the proposal is executed. Because `draw_maturity_from_neurons_fund` reads the live membership list at execution time rather than a snapshot taken at proposal submission, the exiting neuron's maturity is never drawn. The neuron owner can then immediately rejoin the Neurons' Fund after the swap settles, sandwiching the maturity-draw event and shifting the matched-funding burden onto remaining participants.

### Finding Description

**Root cause — no snapshot at proposal submission time.**

When a `CreateServiceNervousSystem` proposal is adopted and executed, `do_create_service_nervous_system` calls `draw_maturity_from_neurons_fund`: [1](#0-0) 

Inside `draw_maturity_from_neurons_fund`, the live membership list is fetched at execution time: [2](#0-1) 

`list_active_neurons_fund_neurons` simply iterates over neurons whose `joined_community_fund_timestamp_seconds` is non-zero at the moment of the call: [3](#0-2) 

Membership is determined by the `joined_community_fund_timestamp_seconds` field: [4](#0-3) 

**The exit path — `leave_community_fund` has no timing restriction.**

`leave_community_fund` simply clears the timestamp field with no guard against pending proposals: [5](#0-4) 

**The re-entry path — `join_community_fund` has no cooldown.**

After the swap settles, the neuron owner can immediately rejoin: [6](#0-5) 

Both operations are exposed as standard `manage_neuron` `Configure` operations (`JoinCommunityFund` / `LeaveCommunityFund`) callable by any neuron controller: [7](#0-6) 

**Attack flow:**

1. Neuron N holds large maturity and is a Neurons' Fund member.
2. A `CreateServiceNervousSystem` proposal is submitted (voting period begins — typically days).
3. N's controller calls `LeaveCommunityFund` during the voting period.
4. The proposal is adopted; `do_create_service_nervous_system` executes synchronously.
5. `list_active_neurons_fund_neurons()` no longer includes N; N's maturity is not drawn.
6. The swap runs to completion (committed or aborted).
7. N's controller calls `JoinCommunityFund`; N is back in the fund for future swaps.

### Impact Explanation

**Governance authorization / accounting bug — Medium-High impact.**

- The SNS treasury receives less matched ICP than the Neurons' Fund participation constraints advertised to direct participants, potentially causing the swap to fall short of its minimum or to deliver fewer SNS neurons than expected.
- The exiting neuron avoids the maturity lock-up entirely: if the swap commits, it avoids the permanent conversion of maturity to ICP sent to the SNS; if the swap aborts, it avoids even the temporary lock-up.
- Because the matched-funding polynomial is computed over the *remaining* NF total maturity, a large exit shifts the effective participation curve, changing every remaining neuron's proportional contribution without their consent.
- No privileged access is required; any neuron controller can execute this with two standard `manage_neuron` calls.

### Likelihood Explanation

**Medium likelihood.**

- NNS proposals have voting periods of days to weeks, giving ample time to observe a pending `CreateServiceNervousSystem` proposal and act.
- The two required calls (`LeaveCommunityFund`, later `JoinCommunityFund`) are standard, unprivileged operations with no fees or delays.
- The attack is most profitable for large-maturity neurons whose proportional contribution to matched funding is significant.
- Monitoring pending proposals is trivial via the public NNS governance query API.

### Recommendation

Take an immutable snapshot of Neurons' Fund membership and maturity at **proposal submission time** (inside `make_proposal` or the initial proposal validation), store it in `ProposalData`, and use that frozen snapshot in `draw_maturity_from_neurons_fund` instead of calling `list_active_neurons_fund_neurons()` at execution time. This mirrors the approach already used for ballots (voting power is snapshotted when a proposal is created, not re-read at settlement).

Alternatively, introduce a lock that prevents `LeaveCommunityFund` while any `CreateServiceNervousSystem` proposal is in the `Open` or `Adopted` lifecycle state, analogous to the two-step delay recommended in the original report.

### Proof of Concept

```
# Step 1 – Neuron N (large maturity) is in the Neurons' Fund.
manage_neuron(N, Configure { JoinCommunityFund {} })   # already done

# Step 2 – Attacker observes a CreateServiceNervousSystem proposal P
#           enter the voting period.

# Step 3 – Attacker exits the fund before P is adopted.
manage_neuron(N, Configure { LeaveCommunityFund {} })

# Step 4 – P is adopted; do_create_service_nervous_system executes.
#           list_active_neurons_fund_neurons() does NOT include N.
#           draw_maturity_from_neurons_fund skips N entirely.
#           SNS swap opens with reduced matched-funding commitment.

# Step 5 – Swap reaches terminal state (Committed or Aborted).
#           settle_neurons_fund_participation is called; N is unaffected.

# Step 6 – Attacker rejoins immediately.
manage_neuron(N, Configure { JoinCommunityFund {} })
# N is back in the fund, maturity intact, ready for the next swap.
```

**Expected vs. observed:** The Neurons' Fund participation constraints published to the SNS swap canister (computed at proposal execution from the live membership) reflect a smaller pool than was present when the proposal was submitted, violating the invariant that matched-funding commitments are stable across the proposal lifecycle.

### Citations

**File:** rs/nns/governance/src/governance.rs (L4418-4432)
```rust
        let (initial_neurons_fund_participation_snapshot, neurons_fund_participation_constraints) =
            if swap_parameters.neurons_fund_participation.unwrap_or(false) {
                let (
                    initial_neurons_fund_participation_snapshot,
                    neurons_fund_participation_constraints,
                ) = self
                    .draw_maturity_from_neurons_fund(&proposal_id, create_service_nervous_system)?;
                (
                    initial_neurons_fund_participation_snapshot,
                    Some(neurons_fund_participation_constraints),
                )
            } else {
                self.record_neurons_fund_participation_not_requested(&proposal_id)?;
                (NeuronsFundSnapshot::empty(), None)
            };
```

**File:** rs/nns/governance/src/governance.rs (L7381-7386)
```rust
        let neurons_fund = self.neuron_store.list_active_neurons_fund_neurons();
        let initial_neurons_fund_participation = PolynomialNeuronsFundParticipation::new(
            neurons_fund_participation_limits,
            swap_participation_limits,
            neurons_fund,
        )?;
```

**File:** rs/nns/governance/src/neuron_store.rs (L552-575)
```rust
    fn is_active_neurons_fund_neuron(neuron: &Neuron, now: u64) -> bool {
        !neuron.is_inactive(now) && neuron.is_a_neurons_fund_member()
    }

    /// List all neuron ids that are in the Neurons' Fund.
    pub fn list_active_neurons_fund_neurons(&self) -> Vec<NeuronsFundNeuron> {
        let now = self.now();
        self.with_active_neurons_iter_sections(
            |iter| {
                iter.filter(|neuron| Self::is_active_neurons_fund_neuron(neuron, now))
                    .map(|neuron| NeuronsFundNeuron {
                        id: neuron.id(),
                        controller: neuron.controller(),
                        hotkeys: pick_most_important_hotkeys(&neuron.hot_keys),
                        maturity_equivalent_icp_e8s: neuron.maturity_e8s_equivalent,
                    })
                    .collect()
            },
            NeuronSections {
                hot_keys: true,
                ..NeuronSections::NONE
            },
        )
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L297-302)
```rust
    /// Returns whether self is a member of Neurons Fund.
    pub(crate) fn is_a_neurons_fund_member(&self) -> bool {
        self.joined_community_fund_timestamp_seconds
            .unwrap_or_default()
            > 0
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L599-609)
```rust
    /// Join the Internet Computer's Neurons' Fund. If this neuron is
    /// already a member of the Neurons' Fund, an error is returned.
    fn join_community_fund(&mut self, now_seconds: u64) -> Result<(), GovernanceError> {
        if self.joined_community_fund_timestamp_seconds.unwrap_or(0) == 0 {
            self.joined_community_fund_timestamp_seconds = Some(now_seconds);
            Ok(())
        } else {
            // Already joined...
            Err(GovernanceError::new(ErrorType::AlreadyJoinedCommunityFund))
        }
    }
```

**File:** rs/nns/governance/src/neuron/types.rs (L611-620)
```rust
    /// Leave the Internet Computer's Neurons' Fund. If this neuron is not a
    /// member of the Neurons' Fund, an error will be returned.
    fn leave_community_fund(&mut self) -> Result<(), GovernanceError> {
        if self.joined_community_fund_timestamp_seconds.unwrap_or(0) != 0 {
            self.joined_community_fund_timestamp_seconds = None;
            Ok(())
        } else {
            Err(GovernanceError::new(ErrorType::NotInTheCommunityFund))
        }
    }
```

**File:** rs/nns/governance/canister/governance.did (L1044-1046)
```text
  JoinCommunityFund : record {};
  LeaveCommunityFund : record {};
  SetDissolveTimestamp : SetDissolveTimestamp;
```
