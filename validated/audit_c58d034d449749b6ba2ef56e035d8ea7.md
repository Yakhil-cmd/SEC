### Title
Two-Phase Canister Signature Re-Validation Fails After NNS Root Key Rotation Between Ingress Pool Admission and Block Payload Selection - (`rs/ingress_manager/src/ingress_handler.rs`, `rs/ingress_manager/src/ingress_selector.rs`)

### Summary

The IC ingress pipeline validates canister-signed ingress messages twice: once when admitting them from the unvalidated pool to the validated pool (`validate_ingress_pool_object`), and again when selecting them for a consensus block payload (`validate_ingress`). Each phase resolves the IC root of trust (the NNS subnet's threshold BLS public key) from the registry at a **different registry version**. If the NNS subnet's threshold signing key is updated in the registry between these two phases — which occurs during NNS subnet membership changes that trigger a new DKG — canister-signed messages that passed phase 1 will be silently rejected in phase 2 and never executed.

### Finding Description

**Phase 1 — Pool admission** (`rs/ingress_manager/src/ingress_handler.rs`):

`on_state_change` calls `validate_ingress_pool_object`, which validates the ingress message using `registry_client.get_latest_version()` as the registry version for the root-of-trust lookup:

```rust
let registry_version = self.registry_client.get_latest_version();
// ...
self.request_validator.validate_request(
    ingress_object.signed_ingress.as_ref(),
    consensus_time,
    &self.registry_root_of_trust_provider(registry_version),  // latest version
)
``` [1](#0-0) [2](#0-1) 

**Phase 2 — Payload selection** (`rs/ingress_manager/src/ingress_selector.rs`):

`validate_ingress` re-validates the same message using `context.registry_version` — the consensus registry version pinned to the block being built — which is a **different** registry version:

```rust
self.request_validator.validate_request(
    signed_ingress.as_ref(),
    context.time,
    &self.registry_root_of_trust_provider(context.registry_version),  // consensus version
)
``` [3](#0-2) 

**Root of trust resolution** (`rs/registry/helpers/src/crypto.rs`):

`RegistryRootOfTrustProvider::root_of_trust()` fetches the NNS subnet's threshold BLS public key at the stored `registry_version`. If the NNS subnet's key has been updated in the registry between the two registry versions, the two phases resolve **different** root public keys:

```rust
fn root_of_trust(&self) -> Result<IcRootOfTrust, Self::Error> {
    let root_subnet_id = self.registry_client
        .get_root_subnet_id(self.registry_version)...;
    self.registry_client
        .get_threshold_signing_public_key_for_subnet(root_subnet_id, self.registry_version)
        ...
}
``` [4](#0-3) 

**Canister signature verification** (`rs/validator/src/ingress_validation.rs`) verifies the certificate embedded in the canister signature against the root of trust resolved above. A certificate signed under the old NNS key will fail verification when the root of trust resolves to the new key: [5](#0-4) 

### Impact Explanation

Any ingress message authenticated via a canister signature (e.g., Internet Identity delegations, canister-controlled wallets) that is admitted to the validated ingress pool during registry version V1 will be silently dropped — never included in any block — if the NNS subnet's threshold BLS key is updated to a new value at registry version V2 before the block-building phase resolves `context.registry_version >= V2`. The message expires after `MAX_INGRESS_TTL` (5 minutes) with no execution and no actionable error returned to the user. The user must resubmit with a fresh canister signature issued under the new key. [6](#0-5) 

### Likelihood Explanation

The NNS subnet's threshold signing public key changes whenever the NNS subnet's DKG is refreshed, which occurs on every NNS subnet membership change (node additions or removals via NNS governance proposals). These are routine operational events on the IC mainnet. The window of vulnerability is the `MAX_INGRESS_TTL` window (up to 5 minutes) during which a message sits in the validated pool while a registry update propagates. Any canister-signed message submitted just before such a registry update is at risk. An unprivileged user who submits a canister-signed ingress message during this window is the affected party; no attacker action is required — the failure is triggered by normal NNS governance activity.

### Recommendation

Align the registry version used in both validation phases. The simplest fix is to record the `registry_version` used during pool admission alongside the validated artifact, and reuse that same version during payload selection for the root-of-trust lookup. Alternatively, phase 2 should only re-validate the ingress expiry and signature structure (which are immutable), and skip re-resolving the root of trust for messages already in the validated pool — analogous to the Wormhole report's recommendation to skip re-verification for non-fast-tracked proposals.

### Proof of Concept

1. User submits an ingress message signed with a canister signature (e.g., via Internet Identity) at time T.
2. `on_state_change` runs, calls `validate_ingress_pool_object` with `registry_version = get_latest_version()` = V1. The NNS subnet's BLS key at V1 is K1. The canister signature certificate is valid under K1. Message moves to validated pool.
3. An NNS governance proposal executes, changing the NNS subnet membership. A new DKG runs. The new threshold BLS key K2 is written to the registry at version V2 > V1.
4. Consensus advances. The next block's `ValidationContext` has `registry_version = V2`.
5. `get_ingress_payload` iterates the validated pool, calls `validate_ingress` with `context.registry_version = V2`. `RegistryRootOfTrustProvider` resolves K2. The canister signature certificate (signed under K1) fails BLS verification against K2.
6. The message is excluded from the block payload with `InvalidIngressPayloadReason::IngressValidationError`. It is never executed and expires after 5 minutes. [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ingress_manager/src/ingress_handler.rs (L23-26)
```rust
    fn on_state_change(&self, pool: &T) -> Mutations {
        // Skip on_state_change when ingress_message_setting is not available in registry.
        let registry_version = self.registry_client.get_latest_version();
        let ingress_message_settings = match self.get_ingress_message_settings(registry_version) {
```

**File:** rs/ingress_manager/src/ingress_handler.rs (L197-201)
```rust
        if let Err(err) = self.request_validator.validate_request(
            ingress_object.signed_ingress.as_ref(),
            consensus_time,
            &self.registry_root_of_trust_provider(registry_version),
        ) {
```

**File:** rs/ingress_manager/src/ingress_selector.rs (L597-615)
```rust
        // Do not include the message if it is considered invalid with
        // respect to the given context (expiry & registry_version).
        if let Err(err) = self.request_validator.validate_request(
            signed_ingress.as_ref(),
            context.time,
            &self.registry_root_of_trust_provider(context.registry_version),
        ) {
            let message_id = MessageId::from(&ingress_id);
            return Err(ValidationError::InvalidArtifact(match err {
                RequestValidationError::InvalidRequestExpiry(msg)
                | RequestValidationError::InvalidDelegationExpiry(msg) => {
                    InvalidIngressPayloadReason::IngressExpired(message_id, msg)
                }
                err => InvalidIngressPayloadReason::IngressValidationError(
                    message_id,
                    format!("{err}"),
                ),
            }));
        }
```

**File:** rs/registry/helpers/src/crypto.rs (L203-207)
```rust
    pub struct RegistryRootOfTrustProvider {
        registry_client: Arc<dyn RegistryClient>,
        registry_version: RegistryVersion,
        additional_root_of_trust: Option<IcRootOfTrust>,
    }
```

**File:** rs/registry/helpers/src/crypto.rs (L251-268)
```rust
        fn root_of_trust(&self) -> Result<IcRootOfTrust, Self::Error> {
            let root_subnet_id = self
                .registry_client
                .get_root_subnet_id(self.registry_version)
                .map_err(RegistryRootOfTrustProviderError::RegistryError)?
                .ok_or(RegistryRootOfTrustProviderError::RootSubnetNotFound {
                    registry_version: self.registry_version,
                })?;
            self.registry_client
                .get_threshold_signing_public_key_for_subnet(root_subnet_id, self.registry_version)
                .map_err(RegistryRootOfTrustProviderError::RegistryError)?
                .ok_or(
                    RegistryRootOfTrustProviderError::RootSubnetPublicKeyNotFound {
                        registry_version: self.registry_version,
                    },
                )
                .map(IcRootOfTrust::from)
        }
```

**File:** rs/validator/src/ingress_validation.rs (L819-830)
```rust
        KeyBytesContentType::IcCanisterSignatureAlgPublicKeyDer => {
            let canister_sig = CanisterSigOf::from(CanisterSig(signature.to_vec()));
            verify_canister_sig_with_fallback!(
                validator,
                &canister_sig,
                delegation,
                &pk,
                root_of_trust_provider,
                |e| InvalidCanisterSignature(e.to_string()),
                |e: <R as RootOfTrustProvider>::Error| InvalidCanisterSignature(e.to_string())
            );
        }
```

**File:** rs/limits/src/lib.rs (L9-17)
```rust
/// This constant defines the maximum amount of time an ingress message can wait
/// to start executing after submission before it is expired.  Hence, if an
/// ingress message is submitted at time `t` and it has not been scheduled for
/// execution till time `t+MAX_INGRESS_TTL`, it will be expired.
///
/// At the time of writing, this constant is also used to control how long the
/// status of a completed ingress message (IngressStatus ∈ [Completed, Failed])
/// is maintained by the IC before it is deleted from the ingress history.
pub const MAX_INGRESS_TTL: Duration = Duration::from_secs(5 * 60); // 5 minutes
```
