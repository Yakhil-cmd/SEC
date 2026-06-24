### Title
`ManageLedgerParameters` Proposal Action Not Restricted During `PreInitializationSwap` Mode - (File: `rs/sns/governance/src/types.rs`)

### Summary
The SNS Governance canister enforces a `PreInitializationSwap` mode to protect the integrity of the initial token swap. A hardcoded denylist gates which proposal actions are forbidden in this mode. However, `ManageLedgerParameters` (Id = 13) — which can raise the SNS ledger's `transfer_fee` — is absent from that denylist. Because developer neurons hold 100% of voting power before any swap participant receives tokens, a malicious developer can pass a `ManageLedgerParameters` proposal mid-swap, increasing the transfer fee and reducing the SNS tokens each participant receives during finalization.

### Finding Description
`functions_disallowed_in_pre_initialization_swap()` in `rs/sns/governance/src/types.rs` enumerates the six proposal actions blocked during `PreInitializationSwap`:

```
manage_nervous_system_parameters
transfer_sns_treasury_funds
mint_sns_tokens
upgrade_sns_controlled_canister
register_dapp_canisters
deregister_dapp_canisters
``` [1](#0-0) 

The gating logic in `proposal_action_is_allowed_in_pre_initialization_swap_or_err` performs a simple membership check against that list and returns `Ok(())` for every action not present: [2](#0-1) 

`ManageLedgerParameters` (Id = 13) is a native action defined in the governance proto and is reachable via `make_proposal` by any neuron holder: [3](#0-2) 

Its execution path in `perform_action` calls `perform_manage_ledger_parameters`, which pushes the new fee to the SNS ledger canister with no lifecycle-state guard: [4](#0-3) 

During `PreInitializationSwap`, developer neurons own the entire outstanding token supply (swap participants have not yet received any SNS tokens). They therefore command 100% of voting power and can unilaterally adopt any proposal that is not explicitly blocked.

The `Mode::PreInitializationSwap` state is set when the SNS is created and persists until the swap canister calls `set_mode(Normal)` after a successful finalization: [5](#0-4) 

The swap canister's `Init` struct records `transaction_fee_e8s` at deployment time, but the SNS ledger itself is the authoritative source for the fee charged on each token transfer. Raising the ledger fee mid-swap means every SNS token transfer during finalization (one per participant basket neuron) incurs the higher fee, reducing the net tokens credited to participants.

### Impact Explanation
A malicious SNS developer can:
1. Submit a `ManageLedgerParameters` proposal raising `transfer_fee` to an arbitrarily large value while the swap is open.
2. Self-adopt the proposal (100% voting power pre-swap).
3. Allow the swap to commit normally.
4. During finalization the swap canister transfers SNS tokens to each participant; each transfer is charged the inflated fee, silently reducing participant allocations.

Participants who have already transferred ICP cannot withdraw it once the swap is committed (`LIFECYCLE_COMMITTED`), so they cannot avoid the impact. The developer retains their genesis allocation unaffected.

This is a direct governance authorization bypass: an action that undermines the integrity of the initial swap is executable before the system reaches its "initialized" (`Normal`) state, mirroring the Eggs.sol pattern where `leverage` was callable before `start == true`.

### Likelihood Explanation
- The attack window is the entire `PreInitializationSwap` period (from SNS creation until swap finalization).
- No special capability beyond holding developer neurons is required; the proposal path is a standard ingress call to `manage_neuron` → `MakeProposal`.
- Developer neurons hold 100% of voting power during this window, so no external collusion is needed.
- The `ManageLedgerParameters` action is fully implemented and callable on mainnet SNS instances today.

### Recommendation
Add `NervousSystemFunction::manage_ledger_parameters()` to the `functions_disallowed_in_pre_initialization_swap()` vector in `rs/sns/governance/src/types.rs`:

```rust
pub fn functions_disallowed_in_pre_initialization_swap() -> Vec<NervousSystemFunction> {
    vec![
        NervousSystemFunction::manage_nervous_system_parameters(),
        NervousSystemFunction::transfer_sns_treasury_funds(),
        NervousSystemFunction::mint_sns_tokens(),
        NervousSystemFunction::upgrade_sns_controlled_canister(),
        NervousSystemFunction::register_dapp_canisters(),
        NervousSystemFunction::deregister_dapp_canisters(),
        NervousSystemFunction::manage_ledger_parameters(), // ADD THIS
    ]
}
```

Additionally audit `AdvanceSnsTargetVersion` (Id = 15) and `UpgradeSnsToNextVersion` (Id = 7) for the same class of issue, as neither is currently blocked during `PreInitializationSwap`. [1](#0-0) 

### Proof of Concept
1. Deploy an SNS via NNS `CreateServiceNervousSystem`; governance enters `PreInitializationSwap` mode.
2. The swap opens (`LIFECYCLE_OPEN`); participants begin transferring ICP.
3. Developer calls `manage_neuron` → `MakeProposal` with `Action::ManageLedgerParameters { transfer_fee: Some(100_000_000) }` (100 SNS tokens per transfer).
4. Developer's neuron self-adopts the proposal (100% voting power); `perform_manage_ledger_parameters` executes and updates the SNS ledger.
5. Swap reaches `LIFECYCLE_COMMITTED`; `finalize` distributes SNS tokens — each basket-neuron transfer is charged 100 SNS tokens, silently reducing every participant's allocation.
6. Developer's genesis neurons are unaffected; participants receive materially fewer tokens than the swap parameters implied.

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

**File:** rs/sns/governance/src/types.rs (L279-298)
```rust
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

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L709-712)
```text
    // Change some parameters on the ledger.
    //
    // Id = 13.
    ManageLedgerParameters manage_ledger_parameters = 17;
```

**File:** rs/sns/governance/src/governance.rs (L785-801)
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
    }
```

**File:** rs/sns/governance/src/governance.rs (L2213-2216)
```rust
            Action::ManageLedgerParameters(manage_ledger_parameters) => {
                self.perform_manage_ledger_parameters(proposal_id, manage_ledger_parameters)
                    .await
            }
```
