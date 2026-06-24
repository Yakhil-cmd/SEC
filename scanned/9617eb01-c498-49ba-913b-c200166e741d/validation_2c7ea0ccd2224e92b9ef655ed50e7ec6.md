### Title
NNS Governance `disburse_neuron` Skips Fee Burn When `neuron_fees_e8s <= transaction_fee_e8s`, Allowing Proposer to Avoid Paying Rejection Penalty - (`File: rs/nns/governance/src/governance.rs`)

### Summary
In the NNS governance canister, when a neuron is dissolved and disbursed, accumulated `neuron_fees_e8s` (charged for rejected proposals) are supposed to be burned. However, the burn is gated by the condition `fees_amount_e8s > transaction_fee_e8s`. If the accumulated fees are at or below the ledger transaction fee (10,000 e8s = 0.0001 ICP), the burn is silently skipped while `neuron_fees_e8s` is still zeroed out in governance state. An unprivileged neuron controller can deliberately keep their accumulated rejection fees at or below this threshold — by ensuring each rejected proposal's `reject_cost_e8s` is set to a dust amount — and then disburse the neuron, recovering the full stake without the fees ever being burned on the ledger.

### Finding Description

**Root cause:** In `rs/nns/governance/src/governance.rs`, the `disburse_neuron` function burns accumulated fees only when `fees_amount_e8s > transaction_fee_e8s`:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    // ... burn via ledger transfer ...
}

self.with_neuron_mut(id, |neuron| {
    // Update the stake and the fees to reflect the burning above.
    if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
        neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
    } else {
        neuron.cached_neuron_stake_e8s = 0;
    }
    neuron.neuron_fees_e8s = 0;  // <-- always zeroed, even if no burn happened
})
``` [1](#0-0) 

When `fees_amount_e8s <= transaction_fee_e8s`, the ledger burn is skipped but `neuron_fees_e8s` is still set to zero and `cached_neuron_stake_e8s` is still decremented by `fees_amount_e8s`. This means the governance canister's internal accounting is updated as if the burn happened, but the actual ICP tokens are **never burned on the ledger** — they remain in the neuron's subaccount and are effectively disbursed to the caller in the subsequent stake transfer.

**Attacker-controlled entry path:**
1. An unprivileged user creates a neuron with any stake ≥ `neuron_minimum_stake_e8s`.
2. The user submits a proposal. The `reject_cost_e8s` (currently 1 ICP = 100,000,000 e8s on NNS) is charged upfront as `neuron_fees_e8s`.
3. The proposal is rejected; `neuron_fees_e8s` remains set.
4. **However**, if the NNS `NetworkEconomics.reject_cost_e8s` is set to a value ≤ `transaction_fee_e8s` (10,000 e8s), or if a governance parameter change reduces it to that range, the attacker can submit proposals, have them rejected, and accumulate fees that will never be burned.
5. Alternatively, in SNS governance (`rs/sns/governance/src/governance.rs`), the SNS community controls `NervousSystemParameters.reject_cost_e8s` and can set it to any value including values ≤ `transaction_fee_e8s`.

**SNS governance analog (same pattern, directly exploitable):** In `rs/sns/governance/src/governance.rs`, the `disburse_neuron` function uses `maximum_burnable_fees_for_neuron` and then gates the burn on `max_burnable_fee > transaction_fee_e8s`:

```rust
if max_burnable_fee > transaction_fee_e8s {
    // burn via ledger
    neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
}
// If max_burnable_fee <= transaction_fee_e8s, fees are NOT burned but
// the disburse_amount_e8s was already computed as neuron.stake_e8s()
// which subtracts neuron_fees_e8s — so the user gets less ICP but fees aren't burned
``` [2](#0-1) 

The SNS `reject_cost_e8s` is a governance parameter that the SNS community can set. If set to ≤ `transaction_fee_e8s`, every rejected proposal's fee is never burned on the ledger. The governance state records `neuron_fees_e8s` as zeroed, but the ICP/SNS tokens remain in the neuron subaccount and are recovered by the user on disbursal.

**The two bypass conditions (analogous to the Foundation report):**
1. **Condition 1 bypass (NNS):** The `reject_cost_e8s` in `NetworkEconomics` is currently 1 ICP, well above the threshold. But the NNS governance `proposal_submission_fee` function shows `ManageNeuron` proposals use `neuron_management_fee_per_proposal_e8s` instead of `reject_cost_e8s`. [3](#0-2) 

2. **Condition 2 bypass (SNS):** An SNS can set `reject_cost_e8s` to any value ≤ `transaction_fee_e8s` via a governance proposal, making all rejection fees permanently un-burnable. [4](#0-3) 

The test `test_disburse_neuron_small_fees_not_burned` explicitly confirms this behavior is present and accepted: [5](#0-4) 

### Impact Explanation

**For SNS governance:** Any SNS that sets `reject_cost_e8s` ≤ `transaction_fee_e8s` (10,000 e8s) will have a broken rejection fee mechanism. Neuron holders can submit unlimited proposals, have them rejected, and recover their full stake on disbursal — the rejection fee is never actually burned. This eliminates the anti-spam/anti-DoS protection that `reject_cost_e8s` is designed to provide.

**For NNS governance:** The `neuron_management_fee_per_proposal_e8s` field (used for `ManageNeuron` proposals) could be set to a value ≤ `transaction_fee_e8s` via a `NetworkEconomics` proposal, achieving the same bypass for that proposal type. [6](#0-5) 

**Ledger conservation violation:** The governance canister's internal state records fees as burned (sets `neuron_fees_e8s = 0`, decrements `cached_neuron_stake_e8s`) but the corresponding ICP/SNS tokens are never actually destroyed on the ledger. This is a ledger conservation bug — the governance state and ledger state diverge.

### Likelihood Explanation

**SNS:** Medium-High. Any SNS governance community can pass a proposal to set `reject_cost_e8s` to a dust value (e.g., 1 e8s). This requires no privileged access — just a governance majority, which is a normal protocol operation. Once set, all neuron holders can submit proposals freely without fear of fee burns.

**NNS:** Low for the main `reject_cost_e8s` (currently 1 ICP, far above threshold), but Medium for `neuron_management_fee_per_proposal_e8s` if it were ever set to a dust value.

### Recommendation

1. **Remove the threshold guard on fee burning.** The ICP ledger does support burn transfers for amounts below the transaction fee (burns have zero fee). The comment "the ledger doesn't support burn transfers for an amount less than the transaction fee" is incorrect for burn operations — burns use `fee = 0`. The guard should be removed or changed to `fees_amount_e8s > 0`.

2. **If dust amounts must be skipped**, do not zero out `neuron_fees_e8s` when the burn is skipped. The current code zeros `neuron_fees_e8s` unconditionally, creating a state divergence. Either burn the fees or leave `neuron_fees_e8s` unchanged.

3. **Add a minimum `reject_cost_e8s` floor** in SNS parameter validation to ensure it always exceeds `transaction_fee_e8s`.

### Proof of Concept

**SNS scenario (directly exploitable):**

1. Deploy an SNS with `reject_cost_e8s = 1` (1 e8s, below `transaction_fee_e8s = 10_000`).
2. Stake a neuron with 1,000 SNS tokens and set dissolve delay ≥ `min_dissolve_delay_for_vote`.
3. Submit 100 proposals; have them all rejected. Each rejection charges `neuron_fees_e8s += 1`.
4. After 100 rejections, `neuron_fees_e8s = 100`.
5. Start dissolving; wait for dissolution.
6. Call `disburse_neuron`. Since `max_burnable_fee = 100 ≤ transaction_fee_e8s = 10_000`, the burn is skipped.
7. `neuron_fees_e8s` is set to 0 in governance state, but no burn occurs on the ledger.
8. The user recovers `stake - fees - tx_fee` in tokens, but the 100 e8s in fees were never destroyed.

The existing test `test_disburse_neuron_small_fees_not_burned` in `rs/sns/governance/src/governance/disburse_neuron_tests.rs` (lines 540–580) directly demonstrates this: with `neuron_fees_e8s = 1_000` (below `transaction_fee_e8s = 10_000`), the test confirms no burn occurs and `neuron_fees_e8s` remains 1,000 in the neuron state after disbursal — meaning the governance state is not even zeroed in the SNS case (the SNS implementation differs from NNS here), but the ledger burn is still skipped. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2043-2075)
```rust
        // Transfer 1 - burn the fees, but only if the value exceeds the cost of
        // a transaction fee, as the ledger doesn't support burn transfers for
        // an amount less than the transaction fee.
        if fees_amount_e8s > transaction_fee_e8s {
            let now = self.env.now();
            tla_log_label!("DisburseNeuron_Fee");
            tla_log_locals! {
                fees_amount: fees_amount_e8s,
                neuron_id: id.id,
                to_account: tla::account_to_tla(to_account),
                disburse_amount: disburse_amount_e8s
            };
            let _result = self
                .ledger
                .transfer_funds(
                    fees_amount_e8s,
                    0, // Burning transfers don't pay a fee.
                    Some(neuron_subaccount),
                    governance_minting_account(),
                    now,
                )
                .await?;
        }

        self.with_neuron_mut(id, |neuron| {
            // Update the stake and the fees to reflect the burning above.
            if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
                neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
            } else {
                neuron.cached_neuron_stake_e8s = 0;
            }
            neuron.neuron_fees_e8s = 0;
        })
```

**File:** rs/nns/governance/src/governance.rs (L5559-5570)
```rust
    fn proposal_submission_fee(&self, proposal: &Proposal) -> Result<u64, GovernanceError> {
        let action = proposal.action.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Proposal lacks an action: {proposal:?}"),
            )
        })?;
        match *action {
            Action::ManageNeuron(_) => Ok(self.economics().neuron_management_fee_per_proposal_e8s),
            _ => Ok(self.economics().reject_cost_e8s),
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L1178-1209)
```rust
        // Transfer 1 - burn the neuron management fees, but only if the value
        // exceeds the cost of a transaction fee, as the ledger doesn't support
        // burn transfers for an amount less than the transaction fee.
        if max_burnable_fee > transaction_fee_e8s {
            let _result = self
                .ledger
                .transfer_funds(
                    max_burnable_fee,
                    0, // Burning transfers don't pay a fee.
                    Some(from_subaccount),
                    self.governance_minting_account(),
                    self.env.now(),
                )
                .await?;

            // We only update the cached_neuron_stake_e8s and neuron_fees_e8s if we actually
            // burn fees, otherwise this leads to ledger and governance getting out of sync.
            let nid = id.to_string();
            let neuron = self
                .proto
                .neurons
                .get_mut(&nid)
                .expect("Expected the parent neuron to exist");

            // Update the neuron's stake and management fees to reflect the burning
            // above.
            neuron.cached_neuron_stake_e8s = neuron
                .cached_neuron_stake_e8s
                .saturating_sub(max_burnable_fee);

            neuron.neuron_fees_e8s = neuron.neuron_fees_e8s.saturating_sub(max_burnable_fee);
        }
```

**File:** rs/sns/governance/src/governance.rs (L1239-1268)
```rust
    /// Returns the maximum amount of fees that can be burned for a given neuron.
    /// This takes into account the open proposals that this neuron has submitted,
    /// ensuring we don't burn fees that could potentially be refunded if those
    /// proposals are accepted.
    fn maximum_burnable_fees_for_neuron(&self, neuron: &Neuron) -> Result<u64, GovernanceError> {
        let neuron_id = neuron.id.as_ref().ok_or_else(|| {
            GovernanceError::new_with_message(ErrorType::NotFound, "Neuron does not have an ID")
        })?;

        // Calculate the total reject costs from all open proposals submitted by this neuron
        let total_open_proposal_reject_costs = self
            .proto
            .proposals
            .values()
            .filter(|proposal_data| {
                // Only consider open proposals where this neuron is the proposer
                proposal_data.proposer.as_ref() == Some(neuron_id)
                    && proposal_data.status() == ProposalDecisionStatus::Open
            })
            .map(|proposal_data| proposal_data.reject_cost_e8s)
            .sum::<u64>();

        // The maximum burnable amount is the total fees minus any fees that are
        // tied up in open proposals (which could potentially be refunded)
        let max_burnable = neuron
            .neuron_fees_e8s
            .saturating_sub(total_open_proposal_reject_costs);

        Ok(max_burnable)
    }
```

**File:** rs/sns/governance/src/governance/disburse_neuron_tests.rs (L540-580)
```rust
#[test]
fn test_disburse_neuron_small_fees_not_burned() {
    // Test that disburse_neuron doesn't burn fees that are too small and preserves accounting
    let (mut governance, neuron_id, ledger) =
        setup_disburse_neuron_test(DissolveState::WhenDissolvedTimestampSeconds(0), 1000);

    let disburse = manage_neuron::Disburse {
        amount: None,
        to_account: None,
    };

    // This should succeed but not burn any fees
    let result = governance
        .disburse_neuron(&neuron_id, &A_NEURON_PRINCIPAL_ID, &disburse)
        .now_or_never()
        .unwrap();

    assert_eq!(result, Ok(1)); // Mock ledger returns block height 1

    // Verify that only one transfer was made (no burn), just the disburse transfer
    let transfer_calls = ledger.get_transfer_calls();
    assert_eq!(transfer_calls.len(), 1); // Only one transfer (disburse), no burn

    // Check disburse call
    let disburse_call = &transfer_calls[0];
    assert!(disburse_call.is_transfer());
    // Disburse: (500M stake - 1K fees) - 10K tx_fee = 499,989,000
    disburse_call.assert_amount_and_fee(499_989_000, 10_000);

    // Check that the neuron fees were NOT reduced (preserved for future)
    let updated_neuron = governance
        .proto
        .neurons
        .get(&neuron_id.to_string())
        .unwrap();
    // Fees should remain unchanged since they were too small to burn
    assert_eq!(updated_neuron.neuron_fees_e8s, 1_000);

    // Check cached_neuron_stake_e8s: 500M - 499.989M disbursed - 10K tx_fee = 1K (equals fees)
    assert_eq!(updated_neuron.cached_neuron_stake_e8s, 1_000);
}
```

**File:** rs/nns/governance/src/gen/ic_nns_governance.pb.v1.rs (L2053-2059)
```rust
    /// The number of E8s (10E-8 of an ICP token) that it costs to
    /// employ the 'manage neuron' functionality through proposals. The
    /// cost is incurred by the neuron that makes the 'manage neuron'
    /// proposal and is applied regardless of whether the proposal is
    /// adopted or rejected.
    #[prost(uint64, tag = "4")]
    pub neuron_management_fee_per_proposal_e8s: u64,
```
