Audit Report

## Title
Hot Keys Inherited Without Clearing on `spawn_neuron` with `new_controller` - (File: `rs/nns/governance/src/governance.rs`)

## Summary
When `spawn_neuron` is called with a `new_controller` that differs from the parent neuron's controller, the child neuron is built with an unconditional clone of the parent's `hot_keys` list. Those hot keys remain authorized to vote, change followees, and join/leave the Neuron Fund on the child neuron — actions the new controller never consented to and may not be aware of.

## Finding Description
In `spawn_neuron`, the child controller is resolved at lines 2651–2655: if `spawn.new_controller` is `Some(p)`, `child_controller = p`; otherwise it falls back to the parent's controller. Regardless of which branch is taken, the `NeuronBuilder` at lines 2704–2718 unconditionally calls `.with_hot_keys(parent_neuron.hot_keys.clone())`. There is no conditional that clears the hot keys when `child_controller != parent_neuron.controller()`.

The authorization functions confirm the impact:
- `is_authorized_to_vote` (types.rs L243–244) returns `true` for any hot key, allowing the old hot key holder to vote on NNS proposals on behalf of the new controller's neuron.
- `is_authorized_to_configure_or_err` (types.rs L780–790) explicitly permits hot keys to call `JoinCommunityFund`/`LeaveCommunityFund`, allowing the old hot key holder to commit the new controller's maturity to the Neuron Fund without consent.

No existing guard in `spawn_neuron` checks whether the hot keys should be cleared. The new controller receives a neuron with inherited hot keys and no in-protocol notification of their presence.

## Impact Explanation
This is **unauthorized access to a governance neuron** by a principal with no legitimate claim after the controller transfer. Concretely: the old hot key holder can (a) vote on NNS governance proposals using the new controller's neuron stake, influencing governance outcomes without consent; (b) redirect the neuron's voting power by changing followees; and (c) join the Neuron Fund, committing the new controller's maturity without consent. This matches the High ($2,000–$10,000) impact class: "Unauthorized access to neurons, governance assets … where exploitation requires meaningful per-target work or other constraints."

## Likelihood Explanation
The exploit requires no special privileges. Any neuron controller who has previously added a hot key can trigger this by calling `manage_neuron` → `Command::Spawn { new_controller: Some(other_principal), ... }` via unprivileged ingress. The hot keys are copied unconditionally on every such call. The new controller has no in-protocol notification and must proactively audit and remove inherited hot keys to remediate — a step most users will not know to take.

## Recommendation
In `spawn_neuron`, only inherit hot keys when the child controller equals the parent controller:

```rust
let child_hot_keys = if child_controller == parent_neuron.controller() {
    parent_neuron.hot_keys.clone()
} else {
    vec![]
};
// then: .with_hot_keys(child_hot_keys)
```

Apply the same fix to `split_neuron` if a `new_controller` parameter is ever added to that path. Consider also clearing `joined_community_fund_timestamp_seconds` on controller change, since the new controller did not opt in to the Neuron Fund.

## Proof of Concept
1. Alice controls neuron N and adds hot key H (e.g., a dapp session key).
2. Alice calls `manage_neuron(N, Command::Spawn { new_controller: Some(Bob), percentage_to_spawn: 100, nonce: None })`.
3. Child neuron C is created with `controller = Bob`, `hot_keys = [H]` — confirmed by lines 2704–2718 of `governance.rs`.
4. Alice (via H) calls `manage_neuron(C, Command::Configure(JoinCommunityFund {}))` — succeeds because `is_authorized_to_configure_or_err` at types.rs L780–790 allows hot keys for this operation.
5. Alice (via H) calls `manage_neuron(C, Command::Follow(...))` — succeeds because `is_authorized_to_vote` at types.rs L243–244 returns `true` for hot keys.
6. Bob has no in-protocol notification; he must manually call `manage_neuron(C, Command::Configure(RemoveHotKey { hot_key_to_remove: H }))` to remediate, which he is unlikely to know to do.

A deterministic unit test in `rs/nns/governance/tests/` can reproduce this by: creating a neuron with a hot key, spawning it to a new controller principal, then asserting that the hot key is present on the child neuron and that a `manage_neuron` call from the hot key principal succeeds for `Follow` and `JoinCommunityFund` commands.