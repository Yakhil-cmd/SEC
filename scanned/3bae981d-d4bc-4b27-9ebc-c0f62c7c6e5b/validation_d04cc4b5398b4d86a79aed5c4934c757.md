### Title
Missing Blob Field Validation in Boundary Node Request Preprocessing Middleware - (File: rs/boundary_node/ic_boundary/src/http/middleware/process.rs)

### Summary
The boundary node's `preprocess_request` middleware parses incoming IC API requests and constructs a `RequestContext` without validating the size of Blob-typed fields (`nonce`, `arg`, `paths` elements). An explicit developer TODO acknowledges this gap. As a result, requests carrying oversized Blob fields that violate IC spec limits pass through the boundary node unrejected and are forwarded to replica nodes, which must then perform full CBOR parsing and validation before rejecting them. This is the direct IC analog of the reported pattern: an incoming message listener that dispatches without sanitizing the request object.

### Finding Description
In `rs/boundary_node/ic_boundary/src/http/middleware/process.rs`, the `ICRequestContent` struct is defined with several `Option<Blob>` fields:

```rust
/// TODO: add sanity checks for Blob fields so that
/// we don't process too big forged requests.
/// E.g. the nonce is probably fixed-length etc.
struct ICRequestContent {
    sender: Principal,
    canister_id: Option<Principal>,
    #[serde(default, deserialize_with = "check_method_name_length")]
    method_name: Option<String>,
    nonce: Option<Blob>,
    ingress_expiry: Option<u64>,
    arg: Option<Blob>,
    paths: Option<Vec<Vec<Blob>>>,
}
``` [1](#0-0) 

The `method_name` field has a custom deserializer `check_method_name_length` enforcing a 20,000-character cap. However, `nonce`, `arg`, and `paths` Blob fields have **no individual size or count validation**. The `preprocess_request` function only enforces an overall body size cap (`MAX_REQUEST_BODY_SIZE`) before parsing, then immediately constructs a `RequestContext` and forwards the request downstream: [2](#0-1) 

The IC specification and the replica-side validator (`rs/validator/ingress_message`) enforce:
- **Nonce**: maximum 32 bytes (`NonceTooBigError`)
- **Paths** (read_state): maximum path count and maximum labels per path (`TooManyPathsError`, `PathTooLongError`) [3](#0-2) 

These limits are enforced only at the replica validator level, not at the boundary node: [4](#0-3) 

### Impact Explanation
An unprivileged HTTP sender can craft a CBOR-encoded IC request with a `nonce` field of up to `MAX_REQUEST_BODY_SIZE` bytes (far exceeding the spec-mandated 32-byte maximum), or a `read_state` request with hundreds of deeply nested paths. The boundary node's `preprocess_request` middleware will:
1. Accept the request (overall body size check passes)
2. Parse the full CBOR payload
3. Construct a `RequestContext` with the oversized fields
4. Forward the request to a replica node

The replica must then re-parse the full CBOR, extract the oversized field, and reject it. This forces replicas to perform redundant work for requests that the boundary node should have rejected early. At scale, this amplifies resource consumption on replica nodes for requests that carry no legitimate value, constituting a resource-exhaustion amplification path through the boundary node.

**Impact: 3**

### Likelihood Explanation
The boundary node's `/api/v2/canister/{id}/call`, `/api/v2/canister/{id}/query`, and `/api/v2/canister/{id}/read_state` endpoints are publicly reachable by any unprivileged sender. No authentication, key material, or special role is required to send a crafted CBOR body. The developer TODO comment confirms the gap is known but unaddressed. Any attacker with HTTP access to the boundary node can exploit this.

**Likelihood: 3**

### Recommendation
Add field-level size validation in `preprocess_request` (or in the `ICRequestContent` deserialization) for all Blob fields, consistent with the IC specification limits already enforced by the replica validator:
- `nonce`: reject if `nonce.0.len() > 32`
- `paths`: reject if `paths.len() > MAXIMUM_NUMBER_OF_PATHS` or any path has `path.len() > MAXIMUM_NUMBER_OF_LABELS_PER_PATH`
- `arg`: consider a maximum standalone arg size limit

This mirrors the existing `check_method_name_length` pattern already applied to `method_name`. [5](#0-4) 

### Proof of Concept
An attacker sends a CBOR-encoded `HttpRequestEnvelope` to the boundary node's call endpoint with a nonce field of 1,000,000 bytes (well within `MAX_REQUEST_BODY_SIZE` but 31,250× the spec maximum of 32 bytes):

```python
import cbor2, requests

# Craft envelope with oversized nonce
envelope = {
    "content": {
        "request_type": "call",
        "sender": bytes([4]),           # anonymous
        "canister_id": bytes([0]*10),
        "method_name": "test",
        "arg": b"",
        "ingress_expiry": 9999999999999999999,
        "nonce": b"A" * 100_000,        # 100 KB nonce, spec max is 32 bytes
    }
}
body = cbor2.dumps(envelope)
r = requests.post(
    "https://<boundary-node>/api/v2/canister/aaaaa-aa/call",
    data=body,
    headers={"Content-Type": "application/cbor"}
)
# Boundary node accepts and forwards; replica rejects with NonceTooBigError
# Repeat at high rate to amplify replica-side validation load
```

The boundary node's `preprocess_request` will parse the full CBOR, construct a `RequestContext` with the 100 KB nonce, and forward the request to a replica. The replica's `validate_request` will reject it with `NonceTooBigError`. The boundary node performs no early rejection. [6](#0-5) [7](#0-6)

### Citations

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L28-43)
```rust
/// This is the subset of the request fields.
///
/// TODO: add sanity checks for Blob fields so that
/// we don't process too big forged requests.
/// E.g. the nonce is probably fixed-length etc.
#[derive(Clone, Debug, Deserialize, Serialize)]
struct ICRequestContent {
    sender: Principal,
    canister_id: Option<Principal>,
    #[serde(default, deserialize_with = "check_method_name_length")]
    method_name: Option<String>,
    nonce: Option<Blob>,
    ingress_expiry: Option<u64>,
    arg: Option<Blob>,
    paths: Option<Vec<Vec<Blob>>>,
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L50-67)
```rust
/// Restrict the method name to its max length
pub const MAX_METHOD_NAME_LENGTH: usize = 20_000;

fn check_method_name_length<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let s: Option<String> = Option::<String>::deserialize(deserializer)?;
    if let Some(val) = &s
        && val.len() > MAX_METHOD_NAME_LENGTH
    {
        return Err(D::Error::custom(format!(
            "Method name exceeds maximum allowed length of {MAX_METHOD_NAME_LENGTH}"
        )));
    }

    Ok(s)
}
```

**File:** rs/boundary_node/ic_boundary/src/http/middleware/process.rs (L94-176)
```rust
pub async fn preprocess_request(
    Extension(request_type): Extension<RequestType>,
    request: Request,
    next: Next,
) -> Result<impl IntoResponse, ApiError> {
    // Consume body
    let (mut parts, body) = request.into_parts();

    // Early check for the body size to avoid streaming too big requests
    if body.size_hint().exact() > Some(MAX_REQUEST_BODY_SIZE as u64) {
        return Err(ErrorCause::PayloadTooLarge(MAX_REQUEST_BODY_SIZE).into());
    }

    let body = buffer_body_to_bytes(body, MAX_REQUEST_BODY_SIZE, Duration::from_secs(60)).await?;

    // Parse the request body
    let envelope: ICRequestEnvelope = serde_cbor::from_slice(&body)
        .map_err(|err| ErrorCause::UnableToParseCBOR(err.to_string()))?;
    let content = envelope.content;

    // Check if the request is HTTP and try to parse the arg
    let (arg, http_request) = match (&content.method_name, content.arg) {
        (Some(method), Some(arg)) => {
            if request_type.is_query() && method == METHOD_HTTP {
                let mut req: HttpRequest = Decode!([decoder_config()]; &arg.0, HttpRequest)
                    .map_err(|err| {
                        ErrorCause::UnableToParseHTTPArg(format!(
                            "unable to decode arg as HttpRequest: {err}"
                        ))
                    })?;

                // Remove specific headers
                req.headers
                    .retain(|x| !HEADERS_HIDE_HTTP_REQUEST.contains(&(x.0.as_str())));

                // Drop the arg as it's now redundant
                (None, Some(req))
            } else {
                (Some(arg), None)
            }
        }

        (_, arg) => (arg, None),
    };

    // Check if it's a subnet read state request & it's eligible for caching.
    // If it is - insert the paths into extensions.
    if request_type.is_read_state_subnet()
        && let Some(x) = content.paths
        && should_cache_paths(&x)
    {
        parts.extensions.insert(ReadStatePaths::from(x));
    }

    // Construct the context
    let ctx = RequestContext {
        request_type,
        request_size: body.len() as u32,
        sender: Some(content.sender),
        canister_id: content.canister_id,
        method_name: content.method_name,
        ingress_expiry: content.ingress_expiry,
        arg: arg.map(|x| x.0),
        nonce: content.nonce.map(|x| x.0),
        http_request,
    };

    let ctx = Arc::new(ctx);

    // Reconstruct request back from parts
    let mut request = Request::from_parts(parts, Body::from(body));

    // Inject variables into the request
    request.extensions_mut().insert(ctx.clone());

    // Pass request to the next processor
    let mut response = next.run(request).await;

    // Inject context into the response for access by other middleware
    response.extensions_mut().insert(ctx);

    Ok(response)
}
```

**File:** rs/validator/ingress_message/src/lib.rs (L54-68)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Hash, Debug)]
pub enum RequestValidationError {
    InvalidIngressExpiry(String),
    InvalidDelegationExpiry(String),
    UserIdDoesNotMatchPublicKey(UserId, Vec<u8>),
    InvalidSignature(AuthenticationError),
    InvalidDelegation(AuthenticationError),
    MissingSignature(UserId),
    AnonymousSignatureNotAllowed,
    CanisterNotInDelegationTargets(CanisterId),
    TooManyPathsError { length: usize, maximum: usize },
    PathTooLongError { length: usize, maximum: usize },
    NonceTooBigError { num_bytes: usize, maximum: usize },
    InvalidSenderInfo(String),
}
```

**File:** rs/validator/ingress_message/src/lib.rs (L105-111)
```rust
            RequestValidationError::NonceTooBigError {
                num_bytes: length,
                maximum,
            } => write!(
                f,
                "Nonce in request is too big: got {length} bytes, but at most {maximum} are allowed"
            ),
```

**File:** rs/validator/src/ingress_validation.rs (L178-194)
```rust
fn validate_paths_width_and_depth(paths: &[Path]) -> Result<(), RequestValidationError> {
    if paths.len() > MAXIMUM_NUMBER_OF_PATHS {
        return Err(TooManyPaths {
            maximum: MAXIMUM_NUMBER_OF_PATHS,
            length: paths.len(),
        });
    }
    for path in paths {
        if path.len() > MAXIMUM_NUMBER_OF_LABELS_PER_PATH {
            return Err(PathTooLong {
                maximum: MAXIMUM_NUMBER_OF_LABELS_PER_PATH,
                length: path.len(),
            });
        }
    }
    Ok(())
}
```
