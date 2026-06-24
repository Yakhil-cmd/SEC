Audit Report

## Title
Hot Keys Not Cleared When Spawning NNS Neuron with a New Controller - (File: `rs/nns/governance/src/governance.rs`)

## Summary
When `spawn_neuron` is called with `spawn.new_controller` set to a different principal than the parent neuron's controller, the newly created child neuron unconditionally inherits the parent's full hot key list. Because hot keys grant the ability to vote and follow on behalf of a neuron, the parent's hot key holders retain unauthorized governance rights over the child neuron after effective ownership has been transferred to the new controller, without the new controller's knowledge or consent.

## Finding Description
In `spawn_neuron` (`rs/nns/governance/src/governance.rs`), the child controller is resolved at lines 2651–2655: if `spawn.new_controller` is `Some(principal)`, that principal becomes `child_controller`; otherwise it falls back to `parent_neuron.controller()`. The child neuron is then constructed at lines 2704–2718 with `child_controller` as its controller, but `.with_hot_keys(parent_neuron.hot_keys.clone())` is called unconditionally at line 2714 — there is no branch that skips or clears hot keys when `child_controller` differs from the parent's controller.

The authorization check `is_authorized_to_vote` in `rs/nns/governance/src/neuron/types.rs` at lines 243–255 delegates to `is_hotkey_or_controller`, which returns `true` for any principal that is either the controller **or** present in `hot_keys`. As a result, every hot key inherited from the parent neuron can immediately call `ManageNeuron` with `Follow` or `RegisterVote` on the child neuron and pass authorization, even though the new controller never added those hot keys.

No existing guard in `spawn_neuron` checks whether `child_controller != parent_neuron.controller()` before copying hot keys. The only authorization check present (line 2631–2633) verifies that the *caller* is the parent's controller, which is unrelated to the hot key inheritance problem.

## Impact Explanation
This is unauthorized access to NNS governance rights on a neuron whose effective ownership has been transferred to a new principal. The parent's hot key holders can vote and set followees on behalf of the new controller's neuron without authorization, silently influencing NNS governance outcomes. This matches the **High ($2,000–$10,000)** bounty impact: "Unauthorized access to neurons, governance assets, wallets, identities, ledgers, or canister-controlled funds where exploitation requires meaningful per-target work or other constraints." The new controller's voting power is exercised by third parties they never authorized, and the new controller may never discover this unless they explicitly inspect `hot_keys` via `get_full_neuron`.

## Likelihood Explanation
Medium. Both preconditions are common in practice: (1) parent neurons routinely register hot keys for cold-key security, and (2) `Spawn` with `new_controller` is a documented, publicly accessible ingress call used when gifting neurons or distributing rewards. The parent controller needs only their own signature to trigger the spawn; no victim mistake or social engineering is required. The exploit is repeatable for every such spawn.

## Recommendation
In `spawn_neuron`, condition the hot key copy on whether the child controller equals the parent controller. When `spawn.new_controller` is `Some(c)` and `c != parent_neuron.controller()`, initialize the child neuron with an empty hot key list (omit `.with_hot_keys(...)` or pass an empty `Vec`). When `new_controller` is `None` (same controller), copying hot keys is acceptable. The same review should be applied to `split_neuron` for consistency, though its impact is lower because the child controller is always the caller.

## Proof of Concept
1. Alice creates NNS neuron N and registers hot key `H` on it.
2. Neuron N accumulates maturity.
3. Alice calls `manage_neuron` → `Spawn { new_controller: Some(Bob), percentage_to_spawn: 100, nonce: None }`.
4. `spawn_neuron` executes: `child_controller = Bob` (line 2651–2655); child neuron C is built with `controller = Bob` and `hot_keys = [H]` (line 2714).
5. Principal `H` calls `manage_neuron` on neuron C with `Follow { topic: ..., followees: [...] }`.
6. `is_authorized_to_vote` at line 243–255 returns `true` because `H ∈ C.hot_keys`.
7. Bob's neuron now follows `H`'s chosen followees. Bob has no visibility into this unless he calls `get_full_neuron` and inspects the `hot_keys` field.
8. A deterministic integration test can confirm this by: spawning a neuron to a new controller, asserting `child_neuron.hot_keys == parent_neuron.hot_keys`, then calling `RegisterVote` from the inherited hot key and asserting success.