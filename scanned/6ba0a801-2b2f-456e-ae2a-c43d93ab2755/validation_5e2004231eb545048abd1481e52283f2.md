### Title
SNS Governance Permanently Stuck in `PreInitializationSwap` Mode With No Alternative Exit Mechanism — (`rs/sns/governance/src/governance.rs`, `rs/sns/swap/src/swap.rs`)

### Summary

The SNS governance canister enters `PreInitializationSwap` mode at SNS creation and can only exit it via a single code path: the swap canister calling `set_mode(Normal)` inside `finalize_inner()`. The `set_mode()` function in SNS governance enforces that **only the swap canister** may call it, and **only to set `Normal` mode**. No governance proposal action, no NNS-level direct call, and no other principal can change the mode. If the `set_mode` call consistently fails after all other finalization steps have completed (ICP swept, SNS tokens distributed, neurons claimed), the SNS governance is permanently stuck in `PreInitializationSwap` mode with no on-chain exit path.

### Finding Description

SNS governance is initialized in `PreInitializationSwap` mode. The sole exit mechanism is `set_mode()`, callable only by the swap canister: [1](#0-0) 

The function panics — and thus the call fails — if the caller is not the registered swap canister, or if the requested mode is anything other than `Normal`. No governance proposal action can change the mode: [2](#0-1) 

The swap canister calls `set_mode(Normal)` only in the committed finalization path, as the last substantive step of `finalize_inner()`: [3](#0-2) 

If this call fails, `set_set_mode_call_result` sets an error and `finalize_inner` returns without completing. The `finalize` function releases its lock and returns the error to the caller: [4](#0-3) 

While `finalize` is idempotent and can be retried (already-completed transfer steps are skipped), if `set_mode` consistently fails — e.g., because the governance canister is stopped, out of cycles, or a subsequent upgrade introduces a panic in the `set_mode` handler — there is **no alternative on-chain mechanism** to transition governance out of `PreInitializationSwap` mode. The mode is also permanently set for any **aborted** swap, by design, with no exit path: [5](#0-4) 

The operations blocked in `PreInitializationSwap` mode include dissolving neurons, disbursing maturity, transferring treasury funds, minting SNS tokens, upgrading SNS-controlled canisters, and managing nervous system parameters: [6](#0-5) [7](#0-6) 

### Impact Explanation

**Impact: Medium.**

If the SNS governance canister is stuck in `PreInitializationSwap` mode after a committed swap (where ICP has been swept and SNS tokens distributed to participants):

- Token holders cannot dissolve or disburse their neurons — their staked tokens are permanently locked.
- The SNS treasury cannot be accessed via governance proposals.
- No `ManageNervousSystemParameters`, `TransferSnsTreasuryFunds`, `MintSnsTokens`, or `UpgradeSnsControlledCanister` proposals can execute.
- The SNS is operationally dead despite having successfully distributed tokens.

The only recovery path is an NNS governance proposal to upgrade the SNS governance canister with a post-upgrade hook that resets the mode — a privileged, slow, and uncertain path.

### Likelihood Explanation

**Likelihood: Low.**

The `set_mode` function is simple and should succeed under normal conditions. However, realistic failure scenarios include:

1. The SNS governance canister running out of cycles between the neuron-claiming step and the `set_mode` call.
2. A subsequent SNS governance upgrade (approved by NNS) introducing a regression that causes the `set_mode` handler to panic.
3. The governance canister being stopped by the SNS root (which is itself controlled by the stuck governance — a deadlock).

The aborted-swap case is by design but still leaves developer neuron holders permanently unable to dissolve their neurons, which is a concrete user-facing impact with 100% likelihood for any failed SNS swap.

### Recommendation

1. **Add a governance proposal action** (e.g., `ExitPreInitializationSwapMode`) that allows SNS token holders to vote to transition governance to `Normal` mode after a swap has concluded (committed or aborted), as a fallback when the swap canister cannot call `set_mode`.
2. **Alternatively**, allow the NNS governance canister (in addition to the swap canister) to call `set_mode`, providing an administrative escape hatch without requiring a full canister upgrade.
3. For the aborted-swap case specifically, consider automatically transitioning governance to `Normal` mode (or a new `Abandoned` mode that permits neuron dissolution) when the swap canister finalizes an aborted swap, so developer neuron holders are not permanently locked.

### Proof of Concept

**Scenario (committed swap, `set_mode` failure):**

1. SNS is created; governance enters `PreInitializationSwap` mode.
2. Swap reaches `Committed` state; `finalize` is called.
3. `sweep_icp`, `settle_neurons_fund_participation`, `create_sns_neuron_recipes`, `sweep_sns`, and `claim_swap_neurons` all succeed — ICP is transferred to SNS governance, SNS tokens are distributed, neurons are claimed.
4. `set_sns_governance_to_normal_mode` is called; the governance canister is out of cycles and rejects the call.
5. `finalize` returns with error `"Setting the SNS Governance mode to normal did not complete fully."` and releases its lock.
6. Cycles are not replenished (or a governance upgrade introduced a regression); every subsequent `finalize` retry fails at the same step.
7. SNS governance remains in `PreInitializationSwap` mode indefinitely. All token holders' neurons are locked; no governance proposals can execute; the SNS treasury is inaccessible.

The test `test_finalization_halts_when_set_mode_fails` in `rs/sns/swap/tests/swap.rs` confirms this exact failure mode is reachable: [8](#0-7)

### Citations

**File:** rs/sns/governance/src/governance.rs (L785-800)
```rust
    pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
        let mode =
            governance::Mode::try_from(mode).unwrap_or_else(|_| panic!("Unknown mode: {mode}"));

        if !self.is_swap_canister(caller) {
            panic!("Caller must be the swap canister.");
        }

        // As of Aug, 2022, the only use-case we have for set_mode is to enter
        // Normal mode (from PreInitializationSwap). Therefore, this is here
        // just to make sure we do not proceed with unexpected operations.
        if mode != governance::Mode::Normal {
            panic!("Entering {mode:?} mode is not allowed.");
        }

        self.proto.mode = mode as i32;
```

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

**File:** rs/sns/governance/src/types.rs (L229-251)
```rust
    pub fn allows_proposal_action_or_err(
        &self,
        action: &Action,
        disallowed_target_canister_ids: &HashSet<CanisterId>,
        id_to_nervous_system_function: &BTreeMap<u64, NervousSystemFunction>,
    ) -> Result<(), GovernanceError> {
        use governance::Mode;
        match self {
            Mode::Normal => Ok(()),

            Mode::PreInitializationSwap => {
                Self::proposal_action_is_allowed_in_pre_initialization_swap_or_err(
                    action,
                    disallowed_target_canister_ids,
                    id_to_nervous_system_function,
                )
            }

            Mode::Unspecified => {
                panic!("Governance's mode is not specified.");
            }
        }
    }
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

**File:** rs/sns/swap/src/swap.rs (L1514-1531)
```rust
        if finalize_swap_response.has_error_message() {
            log!(
                ERROR,
                "The swap did not finalize successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        } else {
            log!(
                INFO,
                "The swap finalized successfully. \n\
                finalize_swap_response: {finalize_swap_response:?}"
            );
        }

        // Release the lock. Note, if there is a panic, the lock will
        // not be released. In that case, the Swap canister will need
        // to be upgraded to release the lock.
        self.unlock_finalize_swap();
```

**File:** rs/sns/swap/src/swap.rs (L1610-1612)
```rust
        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L127-129)
```rust
///     1. State machine:
///         1. `{ governance::Mode::PreInitializationSwap } FinalizeUnSuccessfully { governance::Mode::PreInitializationSwap }`
///         2. `{ governance::Mode::PreInitializationSwap } FinalizeSuccessfully   { governance::Mode::Normal }`
```

**File:** rs/sns/swap/tests/swap.rs (L3071-3151)
```rust
#[tokio::test]
async fn test_finalization_halts_when_set_mode_fails() {
    // Step 1: Prepare the world

    let mut swap = Swap {
        lifecycle: Committed as i32,
        init: Some(init()),
        params: Some(params()),
        buyers: buyers(),
        direct_participation_icp_e8s: Some(
            buyers()
                .values()
                .map(|buyer_state| buyer_state.icp.as_ref().unwrap().amount_e8s)
                .sum(),
        ),
        ..Default::default()
    };

    let expected_canister_call_error = CanisterCallError {
        code: Some(0),
        description: "BAD REPLY".to_string(),
    };

    let mut clients = CanisterClients {
        sns_governance: SpySnsGovernanceClient::new(vec![
            SnsGovernanceClientReply::ClaimSwapNeurons(ClaimSwapNeuronsResponse::new(
                create_successful_swap_neuron_basket_for_one_direct_participant(
                    PrincipalId::new_user_test_id(1001),
                    3,
                ),
            )),
            SnsGovernanceClientReply::CanisterCallError(expected_canister_call_error.clone()),
        ]),
        nns_governance: SpyNnsGovernanceClient::new(vec![
            NnsGovernanceClientReply::SettleNeuronsFundParticipation(
                SettleNeuronsFundParticipationResponse {
                    result: Some(settle_neurons_fund_participation_response::Result::Ok(
                        settle_neurons_fund_participation_response::Ok {
                            neurons_fund_neuron_portions: vec![],
                        },
                    )),
                },
            ),
        ]),
        icp_ledger: SpyLedger::new(vec![LedgerReply::TransferFunds(Ok(1000))]),
        sns_ledger: SpyLedger::new(vec![
            LedgerReply::TransferFunds(Ok(1000)),
            LedgerReply::TransferFunds(Ok(1001)),
            LedgerReply::TransferFunds(Ok(1002)),
        ]),
        ..spy_clients()
    };

    // Step 2: Call finalize
    let result = swap.finalize(now_fn, &mut clients).await;

    assert_eq!(
        result.set_mode_call_result,
        Some(SetModeCallResult {
            possibility: Some(set_mode_call_result::Possibility::Err(
                expected_canister_call_error
            )),
        })
    );

    assert_eq!(
        result.error_message,
        Some(String::from(
            "Setting the SNS Governance mode to normal did not complete fully. Halting swap finalization"
        ))
    );

    // Assert that sweep_icp was executed correctly, but ignore the specific values
    assert!(result.sweep_icp_result.is_some());
    assert!(result.settle_neurons_fund_participation_result.is_some());
    assert!(result.create_sns_neuron_recipes_result.is_some());
    assert!(result.sweep_sns_result.is_some());
    assert!(result.claim_neuron_result.is_some());
    // set_dapp_controllers_result is None as this is not the aborted path
    assert!(result.set_dapp_controllers_call_result.is_none());
}
```
