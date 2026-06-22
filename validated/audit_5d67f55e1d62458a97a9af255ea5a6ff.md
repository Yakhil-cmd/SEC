### Title
Single-Step Neuron Controller Assignment Without Recipient Confirmation Enables Permanent ICP Loss - (File: `rs/nns/governance/src/governance.rs`)

### Summary
The NNS Governance canister's `disburse_to_neuron` and `spawn_neuron` functions allow a neuron controller to create new neurons assigned to an arbitrary `new_controller` principal in a single step, with no confirmation from the designated controller. If the specified controller is an uncontrolled, non-existent, or mistyped principal, the ICP stake or maturity transferred to the new neuron is permanently locked with no recovery path.

### Finding Description
`disburse_to_neuron` accepts a caller-supplied `new_controller: PrincipalId` and immediately creates a child neuron assigned to that principal, transferring real ICP stake from the parent neuron's ledger subaccount: [1](#0-0) 

The only check performed is that the field is not `None`. No validation confirms that `new_controller` is reachable, self-authenticating, or controlled by any live principal. The function then builds the child neuron and calls `add_neuron`: [2](#0-1) 

The inline comment at line 2720 states that `add_neuron` will enforce `is_self_authenticating()`, but a June 2024 change explicitly removed that restriction, as documented in the test: [3](#0-2) 

The same pattern exists in `spawn_neuron`, which transfers maturity to a new neuron with an arbitrary `new_controller`: [4](#0-3) 

The `DisburseToNeuron` command is exposed as a standard `manage_neuron` ingress endpoint: [5](#0-4) [6](#0-5) 

### Impact Explanation
Once a child neuron is created with an uncontrolled `new_controller`, the ICP stake locked in that neuron is permanently inaccessible. Only the controller can call `disburse_neuron` to recover the funds. There is no admin override, no governance recovery path, and no timeout mechanism. The parent neuron's stake is irreversibly reduced by the disbursed amount. This constitutes a **ledger conservation bug** — ICP is minted into a neuron subaccount on the ICP ledger but can never be disbursed back.

### Likelihood Explanation
The attack surface is every NNS neuron controller. The scenario is triggered by:
- A typo in the `new_controller` principal (e.g., copy-paste error in a 29-byte principal)
- Social engineering a user into specifying an attacker-controlled or burned principal
- A buggy dapp/wallet that constructs the `DisburseToNeuron` command with a wrong principal

Since the restriction on self-authenticating controllers was removed in June 2024, canister IDs (including deleted or never-deployed canisters) are now valid `new_controller` values, widening the set of uncontrolled addresses that can be accidentally used.

### Recommendation
Implement a two-step controller assignment for `disburse_to_neuron` and `spawn_neuron`:
1. **Step 1**: The parent neuron controller initiates the operation, creating a pending child neuron in an unclaimed state.
2. **Step 2**: The designated `new_controller` must explicitly claim the neuron (e.g., via a `ClaimOrRefresh` call signed by that principal) within a time window, after which the ICP is either transferred or refunded to the parent.

Alternatively, at minimum, validate that `new_controller` is self-authenticating (i.e., a user principal, not an opaque canister ID) before accepting the operation, restoring the pre-June-2024 guard that was removed.

### Proof of Concept
1. Alice controls neuron `N` with 100 ICP staked and `kyc_verified = true`, in `Dissolved` state.
2. Alice (or a buggy wallet on her behalf) calls `manage_neuron` via ingress with:
   ```
   DisburseToNeuron {
     new_controller: Some(<burned_or_nonexistent_principal>),
     amount_e8s: 50_0000_0000,  // 50 ICP
     dissolve_delay_seconds: 0,
     kyc_verified: true,
     nonce: 1234,
   }
   ```
3. `disburse_to_neuron` passes all checks (caller is controller, amount is valid, neuron is dissolved, kyc verified, `new_controller` is `Some`).
4. A child neuron is created with `<burned_or_nonexistent_principal>` as controller; 50 ICP is transferred from neuron `N`'s ledger subaccount to the child neuron's subaccount.
5. No entity controls `<burned_or_nonexistent_principal>`. The 50 ICP is permanently locked. Alice's neuron `N` is permanently reduced by 50 ICP with no recourse. [7](#0-6)

### Citations

**File:** rs/nns/governance/src/governance.rs (L2649-2655)
```rust
        // Validate that if a child neuron controller was provided, it is a valid
        // principal.
        let child_controller = if let Some(child_controller) = &spawn.new_controller {
            *child_controller
        } else {
            parent_neuron.controller()
        };
```

**File:** rs/nns/governance/src/governance.rs (L2704-2721)
```rust
        let child_neuron = NeuronBuilder::new(
            child_nid,
            to_subaccount,
            child_controller,
            DissolveStateAndAge::DissolvingOrDissolved {
                when_dissolved_timestamp_seconds: dissolve_and_spawn_at_timestamp_seconds,
            },
            created_timestamp_seconds,
        )
        .with_spawn_at_timestamp_seconds(dissolve_and_spawn_at_timestamp_seconds)
        .with_hot_keys(parent_neuron.hot_keys.clone())
        .with_followees(parent_neuron.followees.clone())
        .with_kyc_verified(parent_neuron.kyc_verified)
        .with_maturity_e8s_equivalent(maturity_to_spawn)
        .build();

        // `add_neuron` will verify that `child_neuron.controller` `is_self_authenticating()`, so we don't need to check it here.
        self.add_neuron(child_nid.id, child_neuron)?;
```

**File:** rs/nns/governance/src/governance.rs (L2868-2902)
```rust
    pub async fn disburse_to_neuron(
        &mut self,
        id: &NeuronId,
        caller: &PrincipalId,
        disburse_to_neuron: &manage_neuron::DisburseToNeuron,
    ) -> Result<NeuronId, GovernanceError> {
        let neuron_limit_reservation = self.rate_limiter.try_reserve(
            self.env.now_system_time(),
            NEURON_RATE_LIMITER_KEY.to_string(),
            1,
        )?;

        let economics = self
            .heap_data
            .economics
            .as_ref()
            .expect("Governance must have economics.")
            .clone();

        let created_timestamp_seconds = self.env.now();
        let transaction_fee_e8s = self.transaction_fee();

        let parent_neuron = self.with_neuron(id, |neuron| neuron.clone())?;
        let parent_nid = parent_neuron.id();

        if parent_neuron.state(self.env.now()) == NeuronState::Spawning {
            return Err(GovernanceError::new_with_message(
                ErrorType::PreconditionFailed,
                "Can't perform operation on neuron: Neuron is spawning.",
            ));
        }

        if !parent_neuron.is_controlled_by(caller) {
            return Err(GovernanceError::new(ErrorType::NotAuthorized));
        }
```

**File:** rs/nns/governance/src/governance.rs (L2956-2963)
```rust
        // Validate that if a child neuron controller was provided, it is a valid
        // principal.
        let child_controller = disburse_to_neuron.new_controller.ok_or_else(|| {
            GovernanceError::new_with_message(
                ErrorType::InvalidCommand,
                "Must specify a new controller for disburse to neuron.",
            )
        })?;
```

**File:** rs/nns/governance/tests/governance.rs (L6503-6512)
```rust
/// It used to be that controllers must be self-authenticating. Later (Jun, 2024) we got rid of that
/// requirement. That is, the controller can be any type of principal (including canister).
/// Discussed here:
/// https://forum.dfinity.org/t/reevaluating-neuron-control-restrictions/28597
#[tokio::test]
async fn test_neuron_with_non_self_authenticating_controller_is_now_allowed() {
    // Step 1: Prepare the world.

    let controller = PrincipalId::new_user_test_id(42);
    assert!(!controller.is_self_authenticating(), "{controller:?}");
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L895-900)
```text
  message DisburseToNeuron {
    // The controller of the new neuron (must be set).
    ic_base_types.pb.v1.PrincipalId new_controller = 1;
    // The amount to disburse.
    uint64 amount_e8s = 2;
    // The dissolve delay of the new neuron.
```

**File:** rs/nns/governance/proto/ic_nns_governance/pb/v1/governance.proto (L1009-1009)
```text
    DisburseToNeuron disburse_to_neuron = 9;
```
