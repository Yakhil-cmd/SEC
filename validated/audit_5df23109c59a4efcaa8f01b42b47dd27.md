### Title
Unprivileged Canister Can Monopolize the Global Threshold-Signing Queue, Denying Service to All Other Canisters - (File: `rs/execution_environment/src/execution_environment.rs`)

### Summary
The threshold-signing request queue (`sign_with_threshold_contexts`) is a global, per-key-ID bounded queue with no per-caller fairness or eviction mechanism. Any canister that can pay the signing fee can fill the entire queue, permanently preventing all other canisters on the subnet from submitting `sign_with_ecdsa`, `sign_with_schnorr`, or `vetkd_derive_key` requests for that key.

### Finding Description

In `rs/execution_environment/src/execution_environment.rs`, the `sign_with_threshold` function enforces a single global count check before accepting a new signing request: [1](#0-0) 

The count is computed by `sign_with_threshold_contexts_count`, which iterates over **all** contexts for the given key ID across **all** canisters with no per-caller breakdown: [2](#0-1) 

The effective queue capacity is `get_dynamic_signature_queue_size`, which is bounded by `MAX_PAIRED_PRE_SIGNATURES = 100`: [3](#0-2) [4](#0-3) 

The `sign_with_threshold_contexts` map in `SubnetCallContextManager` is a flat `BTreeMap<CallbackId, SignWithThresholdContext>` with no per-canister partitioning: [5](#0-4) 

The `signature_request_timeout_ns` field is `Option<u64>` — explicitly documented as "if none is specified, no request will time out": [6](#0-5) 

When no timeout is configured, the attacker's requests persist in the queue until consensus produces signatures for them. The attacker can immediately resubmit to refill the queue, sustaining the DoS indefinitely.

### Impact Explanation

A malicious canister submits exactly `dynamic_queue_size` signing requests (up to 100 for ECDSA/Schnorr with a full pre-signature stash), paying the required cycles fee. The queue fills. Every subsequent `sign_with_ecdsa`, `sign_with_schnorr`, or `vetkd_derive_key` call from any other canister on the subnet is rejected with:

```
"<method> request failed: request queue for key <key_id> is full."
``` [7](#0-6) 

The attacker receives valid signatures in return (the fee is the only cost), and can immediately resubmit to keep the queue saturated. All other canisters on the subnet are denied threshold-signing service for the targeted key for as long as the attacker continues.

### Likelihood Explanation

- **Entry path**: Any canister on the subnet can call `sign_with_ecdsa`/`sign_with_schnorr`/`vetkd_derive_key` — no privileged role required.
- **Cost**: The attacker pays the signing fee per request and receives valid signatures, making the net cost only the fee overhead.
- **Queue size**: At most 100 slots (with a full pre-signature stash), or the registry-configured `max_queue_size` (default 20 in tests). Both are trivially fillable.
- **Persistence**: Without `signature_request_timeout_ns`, the DoS is sustained indefinitely by continuous refilling.
- **Detectability**: The attacker's canister ID is recorded in each `SignWithThresholdContext.request.sender`, but there is no on-chain enforcement to evict or rate-limit a single sender.

### Recommendation

1. **Per-canister quota**: Track signing-request counts per sender canister and enforce a per-canister cap (e.g., `max_queue_size / N` where N is a configured fairness divisor), preventing any single canister from monopolizing the global queue.
2. **Mandatory timeout**: Make `signature_request_timeout_ns` required (non-optional) so that stale requests are automatically purged, bounding the duration of any DoS.
3. **Eviction of oldest requests**: When the queue is full, consider evicting the oldest unmatched request (with a reject response to its sender) to allow newer requests from other canisters to enter.

### Proof of Concept

1. Deploy a malicious canister `M` on a subnet with key `ecdsa:Secp256k1:key_1` and `max_queue_size = 20`, no `signature_request_timeout_ns`.
2. `M` calls `sign_with_ecdsa` 20 times in rapid succession, each with sufficient cycles. All 20 are accepted and enqueued.
3. A victim canister `V` calls `sign_with_ecdsa` — it receives `"SignWithECDSA request failed: request queue for key ecdsa:Secp256k1:key_1 is full."` immediately.
4. As consensus processes `M`'s requests and returns signatures, `M` immediately resubmits, keeping the queue at capacity.
5. `V` is denied service indefinitely.

The existing test `test_sign_with_threshold_key_queue_fills_up` already demonstrates step 2–3 from a single canister: [7](#0-6)

### Citations

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

**File:** rs/execution_environment/src/execution_environment.rs (L5060-5078)
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

**File:** rs/limits/src/lib.rs (L100-102)
```rust
/// The maximum number of pre-signatures that may be paired with signature requests,
/// per key ID.
pub const MAX_PAIRED_PRE_SIGNATURES: usize = 100;
```

**File:** rs/registry/admin/bin/create_subnet.rs (L144-147)
```rust
    /// The number of nanoseconds that a chain key signature request will time out.
    /// If none is specified, no request will time out.
    #[clap(long)]
    pub signature_request_timeout_ns: Option<u64>,
```

**File:** rs/execution_environment/tests/threshold_signatures.rs (L849-905)
```rust
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
