### Title
`CreateCanisterAndInstallCode` NNS Governance Proposal Always Creates Zero-Cycles (Immediately Frozen) Canisters — (`File: rs/nns/handlers/root/impl/src/canister_management.rs`)

---

### Summary

The NNS Root canister's `create_canister_and_install_code` function calls the subnet's `create_canister` management method without attaching any cycles. Because the `CreateCanisterAndInstallCodeRequest` struct also has no `cycles` field, there is no mechanism to fund the newly created canister. Every adopted `CreateCanisterAndInstallCode` governance proposal results in a canister created with 0 cycles, which is immediately frozen and permanently non-functional, while the proposal is recorded as successfully executed.

---

### Finding Description

In `rs/nns/handlers/root/impl/src/canister_management.rs`, the `create_canister_and_install_code` function performs two steps: (1) call `create_canister` on the target subnet, then (2) call `install_code`. The comment on line 287 even reads *"attach cycles"*, but the actual call at line 292 uses only `.with_arg(...)` — no `.with_cycles(...)` is ever chained:

```rust
// Step 1.1: Send create_canister call (to the subnet, not "aaaaa-aa", and attach cycles).
let create_canister_result = Call::bounded_wait(callee, "create_canister")
    .with_arg(create_canister_args)
    .await;                          // ← no .with_cycles(...)
``` [1](#0-0) 

On the IC, cycles attached to a `create_canister` call become the initial balance of the new canister. With 0 cycles attached, the new canister is created with a 0-cycle balance. The IC's freezing mechanism immediately freezes any canister whose balance falls below the freezing threshold (which covers storage costs for the default 30-day window). A frozen canister cannot process ingress messages, respond to inter-canister calls, or execute heartbeats.

The upstream `CreateCanisterAndInstallCodeRequest` struct has no `cycles` field at all, so the proposal submitter has no way to specify an initial funding amount:

```rust
pub struct CreateCanisterAndInstallCodeRequest {
    pub host_subnet_id: PrincipalId,
    pub canister_settings: Option<CanisterSettings>,
    pub wasm_module: Vec<u8>,
    pub install_arg: Vec<u8>,
    // ← no cycles field
}
``` [2](#0-1) 

The NNS Governance canister's `perform_call_canister` dispatch for `CreateCanisterAndInstallCode` simply forwards to Root with no cycles context: [3](#0-2) 

The governance-level proposal struct also carries no cycles field: [4](#0-3) 

Contrast this with every other correct `create_canister` call in the codebase, which explicitly attaches cycles: [5](#0-4) 

---

### Impact Explanation

Every `CreateCanisterAndInstallCode` NNS governance proposal that is adopted and executed will:

1. Successfully create a canister on the target subnet with **0 cycles**.
2. Successfully install the specified WASM (the Root canister, as controller, can `install_code` even on a frozen canister).
3. Record the proposal as **executed** (`executed_timestamp_seconds > 0`, `failure_reason = None`).
4. Leave the created canister **permanently frozen** — it cannot process any messages, run timers, or make outgoing calls.

The governance system provides no retry or re-execution path for a proposal already marked executed. The frozen canister can only be rescued by a separate top-up action (e.g., via the CMC), but there is no automated or governance-native mechanism to do so. Any NNS-governed canister deployment via this proposal type is silently broken.

---

### Likelihood Explanation

The `CreateCanisterAndInstallCode` proposal type is a live, production NNS action type exposed in the governance DID and tested in integration tests. Any NNS neuron holder with sufficient voting power can submit such a proposal. The bug is triggered unconditionally on every execution — there is no configuration or parameter that avoids it. The integration test at `rs/nervous_system/integration_tests/tests/create_canister_and_install_code.rs` does not assert the cycles balance of the created canister, so the defect is not caught by existing test coverage. [6](#0-5) 

---

### Recommendation

1. Add a `cycles_for_new_canister: u64` field to `CreateCanisterAndInstallCodeRequest` in `rs/nns/handlers/root/interface/src/lib.rs` and to the corresponding governance protobuf/API types.
2. In `create_canister_and_install_code` (`rs/nns/handlers/root/impl/src/canister_management.rs`), chain `.with_cycles(request.cycles_for_new_canister as u128)` onto the `create_canister` call.
3. Add a minimum cycles validation in `CreateCanisterAndInstallCode::validate` (e.g., reject proposals specifying 0 cycles).
4. Update the integration test to assert the created canister has a non-zero, non-frozen cycle balance after execution.

---

### Proof of Concept

**Trigger path (no privileged access required beyond normal NNS neuron ownership):**

1. Any NNS neuron holder submits a `CreateCanisterAndInstallCode` proposal targeting any system subnet, with a valid WASM module.
2. The proposal is adopted through normal NNS voting.
3. NNS Governance calls Root's `create_canister_and_install_code`.
4. Root calls `create_canister` on the target subnet with **0 cycles attached** (line 292 of `canister_management.rs`).
5. The subnet creates the canister with 0-cycle balance.
6. Root calls `install_code` — this succeeds because Root is the controller.
7. The proposal is marked `executed`.
8. The new canister is frozen: `canister_status` returns `Frozen`; any ingress call to it returns `CanisterOutOfCycles`.

The comment on line 287 (`"attach cycles"`) confirms the intent was to attach cycles, making this an implementation omission rather than a design decision. [7](#0-6)

### Citations

**File:** rs/nns/handlers/root/impl/src/canister_management.rs (L284-294)
```rust
    let main = async {
        // Step 1: Create canister.

        // Step 1.1: Send create_canister call (to the subnet, not "aaaaa-aa", and attach cycles).
        let create_canister_args = CreateCanisterArgs {
            settings: canister_settings.map(management_canister::CanisterSettingsArgs::from),
            sender_canister_version: Some(ic_cdk::api::canister_version()),
        };
        let create_canister_result = Call::bounded_wait(callee, "create_canister")
            .with_arg(create_canister_args)
            .await;
```

**File:** rs/nns/handlers/root/interface/src/lib.rs (L100-114)
```rust
#[derive(Clone, PartialEq, Debug, CandidType, Deserialize)]
pub struct CreateCanisterAndInstallCodeRequest {
    /// The subnet where the canister will be created.
    pub host_subnet_id: PrincipalId,

    /// Settings for the new canister. If controllers is not specified, Root
    /// will be the sole controller.
    pub canister_settings: Option<CanisterSettings>,

    /// The WASM module to install.
    pub wasm_module: Vec<u8>,

    /// The argument to pass to the canister's install handler.
    pub install_arg: Vec<u8>,
}
```

**File:** rs/nns/governance/src/governance.rs (L4281-4284)
```rust
            ValidProposalAction::CreateCanisterAndInstallCode(create_canister_and_install_code) => {
                self.perform_call_canister(pid, create_canister_and_install_code)
                    .await;
            }
```

**File:** rs/nns/governance/src/proposals/create_canister_and_install_code.rs (L217-234)
```rust
impl CallCanister for CreateCanisterAndInstallCode {
    type Reply = root::CreateCanisterAndInstallCodeOk;

    fn canister_and_function(&self) -> Result<(CanisterId, &str), GovernanceError> {
        Ok((ROOT_CANISTER_ID, "create_canister_and_install_code"))
    }

    fn payload(&self) -> Result<Vec<u8>, GovernanceError> {
        let request = root::CreateCanisterAndInstallCodeRequest::try_from(self.clone())?;

        Encode!(&request).map_err(|e| {
            GovernanceError::new_with_message(
                ErrorType::InvalidProposal,
                format!("Failed to encode CreateCanisterAndInstallCode: {}", e),
            )
        })
    }
}
```

**File:** rs/ethereum/ledger-suite-orchestrator/src/management/mod.rs (L242-244)
```rust
        let result = Call::unbounded_wait(Principal::management_canister(), "create_canister")
            .with_arg(&create_args)
            .with_cycles(payment)
```

**File:** rs/nervous_system/integration_tests/tests/create_canister_and_install_code.rs (L72-94)
```rust
    // Step 3: Verify results.

    // Step 3.1: Inspect timestamp fields.
    assert_eq!(
        proposal_info.failure_reason, None,
        "Proposal failed: {:?}",
        proposal_info.failure_reason
    );
    assert!(
        proposal_info.executed_timestamp_seconds > 0,
        "Proposal was not executed"
    );

    // Step 3.2: Get the canister ID from proposal's success_value.
    let canister_id: PrincipalId = match proposal_info.success_value {
        Some(SuccessfulProposalExecutionValue::CreateCanisterAndInstallCode(ok)) => {
            ok.canister_id.unwrap()
        }
        wrong => panic!(
            "Expected CreateCanisterAndInstallCode success_value, got: {:?}",
            wrong
        ),
    };
```
