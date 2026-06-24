Audit Report

## Title
Unbounded Synchronous Neuron Iteration in `compute_ballots_for_new_proposal()` Can Cause Governance DoS - (File: `rs/sns/governance/src/governance.rs`)

## Summary
`compute_ballots_for_new_proposal()` iterates every neuron in `self.proto.neurons` in a single synchronous message execution with no instruction-limit guard. It is called directly inside the `make_proposal()` update handler, which is reachable by any principal holding a neuron with `SubmitProposal` permission. As neuron count grows, the per-message instruction cost grows linearly and can exceed the IC's update-call instruction limit, permanently blocking all new governance proposals.

## Finding Description
`compute_ballots_for_new_proposal()` at line 5226 iterates the entire in-memory neuron map:

```rust
for (k, v) in self.proto.neurons.iter() {
    if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote { continue; }
    let voting_power = v.voting_power(...);
    total_power += voting_power as u128;
    electoral_roll.insert(k.clone(), Ballot { ... });
}
```

There is no call to `ic_cdk::api::instruction_counter()`, no early-exit, and no batching — confirmed by grep returning zero matches for `instruction_counter` or `instruction_limit` in the file. The function is called synchronously at line 3557–3559 inside `make_proposal()`, before any state mutation, and `make_proposal()` is an update call reachable by any neuron holder with `SubmitProposal` permission (line 3503).

The `max_number_of_neurons` ceiling is a governance-controlled `NervousSystemParameters` field (line 6366–6369) with no hard upper bound enforced at the replica level. The only enforcement is the soft check at line 6371 that prevents adding neurons beyond the current parameter value — but that parameter can be raised by governance proposal, and neurons accumulate naturally as users stake tokens.

NNS Governance has already addressed this exact pattern: `compute_ballots_for_standard_proposal()` (NNS line 5486) reads from a pre-computed voting-power snapshot maintained by a timer task rather than iterating neurons inline. The SNS canister has not received the equivalent refactor, and no SNS-equivalent benchmark exists to enforce an instruction-ceiling regression gate.

## Impact Explanation
When the instruction limit is exceeded, every call to `make_proposal()` fails. Because ballot creation is a prerequisite for any proposal, no new governance proposals can be submitted — upgrades, parameter changes, treasury transfers, and emergency actions are all blocked. The condition is self-reinforcing: the canister cannot be upgraded to fix itself because the upgrade proposal cannot be submitted. This is a concrete application/platform-level DoS of SNS governance, matching the High ($2,000–$10,000) allowed impact: *"Application/platform-level DoS … or subnet availability impact not based on raw volumetric DDoS."*

## Likelihood Explanation
- Any principal with a neuron holding `SubmitProposal` permission can trigger `make_proposal()` — no privileged role is required.
- Neurons accumulate naturally as users stake tokens; an adversary can also deliberately stake many small neurons up to `max_number_of_neurons`.
- The `max_number_of_neurons` parameter is governance-controlled; a community that raises it without understanding the instruction-cost implication accelerates the risk.
- Unlike NNS Governance, the SNS canister has no snapshot mechanism, no instruction-limit guard, and no timer-based ballot pre-computation.
- The exact neuron count at which the IC's 40-billion-instruction update-call limit is exceeded depends on SNS heap-memory access costs (cheaper than NNS stable memory), so the threshold is higher than the NNS benchmark projects — but the architectural exposure is confirmed and the NNS precedent demonstrates DFINITY has already treated this as a real risk in the analogous codebase.

## Recommendation
1. **Adopt the NNS snapshot pattern:** Compute voting-power snapshots in a recurring timer task and read from the snapshot inside `make_proposal()` instead of iterating all neurons synchronously, mirroring `compute_ballots_for_standard_proposal()` in NNS governance.
2. **Add an instruction-limit guard as an interim measure:** Insert `ic_cdk::api::instruction_counter()` checks inside the loop and return a retriable error if the soft limit is approached.
3. **Add a canbench entry:** Add a `compute_ballots_for_new_proposal` benchmark for SNS governance (analogous to the NNS one at `rs/nns/governance/src/governance/benches.rs` line 441) that projects instruction cost to `max_number_of_neurons` and enforces a ceiling, so regressions are caught in CI.

## Proof of Concept
1. Deploy an SNS with `max_number_of_neurons` set to a large value.
2. Stake tokens and claim neurons until the neuron count approaches the maximum (`claim_or_refresh_neuron` is permissionless for any token holder).
3. Call `manage_neuron` with `Command::MakeProposal(...)` from any neuron with `SubmitProposal` permission.
4. Observe that `compute_ballots_for_new_proposal()` iterates all neurons synchronously; at sufficient neuron count the call fails with `CanisterInstructionLimitExceeded`.
5. Confirm no proposal can be submitted from any neuron, freezing governance.
6. A deterministic unit test can instrument the loop with a synthetic neuron count and measure instruction consumption via `canbench` to establish the exact threshold.