### Title
SNS Governance Permanently Locked in `PreInitializationSwap` Mode When Swap Canister Is the Sole Mode-Transition Authority — (File: `rs/sns/governance/src/governance.rs`)

---

### Summary

The SNS Governance canister starts every SNS launch in `PreInitializationSwap` mode, which blocks token disbursement, neuron dissolving, treasury transfers, and other critical operations. The **only** entity that can transition governance to `Normal` mode is the paired Swap canister, via `set_mode`. If the Swap canister becomes permanently unavailable (e.g., deleted, trapped in a broken state, or its `finalize` call permanently fails at the `set_mode` step), SNS Governance is permanently locked in `PreInitializationSwap` mode with no time-based or alternative-caller escape hatch. All SNS token holders lose the ability to disburse, dissolve, or transfer their neurons indefinitely.

---

### Finding Description

SNS Governance is initialized in `PreInitializationSwap` mode for every new SNS. The mode is stored in `GovernanceProto.mode` and enforced at every `manage_neuron` and proposal submission call.

The `set_mode` endpoint in `rs/sns/governance/canister/canister.rs` is the **only** way to transition governance to `Normal` mode:

```rust
// rs/sns/governance/canister/canister.rs:537-547
/// Sets the mode. Only the swap canister is allowed to call this.
#[update]
fn set_mode(request: SetMode) -> SetModeResponse {
    governance_mut().set_mode(request.mode, caller());
    SetModeResponse {}
}
```

The implementation enforces a strict single-caller check with no fallback:

```rust
// rs/sns/governance/src/governance.rs:785-801
pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
    let mode = governance::Mode::try_from(mode)...;
    if !self.is_swap_canister(caller) {
        panic!("Caller must be the swap canister.");
    }
    if mode != governance::Mode::Normal {
        panic!("Entering {mode:?} mode is not allowed.");
    }
    self.proto.mode = mode as i32;
}
```

The `is_swap_canister` check compares the caller against `self.proto.swap_canister_id`, which is set at initialization and is immutable. No other principal — not the SNS root, not NNS governance, not any neuron holder — can call `set_mode`.

The Swap canister calls `set_mode` only inside `finalize_inner`, at the very last step of a successful committed-swap finalization:

```rust
// rs/sns/swap/src/swap.rs:1610-1612
finalize_swap_response.set_set_mode_call_result(
    Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
);
```

If `set_sns_governance_to_normal_mode` fails (e.g., the governance canister rejects the call, or the Swap canister itself is deleted or permanently broken), `finalize_inner` records an error and halts — but governance remains in `PreInitializationSwap` mode forever. There is no time-based fallback, no NNS override path, and no alternative caller.

The `PreInitializationSwap` mode blocks the following operations for all token holders:

- `Disburse`, `Split`, `DisburseMaturity`, `MergeMaturity`, `Configure` (neuron dissolve state changes) — all blocked
- `TransferSnsTreasuryFunds`, `MintSnsTokens`, `UpgradeSnsControlledCanister`, `ManageNervousSystemParameters` — all blocked

---

### Impact Explanation

If the Swap canister is deleted (which is the intended lifecycle after finalization), or if `finalize` permanently fails at the `set_mode` step (e.g., due to a governance canister bug, a message-routing failure, or a cycles exhaustion event on the governance canister at that exact moment), all SNS token holders are permanently locked out of:

- Dissolving and disbursing their neurons (cannot retrieve their staked SNS tokens)
- Transferring SNS treasury funds
- Upgrading SNS-controlled dapp canisters

This is a **permanent, unrecoverable lock** of all SNS token holder assets with no on-chain escape path. The impact is equivalent to the Fantom `tokensTradeable` bug: a single point of failure that can permanently paralyze all token operations.

---

### Likelihood Explanation

The scenario is realistic in at least two ways:

1. **Swap canister deletion after a failed `set_mode`**: The Swap canister is designed to be deleted after finalization. If `set_mode` fails transiently and the Swap canister is subsequently deleted before a retry succeeds, governance is permanently locked. The `finalize` function is idempotent and retryable, but only while the Swap canister still exists.

2. **Governance canister temporarily out of cycles or trapped**: If the SNS Governance canister is out of cycles or in a trapped state at the exact moment `set_mode` is called during finalization, the call fails, `finalize_inner` halts with an error, and the Swap canister has no mechanism to retry `set_mode` independently of the full `finalize` flow.

The `auto_finalize` path in `run_periodic_tasks` does retry, but only if `can_auto_finalize()` returns `Ok` — which requires the swap to still be in `Committed` state and not yet finalized. Once partial finalization has occurred (ICP swept, neurons claimed), the retry logic may not re-enter the `set_mode` step correctly.

---

### Recommendation

Add a time-based fallback to `set_mode` that allows any caller to transition governance to `Normal` mode after a sufficient delay past the swap's end date, analogous to the Fantom recommendation:

```rust
pub fn set_mode(&mut self, mode: i32, caller: PrincipalId) {
    let mode = governance::Mode::try_from(mode)...;
    let caller_is_swap = self.is_swap_canister(caller);
    let deadline_passed = self.env.now() > self.proto.genesis_timestamp_seconds
        + SWAP_FINALIZATION_DEADLINE_SECONDS;
    if !caller_is_swap && !deadline_passed {
        panic!("Caller must be the swap canister, or the finalization deadline must have passed.");
    }
    if mode != governance::Mode::Normal {
        panic!("Entering {mode:?} mode is not allowed.");
    }
    self.proto.mode = mode as i32;
}
```

Alternatively, allow the NNS root canister or SNS root canister to call `set_mode` as a recovery path, since both are trusted system-level principals.

---

### Proof of Concept

1. An SNS is created and enters `PreInitializationSwap` mode. The Swap canister ID is recorded in `GovernanceProto.swap_canister_id`.
2. The swap reaches `Committed` state and `finalize_inner` begins execution.
3. All steps succeed (ICP swept, Neurons' Fund settled, SNS tokens swept, neurons claimed) but the final `set_sns_governance_to_normal_mode` call fails — e.g., because the governance canister is temporarily out of cycles.
4. `finalize_inner` returns with `error_message = Some("Setting the SNS Governance mode to normal did not complete fully. Halting swap finalization")`.
5. The Swap canister is subsequently deleted (normal lifecycle).
6. SNS Governance remains in `PreInitializationSwap` mode permanently.
7. Any neuron holder calling `Disburse` or `Configure(StartDissolving)` receives `PreconditionFailed: "Because governance is currently in PreInitializationSwap mode, manage_neuron commands of this type are not allowed"`.
8. No on-chain path exists to recover, since `set_mode` panics for any caller that is not the (now-deleted) Swap canister. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** rs/sns/governance/canister/canister.rs (L537-547)
```rust
/// Sets the mode. Only the swap canister is allowed to call this.
///
/// In practice, the only mode that the swap canister would ever choose is
/// Normal. Also, in practice, the current value of mode should be
/// PreInitializationSwap.  whenever the swap canister calls this.
#[update]
fn set_mode(request: SetMode) -> SetModeResponse {
    log!(INFO, "set_mode");
    governance_mut().set_mode(request.mode, caller());
    SetModeResponse {}
}
```

**File:** rs/sns/governance/src/types.rs (L182-211)
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

        if ok {
            return Ok(());
        }

        Err(GovernanceError::new_with_message(
            ErrorType::PreconditionFailed,
            format!(
                "Because governance is currently in PreInitializationSwap mode, \
                 manage_neuron commands of this type are not allowed \
                 (caller_is_swap_canister={caller_is_swap_canister}). command: {command:#?}",
            ),
        ))
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

**File:** rs/sns/swap/src/swap.rs (L1608-1612)
```rust
        }

        finalize_swap_response.set_set_mode_call_result(
            Self::set_sns_governance_to_normal_mode(environment.sns_governance_mut()).await,
        );
```

**File:** rs/sns/swap/src/types.rs (L930-937)
```rust
    pub fn set_set_mode_call_result(&mut self, set_mode_call_result: SetModeCallResult) {
        if !set_mode_call_result.is_successful_set_mode_call() {
            self.set_error_message(
                "Setting the SNS Governance mode to normal did not complete fully. Halting swap finalization".to_string()
            );
        }
        self.set_mode_call_result = Some(set_mode_call_result);
    }
```

**File:** rs/sns/governance/proto/ic_sns_governance/pb/v1/governance.proto (L1591-1609)
```text
  enum Mode {
    // This forces people to explicitly populate the mode field.
    MODE_UNSPECIFIED = 0;

    // All operations are allowed.
    MODE_NORMAL = 1;

    // In this mode, various operations are not allowed in order to ensure the
    // integrity of the initial token swap.
    MODE_PRE_INITIALIZATION_SWAP = 2;
  }

  Mode mode = 19;

  // The canister ID of the swap canister.
  //
  // When this is unpopulated, mode should be Normal, and when this is
  // populated, mode should be PreInitializationSwap.
  ic_base_types.pb.v1.PrincipalId swap_canister_id = 20;
```
