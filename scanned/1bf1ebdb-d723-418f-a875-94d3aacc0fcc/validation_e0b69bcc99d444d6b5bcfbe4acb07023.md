### Title
SNS Governance Allows `AddGenericNervousSystemFunction` + `ExecuteGenericNervousSystemFunction` During `PreInitializationSwap` Mode With Non-Representative Voting Power - (`File: rs/sns/governance/src/types.rs`)

---

### Summary

During `PreInitializationSwap` mode, SNS Governance explicitly permits `MakeProposal` and `RegisterVote` commands, and the proposal-action allowlist omits several impactful action types — including `AddGenericNervousSystemFunction`, `ExecuteGenericNervousSystemFunction` (targeting non-SNS canisters), `ManageLedgerParameters`, and `ManageDappCanisterSettings`. Because swap participants have not yet received their neurons at this stage, all voting power is concentrated in the founders' initial neurons, making the voting power distribution non-representative of the final token holder community.

---

### Finding Description

SNS Governance defines two modes: `Normal` and `PreInitializationSwap`. The `PreInitializationSwap` mode is entered when an SNS token swap is initiated and is intended to restrict dangerous governance actions while the swap is in progress.

`allows_manage_neuron_command_or_err` in `rs/sns/governance/src/types.rs` explicitly allows `MakeProposal` and `RegisterVote` commands during `PreInitializationSwap`: [1](#0-0) 

The proposal-action filter `functions_disallowed_in_pre_initialization_swap()` only blocks six specific action types: [2](#0-1) 

The following native action types are **not** in the disallowed list and are therefore fully executable during `PreInitializationSwap`:

- `AddGenericNervousSystemFunction` (id=4)
- `RemoveGenericNervousSystemFunction` (id=5)
- `ExecuteGenericNervousSystemFunction` targeting non-SNS canisters
- `ManageLedgerParameters`
- `ManageDappCanisterSettings`
- `UpgradeSnsToNextVersion`
- `AdvanceSnsTargetVersion`
- `RegisterExtension` / `ExecuteExtensionOperation` / `UpgradeExtension`

This is confirmed by the test fixture in `rs/sns/governance/src/types/tests.rs`: [3](#0-2) 

The `make_proposal` function in `rs/sns/governance/src/governance.rs` calls `allows_proposal_action_or_err` but only at the proposal-action level — there is no additional mode check that blocks the above action types: [4](#0-3) 

The `perform_add_generic_nervous_system_function` function validates that the target is not a reserved SNS canister, but permits targeting arbitrary external/dapp canisters: [5](#0-4) 

---

### Impact Explanation

During `PreInitializationSwap`, 100% of voting power belongs to the initial founders' neurons — swap participants have not yet received their neurons. An SNS founder with majority voting power can:

1. Submit an `AddGenericNervousSystemFunction` proposal targeting a dapp canister or any external canister not in the reserved set.
2. Vote it through with 100% of voting power (no swap participants exist yet to oppose).
3. Submit an `ExecuteGenericNervousSystemFunction` proposal invoking the newly registered function.
4. Vote it through again with 100% of voting power.

This allows founders to execute arbitrary calls to dapp canisters controlled by the SNS before the community gets any say. Similarly, `ManageLedgerParameters` proposals can alter token economics (fees, token name/symbol) and `ManageDappCanisterSettings` can alter dapp canister configurations — all with non-representative voting power.

The impact mirrors the veRAACToken report: governance decisions are made during a restricted period when voting power is unreliable/non-representative, potentially allowing founders to manipulate the SNS in ways that harm future swap participants.

---

### Likelihood Explanation

The `PreInitializationSwap` mode is a normal, expected state for every SNS launch. Any SNS whose founders hold majority voting power (which is always the case during the swap, since swap participants have no neurons yet) can exploit this. The attacker-controlled entry path is a standard `manage_neuron` ingress call — no special privileges, leaked keys, or threshold attacks are required. The call path is:

`manage_neuron` ingress → `allows_manage_neuron_command_or_err` (passes for `MakeProposal`) → `make_proposal` → `allows_proposal_action_or_err` (passes for `AddGenericNervousSystemFunction`) → proposal created and voted through with founder majority. [6](#0-5) 

---

### Recommendation

Add `AddGenericNervousSystemFunction`, `ManageLedgerParameters`, `ManageDappCanisterSettings`, `ExecuteExtensionOperation`, `RegisterExtension`, and `UpgradeExtension` to `functions_disallowed_in_pre_initialization_swap()`. Alternatively, adopt an allowlist approach: only explicitly safe actions (e.g., `Motion`) should be permitted during `PreInitializationSwap`, with all others blocked by default. [2](#0-1) 

---

### Proof of Concept

1. SNS is created; governance enters `PreInitializationSwap` mode.
2. Founder (holding 100% of voting power) calls `manage_neuron` with `MakeProposal(AddGenericNervousSystemFunction { target: dapp_canister, method: "drain_funds", ... })`.
3. `allows_manage_neuron_command_or_err` returns `Ok` (line 189).
4. `allows_proposal_action_or_err` returns `Ok` — `AddGenericNervousSystemFunction` is not in the disallowed list (line 281-283).
5. Proposal is created with ballots only for founder neurons.
6. Founder votes `Yes`; proposal passes with 100% of voting power.
7. Founder submits `ExecuteGenericNervousSystemFunction` for the newly registered function targeting the dapp canister.
8. Same flow — passes with 100% of voting power.
9. Dapp canister executes the call before any swap participant has received a neuron. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/governance/src/types.rs (L182-197)
```rust
    fn manage_neuron_command_is_allowed_in_pre_initialization_swap_or_err(
        command: &manage_neuron::Command,
        caller_is_swap_canister: bool,
    ) -> Result<(), GovernanceError> {
        use manage_neuron::Command as C;
        let ok = match command {
            C::Follow(_)
            | C::MakeProposal(_)
            | C::RegisterVote(_)
            | C::AddNeuronPermissions(_)
            | C::RemoveNeuronPermissions(_) => true,

            C::ClaimOrRefresh(_) => caller_is_swap_canister,

            _ => false,
        };
```

**File:** rs/sns/governance/src/types.rs (L253-262)
```rust
    pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
        vec![
            NervousSystemFunction::manage_nervous_system_parameters(),
            NervousSystemFunction::transfer_sns_treasury_funds(),
            NervousSystemFunction::mint_sns_tokens(),
            NervousSystemFunction::upgrade_sns_controlled_canister(),
            NervousSystemFunction::register_dapp_canisters(),
            NervousSystemFunction::deregister_dapp_canisters(),
        ]
    }
```

**File:** rs/sns/governance/src/types.rs (L264-297)
```rust
    fn proposal_action_is_allowed_in_pre_initialization_swap_or_err(
        action: &Action,
        disallowed_target_canister_ids: &HashSet<CanisterId>,
        id_to_nervous_system_function: &BTreeMap<u64, NervousSystemFunction>,
    ) -> Result<(), GovernanceError> {
        // ExecuteGenericNervousSystemFunction is special in that it
        // is only disallowed in some cases.
        if let Action::ExecuteGenericNervousSystemFunction(execute) = action {
            return Self::execute_generic_nervous_system_function_is_allowed_in_pre_initialization_swap_or_err(
                    execute,
                    disallowed_target_canister_ids,
                    id_to_nervous_system_function,
                );
        }

        let nervous_system_function = NervousSystemFunction::from(action.clone());

        let is_action_disallowed = Self::functions_disallowed_in_pre_initialization_swap()
            .into_iter()
            .any(|t| t.id == nervous_system_function.id);

        if is_action_disallowed {
            Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Proposal type for {:?} is not allowed while governance is in \
                     PreInitializationSwap ({}) mode.",
                    nervous_system_function,
                    Mode::PreInitializationSwap as i32,
                ),
            ))
        } else {
            Ok(())
        }
```

**File:** rs/sns/governance/src/types/tests.rs (L401-405)
```rust
        let allowed_in_pre_initialization_swap = vec! [
            Action::Motion(Default::default()),
            Action::AddGenericNervousSystemFunction(Default::default()),
            Action::RemoveGenericNervousSystemFunction(Default::default()),
        ]; 
```

**File:** rs/sns/governance/src/governance.rs (L2278-2284)
```rust
                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
```

**File:** rs/sns/governance/src/governance.rs (L3457-3487)
```rust
    pub async fn make_proposal(
        &mut self,
        proposer_id: &NeuronId,
        caller: &PrincipalId,
        proposal: &Proposal,
    ) -> Result<ProposalId, GovernanceError> {
        let now_seconds = self.env.now();

        // Validate proposal
        // TODO: return the optional extension spec
        let (rendering, action_auxiliary) = self.validate_and_render_proposal(proposal).await?;

        let nervous_system_parameters = self.nervous_system_parameters_or_panic();

        // This should not panic, because the proposal was just validated.
        let action = proposal.action.as_ref().expect("No action.");

        // These cannot be the target of a ExecuteGenericNervousSystemFunction proposal.
        let disallowed_target_canister_ids = hashset! {
            self.proto.root_canister_id_or_panic(),
            self.proto.ledger_canister_id_or_panic(),
            self.env.canister_id(),
            // TODO add ledger archives
            // TODO add swap canister here?
        };

        self.mode().allows_proposal_action_or_err(
            action,
            &disallowed_target_canister_ids,
            &self.proto.id_to_nervous_system_functions,
        )?;
```
