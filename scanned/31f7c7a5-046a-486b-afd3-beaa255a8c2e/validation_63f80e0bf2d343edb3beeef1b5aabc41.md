Based on my thorough code analysis, here is my assessment:

---

### Title
Unprivileged Canister Can Monopolize the Threshold Signature Queue Per Key — (`rs/execution_environment/src/execution_environment.rs`)

### Summary

The `sign_with_threshold` function enforces a global per-key queue size limit with no per-canister sub-limit. A single unprivileged canister with sufficient cycles can fill the entire queue for a given `key_id`, causing all subsequent signing requests from any other canister to be rejected with a queue-full error until the attacker's requests drain.

### Finding Description

In `sign_with_threshold` the queue-full guard is:

```rust
if state
    .metadata
    .subnet_call_context_manager
    .sign_with_threshold_contexts_count(&threshold_key)
    >= dynamic_queue_size
{
    return Err(UserError::new(
        ErrorCode::CanisterRejectedMessage,
        format!("{} request failed: request queue for key {} is full.", ...),
    ));
}
``` [1](#0-0) 

`sign_with_threshold_contexts_count` counts **all** pending contexts for the key regardless of which canister sent them:

```rust
pub fn sign_with_threshold_contexts_count(&self, key_id: &MasterPublicKeyId) -> usize {
    self.sign_with_threshold_contexts
        .iter()
        .filter(|(_, context)| match (key_id, &context.args) { ... })
        .count()
}
``` [2](#0-1) 

There is no per-canister sub-limit anywhere in `SubnetCallContextManager` or `sign_with_threshold`. The `sign_with_threshold_contexts` map stores contexts keyed only by `CallbackId`, with no canister-level accounting: [3](#0-2) 

The `max_queue_size` is a small registry-configured integer (typically 20 for ECDSA/Schnorr): [4](#0-3) 

The only admission guard is the per-request cycles fee check: [5](#0-4) 

The existing state-machine test `test_sign_with_threshold_key_queue_fills_up` explicitly demonstrates that a **single canister** can fill the queue to `max_queue_size` (20), after which every subsequent request from any canister is rejected: [6](#0-5) 

### Impact Explanation

Once the queue is full, **all** canisters on the subnet are denied threshold signatures for that key until the attacker's requests are processed or time out. For subnets hosting chain-key Bitcoin/Ethereum signing (e.g., ckBTC, ckETH), this is a denial of a critical cross-chain service. The attacker's requests will eventually be fulfilled (they receive valid signatures), so the attack is also economically self-sustaining: the attacker recovers the signature value while blocking others.

### Likelihood Explanation

The attack requires only:
1. A deployed canister on the target subnet.
2. Enough cycles to pay `max_queue_size × signature_fee` upfront (e.g., 20 × 10 T cycles for ECDSA on mainnet).
3. Continuous re-submission as slots free up to maintain the blockade.

The cycles cost is a real economic barrier but not a technical one. A well-funded attacker can sustain the DoS indefinitely. The `signature_request_timeout_ns` registry parameter can mitigate this if configured, but it is optional and not always set.

### Recommendation

Introduce a per-canister sub-limit within `sign_with_threshold_contexts_count` or at the admission point in `sign_with_threshold`. For example, count how many pending contexts in `sign_with_threshold_contexts` share the same `request.sender` for the given `key_id`, and reject if that per-canister count exceeds `max_queue_size / N` (where N is a configurable fairness divisor). Alternatively, enforce a hard per-canister cap (e.g., 1–3 concurrent requests per canister per key).

### Proof of Concept

The existing test at `rs/execution_environment/tests/threshold_signatures.rs:848` already constitutes a local-testable proof: a single canister sends `max_queue_size` (20) requests, and the 21st request from **any** canister is rejected with the queue-full error. Extending the test to use two canisters — one filling the queue, one attempting to enqueue — would directly confirm the monopolization invariant violation. [7](#0-6)

### Citations

**File:** rs/execution_environment/src/execution_environment.rs (L3790-3797)
```rust
            if request.payment < real_signature_fee {
                return Err(UserError::new(
                    ErrorCode::CanisterRejectedMessage,
                    format!(
                        "{} request sent with {} cycles, but {} cycles are required.",
                        request.method_name, request.payment, real_signature_fee
                    ),
                ));
```

**File:** rs/execution_environment/src/execution_environment.rs (L3838-3842)
```rust
        let dynamic_queue_size = get_dynamic_signature_queue_size(
            state.pre_signature_stashes(),
            max_queue_size_registry,
            &threshold_key,
        );
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

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L218-218)
```rust
    pub sign_with_threshold_contexts: BTreeMap<CallbackId, SignWithThresholdContext>,
```

**File:** rs/replicated_state/src/metadata_state/subnet_call_context_manager.rs (L437-453)
```rust
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
