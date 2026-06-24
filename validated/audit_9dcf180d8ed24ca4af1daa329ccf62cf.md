Audit Report

## Title
SNS Governance `remove_neuron_permissions` Allows Stripping `Vote` Permission During Open Proposal - (File: `rs/sns/governance/src/governance.rs`)

## Summary
The `remove_neuron_permissions` function in SNS governance performs no check for open proposals before removing the `Vote` permission from a neuron's principal. Because ballots are immutable snapshots fixed at proposal creation, a principal holding `ManagePrincipals` or `ManageVotingPermission` can permanently silence a neuron's ballot while its voting power remains in the `total` denominator of every subsequent tally, distorting quorum and majority calculations for the lifetime of the proposal.

## Finding Description
`compute_ballots_for_new_proposal` (L5225–5295) iterates all eligible neurons and records each neuron's voting power in a `Ballot { vote: Vote::Unspecified, voting_power, ... }` entry. This snapshot is immutable: the `voting_power` is fixed at creation and the ballot persists until the proposal settles.

`remove_neuron_permissions` (L4659–4716) checks only (a) the caller holds `ManagePrincipals` or `ManageVotingPermission`, (b) the permission list is not oversized, and (c) the caller is authorized to change permissions. There is no guard equivalent to the NNS's `is_neuron_involved_with_open_proposals` check used in `validate_merge_neurons_before_commit` (NNS `merge_neurons.rs` L317–356).

After the permission is stripped, `register_vote` (L3862–3870) calls `neuron.check_authorized(caller, NeuronPermissionType::Vote)` and returns `NotAuthorized`, permanently blocking any vote for that neuron on the open proposal.

`recompute_tally` (proposal.rs L2210–2230) computes `total = yes + no + undecided`, where the silenced neuron's voting power accumulates in `undecided`. `is_accepted` (proposal.rs L2291–2307) then evaluates `quorum_met = yes * 10_000 >= total * minimum_yes_proportion_of_total_basis_points`, using the inflated `total`. For critical proposals the `minimum_yes_proportion_of_total` threshold is 20% (2000 basis points), making the quorum impact significant.

## Impact Explanation
A principal holding `ManagePrincipals` on a neuron where a separate principal holds `Vote` can, during an open proposal, remove the `Vote` permission from every voting principal. The neuron's ballot is then permanently frozen as `Unspecified` while its voting power remains in `tally.total`. This inflates the denominator without contributing to `yes`, making the `minimum_yes_proportion_of_total` quorum harder to reach. For critical SNS proposals (20% of total required), a single large neuron being silenced this way can block adoption entirely. This constitutes a significant SNS governance security impact with concrete protocol harm — mapping to the High ($2,000–$10,000) impact class.

## Likelihood Explanation
The attack requires only `ManagePrincipals` or `ManageVotingPermission` on a neuron — a standard, publicly documented delegation pattern in SNS DAOs. No privileged system role, threshold key, or subnet-majority corruption is needed. The attacker submits a standard `manage_neuron` ingress message with `RemoveNeuronPermissions` while a proposal is open. The scenario where Alice holds `ManagePrincipals` but not `Vote`, and Bob holds `Vote`, is a common split-permission pattern. The attack is repeatable across any open proposal.

## Recommendation
Mirror the NNS governance pattern: before executing `remove_neuron_permissions`, check whether the target neuron has any open (undecided) proposals in `self.proto.proposals` for which it holds an unvoted ballot. If such a proposal exists, return a `PreconditionFailed` error. The guard can be scoped narrowly to the `Vote` and `SubmitProposal` permission types, since removing unrelated permissions (e.g., `Disburse`) during an open proposal is harmless.

## Proof of Concept
1. Deploy an SNS. Alice stakes and claims Neuron X with `ManagePrincipals`. Bob is granted `Vote` on Neuron X. Alice does not hold `Vote`.
2. Bob submits a `Motion` proposal. `compute_ballots_for_new_proposal` records Neuron X's voting power in the ballot map with `Vote::Unspecified`; the proposal enters `Open` status.
3. Alice immediately calls `manage_neuron` with `RemoveNeuronPermissions { principal_id: Bob, permissions_to_remove: [Vote] }`. The call succeeds — `remove_neuron_permissions` (L4659–4716) has no open-proposal guard.
4. Bob attempts `manage_neuron` with `RegisterVote` for the open proposal. The call fails at L3870 (`check_authorized(Bob, Vote)` returns `NotAuthorized`).
5. Neuron X's ballot remains `Unspecified` for the entire voting period. `recompute_tally` (proposal.rs L2215–2230) accumulates its `voting_power` into `undecided`, and `total = yes + no + undecided` is inflated. `is_accepted` (proposal.rs L2305) evaluates `yes * 10_000 >= total * threshold_basis_points` against the inflated `total`, making quorum harder to reach.
6. A deterministic integration test using PocketIC can confirm this by asserting that after the permission removal, `register_vote` returns `NotAuthorized` and the final `tally.total > tally.yes + tally.no` with the gap equal to Neuron X's voting power.