Audit Report

## Title
Unbounded `allowed_when_resources_are_low` Proposals Bypass Both Spam Guards, Enabling NNS Governance Heap Exhaustion - (File: `rs/nns/governance/src/governance.rs`)

## Summary
The `make_proposal` function in the NNS governance canister unconditionally skips both its heap-growth guard and the 200-proposal ballot cap for any action returning `true` from `allowed_when_resources_are_low()`. No separate cap exists for these "emergency" proposal types. An unprivileged actor holding a neuron with ≥ 6-month dissolve delay and sufficient ICP stake can submit an unbounded stream of `InstallCode` proposals targeting protocol canisters, growing the governance heap without limit until the canister becomes unresponsive and all NNS governance operations halt.

## Finding Description

**Gate 1 — heap growth check unconditionally skipped:**

In `make_proposal` at lines 5143–5145 of `rs/nns/governance/src/governance.rs`, the call to `self.check_heap_can_grow()?` is wrapped in `if !action.allowed_when_resources_are_low()`. For any qualifying action this check is never executed, regardless of current heap utilisation.

**Gate 2 — `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (200) cap bypassed:**

At lines 5254–5269, the count of unsettled proposals is compared against `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` only when `!action.allowed_when_resources_are_low()`. When the action qualifies, the entire branch is skipped and proposals are inserted without limit.

**Which proposals qualify:**

`InstallCode` proposals targeting any canister whose topic resolves to `Topic::ProtocolCanisterManagement` return `true` from `allowed_when_resources_are_low()` (lines 146–151 of `rs/nns/governance/src/proposals/install_code.rs`). The same bypass is available for `UpdateCanisterSettings` and certain `ExecuteNnsFunction` variants via the dispatch in `rs/nns/governance/src/proposals/mod.rs` lines 195–208.

**No per-neuron or per-type cap exists:**

A grep for any per-neuron or per-proposer open-proposal limit in `governance.rs` returns no results. The only other cap (`MAX_NUMBER_OF_OPEN_MANAGE_NEURON_PROPOSALS = 10,000`) applies only to `ManageNeuron` proposals (lines 5230–5246), which is a separate branch.

**Fee check does not prevent unbounded submission:**

The submission fee is added to `neuron.neuron_fees_e8s` (line 5357), and the pre-submission check at line 5206 is `proposer_minted_stake_e8s < proposal_submission_fee`. Since `minted_stake_e8s()` returns `cached_neuron_stake_e8s - neuron_fees_e8s`, each submitted proposal reduces the available stake by one fee unit. An attacker pre-staking N ICP can submit N proposals before the check blocks further submissions.

**Ballot allocation per proposal:**

Each accepted proposal calls `compute_ballots_for_new_proposal` (line 5272–5273), which allocates one ballot entry per eligible neuron. With up to `MAX_NUMBER_OF_NEURONS = 500,000` neurons, each proposal can add ~10 MB of ballot data to the heap. The heap soft limit is 3.5 GiB (`HEAP_SIZE_SOFT_LIMIT_IN_WASM32_PAGES`, lines 267–268).

## Impact Explanation

This is a **High** severity finding matching the allowed impact: *"Application/platform-level DoS, crash, consensus blocking, certified-state disruption, or subnet availability impact not based on raw volumetric DDoS."*

At ~10 MB of ballot data per proposal, approximately 350–450 proposals exhaust the 3.5 GiB governance heap soft limit. Once the heap is exhausted, all state-modifying calls to the governance canister trap with an out-of-memory error. This freezes the entire NNS: no new proposals can be submitted, no votes can be cast, no neuron management operations can execute, and no reward distributions can occur. This is a complete, persistent governance freeze of the Internet Computer's root nervous system, not a transient or self-recovering condition.

## Likelihood Explanation

**Entry requirements (no privileged role):**
- A neuron with ≥ 6-month dissolve delay (the fixed minimum to propose non-ManageNeuron proposals).
- Sufficient ICP stake: staking N ICP allows submitting N proposals at 1 ICP fee each. Exhausting the heap requires ~350–450 ICP.
- No governance majority, no admin key, no social engineering, and no threshold corruption is required.

**Likelihood: Low-Medium.** The economic barrier (350–450 ICP, currently a few thousand USD) limits casual abuse. However, a well-funded adversary targeting the NNS can execute this attack with only a staked neuron and repeated `manage_neuron` calls. The attack is deterministic, requires no timing precision, and is not recoverable without an out-of-band governance intervention (which itself requires a functioning governance canister).

## Recommendation

1. **Add a separate cap** for `allowed_when_resources_are_low` proposals (e.g., `MAX_NUMBER_OF_EMERGENCY_PROPOSALS_WITH_BALLOTS = 10`). These proposals are intended for rare recovery scenarios, not bulk submission.
2. **Apply the heap-growth check to all proposals**, but use a higher threshold for emergency proposals (e.g., allow submission up to 95% heap utilisation instead of the current soft limit, while still blocking at 100%).
3. **Add a per-neuron open-proposal limit** across all proposal types to prevent a single actor from monopolising the proposal queue.
4. Consider **increasing `reject_cost_e8s`** for `ProtocolCanisterManagement` proposals specifically, since their ballot maps are the largest.

## Proof of Concept

```
1. Create neuron N:
     cached_neuron_stake_e8s = 500 * E8  (500 ICP)
     dissolve_delay = 6 months

2. Loop i = 1..500:
     submit manage_neuron {
       command: MakeProposal {
         action: InstallCode {
           canister_id: GOVERNANCE_CANISTER_ID,
           install_mode: Upgrade,
           wasm_module: [0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00],
           wasm_module_hash: sha256(wasm_module),
         }
       }
     }

   Each call succeeds because:
     - allowed_when_resources_are_low() == true  → heap check at L5143 skipped
     - allowed_when_resources_are_low() == true  → 200-proposal cap at L5261 skipped
     - minted_stake_e8s (500 ICP - i ICP fees) >= reject_cost_e8s (1 ICP)

3. After ~350–450 iterations the governance canister heap reaches the 3.5 GiB
   soft limit. Subsequent state-modifying calls trap with out-of-memory,
   freezing all NNS governance operations.

Verifiable via a PocketIC integration test: instantiate the NNS governance
canister, create the neuron, submit proposals in a loop, and assert that
heap utilisation crosses HEAP_SIZE_SOFT_LIMIT_IN_WASM32_PAGES and that
subsequent make_proposal / register_vote calls return a trap error.
```