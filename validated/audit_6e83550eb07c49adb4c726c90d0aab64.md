### Title
NNS Governance `disburse_neuron` Burns Full `neuron_fees_e8s` Without Accounting for Open Proposals, Causing Irrecoverable ICP Loss - (File: `rs/nns/governance/src/governance.rs`)

---

### Summary

The NNS Governance `disburse_neuron` function unconditionally burns the entire `neuron_fees_e8s` balance during disbursement, without checking whether any portion of those fees is tied to still-open proposals that could be accepted and refunded. This mirrors the exact bug class described in the external report: a unilateral action (disbursal) immediately applies a fee deduction that should only be finalized after a two-party outcome (proposal vote). The SNS Governance canister already received a fix for this identical bug (Proposal 137687), but the NNS Governance canister remains unpatched.

---

### Finding Description

When a neuron submits a non-`ManageNeuron` proposal, the NNS Governance charges the `reject_cost_e8s` upfront by incrementing `neuron_fees_e8s`: [1](#0-0) 

If the proposal is later **adopted**, `process_proposal` refunds the fee by decrementing `neuron_fees_e8s`, but only if `neuron_fees_e8s >= rejection_cost`: [2](#0-1) 

The critical flaw is in `disburse_neuron`. When called on a dissolved neuron, it reads the full `fees_amount_e8s = neuron.neuron_fees_e8s` and burns it unconditionally — with no check for open proposals: [3](#0-2) 

After the burn, `neuron_fees_e8s` is set to `0`. If the open proposal subsequently passes, the refund path in `process_proposal` silently no-ops because `0 >= rejection_cost` is false. The ICP is permanently destroyed.

**The SNS Governance canister has an explicit fix for this exact bug** — `maximum_burnable_fees_for_neuron` — which subtracts the sum of `reject_cost_e8s` from all open proposals before computing the burnable amount: [4](#0-3) 

The SNS `disburse_neuron` then burns only `max_burnable_fee` instead of the full `neuron_fees_e8s`: [5](#0-4) 

The NNS Governance `disburse_neuron` has no equivalent guard. The SNS CHANGELOG explicitly documents this as a fixed bug: [6](#0-5) 

---

### Impact Explanation

A neuron controller who submits a non-`ManageNeuron` proposal (paying `reject_cost_e8s`, currently 1 ICP = 100,000,000 e8s) and then disburses their dissolved neuron while the proposal is still open will have the full fee burned on-ledger. When the proposal passes, the governance refund is silently skipped because `neuron_fees_e8s` is already `0`. The 1 ICP is permanently removed from the ICP supply — a ledger conservation violation. The neuron controller receives fewer ICP than they are entitled to.

---

### Likelihood Explanation

The scenario requires:
1. A neuron with a dissolve delay just above `NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS` submits a non-`ManageNeuron` proposal.
2. The neuron's dissolve delay expires (neuron enters `Dissolved` state) before the proposal's voting period ends (NNS voting periods can be extended by the wait-for-quiet mechanism beyond the base 4-day period).
3. The neuron controller calls `disburse_neuron` while the proposal is still open.

This is a realistic timing window: a neuron with a dissolve delay of exactly the minimum plus a few days can submit a proposal, dissolve, and be disbursed before the proposal is decided. The wait-for-quiet extension makes this window wider. The likelihood is **low but non-zero**, and the scenario can occur accidentally (not just maliciously).

---

### Recommendation

Apply the same fix used in SNS Governance to NNS Governance's `disburse_neuron`. Before burning fees, compute the maximum burnable amount by subtracting the sum of `reject_cost_e8s` from all open proposals submitted by the neuron:

```rust
// In rs/nns/governance/src/governance.rs, disburse_neuron
let total_open_proposal_reject_costs: u64 = self
    .heap_data
    .proposals
    .values()
    .filter(|p| p.proposer == Some(*id) && p.status() == ProposalStatus::Open)
    .map(|p| p.reject_cost_e8s)
    .sum();

let max_burnable_fees = fees_amount_e8s.saturating_sub(total_open_proposal_reject_costs);

if max_burnable_fees > transaction_fee_e8s {
    // burn max_burnable_fees instead of fees_amount_e8s
}
// Update neuron_fees_e8s by subtracting max_burnable_fees, not zeroing it
```

---

### Proof of Concept

1. Neuron N has `dissolve_delay = NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS + 5 days`, is in `Dissolving` state.
2. After `NEURON_MINIMUM_DISSOLVE_DELAY_TO_PROPOSE_SECONDS` elapses, N has 5 days remaining. N submits a `Motion` proposal. `neuron_fees_e8s` becomes `reject_cost_e8s` (1 ICP).
3. After 5 more days, N is `Dissolved`. The proposal's voting period has been extended by wait-for-quiet and is still `Open`.
4. N's controller calls `manage_neuron` → `Disburse`. `disburse_neuron` reads `fees_amount_e8s = 1 ICP`, burns it on the ledger, sets `neuron_fees_e8s = 0`.
5. The proposal passes. `process_proposal` checks `neuron.neuron_fees_e8s >= rejection_cost` → `0 >= 100_000_000` → false. No refund. 1 ICP is permanently burned.

The NNS governance `disburse_neuron` at line 2046 is the necessary vulnerable step; the SNS governance equivalent at line 1181 is not vulnerable because it uses `max_burnable_fee` computed by `maximum_burnable_fees_for_neuron`. [7](#0-6) [8](#0-7) [2](#0-1)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2046-2074)
```rust
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
```

**File:** rs/nns/governance/src/governance.rs (L3752-3761)
```rust
        if !proposal.is_manage_neuron()
            && let Some(nid) = proposal.proposer
        {
            let rejection_cost = proposal.reject_cost_e8s;
            self.with_neuron_mut(&nid, |neuron| {
                if neuron.neuron_fees_e8s >= rejection_cost {
                    neuron.neuron_fees_e8s -= rejection_cost;
                }
            })
            .ok();
```

**File:** rs/nns/governance/src/governance.rs (L5356-5358)
```rust
        self.with_neuron_mut(proposer_id, |neuron| {
            neuron.neuron_fees_e8s += proposal_submission_fee;
        })
```

**File:** rs/sns/governance/src/governance.rs (L1156-1156)
```rust
        let max_burnable_fee = self.maximum_burnable_fees_for_neuron(neuron)?;
```

**File:** rs/sns/governance/src/governance.rs (L1181-1208)
```rust
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
```

**File:** rs/sns/governance/src/governance.rs (L1243-1268)
```rust
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

**File:** rs/sns/governance/CHANGELOG.md (L89-93)
```markdown
Fixed multiple issues in `disburse_neuron` functionality:

- Fixed a bug that could allow an SNS Neuron to burn fees that would have been refunded after proposal acceptance.
- Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned.
- Added comprehensive tests to ensure the correct behavior in the future.
```
