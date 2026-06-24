Audit Report

## Title
Known Neuron Name Squatting via Concurrent `RegisterKnownNeuron` Proposals - (File: `rs/nns/governance/src/proposals/register_known_neuron.rs`)

## Summary
`KnownNeuron::validate` checks name uniqueness only against the committed `KnownNeuronIndex`, not against other pending proposals. Two `RegisterKnownNeuron` proposals for different neuron IDs but the same name can both pass submission-time validation simultaneously. Whichever executes second fails at execution time, burning the proposer's fee, and the attacker's neuron permanently holds the name in the known-neuron list — enabling impersonation of high-profile entities in NNS liquid democracy.

## Finding Description
`KnownNeuron::validate` (L132–143) queries `neuron_store.known_neuron_id_by_name`, which reads only from the committed `KnownNeuronIndex` (`StableBTreeMap` in stable storage). There is no scan of open/pending proposals for conflicting names. `KnownNeuron::execute` (L152–154) re-runs the same `validate` call before writing state, but by that point the race has already been won or lost.

The exploit path:
1. Attacker observes (or predicts) that a legitimate neuron will register a well-known name (e.g., "DFINITY Foundation").
2. Attacker submits their own `RegisterKnownNeuron` proposal for their neuron ID with the identical name. Both proposals pass `validate` because `known_neuron_id_by_name` returns `None` for both — the name is not yet committed.
3. Both proposals enter the voting period. NNS liquid democracy causes both to be adopted.
4. The attacker's proposal executes first, writing `"DFINITY Foundation" → attacker_neuron_id` into `KnownNeuronIndex`.
5. The legitimate proposal's `execute` calls `validate`, which now finds `existing_neuron_id != legitimate_neuron_id`, returns `PreconditionFailed`, and the proposal is marked `Failed`. The legitimate owner's proposal fee is burned.
6. The attacker's neuron now appears in the known-neuron list under the squatted name.

Additionally, `validate` does not verify that the proposing neuron is the same neuron being registered (L36–50), so any neuron holder can submit a `RegisterKnownNeuron` for any existing neuron ID, lowering the barrier further.

## Impact Explanation
The known-neuron list is the primary discovery mechanism for NNS liquid democracy. Users and wallets browse it by name to configure neuron following. An attacker who squats "DFINITY Foundation" or another high-profile name causes new participants to follow the attacker's neuron instead of the legitimate one, directly influencing NNS vote outcomes. This constitutes significant NNS governance security impact with concrete protocol harm, fitting the High ($2,000–$10,000) impact class: "Significant NNS security impact with concrete user or protocol harm."

## Likelihood Explanation
All NNS proposals are publicly visible immediately after submission. Any principal holding a neuron with sufficient dissolve delay can submit a `RegisterKnownNeuron` proposal. The attacker does not need to observe a pending proposal — they can preemptively register predictable names (major node providers, DFINITY, ICA) before the legitimate owner does. The proposal rejection fee is a modest deterrent. The attack is repeatable: if the legitimate owner resubmits with a new name, the attacker can repeat the squatting. No special privileges or unrealistic assumptions are required.

## Recommendation
1. **Reject duplicate pending proposals at submission time**: When a `RegisterKnownNeuron` proposal is submitted, scan all open proposals for any other `RegisterKnownNeuron` action claiming the same name for a different neuron ID, and reject the new submission immediately.
2. **Reserve the name at proposal-open time**: Insert a tentative entry in `KnownNeuronIndex` when the proposal opens, and remove it if the proposal is rejected or fails. This makes the name unavailable to concurrent proposals without requiring a full proposal scan.
3. **Bind the proposal to the proposer's neuron**: Require that the neuron ID in the `RegisterKnownNeuron` action matches the neuron submitting the proposal, preventing any neuron from squatting a name on behalf of a neuron it does not control.

## Proof of Concept
A deterministic integration test using PocketIC:
1. Create two neurons A (id=42) and B (id=999), both with sufficient dissolve delay to submit proposals.
2. Submit P1 from neuron A: `RegisterKnownNeuron { id: 42, known_neuron_data: { name: "DFINITY Foundation", ... } }`. Assert P1 is accepted (validate passes, `known_neuron_id_by_name` returns `None`).
3. Without advancing time, submit P2 from neuron B: `RegisterKnownNeuron { id: 999, known_neuron_data: { name: "DFINITY Foundation", ... } }`. Assert P2 is also accepted (validate passes, name still not committed).
4. Cast votes to adopt both P1 and P2.
5. Advance time so P2 executes first (submit P2 slightly earlier so it reaches majority first, or manipulate proposal ordering).
6. Assert: `known_neuron_id_by_name("DFINITY Foundation") == Some(NeuronId { id: 999 })`.
7. Assert: P1 is in `Failed` status with `PreconditionFailed` error.
8. Assert: neuron 42 has no known neuron data; neuron 999 appears in the known-neuron list as "DFINITY Foundation".