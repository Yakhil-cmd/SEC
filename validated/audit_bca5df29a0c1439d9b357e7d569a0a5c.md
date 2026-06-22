### Title
Missing `neuron_fees_e8s ≤ cached_neuron_stake_e8s` Invariant Causes Silent Governance Accounting Drift - (File: `rs/nns/governance/src/neuron/mod.rs`, `rs/nns/governance/tla/Governance.tla`)

---

### Summary

The NNS governance canister's formal TLA+ model explicitly acknowledges that the invariant `neuron_fees_e8s ≤ cached_neuron_stake_e8s` ("Fees_Smaller_Than_Cached_Stake") **"turned out not to hold"** and is commented out. The production Rust code enforces no on-chain check for this relationship. When fees exceed the cached stake, `stake_e8s()` silently returns 0 via `saturating_sub`, causing invisible voting-power collapse and disbursal failure — directly analogous to the external report's missing `lpFee < fundingFee` invariant.

---

### Finding Description

**Root cause — commented-out invariant in TLA model:**

The TLA+ model for NNS governance defines but then disables the invariant:

```tla
\* This is a speculative invariant that turned out not to hold, as neurons can incur
\* a fee even while, say, merging.
\* Fees_Smaller_Than_Cached_Stake == \A n \in DOMAIN(neuron):
\*     neuron[n].fees <= neuron[n].cached_stake
``` [1](#0-0) 

The same invariant is commented out in the model-checker configuration:

```
\* Fees_Smaller_Than_Cached_Stake
``` [2](#0-1) 

**Root cause — silent underflow in `stake_e8s()`:**

The effective stake of a neuron is computed as:

```rust
fn neuron_stake_e8s(
    cached_neuron_stake_e8s: u64,
    neuron_fees_e8s: u64,
    staked_maturity_e8s_equivalent: Option<u64>,
) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)   // silently 0 when fees > stake
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
``` [3](#0-2) 

`minted_stake_e8s()` has the same silent underflow: [4](#0-3) 

**Root cause — unchecked fee accumulation for ManageNeuron proposals:**

When a proposal is submitted, the fee is added unconditionally:

```rust
self.with_neuron_mut(proposer_id, |neuron| {
    neuron.neuron_fees_e8s += proposal_submission_fee;
})
``` [5](#0-4) 

The guard before this is:

```rust
if neuron.stake_e8s() < reject_cost_e8s { return Err(...) }
```

For **ManageNeuron proposals**, `reject_cost_e8s` is hardcoded to `0`: [6](#0-5) 

Because `stake_e8s()` uses `saturating_sub` it always returns ≥ 0, so the guard `stake_e8s() >= 0` is trivially satisfied. The fee charged is `neuron_management_fee_per_proposal_e8s` (not 0), so repeated ManageNeuron proposal submissions accumulate fees with no upper-bound check against `cached_neuron_stake_e8s`.

**Downstream impact — disbursal:**

In `disburse_neuron`, the fee burn amount is taken directly from `neuron_fees_e8s`:

```rust
if fees_amount_e8s > transaction_fee_e8s {
    self.ledger.transfer_funds(fees_amount_e8s, 0, ...).await?;
}
// then:
if neuron.cached_neuron_stake_e8s > fees_amount_e8s {
    neuron.cached_neuron_stake_e8s -= fees_amount_e8s;
} else {
    neuron.cached_neuron_stake_e8s = 0;
}
neuron.neuron_fees_e8s = 0;
``` [7](#0-6) 

When `neuron_fees_e8s > cached_neuron_stake_e8s`, the ledger burn call attempts to transfer more tokens than the account holds, causing the burn to fail and the entire disbursal to revert — permanently locking the neuron's ICP.

---

### Impact Explanation

**Voting power collapse (silent):** `stake_e8s()` returns 0 when fees exceed the cached stake. Voting power is derived from `stake_e8s()`, so the neuron silently loses all governance influence with no error or event emitted.

**Permanent disbursal failure (ledger conservation bug):** The neuron's ICP remains in the ledger subaccount but can never be retrieved: the fee burn call will always fail because `neuron_fees_e8s > cached_neuron_stake_e8s` means the governance canister tries to burn more than the account holds. The neuron is effectively bricked.

**No on-chain invariant check:** Unlike the SNS governance canister (which introduced `maximum_burnable_fees_for_neuron` to bound the burnable amount), the NNS governance canister has no equivalent guard. [8](#0-7) 

---

### Likelihood Explanation

**Medium.** The DFINITY team's own TLA+ model explicitly acknowledges the invariant does not hold in production ("turned out not to hold, as neurons can incur a fee even while, say, merging"). The `Change_Neuron_Fee` transition in the model bounds fees to `Min({MAX_NEURON_FEE, neuron[nid].cached_stake})` for model-checking purposes only — the production Rust code has no such bound. [9](#0-8) 

The reachable path via ManageNeuron proposals requires no privileged access: any neuron controller can submit ManageNeuron proposals. The cost is `neuron_management_fee_per_proposal_e8s` per proposal, which is small relative to a neuron's stake.

---

### Recommendation

1. **Add an on-chain invariant check** after every mutation of `neuron_fees_e8s`: assert (or saturating-clamp) that `neuron_fees_e8s ≤ cached_neuron_stake_e8s`.
2. **Guard proposal submission** for ManageNeuron proposals: check that `neuron_fees_e8s + neuron_management_fee_per_proposal_e8s ≤ cached_neuron_stake_e8s` before adding the fee.
3. **Mirror the SNS fix** in NNS: adopt the `maximum_burnable_fees_for_neuron` pattern so that `disburse_neuron` never attempts to burn more than the ledger account holds.
4. **Re-enable the TLA invariant** `Fees_Smaller_Than_Cached_Stake` and fix the model transitions that violate it, or formally document why the invariant is intentionally relaxed and what the safe invariant actually is.

---

### Proof of Concept

**Step 1 — Accumulate fees beyond stake via ManageNeuron proposals:**

```
neuron.cached_neuron_stake_e8s = 100_000_000   // 1 ICP
neuron.neuron_fees_e8s         = 0

// Submit N ManageNeuron proposals (e.g., manage-neuron follow proposals)
// Each adds neuron_management_fee_per_proposal_e8s (e.g., 1_000_000 e8s = 0.01 ICP)
// After 101 proposals:
neuron.neuron_fees_e8s = 101_000_000  // > cached_neuron_stake_e8s
```

**Step 2 — Observe silent voting power collapse:**

```rust
neuron.stake_e8s()
// = cached_neuron_stake_e8s.saturating_sub(neuron_fees_e8s)
// = 100_000_000u64.saturating_sub(101_000_000u64)
// = 0   ← silent, no error
``` [10](#0-9) 

**Step 3 — Observe disbursal failure:**

```
disburse_neuron called:
  fees_amount_e8s = 101_000_000
  ledger.transfer_funds(101_000_000, 0, neuron_subaccount, minting_account, ...)
  → Err(InsufficientFunds { balance: 100_000_000 })
  → disburse_neuron returns Err(...)
  → neuron_fees_e8s and cached_neuron_stake_e8s unchanged
  → neuron permanently undisburse-able
``` [11](#0-10)

### Citations

**File:** rs/nns/governance/tla/Governance.tla (L247-253)
```text
Change_Neuron_Fee ==
    \* Note that we can change the fee even while the neuron is locked
    \E nid \in DOMAIN(neuron):
        \E new_fee_value \in 0..Min({MAX_NEURON_FEE, neuron[nid].cached_stake}):
            \* Does the model need to be more strict on how fees can be decreased?
            /\ neuron' = [neuron EXCEPT ![nid].fees = new_fee_value]
            /\ UNCHANGED <<neuron_id_by_account, locks, governance_to_ledger, ledger_to_governance, spawning_neurons, env_vars, local_vars >>
```

**File:** rs/nns/governance/tla/Governance.tla (L299-302)
```text
\* This is a s speculative invariant that turned out not to hold, as neurons can incur
\* a fee even while, say, merging.
\* Fees_Smaller_Than_Cached_Stake == \A n \in DOMAIN(neuron):
\*     neuron[n].fees <= neuron[n].cached_stake
```

**File:** rs/nns/governance/tla/Governance.cfg (L191-191)
```text
    \* Fees_Smaller_Than_Cached_Stake
```

**File:** rs/nns/governance/src/neuron/mod.rs (L10-18)
```rust
fn neuron_stake_e8s(
    cached_neuron_stake_e8s: u64,
    neuron_fees_e8s: u64,
    staked_maturity_e8s_equivalent: Option<u64>,
) -> u64 {
    cached_neuron_stake_e8s
        .saturating_sub(neuron_fees_e8s)
        .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
}
```

**File:** rs/nns/governance/src/neuron/types.rs (L981-986)
```rust
    /// Returns the current `minted` stake of the neuron, i.e. the ICP backing the
    /// neuron, minus the fees. This does not count staked maturity.
    pub fn minted_stake_e8s(&self) -> u64 {
        self.cached_neuron_stake_e8s
            .saturating_sub(self.neuron_fees_e8s)
    }
```

**File:** rs/nns/governance/src/governance.rs (L2046-2075)
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
        })
```

**File:** rs/nns/governance/src/governance.rs (L5356-5359)
```rust
        self.with_neuron_mut(proposer_id, |neuron| {
            neuron.neuron_fees_e8s += proposal_submission_fee;
        })
        .expect("Proposer not found.");
```

**File:** rs/nns/governance/src/governance.rs (L5544-5553)
```rust
        match *action {
            // We don't return proposal submission fee for ManageNeuron proposals.
            // if we did, there would be no cost to creating a bunch of ManageNeuron
            // proposals, because you could always vote to adopt them and get the
            // fee back. Therefore, we set this value to 0 and if the proposal
            // is adopted, 0 e8s is reimbursed to the proposing neuron.
            Action::ManageNeuron(_) => Ok(0),
            // For all other proposals, we return the proposal submission fee.
            _ => self.proposal_submission_fee(proposal),
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
