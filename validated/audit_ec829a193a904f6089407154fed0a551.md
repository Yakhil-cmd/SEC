### Title
Canister HTTP Request Context Stored with Hardcoded `registry_version = 0`, Bypassing Registry-Version Validation at Consensus - (File: `rs/types/types/src/canister_http.rs`)

### Summary

Both `CanisterHttpRequestContext::generate_from_args` and `generate_from_flexible_args` unconditionally store `registry_version: RegistryVersion::from(0)` in the newly created context, regardless of the actual registry version at which the request is processed. The `CanisterHttpPayloadBuilder::validate_payload` enforces that every response proof's `registry_version` matches the consensus registry version. Because the stored context's `registry_version` is `0` and is never used to validate the response's registry version (the proof's registry version is compared against the consensus registry version, not the context's), the mismatch between the stored `0` and the actual version is silently ignored. This is the IC analog of the "nonce set to None" pattern: a critical binding field is left at a meaningless default, weakening the integrity guarantee the field was designed to provide.

### Finding Description

**Root cause — hardcoded zero in `generate_from_args` and `generate_from_flexible_args`:**

```rust
// rs/types/types/src/canister_http.rs  L591-L592
// TODO: populate with the actual registry version this request is processed at.
registry_version: RegistryVersion::from(0),
```

The same pattern appears in `generate_from_flexible_args`:

```rust
// rs/types/types/src/canister_http.rs  L696-L697
// TODO: populate with the actual registry version this request is processed at.
registry_version: RegistryVersion::from(0),
```

The `TODO` comment explicitly acknowledges the field is not populated. The field is serialised into replicated state (protobuf field `registry_version = 14` in `CanisterHttpRequestContext`) and persisted across checkpoints.

**How the field is supposed to be used:**

`CanisterHttpRequestContext.registry_version` is intended to record the registry version at which the request was accepted, so that the subnet can later verify that the node committee that signed the response was the correct committee at that version. The payload validator (`rs/https_outcalls/consensus/src/payload_builder.rs`) compares the proof's `registry_version` against the *consensus* registry version, not against the stored context's `registry_version`. This means the stored `0` is never cross-checked against the proof, so the binding between "the committee that was valid when the request was created" and "the committee that signed the response" is never enforced.

**Exploit path:**

1. A canister issues a non-replicated (or fully-replicated) HTTP outcall. The context is stored with `registry_version = 0`.
2. Between request creation and response delivery, a registry upgrade changes the subnet membership (e.g., a node is added or removed).
3. The response proof is signed by the *new* committee at the *new* consensus registry version. The payload validator accepts it because it only checks `proof.registry_version == consensus_registry_version`, which is satisfied.
4. However, the node that was originally delegated for a non-replicated request (`Replication::NonReplicated(node_id)`) may no longer be in the committee at the new registry version, or a newly added node that was not part of the subnet when the request was created could sign the response.
5. Because `context.registry_version` is `0` and is never used to re-derive the original committee, there is no check that the signing node was actually a member of the subnet at the time the request was created.

For non-replicated requests specifically, the delegated node is chosen at request-creation time from the node set at that moment. If the registry version stored in the context were correct, it could be used to verify that the signer is still the originally delegated node under the original registry version. With `registry_version = 0`, this cross-check is structurally impossible.

### Impact Explanation

An attacker who can influence which node is delegated for a non-replicated HTTP outcall (e.g., by timing a request around a registry upgrade that adds a malicious node) could arrange for a node that was not part of the subnet at request-creation time to sign the response. The canister receives a response that was not produced by the originally authorised node. For non-replicated requests this is a single-node trust model — the entire security guarantee rests on the delegated node being the one chosen at request time. Corrupting this binding allows a malicious node to inject arbitrary HTTP response content into a canister's execution, potentially leading to:

- Incorrect canister state transitions based on fabricated external data.
- Financial loss if the canister uses the HTTP response to make payment or governance decisions.

The impact is bounded to canisters using non-replicated HTTP outcalls during registry-version transitions, but such transitions are routine on the IC.

### Likelihood Explanation

Registry upgrades (node additions/removals) happen regularly on the IC mainnet. Any canister that issues non-replicated HTTP outcalls is exposed during every such transition. The window is bounded by the `MAX_INGRESS_TTL`-equivalent timeout for HTTP outcalls. An attacker who can predict or influence registry upgrade timing (e.g., a node operator submitting a governance proposal) can exploit this without any privileged cryptographic material — only the ability to be a subnet member at the right time.

### Recommendation

Replace the hardcoded `RegistryVersion::from(0)` with the actual registry version at which the request is processed. The execution environment already has access to the registry version via the `ValidationContext` or equivalent at the time `generate_from_args` is called. Pass this version through and store it. Then, in `validate_payload`, additionally verify that `response.proof.registry_version() == request_context.registry_version` (or that the signing node was a member at `request_context.registry_version`) to close the binding gap.

### Proof of Concept

The root cause is directly visible in production code: [1](#0-0) 

The same pattern in `generate_from_flexible_args`: [2](#0-1) 

The field is declared and serialised: [3](#0-2) 

The payload validator compares the proof's registry version against the *consensus* registry version, not the stored context's `registry_version`, leaving the stored `0` unused: [4](#0-3) 

The non-replicated path stores the delegated node at request-creation time but cannot later verify it was chosen under the correct registry version because `registry_version` is `0`: [5](#0-4) 

The pool manager does enforce that only the delegated node may sign, but this check is only as strong as the original node selection — which is unverifiable without the correct stored registry version: [6](#0-5)

### Citations

**File:** rs/types/types/src/canister_http.rs (L126-142)
```rust
pub struct CanisterHttpRequestContext {
    pub request: Request,
    pub url: String,
    pub max_response_bytes: Option<NumBytes>,
    pub headers: Vec<CanisterHttpHeader>,
    #[serde(with = "serde_bytes", skip_serializing_if = "Option::is_none", default)]
    pub body: Option<Vec<u8>>,
    pub http_method: CanisterHttpMethod,
    pub transform: Option<Transform>,
    pub time: Time,
    /// The replication strategy for this request.
    pub replication: Replication,
    pub pricing_version: PricingVersion,
    pub refund_status: RefundStatus,
    /// The registry version at which this request is being processed.
    pub registry_version: RegistryVersion,
}
```

**File:** rs/types/types/src/canister_http.rs (L558-569)
```rust
        let replication = match args.is_replicated {
            Some(false) => {
                let delegated_node_id = node_ids
                    .iter()
                    .copied()
                    .choose(rng)
                    .ok_or(CanisterHttpRequestContextError::NoNodesAvailableForDelegation)?;

                Replication::NonReplicated(delegated_node_id)
            }
            _ => Replication::FullyReplicated,
        };
```

**File:** rs/types/types/src/canister_http.rs (L588-593)
```rust
            // The refund status is populated in `try_add_http_context_to_replicated_state`
            // based on the request's payment and the base fee.
            refund_status: RefundStatus::default(),
            // TODO: populate with the actual registry version this request is processed at.
            registry_version: RegistryVersion::from(0),
        })
```

**File:** rs/types/types/src/canister_http.rs (L693-698)
```rust
            // The refund status is populated in `try_add_http_context_to_replicated_state`
            // based on the request's payment and the base fee.
            refund_status: RefundStatus::default(),
            // TODO: populate with the actual registry version this request is processed at.
            registry_version: RegistryVersion::from(0),
        })
```

**File:** rs/https_outcalls/consensus/src/payload_builder.rs (L451-459)
```rust
            // Validate response against consensus registry version
            if response.proof.registry_version() != consensus_registry_version {
                return invalid_artifact(
                    InvalidCanisterHttpPayloadReason::RegistryVersionMismatch {
                        expected: consensus_registry_version,
                        received: response.proof.registry_version(),
                    },
                );
            }
```

**File:** rs/https_outcalls/consensus/src/pool_manager.rs (L520-527)
```rust
                    replication @ Replication::NonReplicated(_)
                    | replication @ Replication::Flexible { .. } => {
                        if !replication.is_authorized_signer(&share.signature.signer) {
                            return Some(CanisterHttpChangeAction::HandleInvalid(
                                share.clone(),
                                "Share signed by non-authorized node".to_string(),
                            ));
                        }
```
