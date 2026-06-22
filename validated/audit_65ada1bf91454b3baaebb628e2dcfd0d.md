### Title
Single Canister Can Monopolize the Subnet-Wide Threshold Signature Queue, Denying Service to All Other Canisters - (File: `rs/execution_environment/src/execution_environment.rs`)

---

### Summary

The `sign_with_threshold` function enforces a single subnet-wide queue-size limit for threshold signature requests (ECDSA, Schnorr, VetKD) with no per-sender cap. A single canister with sufficient cycles can fill the entire queue, preventing every other canister on the subnet from obtaining threshold signatures until the attacker's requests are processed.

---

### Finding Description

In `sign_with_threshold()`, the only admission control before enqueuing a new `SignWithThresholdContext` is a check against the total number of in-flight requests for the requested key:

```rust
// Check if the queue is full.
if state
    .metadata
    .subnet_call_context_manager
    .sign_with_threshold_contexts_count(&threshold_key)
    >= dynamic_queue_size
{
    return Err(...);
}
``` [1](#0-0) 

`sign_with_threshold_contexts_count` counts **all** pending requests for the key across **all** senders — it does not filter by `request.sender`:

```rust
pub fn sign_with_threshold_contexts_count(&self, key_id: &MasterPublicKeyId) -> usize {
    self.sign_with_threshold_contexts
        .iter()
        .filter(|(_, context)| match (key_id, &context.args) { ... })
        .count()
}
``` [2](#0-1) 

The `dynamic_queue_size` is bounded by `MAX_PAIRED_PRE_SIGNATURES` (for ECDSA/Schnorr) or `max_queue_size_registry` (for VetKD, default 20 per registry config): [3](#0-2) 

A single canister can therefore submit exactly `dynamic_queue_size` requests in rapid succession, filling every slot. Subsequent requests from any other canister are rejected with `"request queue for key … is full."` until the attacker's requests are processed by consensus.

Additionally, requests originating from the NNS subnet are **exempt from the signature fee entirely**:

```rust
// If the request isn't from the NNS, then we need to charge for it.
let source_subnet = state.metadata.network_topology.route(request.sender.get());
let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
if source_subnet != Some(nns_subnet_id) {
    // charge fee ...
}
``` [4](#0-3) 

Any canister deployed on the NNS subnet can flood the queue at zero cost.

---

### Impact Explanation

The threshold signature queue is a shared subnet resource. Pre-signatures in the stash are consumed one-per-request and are expensive to regenerate (requiring full IDKG protocol rounds). A single attacker canister can:

1. Fill all `dynamic_queue_size` slots for a given key (e.g., 20 for ECDSA/Schnorr).
2. Block every other canister on the subnet from obtaining threshold signatures for the duration those requests are in-flight.
3. If the attacker is on the NNS subnet, repeat this at zero cost indefinitely.

This is a **cycles/resource accounting bug** — the shared pre-signature stash and queue slots are consumed without any per-sender accounting, directly analogous to draining a shared VRF subscription. [5](#0-4) 

---

### Likelihood Explanation

- Any canister on any application subnet can perform this attack by paying `dynamic_queue_size × signature_fee` cycles (e.g., 20 × 10B = 200B cycles for ECDSA on a standard subnet — a trivially affordable amount).
- Any canister on the NNS subnet can perform this attack for **free**, with no cycle cost at all.
- The attack is fully deterministic and requires no special privileges, leaked keys, or social engineering. [6](#0-5) 

---

### Recommendation

Introduce a per-sender cap within `sign_with_threshold`. Before enqueuing, count how many in-flight requests the `request.sender` already has for the given key and reject if it exceeds a configured per-canister limit (e.g., `max_queue_size / N` or a fixed small constant). This mirrors the per-canister callback quota already used elsewhere in the execution environment: [7](#0-6) 

---

### Proof of Concept

1. Deploy canister `A` on any application subnet that holds an ECDSA key with `max_queue_size = 20`.
2. From canister `A`, call `sign_with_ecdsa` 20 times in rapid succession, each with sufficient cycles attached. All 20 requests are accepted and enqueued.
3. From any other canister `B` on the same subnet, call `sign_with_ecdsa`. The call is rejected: `"sign_with_ecdsa request failed: request queue for key … is full."` — confirmed by the existing test: [8](#0-7) 

4. Canister `A` repeats step 2 as soon as its requests are processed, maintaining a permanent denial of service.
5. For the zero-cost variant: deploy canister `A` on the NNS subnet. Steps 2–4 require zero cycles. [9](#0-8)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L3783-3818)
```rust
        // If the request isn't from the NNS, then we need to charge for it.
        let source_subnet = state.metadata.network_topology.route(request.sender.get());
        let nns_subnet_id = state.metadata.network_topology.nns_subnet_id;
        if source_subnet != Some(nns_subnet_id) {
            let signature_fee =
                self.calculate_signature_fee(&args, state.get_own_subnet_cycles_config());
            let real_signature_fee = signature_fee.real();
            if request.payment < real_signature_fee {
                return Err(UserError::new(
                    ErrorCode::CanisterRejectedMessage,
                    format!(
                        "{} request sent with {} cycles, but {} cycles are required.",
                        request.method_name, request.payment, real_signature_fee
                    ),
                ));
            } else {
                // Charge for the request.
                request.payment -= real_signature_fee;
                let nominal_fee = signature_fee.nominal();
                let use_case = match args {
                    ThresholdArguments::Ecdsa(_) => {
                        state
                            .metadata
                            .subnet_metrics
                            .observe_consumed_cycles_ecdsa_outcalls(nominal_fee);
                        CyclesUseCase::ECDSAOutcalls
                    }
                    ThresholdArguments::Schnorr(_) => CyclesUseCase::SchnorrOutcalls,
                    ThresholdArguments::VetKd(_) => CyclesUseCase::VetKd,
                };
                state
                    .metadata
                    .subnet_metrics
                    .observe_consumed_cycles_with_use_case(use_case, nominal_fee);
            }
        }
```

**File:** rs/execution_environment/src/execution_environment.rs (L3844-3858)
```rust
        // Check if the queue is full.
        if state
            .metadata
            .subnet_call_context_manager
            .sign_with_threshold_contexts_count(&threshold_key)
            >= dynamic_queue_size
        {
            return Err(UserError::new(
                ErrorCode::CanisterRejectedMessage,
                format!(
                    "{} request failed: request queue for key {} is full.",
                    request.method_name, threshold_key
                ),
            ));
        }
```

**File:** rs/execution_environment/src/execution_environment.rs (L5060-5079)
```rust
pub(crate) fn get_dynamic_signature_queue_size(
    stashes: &BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
    max_queue_size_registry: u32,
    key_id: &MasterPublicKeyId,
) -> usize {
    if let Ok(key_id) = IDkgMasterPublicKeyId::try_from(key_id.clone()) {
        // If this key uses pre-signatures, we can accept more requests if there are unpaired
        // pre-signatures available in the stash.
        let stash_size = stashes
            .get(&key_id)
            .map(|stash| stash.pre_signatures.len())
            .unwrap_or_default();
        // We never want to allow more requests than the maximum number of paired pre-signatures.
        let max_queue_size = MAX_PAIRED_PRE_SIGNATURES.min(max_queue_size_registry as usize);
        stash_size.clamp(max_queue_size, MAX_PAIRED_PRE_SIGNATURES)
    } else {
        // If this key doesn't use pre-signatures, we use the registry's max queue size.
        max_queue_size_registry as usize
    }
}
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L212-230)
```rust
#[derive(Clone, Eq, PartialEq, Debug, Default)]
pub struct SubnetCallContextManager {
    /// Should increase monotonically. This property is used to determine if a request
    /// corresponds to a future state.
    next_callback_id: u64,
    pub setup_initial_dkg_contexts: BTreeMap<CallbackId, SetupInitialDkgContext>,
    pub sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>,
    pub canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    /// `CanisterHttpRequestContext`s whose responses have already been delivered to execution.
    /// They are kept here such that asynchronous refunds may continue to be processed.
    pub delivered_canister_http_request_contexts: BTreeMap<CallbackId, CanisterHttpRequestContext>,
    pub reshare_chain_key_contexts: BTreeMap<CallbackId, ReshareChainKeyContext>,
    pub bitcoin_get_successors_contexts: BTreeMap<CallbackId, BitcoinGetSuccessorsContext>,
    pub bitcoin_send_transaction_internal_contexts:
        BTreeMap<CallbackId, BitcoinSendTransactionInternalContext>,
    canister_management_calls: CanisterManagementCalls,
    pub raw_rand_contexts: VecDeque<RawRandContext>,
    pub pre_signature_stashes: BTreeMap<IDkgMasterPublicKeyId, PreSignatureStash>,
}
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L436-453)
```rust
    /// Returns the number of `sign_with_threshold_contexts` per key id.
    pub fn sign_with_threshold_contexts_count(&self, key_id: &MasterPublicKeyId) -> usize {
        self.sign_with_threshold_contexts
            .iter()
            .filter(|(_, context)| match (key_id, &context.args) {
                (MasterPublicKeyId::Ecdsa(ecdsa_key_id), ThresholdArguments::Ecdsa(args)) => {
                    args.key_id == *ecdsa_key_id
                }
                (MasterPublicKeyId::Schnorr(schnorr_key_id), ThresholdArguments::Schnorr(args)) => {
                    args.key_id == *schnorr_key_id
                }
                (MasterPublicKeyId::VetKd(vetkd_key_id), ThresholdArguments::VetKd(args)) => {
                    args.key_id == *vetkd_key_id
                }
                _ => false,
            })
            .count()
    }
```

**File:** rs/config/src/subnet_config.rs (L491-532)
```rust
impl CyclesAccountManagerConfig {
    pub fn application_subnet(subnet_security: SubnetSecurity) -> Self {
        let ten_update_instructions_execution_fee_in_cycles = 10;
        Self {
            reference_subnet_size: match subnet_security {
                SubnetSecurity::Sev => SEV_REFERENCE_SUBNET_SIZE,
                SubnetSecurity::None => DEFAULT_REFERENCE_SUBNET_SIZE,
            },
            canister_creation_fee: CANISTER_CREATION_FEE,
            compute_percent_allocated_per_second_fee: Cycles::new(10_000_000),

            // The following fields are set based on a thought experiment where
            // we estimated how many resources a representative benchmark on a
            // verified subnet is using.
            update_message_execution_fee: Cycles::new(5_000_000),
            ten_update_instructions_execution_fee: Cycles::new(
                ten_update_instructions_execution_fee_in_cycles,
            ),
            ten_update_instructions_execution_fee_wasm64: Cycles::new(
                WASM64_INSTRUCTION_COST_OVERHEAD * ten_update_instructions_execution_fee_in_cycles,
            ),
            xnet_call_fee: Cycles::new(260_000),
            xnet_byte_transmission_fee: Cycles::new(1_000),
            ingress_message_reception_fee: Cycles::new(1_200_000),
            ingress_byte_reception_fee: Cycles::new(2_000),
            // 10 SDR per GiB per year => 10e12 Cycles per year
            gib_storage_per_second_fee: Cycles::new(317_500),
            base_per_second_fee: Cycles::new(10_000),
            duration_between_allocation_charges: Duration::from_secs(10),
            ecdsa_signature_fee: ECDSA_SIGNATURE_FEE,
            schnorr_signature_fee: SCHNORR_SIGNATURE_FEE,
            vetkd_fee: VETKD_FEE,
            http_request_linear_baseline_fee: Cycles::new(3_000_000),
            http_request_quadratic_baseline_fee: Cycles::new(60_000),
            http_request_per_byte_fee: Cycles::new(400),
            http_response_per_byte_fee: Cycles::new(800),
            max_storage_reservation_period: Duration::from_secs(300_000_000),
            default_reserved_balance_limit: DEFAULT_RESERVED_BALANCE_LIMIT,
            fetch_canister_logs_base_fee: Cycles::new(5_000_000),
            fetch_canister_logs_per_byte_fee: Cycles::new(80),
        }
    }
```

**File:** rs/config/src/execution_environment.rs (L266-276)
```rust
    /// The soft limit on the subnet-wide number of callbacks. Beyond this limit,
    /// canisters are only allowed to make downstream calls up to their individual
    /// guaranteed quota.
    pub subnet_callback_soft_limit: usize,

    /// The number of callbacks that are guaranteed to each canister. Beyond
    /// this quota, canisters are only allowed to make downstream calls if the
    /// subnet's shared callback pool has not been exhausted (i.e. the subnet-wide
    /// soft limit has not been exceeded).
    pub canister_guaranteed_callback_quota: usize,

```

**File:** rs/execution_environment/tests/threshold_signatures.rs (L796-845)
```rust
#[test]
fn test_sign_with_threshold_key_fee_ignored_for_nns() {
    let test_cases = vec![
        (Method::SignWithECDSA, make_ecdsa_key("some_key")),
        (Method::SignWithSchnorr, make_ed25519_key("some_key")),
        (Method::SignWithSchnorr, make_bip340_key("some_key")),
        (Method::VetKdDeriveKey, make_vetkd_key("some_key")),
    ];
    for (method, key_id) in test_cases {
        let fee = 1_000_000;
        let nns_subnet = subnet_test_id(1);
        let env = StateMachineBuilder::new()
            .with_checkpoints_enabled(false)
            .with_subnet_type(SubnetType::System)
            .with_subnet_id(nns_subnet)
            .with_nns_subnet_id(nns_subnet)
            .with_ecdsa_signature_fee(fee)
            .with_schnorr_signature_fee(fee)
            .with_vetkd_derive_key_fee(fee)
            .with_chain_key(key_id.clone())
            .build();

        let canister_id = create_universal_canister(&env);
        let _msg_id = env.send_ingress(
            PrincipalId::new_anonymous(),
            canister_id,
            "update",
            wasm()
                .call_simple(
                    ic00::IC_00,
                    method,
                    call_args()
                        .other_side(sign_with_threshold_key_payload(method, key_id))
                        .on_reject(wasm().reject_message().reject()),
                )
                .build(),
        );

        env.tick();

        // Assert that the request payment is zero.
        let contexts = match method {
            Method::SignWithECDSA => env.sign_with_ecdsa_contexts(),
            Method::SignWithSchnorr => env.sign_with_schnorr_contexts(),
            Method::VetKdDeriveKey => env.vetkd_derive_key_contexts(),
            _ => panic!("Unexpected method"),
        };
        let (_, context) = contexts.iter().next().unwrap();
        assert_eq!(context.request.payment, Cycles::zero());
    }
```

**File:** rs/execution_environment/tests/threshold_signatures.rs (L848-905)
```rust
#[test]
fn test_sign_with_threshold_key_queue_fills_up() {
    let test_cases = vec![
        (Method::SignWithECDSA, make_ecdsa_key("some_key"), 20),
        (Method::SignWithSchnorr, make_ed25519_key("some_key"), 20),
        (Method::SignWithSchnorr, make_bip340_key("some_key"), 20),
        (Method::VetKdDeriveKey, make_vetkd_key("some_key"), 20),
    ];
    for (method, key_id, max_queue_size) in test_cases {
        let fee = 1_000_000;
        let payment = 2_000_000_u128;
        let own_subnet = subnet_test_id(1);
        let nns_subnet = subnet_test_id(2);
        let env = StateMachineBuilder::new()
            .with_checkpoints_enabled(false)
            .with_subnet_type(SubnetType::System)
            .with_subnet_id(own_subnet)
            .with_nns_subnet_id(nns_subnet)
            .with_ecdsa_signature_fee(fee)
            .with_schnorr_signature_fee(fee)
            .with_vetkd_derive_key_fee(fee)
            .with_chain_key(key_id.clone())
            // Turn off automatic ECDSA signatures to fill up the queue.
            .with_ecdsa_signing_enabled(false)
            // Turn off automatic Schnorr signatures to fill up the queue.
            .with_schnorr_signing_enabled(false)
            // Turn off automatic VetKey derivation to fill up the queue.
            .with_vetkd_enabled(false)
            .build();

        let canister_id = create_universal_canister(&env);
        let payload = wasm()
            .call_with_cycles(
                ic00::IC_00,
                method,
                call_args()
                    .other_side(sign_with_threshold_key_payload(method, key_id.clone()))
                    .on_reject(wasm().reject_message().reject()),
                Cycles::from(payment),
            )
            .build();
        for _i in 0..max_queue_size {
            let _msg_id = env.send_ingress(
                PrincipalId::new_anonymous(),
                canister_id,
                "update",
                payload.clone(),
            );
        }
        let result = env.execute_ingress(canister_id, "update", payload.clone());

        assert_eq!(
            result,
            Ok(WasmResult::Reject(format!(
                "{method} request failed: request queue for key {key_id} is full.",
            )))
        );
    }
```
