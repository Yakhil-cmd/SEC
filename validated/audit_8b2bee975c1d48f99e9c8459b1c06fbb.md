Audit Report

## Title
NNS `disburse_neuron` Burns All Fees Including Refundable Open-Proposal Fees, Causing Permanent ICP Loss — (`rs/nns/governance/src/governance.rs`)

## Summary
In NNS governance, `disburse_neuron` reads `fees_amount_e8s` directly from `neuron.neuron_fees_e8s` without deducting the `reject_cost_e8s` of any open proposals the neuron has submitted. It then burns the full amount and hard-sets `neuron_fees_e8s = 0`. When any of those open proposals is later adopted, the refund guard (`neuron_fees_e8s >= rejection_cost`) always fails because the field was zeroed, so the refund is silently dropped and the neuron owner permanently loses the corresponding ICP. SNS governance fixed this identical bug in proposal 137687.

## Finding Description
**Root cause — NNS burn path:**

`fees_amount_e8s` is assigned directly from `neuron.neuron_fees_e8s` with no deduction for open proposals: [1](#0-0) 

The full amount is burned on the ledger and `neuron_fees_e8s` is unconditionally zeroed: [2](#0-1) 

**Broken refund path — `process_proposal`:**

On proposal adoption, the refund is gated on `neuron_fees_e8s >= rejection_cost`. After `disburse_neuron` has zeroed the field, this guard always evaluates to `false` and the refund is silently skipped: [3](#0-2) 

**SNS fixed path (reference):**

SNS computes `max_burnable_fee` via `maximum_burnable_fees_for_neuron`, which subtracts the sum of `reject_cost_e8s` for all open proposals from `neuron_fees_e8s`, and burns only that safe amount: [4](#0-3) [5](#0-4) 

The SNS CHANGELOG confirms this was a real bug that was fixed: [6](#0-5) 

## Impact Explanation
Any neuron owner who submits a governance proposal, then disburses their neuron while the proposal is still open, permanently loses `reject_cost_e8s` ICP per open proposal that is subsequently adopted. On NNS mainnet the current `reject_cost_e8s` is 10 ICP. The loss is irreversible: the tokens are burned on the ledger and the governance refund path silently no-ops. This constitutes a permanent, protocol-enforced loss of user funds with no recovery path. This matches the **High ($2,000–$10,000)** impact class: significant NNS governance security impact with concrete, permanent user-funds harm.

## Likelihood Explanation
The preconditions are fully reachable by any unprivileged neuron controller with no special privileges required:
1. Submit one or more proposals — `neuron_fees_e8s` is incremented by `reject_cost_e8s` per proposal.
2. Begin dissolving the neuron (or have it already dissolved).
3. Call `ManageNeuron::Disburse` — a standard user action available to any neuron controller once the neuron is dissolved.
4. One or more of the open proposals is later adopted.

There is no warning in the UI or API that disbursing while holding open proposals forfeits the potential refund. The sequence is a natural user flow (submit proposal, dissolve, disburse upon maturity).

## Recommendation
Port the SNS fix to NNS governance. Before burning, compute the maximum safe burn amount by summing `reject_cost_e8s` for all open proposals where `proposer == this neuron`, subtract that from `neuron_fees_e8s` using `saturating_sub`, and burn only the remainder. After burning, update `neuron_fees_e8s` using `saturating_sub(max_burnable)` rather than hard-setting it to `0`, mirroring the SNS implementation at `rs/sns/governance/src/governance.rs` lines 1243–1268. [5](#0-4) 

## Proof of Concept
**State setup:**
- Neuron: `cached_neuron_stake_e8s = 1_100_000_000` (11 ICP), `neuron_fees_e8s = 1_000_000_000` (10 ICP from one open proposal), state = `Dissolved`.
- Open proposal: `reject_cost_e8s = 1_000_000_000`, `proposer = this neuron`, `decided_timestamp_seconds = 0` (still open).

**Step 1 — call `disburse_neuron`:**
- `fees_amount_e8s = 1_000_000_000` (full fees, no open-proposal deduction)
- Ledger burn: 1,000,000,000 e8s destroyed
- `neuron_fees_e8s` set to `0`
- User receives `~100_000_000 - tx_fee` e8s

**Step 2 — proposal adopted (`process_proposal`):**
```
rejection_cost = 1_000_000_000
neuron.neuron_fees_e8s = 0
guard: 0 >= 1_000_000_000  →  false  →  refund silently skipped
```

**Result:** 10 ICP permanently burned that should have been refunded.

A minimal unit test can reproduce this deterministically by constructing the above neuron and proposal state in the NNS governance test harness, calling `disburse_neuron`, then calling `process_proposal` with an adopted result, and asserting that `neuron_fees_e8s` is non-zero (it will be `0`, proving the bug).

### Citations

**File:** rs/nns/governance/src/governance.rs (L1959-1968)
```rust
        ) = self.with_neuron(id, |neuron| {
            (
                neuron.is_controlled_by(caller),
                neuron.state(self.env.now()),
                neuron.kyc_verified,
                neuron.subaccount(),
                neuron.neuron_fees_e8s,
                neuron.minted_stake_e8s(),
            )
        })?;
```

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

**File:** rs/nns/governance/src/governance.rs (L3750-3762)
```rust
        // The proposal was adopted, return the rejection fee for non-ManageNeuron
        // proposals.
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
        }
```

**File:** rs/sns/governance/src/governance.rs (L1156-1208)
```rust
        let max_burnable_fee = self.maximum_burnable_fees_for_neuron(neuron)?;

        // Calculate the amount to transfer and make sure no matter what the user
        // disburses we still take the neuron management fees into account.
        let mut disburse_amount_e8s = disburse
            .amount
            .as_ref()
            .map_or(neuron.stake_e8s(), |a| a.e8s);

        // You cannot disburse more than the neuron's stake, which includes fees.
        disburse_amount_e8s = disburse_amount_e8s.min(neuron.stake_e8s());

        // Subtract the transaction fee from the amount to disburse since it will
        // be deducted from the source (the neuron's) account.
        if disburse_amount_e8s > transaction_fee_e8s {
            disburse_amount_e8s -= transaction_fee_e8s
        }

        // We need to do 2 transfers:
        // 1 - Burn the neuron management fees.
        // 2 - Transfer the disburse_amount to the target account

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

**File:** rs/sns/governance/CHANGELOG.md (L83-93)
```markdown
# 2025-08-01: Proposal 137687

http://dashboard.internetcomputer.org/proposal/137687

## Fixed

Fixed multiple issues in `disburse_neuron` functionality:

- Fixed a bug that could allow an SNS Neuron to burn fees that would have been refunded after proposal acceptance.
- Fees are now only recorded as burned when they exceed the transaction fee threshold and are actually burned.
- Added comprehensive tests to ensure the correct behavior in the future.
```
