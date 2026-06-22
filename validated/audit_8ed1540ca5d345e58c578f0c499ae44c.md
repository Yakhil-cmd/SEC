### Title
`read_state` Signed Message Missing Effective Canister ID Binding Allows Cross-Canister Replay - (File: `rs/types/types/src/messages/http.rs`)

---

### Summary

The `read_state` request's representation-independent hash — the content that is actually signed by the user — does not include the effective canister ID from the URL endpoint. Unlike update calls and queries, which both bind their signed content to a specific `canister_id`, a signed `read_state` message is valid for any canister endpoint. An attacker (e.g., a malicious boundary node) who intercepts a signed `read_state` request can replay it verbatim to a different canister's endpoint, and the replica will accept it as authentic.

---

### Finding Description

**Update calls** and **queries** both include `canister_id` in their representation-independent hash: [1](#0-0) 

The map for `call` and `query` explicitly contains `"canister_id" => Bytes(canister_id)`, binding the signature to a specific canister.

**`read_state`** uses a separate hash function that omits `canister_id` entirely: [2](#0-1) 

The signed map contains only `request_type`, `ingress_expiry`, `paths`, `sender`, and optionally `nonce`. The effective canister ID from the URL (`/api/v2/canister/<effective_canister_id>/read_state`) is never committed to in the signature.

The `HttpReadState` struct itself has no `canister_id` field: [3](#0-2) 

The `HttpRequestVerifier<ReadState>` implementation validates the signature and delegation chain but — unlike `SignedIngressContent` and `Query` — never calls `validate_request_target`, because `ReadState` does not implement `HasCanisterId`: [4](#0-3) 

Compare with the update-call path which does call `validate_request_target`: [5](#0-4) 

The only canister-binding check that exists for `read_state` is inside `verify_paths` at the HTTP endpoint layer, and it only applies to specific path patterns (e.g., `canister/<id>/module_hash` checks that the path's canister ID matches the URL's effective canister ID): [6](#0-5) 

For `request_status` paths, the check is on `ingress_user_id == user` and `targets.contains(&receiver)`, not on the effective canister ID in the URL: [7](#0-6) 

---

### Impact Explanation

A signed `read_state` envelope is cryptographically valid for **any** canister endpoint, not just the one the user intended. An attacker who intercepts or obtains a signed `read_state` request can:

1. **Cross-canister replay for `time` paths**: Submit the signed request to any canister endpoint and receive a valid certified response. Harmless in isolation, but demonstrates the missing binding.

2. **Cross-canister replay for `request_status` paths**: Submit the signed request to a different canister's endpoint. If the request ID does not exist on that canister, `ingress_status.user_id()` and `ingress_status.receiver()` both return `None`, so both authorization checks are skipped and the request succeeds (returning an empty/pruned tree). This allows an attacker to probe any canister's state tree using a legitimately signed `read_state` message the user never intended for that canister.

3. **Delegation scope bypass for `request_status`**: If a user holds a delegation scoped to canister A and signs a `read_state` for canister A, the attacker can replay it to canister B. The `validate_request` for `ReadState` does not enforce delegation targets against the effective canister ID (no `validate_request_target` call), so the delegation-target enforcement only applies inside `verify_paths` for paths that actually reference a receiver in the ingress state.

The root cause is structurally identical to the reported Gateway vulnerability: the signed message is missing a field that binds it to the intended target (account address there; effective canister ID here), allowing the same signature to be used across multiple targets.

---

### Likelihood Explanation

The attacker entry path is a **malicious or compromised boundary node**, or any network-level observer who can intercept HTTPS traffic between a user agent and a replica. Boundary nodes are explicitly part of the IC threat model as semi-trusted components. A malicious boundary node can trivially capture a signed `read_state` envelope and replay it to any other canister endpoint on the same or a different subnet. No privileged key material, governance majority, or threshold corruption is required — only the ability to observe and forward HTTP requests.

---

### Recommendation

Include the effective canister ID in the `read_state` representation-independent hash, analogous to how `canister_id` is included for update calls and queries:

```rust
pub(crate) fn representation_independent_hash_read_state(
    ingress_expiry: u64,
    paths: &[Path],
    sender: &[u8],
    nonce: Option<&[u8]>,
+   effective_canister_id: &[u8],   // add this
) -> [u8; 32] {
    use RawHttpRequestVal::*;
    let mut map = btreemap! {
        "request_type" => String("read_state"),
        "ingress_expiry" => U64(ingress_expiry),
        "paths" => Array(...),
        "sender" => Bytes(sender),
+       "canister_id" => Bytes(effective_canister_id),
    };
    ...
}
```

This change would be a **breaking protocol change** requiring a coordinated upgrade, but it closes the cross-canister replay surface and aligns `read_state` with the binding guarantees already present for update calls and queries.

---

### Proof of Concept

1. User agent constructs a `read_state` request for canister A (`/api/v2/canister/A/read_state`) with paths `["request_status", <request_id>]` and signs it.
2. A malicious boundary node intercepts the serialized CBOR envelope (content + `sender_pubkey` + `sender_sig` + optional `sender_delegation`).
3. The boundary node forwards the **identical** envelope bytes to `/api/v2/canister/B/read_state` for a different canister B.
4. The replica at canister B's subnet calls `validate_request` on the `ReadState` content. The signature verification passes because the signed hash (`representation_independent_hash_read_state`) contains no reference to canister A or canister B — only `paths`, `sender`, `ingress_expiry`.
5. `verify_paths` is called. For the `request_status` path: `state.get_ingress_status(&message_id)` returns `IngressStatus::Unknown` (the request was sent to canister A, not B). Both `if let Some(ingress_user_id)` and `if let Some(receiver)` guards evaluate to `None`, so neither FORBIDDEN check fires.
6. The replica returns a valid certified response for canister B's state tree — authenticated with the user's signature that was never intended for canister B. [2](#0-1) [4](#0-3) [7](#0-6)

### Citations

**File:** rs/types/types/src/messages/http.rs (L43-79)
```rust
pub(crate) fn representation_independent_hash_call_or_query(
    request_type: CallOrQuery,
    canister_id: &[u8],
    method_name: &str,
    arg: &[u8],
    ingress_expiry: u64,
    sender: &[u8],
    nonce: Option<&[u8]>,
    sender_info: Option<RawSignedSenderInfoSlices<'_>>,
) -> [u8; 32] {
    use RawHttpRequestVal::*;
    let mut map = btreemap! {
        "request_type" => match request_type {
            CallOrQuery::Call => String("call"),
            CallOrQuery::Query => String("query"),
        },
        "canister_id" => Bytes(canister_id),
        "method_name" => String(method_name),
        "arg" => Bytes(arg),
        "ingress_expiry" => U64(ingress_expiry),
        "sender" => Bytes(sender),
    };
    if let Some(some_nonce) = nonce {
        map.insert("nonce", Bytes(some_nonce));
    }
    if let Some(RawSignedSenderInfoSlices { info, signer, sig }) = sender_info {
        map.insert(
            "sender_info",
            Map(btreemap! {
                "info" => Bytes(info),
                "signer" => Bytes(signer),
                "sig" => Bytes(sig),
            }),
        );
    }
    hash_of_map(&map, |key, value| hash_key_val(key, value))
}
```

**File:** rs/types/types/src/messages/http.rs (L81-107)
```rust
pub(crate) fn representation_independent_hash_read_state(
    ingress_expiry: u64,
    paths: &[Path],
    sender: &[u8],
    nonce: Option<&[u8]>,
) -> [u8; 32] {
    use RawHttpRequestVal::*;
    let mut map = btreemap! {
        "request_type" => String("read_state"),
        "ingress_expiry" => U64(ingress_expiry),
        "paths" => Array(paths
                .iter()
                .map(|p| {
                    Array(
                        p.iter()
                            .map(|b| Bytes(b.as_bytes()))
                            .collect(),
                    )
                })
                .collect()),
        "sender" => Bytes(sender),
    };
    if let Some(some_nonce) = nonce {
        map.insert("nonce", Bytes(some_nonce));
    }
    hash_of_map(&map, |key, value| hash_key_val(key, value))
}
```

**File:** rs/types/types/src/messages/http.rs (L229-238)
```rust
/// A `read_state` request as defined in `<https://internetcomputer.org/docs/current/references/ic-interface-spec#http-read-state>`.
#[derive(Clone, Eq, PartialEq, Debug, Deserialize, Serialize)]
pub struct HttpReadState {
    pub sender: Blob,
    // A list of paths, where a path is itself a sequence of labels.
    pub paths: Vec<Path>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub nonce: Option<Blob>,
    pub ingress_expiry: u64,
}
```

**File:** rs/validator/src/ingress_validation.rs (L106-127)
```rust
impl<R> HttpRequestVerifier<SignedIngressContent, R> for HttpRequestVerifierImpl
where
    R: RootOfTrustProvider,
    R::Error: std::error::Error,
{
    fn validate_request(
        &self,
        request: &HttpRequest<SignedIngressContent>,
        current_time: Time,
        root_of_trust_provider: &R,
    ) -> Result<CanisterIdSet, RequestValidationError> {
        validate_ingress_expiry(request, current_time)?;
        let delegation_targets = validate_request_content(
            request,
            self.validator.as_ref(),
            current_time,
            root_of_trust_provider,
        )?;
        validate_request_target(request, &delegation_targets)?;
        Ok(delegation_targets)
    }
}
```

**File:** rs/validator/src/ingress_validation.rs (L154-176)
```rust
impl<R> HttpRequestVerifier<ReadState, R> for HttpRequestVerifierImpl
where
    R: RootOfTrustProvider,
    R::Error: std::error::Error,
{
    fn validate_request(
        &self,
        request: &HttpRequest<ReadState>,
        current_time: Time,
        root_of_trust_provider: &R,
    ) -> Result<CanisterIdSet, RequestValidationError> {
        validate_paths_width_and_depth(&request.content().paths)?;
        if !request.sender().get().is_anonymous() {
            validate_ingress_expiry(request, current_time)?;
        }
        validate_request_content(
            request,
            self.validator.as_ref(),
            current_time,
            root_of_trust_provider,
        )
    }
}
```

**File:** rs/http_endpoints/public/src/read_state.rs (L437-459)
```rust
            [b"canister", canister_id, b"controllers" | b"module_hash"]
                if target == Target::Canister =>
            {
                let canister_id = parse_principal_id(canister_id)?;
                verify_principal_ids(&canister_id, &effective_principal_id)?;
                metrics.observe_read_state_path(endpoint, "canister_info");
            }
            [b"canister", canister_id, b"metadata", name] if target == Target::Canister => {
                let name = String::from_utf8(Vec::from(*name)).map_err(|err| HttpError {
                    status: StatusCode::BAD_REQUEST,
                    message: format!("Could not parse the custom section name: {err}."),
                })?;
                // Get principal id from byte slice.
                let principal_id = parse_principal_id(canister_id)?;
                // Verify that canister id and effective canister id match.
                verify_principal_ids(&principal_id, &effective_principal_id)?;
                can_read_canister_metadata(
                    user,
                    &CanisterId::unchecked_from_principal(principal_id),
                    &name,
                    state,
                )?;
                metrics.observe_read_state_path(endpoint, "canister_metadata");
```

**File:** rs/http_endpoints/public/src/read_state.rs (L526-578)
```rust
            [b"request_status", request_id]
            | [
                b"request_status",
                request_id,
                b"status" | b"reply" | b"reject_code" | b"reject_message" | b"error_code",
            ] if target == Target::Canister
                || (target == Target::Subnet && version == Version::V3) =>
            {
                let message_id = MessageId::try_from(*request_id).map_err(|_| HttpError {
                    status: StatusCode::BAD_REQUEST,
                    message: format!(
                        "Invalid request id in paths. \
                        Maybe the request ID is not \
                        of {EXPECTED_MESSAGE_ID_LENGTH} bytes in length?!"
                    ),
                })?;

                if let Some(x) = last_request_status_id
                    && x != message_id
                {
                    return Err(HttpError {
                        status: StatusCode::BAD_REQUEST,
                        message: format!(
                            "More than one non-unique request ID exists in \
                                request_status paths: {x} and {message_id}."
                        ),
                    });
                }
                last_request_status_id = Some(message_id.clone());

                // Verify that the request was signed by the same user.
                let ingress_status = state.get_ingress_status(&message_id);
                if let Some(ingress_user_id) = ingress_status.user_id()
                    && ingress_user_id != *user
                {
                    return Err(HttpError {
                        status: StatusCode::FORBIDDEN,
                        message: "The user tries to access Request ID not signed by the caller."
                            .to_string(),
                    });
                }

                if let Some(receiver) = ingress_status.receiver()
                    && !targets.contains(&receiver)
                {
                    return Err(HttpError {
                        status: StatusCode::FORBIDDEN,
                        message: "The user tries to access request IDs for canisters \
                                      not belonging to sender delegation targets."
                            .to_string(),
                    });
                }
                metrics.observe_read_state_path(endpoint, "request_status");
```
