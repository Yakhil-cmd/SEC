### Title
Developer-Controlled SNS Governance Can Pass Malicious `AddGenericNervousSystemFunction` + `ExecuteGenericNervousSystemFunction` Proposals During the Swap Period to Drain the SNS Treasury After Swap Completes - (File: `rs/sns/governance/src/types.rs`)

---

### Summary

During the SNS `PreInitializationSwap` mode, the developer neuron(s) — which hold the only voting power at genesis — are permitted to submit and pass `AddGenericNervousSystemFunction` and `ExecuteGenericNervousSystemFunction` proposals targeting **any non-SNS-core canister**. Because no swap participants have neurons yet, the developer holds 100% of the voting power at proposal creation time. Ballots are snapshotted at proposal creation, so swap participants who join later cannot vote. The developer can use this window to register and execute a generic function that, once the swap completes and the SNS transitions to `Normal` mode, allows draining the treasury or taking control of dapp canisters.

---

### Finding Description

The SNS `PreInitializationSwap` mode is designed to restrict governance actions while the decentralization swap is running. The disallowed actions are:

- `ManageNervousSystemParameters`
- `TransferSnsTreasuryFunds`
- `MintSnsTokens`
- `UpgradeSnsControlledCanister`
- `RegisterDappCanisters`
- `DeregisterDappCanisters`

However, the following actions are **explicitly allowed** in `PreInitializationSwap` mode:

- `Motion`
- `AddGenericNervousSystemFunction`
- `RemoveGenericNervousSystemFunction`
- `ExecuteGenericNervousSystemFunction` (when targeting a non-SNS-core canister) [1](#0-0) 

The `AddGenericNervousSystemFunction` action allows registering a new callable function that targets **any canister** that is not root, governance, or ledger. [2](#0-1) 

At SNS genesis, only developer neurons exist. Their voting power is reduced by a `voting_power_percentage_multiplier` (e.g., 50% if `initial_swap_amount_e8s / total_e8s = 0.5`), but they are the **only** neurons with any voting power. [3](#0-2) 

When a proposal is created, ballots are snapshotted at that moment from all currently eligible neurons: [4](#0-3) 

Swap participants only receive neurons **after** `finalize_swap` is called and `claim_swap_neurons` completes: [5](#0-4) 

This means any proposal created during the swap period has ballots that include **only developer neurons**. Swap participants who join later cannot vote on these proposals.

The `UpgradeSnsToNextVersion` action is also **not** in the disallowed list for `PreInitializationSwap`, meaning a developer can also propose an SNS framework upgrade during the swap period with 100% of the voting power. [1](#0-0) 

---

### Impact Explanation

**Attack scenario:**

1. Developer creates an SNS with a `voting_power_percentage_multiplier` that gives them majority voting power (e.g., 51% of total tokens are in the swap, so developer multiplier = 49%, but developer holds enough stake to still have majority over zero swap participants).
2. During the swap period (governance is in `PreInitializationSwap` mode), the developer submits an `AddGenericNervousSystemFunction` proposal targeting an attacker-controlled canister (e.g., a canister that calls `transfer` on the SNS ledger or manipulates dapp state).
3. Since no swap participants have neurons yet, the developer's neurons are the only ballots. The proposal passes with 100% of the vote.
4. The developer then submits an `ExecuteGenericNervousSystemFunction` proposal to execute the registered function. This also passes with 100% of the vote.
5. After the swap completes and the SNS transitions to `Normal` mode, the registered generic function is already in `id_to_nervous_system_functions` and can be executed again — or the execution from step 4 already ran.

**Alternatively**, the developer can register a generic function that targets the SNS treasury's ICP account (via a custom canister that calls the ICP ledger on behalf of the SNS governance canister), or targets the dapp canister to exfiltrate data or change ownership.

The impact is: **complete loss of SNS treasury funds or dapp canister control**, affecting all swap participants who contributed ICP in good faith.

---

### Likelihood Explanation

This is reachable by any SNS developer (the entity that submits the `CreateServiceNervousSystem` NNS proposal). The developer controls the initial neuron distribution and is the only party with voting power during the swap period. No privileged IC infrastructure access is required — only the ability to deploy a canister and submit governance proposals via the standard `manage_neuron` Candid API. The attack window is the entire swap duration (typically days to weeks). The developer is an unprivileged ingress sender from the IC's perspective.

---

### Recommendation

1. **Disallow `AddGenericNervousSystemFunction` and `ExecuteGenericNervousSystemFunction` in `PreInitializationSwap` mode.** These actions can be used to register and execute arbitrary canister calls, which is equivalent in power to the already-disallowed `UpgradeSnsControlledCanister` and `TransferSnsTreasuryFunds`.

   In `rs/sns/governance/src/types.rs`, add these to `functions_disallowed_in_pre_initialization_swap()`: [1](#0-0) 

2. **Disallow `UpgradeSnsToNextVersion` in `PreInitializationSwap` mode** for the same reason — it can upgrade SNS canisters while only developer neurons can vote.

3. Alternatively, **delay proposal execution** for any proposal created during `PreInitializationSwap` mode until after the swap finalizes and swap-participant neurons are created, so that all token holders can vote.

---

### Proof of Concept

**Entry path:** Unprivileged developer principal → `manage_neuron` (Candid update call) → SNS Governance canister → `make_proposal` → `AddGenericNervousSystemFunction` targeting attacker canister → proposal passes with developer-only ballots → `ExecuteGenericNervousSystemFunction` → attacker canister drains treasury or manipulates dapp.

**Step-by-step:**

1. Developer deploys SNS with `initial_swap_amount_e8s = 5000`, `total_e8s = 10000` → `voting_power_percentage_multiplier = 50` for developer neurons. Developer holds 1,000,000 e8s stake → effective voting power = 500,000. No swap participants yet → total voting power = 500,000.

2. During swap (governance in `PreInitializationSwap`), developer calls `manage_neuron` with `MakeProposal { action: AddGenericNervousSystemFunction { id: 1000, target_canister_id: attacker_canister, target_method_name: "drain_treasury" } }`.

3. `allows_proposal_action_or_err` in `PreInitializationSwap` mode returns `Ok(())` for `AddGenericNervousSystemFunction`: [6](#0-5) 

4. `compute_ballots_for_new_proposal` snapshots only developer neurons (no swap neurons exist yet): [4](#0-3) 

5. Developer votes YES. Proposal passes with 100% of ballots. `perform_add_generic_nervous_system_function` registers the function: [7](#0-6) 

6. Developer submits `ExecuteGenericNervousSystemFunction { function_id: 1000, payload: ... }`. This is allowed in `PreInitializationSwap` mode (target is not an SNS core canister): [2](#0-1) 

7. Proposal passes. `attacker_canister.drain_treasury()` is called, transferring SNS treasury ICP to the attacker before or after swap finalization.

8. Swap finalizes. `set_sns_governance_to_normal_mode` is called: [8](#0-7) 

9. Swap participants receive neurons but the treasury is already drained.

### Citations

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

**File:** rs/sns/governance/src/types.rs (L264-298)
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
    }
```

**File:** rs/sns/governance/src/types.rs (L300-337)
```rust
    fn execute_generic_nervous_system_function_is_allowed_in_pre_initialization_swap_or_err(
        execute: &ExecuteGenericNervousSystemFunction,
        disallowed_target_canister_ids: &HashSet<CanisterId>,
        id_to_nervous_system_function: &BTreeMap<u64, NervousSystemFunction>,
    ) -> Result<(), GovernanceError> {
        let function_id = execute.function_id;
        let function = id_to_nervous_system_function
            .get(&function_id)
            .ok_or_else(|| {
                // This should never happen in practice, because the caller
                // should have already validated the proposal. This code is just
                // defense in depth.
                GovernanceError::new_with_message(
                    ErrorType::NotFound,
                    format!(
                        "ExecuteGenericNervousSystemFunction specifies an unknown function ID: \
                         {execute:#?}.\nKnown functions: {id_to_nervous_system_function:#?}",
                    ),
                )
            })?;

        let target_canister_id = ValidGenericNervousSystemFunction::try_from(function)
            .expect("Invalid GenericNervousSystemFunction.")
            .target_canister_id;

        let bad = disallowed_target_canister_ids.contains(&target_canister_id);
        if bad {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "ExecuteGenericNervousSystemFunction proposals targeting {target_canister_id:?} are not allowed while \
                     governance is in PreInitializationSwap mode: {execute:#?}"
                ),
            ));
        }

        Ok(())
    }
```

**File:** rs/sns/init/src/distributions.rs (L59-66)
```rust
        // Multiplying this way will give the developer_voting_power_percentage_multiplier
        // as a percentage while also allowing use of checked_div.
        let developer_voting_power_percentage_multiplier = ((swap.initial_swap_amount_e8s as u128)
            * 100)
            .checked_div(swap.total_e8s as u128)
            .expect(
                "Underflow detected when calculating developer voting power percentage multiplier",
            ) as u64;
```

**File:** rs/sns/governance/src/governance.rs (L2247-2298)
```rust
    fn perform_add_generic_nervous_system_function(
        &mut self,
        nervous_system_function: NervousSystemFunction,
    ) -> Result<(), GovernanceError> {
        let id = nervous_system_function.id;

        if nervous_system_function.is_native() {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can only add NervousSystemFunction's of \
                                                          GenericNervousSystemFunction function_type",
            ));
        }

        if is_registered_function_id(id, &self.proto.id_to_nervous_system_functions) {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                format!(
                    "Failed to add NervousSystemFunction. \
                             There is/was already a NervousSystemFunction with id: {id}"
                ),
            ));
        }

        // This validates that it is well-formed, but not the canister targets.
        match ValidGenericNervousSystemFunction::try_from(&nervous_system_function) {
            Ok(valid_function) => {
                let reserved_canisters = self.reserved_canister_targets();
                let target_canister_id = valid_function.target_canister_id;
                let validator_canister_id = valid_function.validator_canister_id;

                if reserved_canisters.contains(&target_canister_id)
                    || reserved_canisters.contains(&validator_canister_id)
                {
                    return Err(GovernanceError::new_with_message(
                        ErrorType::PreconditionFailed,
                        "Cannot add generic nervous system functions that targets sns core canisters, the NNS ledger, or ic00",
                    ));
                }
            }
            Err(msg) => {
                return Err(GovernanceError::new_with_message(
                    ErrorType::PreconditionFailed,
                    msg,
                ));
            }
        }

        self.proto
            .id_to_nervous_system_functions
            .insert(id, nervous_system_function);
        Ok(())
```

**File:** rs/sns/governance/src/governance.rs (L5255-5280)
```rust
        for (k, v) in self.proto.neurons.iter() {
            // If this neuron is eligible to vote, record its
            // voting power at the time of proposal creation (now).
            if v.dissolve_delay_seconds(now_seconds) < min_dissolve_delay_for_vote {
                // Not eligible due to dissolve delay.
                continue;
            }

            let voting_power = v.voting_power(
                now_seconds,
                max_dissolve_delay,
                max_age_bonus,
                max_dissolve_delay_bonus_percentage,
                max_age_bonus_percentage,
            );

            total_power += voting_power as u128;
            electoral_roll.insert(
                k.clone(),
                Ballot {
                    vote: Vote::Unspecified as i32,
                    voting_power,
                    cast_timestamp_seconds: 0,
                },
            );
        }
```

**File:** rs/sns/swap/src/swap.rs (L1600-1612)
```rust
        // Once SNS tokens have been distributed to the correct accounts, claim
        // them as neurons on behalf of the Swap participants.
        finalize_swap_response.set_claim_neuron_result(
            self.claim_swap_neurons(environment.sns_governance_mut())
                .await,
        );
        if finalize_swap_response.has_error_message() {
            return finalize_swap_response;
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );
```

**File:** rs/sns/swap/src/swap.rs (L1893-1904)
```rust
    pub async fn set_sns_governance_to_normal_mode(
        sns_governance_client: &mut impl SnsGovernanceClient,
    ) -> SetModeCallResult {
        // The SnsGovernanceClient Trait converts any errors to Err(CanisterCallError)
        // No panics should occur when issuing this message.
        sns_governance_client
            .set_mode(SetMode {
                mode: governance::Mode::Normal as i32,
            })
            .await
            .into()
    }
```
