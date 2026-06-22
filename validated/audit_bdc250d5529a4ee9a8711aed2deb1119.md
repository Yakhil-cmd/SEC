### Title
SNS Governance Proposal Ballot Cap Bypassed for All `ExecuteGenericNervousSystemFunction` Proposals, Enabling Heap Exhaustion Spam Attack - (File: `rs/sns/governance/src/types.rs`)

### Summary
In SNS governance, `Action::ExecuteGenericNervousSystemFunction` unconditionally returns `true` from `allowed_when_resources_are_low()`, causing both the heap-growth guard and the `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` cap to be skipped for every generic-function proposal. An unprivileged neuron holder can submit an unbounded stream of such proposals, accumulating ballot maps in the governance canister's heap until it is exhausted, analogous to the Aragon spam-vote attack described in the external report.

---

### Finding Description

`Action::allowed_when_resources_are_low()` in SNS governance returns `true` for the entire `ExecuteGenericNervousSystemFunction` variant without any further discrimination: [1](#0-0) 

This single flag controls two independent guards inside `make_proposal`:

**Guard 1 – heap-growth check** (in `validate_and_render_proposal`): [2](#0-1) 

**Guard 2 – ballot-count cap**: [3](#0-2) 

Both guards are skipped whenever `allowed_when_resources_are_low()` is `true`. The only remaining barrier is the per-proposal `reject_cost_e8s` stake check: [4](#0-3) 

The fee is charged by incrementing `neuron_fees_e8s`, which reduces the neuron's effective stake. However, if `reject_cost_e8s` is zero (a valid SNS configuration), there is no barrier at all. Even with a non-zero fee, an attacker with sufficient stake can submit `floor(stake / reject_cost_e8s)` proposals beyond the cap before being blocked.

The comment in the code ("Due to possible need of emergency functions defined as GenericNervousSystemFunctions") reveals the intent was to allow *emergency* generic functions through, but the implementation applies the bypass to *all* generic functions indiscriminately, because there is no mechanism to distinguish emergency from non-emergency registered functions. [5](#0-4) 

---

### Impact Explanation

Each accepted proposal stores a full ballot map (one entry per eligible neuron) in the governance canister's heap. With the cap bypassed, an attacker can accumulate thousands of open proposals, each carrying a large ballot map. This can:

1. **Exhaust the SNS governance canister's heap**, causing it to trap on future updates or upgrades.
2. **Prevent legitimate proposals from being processed**, because the canister becomes unresponsive or unable to allocate memory for new state.
3. **Permanently brick the SNS** if the heap is exhausted before an emergency upgrade proposal can be executed (a circular dependency: the upgrade proposal itself requires heap space to store its ballot map).

The `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` constant exists precisely to prevent this class of attack: [6](#0-5) 

---

### Likelihood Explanation

- **Entry path is unprivileged**: any principal holding a neuron with `NeuronPermissionType::SubmitProposal` in an SNS can trigger this. Neuron creation is permissionless in most SNS deployments.
- **Generic nervous system functions are common**: most production SNS deployments register at least one `ExecuteGenericNervousSystemFunction` to manage their dapp canisters.
- **Cost barrier is low or absent**: `reject_cost_e8s` is SNS-configurable and defaults to a small value; some SNS instances set it to zero to encourage participation.
- **No per-neuron proposal rate limit exists** in the SNS governance code path for this action type.

---

### Recommendation

1. **Short term**: Restrict the `ExecuteGenericNervousSystemFunction` bypass to a designated subset of registered functions explicitly marked as "critical/emergency" (e.g., via a flag on `NervousSystemFunction`), rather than applying it to the entire action variant.
2. **Short term**: Apply the ballot-count cap independently of `allowed_when_resources_are_low`, or introduce a separate, tighter per-neuron cap on outstanding proposals of this type.
3. **Long term**: Introduce a per-neuron outstanding-proposal limit that is enforced regardless of action type, mirroring the NNS `MAX_NUMBER_OF_OPEN_MANAGE_NEURON_PROPOSALS` pattern.

---

### Proof of Concept

1. Deploy an SNS with `reject_cost_e8s = 0` and register one `ExecuteGenericNervousSystemFunction`.
2. Obtain a neuron with `SubmitProposal` permission.
3. In a loop, call `manage_neuron` → `MakeProposal` with `Action::ExecuteGenericNervousSystemFunction { function_id: <registered_id>, payload: vec![] }`.
4. Observe that proposals are accepted indefinitely past `MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS` (confirmed by the test at `rs/nns/governance/tests/governance.rs` line 8062–8087 which explicitly documents this bypass for NNS `InstallCode`; the SNS analog has no equivalent test asserting a bound).
5. After enough proposals, the governance canister's heap is exhausted and all subsequent update calls trap. [7](#0-6) [8](#0-7) [3](#0-2)

### Citations

**File:** rs/sns/governance/src/types.rs (L1748-1762)
```rust
impl Action {
    /// Returns whether proposals with such an action should be allowed to
    /// be submitted when the heap growth potential is low.
    pub(crate) fn allowed_when_resources_are_low(&self) -> bool {
        match self {
            // Due to possible need of an emergency upgrade of the dapp
            Action::UpgradeSnsControlledCanister(_) => true,
            // Due to possible need of an emergency upgrade of the SNS
            Action::UpgradeSnsToNextVersion(_) => true,
            // Due to possible need of emergency functions defined as
            // GenericNervousSystemFunctions
            Action::ExecuteGenericNervousSystemFunction(_) => true,
            _ => false,
        }
    }
```

**File:** rs/sns/governance/src/governance.rs (L3427-3429)
```rust
        if !proposal.allowed_when_resources_are_low() {
            self.check_heap_can_grow()?;
        }
```

**File:** rs/sns/governance/src/governance.rs (L3457-3462)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
```

**File:** rs/sns/governance/src/governance.rs (L3519-3526)
```rust
        // If the current stake of the proposer neuron is less than the cost
        // of having a proposal rejected, the neuron cannot make a proposal.
        if proposer.stake_e8s() < reject_cost_e8s {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Neuron doesn't have enough stake to submit proposal.",
            ));
        }
```

**File:** rs/sns/governance/src/governance.rs (L3528-3547)
```rust
        // Check that there are not too many proposals.  What matters
        // here is the number of proposals for which ballots have not
        // yet been cleared, because ballots take the most amount of
        // space.
        if self
            .proto
            .proposals
            .values()
            .filter(|data| !data.ballots.is_empty())
            .count()
            >= MAX_NUMBER_OF_PROPOSALS_WITH_BALLOTS
            && !proposal.allowed_when_resources_are_low()
        {
            return Err(GovernanceError::new_with_message(
                ErrorType::ResourceExhausted,
                "Reached maximum number of proposals that have not yet \
                been taken into account for voting rewards. \
                Please try again later.",
            ));
        }
```
