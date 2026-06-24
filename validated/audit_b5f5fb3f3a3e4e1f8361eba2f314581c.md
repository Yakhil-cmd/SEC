Audit Report

## Title
Unchecked ManageNeuron Fee Accumulation Causes Silent Voting-Power Collapse and Disbursal Failure — (File: `rs/nns/governance/src/neuron/mod.rs`, `rs/nns/governance/src/governance.rs`)

## Summary
The NNS governance canister unconditionally increments `neuron_fees_e8s` on every ManageNeuron proposal submission with no upper-bound check against `cached_neuron_stake_e8s`. Once accumulated fees exceed the cached stake, `stake_e8s()` silently returns 0 via `saturating_sub`, collapsing the neuron's voting power without any error or event. Concurrently, `disburse_neuron` attempts to burn the full `neuron_fees_e8s` amount from the neuron's ledger subaccount, which holds only `cached_neuron_stake_e8s` worth of ICP, causing the ledger transfer to fail and the disbursal to revert. The state is recoverable only by topping up the neuron's subaccount and refreshing the stake, but the failure is silent and the path to recovery is non-obvious.

## Finding Description

**Commented-out TLA+ invariant (acknowledged by DFINITY):**

`rs/nns/governance/tla/Governance.tla` lines 299–302 explicitly acknowledge the invariant does not hold:

```tla
\* This is a s speculative invariant that turned out not to hold, as neurons can incur
\* a fee even while, say, merging.
\* Fees_Smaller_Than_Cached_Stake == \A n \in DOMAIN(neuron):
\*     neuron[n].fees <= neuron[n].cached_stake
```

`rs/nns/governance/tla/Governance.cfg` line 191 confirms it is disabled in the model checker. The `Change_Neuron_Fee` transition at `Governance.tla` line 250 bounds fees to `Min({MAX_NEURON_FEE, neuron[nid].cached_stake})` for model-checking purposes only; the production Rust code has no equivalent bound.

**Silent underflow in `stake_e8s()` and `minted_stake_e8s()`:**

`rs/nns/governance/src/neuron/mod.rs` lines 15–17:
```rust
cached_neuron_stake_e8s
    .saturating_sub(neuron_fees_e8s)   // returns 0 when fees > stake, no error
    .saturating_add(staked_maturity_e8s_equivalent.unwrap_or(0))
```

`rs/nns/governance/src/neuron/types.rs` lines 983–986:
```rust
pub fn minted_stake_e8s(&self) -> u64 {
    self.cached_neuron_stake_e8s
        .saturating_sub(self.neuron_fees_e8s)
}
```

Both return 0 silently when fees exceed stake.

**Unbounded fee accumulation for ManageNeuron proposals:**

`rs/nns/governance/src/governance.rs` lines 5356–5359 add the fee unconditionally:
```rust
self.with_neuron_mut(proposer_id, |neuron| {
    neuron.neuron_fees_e8s += proposal_submission_fee;
})
.expect("Proposer not found.");
```

`proposal_submission_fee` for ManageNeuron proposals is `neuron_management_fee_per_proposal_e8s` (non-zero), confirmed at line 5567:
```rust
Action::ManageNeuron(_) => Ok(self.economics().neuron_management_fee_per_proposal_e8s),
```

The `reject_cost_e8s` for ManageNeuron is hardcoded to 0 (line 5550), meaning no fees are ever refunded on adoption. There is no guard checking `neuron_fees_e8s + proposal_submission_fee <= cached_neuron_stake_e8s` before the increment.

**Disbursal failure when fees exceed stake:**

`rs/nns/governance/src/governance.rs` lines 2046–2075: `disburse_neuron` burns the full `fees_amount_e8s` from the neuron's ledger subaccount. When `neuron_fees_e8s > cached_neuron_stake_e8s`, the subaccount holds less ICP than the burn amount, causing the ledger `transfer_funds` call to return `Err(InsufficientFunds)`. The `.await?` propagates this error, reverting the entire disbursal without modifying `neuron_fees_e8s` or `cached_neuron_stake_e8s`, leaving the neuron in the same broken state.

**No equivalent guard in NNS (unlike SNS):**

The SNS governance canister (`rs/sns/governance/src/governance.rs` lines 1239–1268) introduced `maximum_burnable_fees_for_neuron` to bound the burnable amount. NNS has no equivalent.

## Impact Explanation

This matches **Medium ($200–$2,000)**: a meaningful security impact requiring a cost-bearing, self-directed sequence of actions with strict preconditions. The neuron controller's voting power silently collapses to zero — a concrete NNS governance impact — and disbursal of their ICP fails until they top up the subaccount and refresh the stake. The failure is silent (no error emitted on `stake_e8s()` returning 0), making diagnosis non-obvious. The ICP is not permanently lost but is inaccessible until the controller takes corrective action, constituting a meaningful user-funds impact within the NNS governance scope.

## Likelihood Explanation

Medium. The path requires only a neuron controller submitting repeated ManageNeuron proposals against their own neuron. No privileged access is needed. The cost is `neuron_management_fee_per_proposal_e8s` per proposal. For a neuron with 1 ICP stake (100,000,000 e8s) and a fee of 1,000,000 e8s per proposal, 101 proposals suffice. This can occur unintentionally (a controller legitimately managing many followees) or intentionally. The DFINITY team's own TLA+ model explicitly acknowledges the invariant does not hold in production.

## Recommendation

1. **Add an upper-bound guard before incrementing `neuron_fees_e8s`**: before `neuron.neuron_fees_e8s += proposal_submission_fee`, assert or return an error if `neuron.neuron_fees_e8s.saturating_add(proposal_submission_fee) > neuron.cached_neuron_stake_e8s`.
2. **Mirror the SNS `maximum_burnable_fees_for_neuron` pattern** in NNS `disburse_neuron` so the burn amount is capped at `min(neuron_fees_e8s, cached_neuron_stake_e8s)`.
3. **Re-enable or formally replace the `Fees_Smaller_Than_Cached_Stake` TLA+ invariant** with a weaker invariant that accurately captures the safe relationship, and fix the model transitions that violate it.

## Proof of Concept

**Step 1 — Accumulate fees beyond stake:**
```
neuron.cached_neuron_stake_e8s = 100_000_000   // 1 ICP
neuron.neuron_fees_e8s         = 0

// Submit 101 ManageNeuron proposals (e.g., follow proposals)
// Each adds neuron_management_fee_per_proposal_e8s = 1_000_000 e8s
// After 101 proposals:
neuron.neuron_fees_e8s = 101_000_000  // > cached_neuron_stake_e8s
```

**Step 2 — Observe silent voting power collapse:**
```rust
neuron.stake_e8s()
// = 100_000_000u64.saturating_sub(101_000_000u64)
// = 0   ← no error, no event
```

**Step 3 — Observe disbursal failure:**
```
disburse_neuron called:
  fees_amount_e8s = 101_000_000
  ledger.transfer_funds(101_000_000, 0, neuron_subaccount, minting_account, ...)
  → Err(InsufficientFunds { balance: 100_000_000 })
  → disburse_neuron returns Err(...)
  → neuron_fees_e8s and cached_neuron_stake_e8s unchanged
  → neuron remains undisburse-able until controller tops up subaccount
```

**Reproducible test plan:** Write a PocketIC integration test that creates a neuron with 1 ICP, submits 101 ManageNeuron follow proposals, asserts `stake_e8s() == 0`, then calls `disburse_neuron` and asserts it returns an `InsufficientFunds` error.