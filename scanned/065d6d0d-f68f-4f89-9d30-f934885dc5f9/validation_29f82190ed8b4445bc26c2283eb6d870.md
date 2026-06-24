### Title
Verbose Internal Error Disclosure via `ApiError::Unspecified` in IC Boundary Node HTTP API - (File: rs/boundary_node/ic_boundary/src/errors.rs)

### Summary
The IC boundary node's `ApiError::Unspecified` variant directly serializes raw `anyhow::Error` chains into HTTP response bodies returned to unauthenticated callers. Any unrecognized tower middleware `BoxError` is wrapped into this variant and its full string representation is sent to the client, bypassing the sanitized `ErrorClientFacing` layer that all other error paths use.

### Finding Description
The boundary node defines a two-tier error system. Internal errors are represented by `ErrorCause`, which is converted to a sanitized `ErrorClientFacing` before being sent to clients. However, `ApiError::Unspecified` is a third variant that completely bypasses this sanitization:

```rust
// rs/boundary_node/ic_boundary/src/errors.rs, lines 210-219
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::_Custom(c, b) => (c, b).into_response(),
            ApiError::ProxyError(c) => c.into_response(),   // goes through ErrorClientFacing
            ApiError::Unspecified(err) => {
                (StatusCode::INTERNAL_SERVER_ERROR, err.to_string()).into_response()
                // ^^^ raw anyhow error chain sent directly to client
            }
        }
    }
}
``` [1](#0-0) 

This variant is populated by the `From<BoxError>` implementation for any tower middleware error that is not a `GovernorError`:

```rust
// lines 228-232
impl From<BoxError> for ApiError {
    fn from(item: BoxError) -> Self {
        if !item.is::<GovernorError>() {
            return ApiError::Unspecified(anyhow!(item.to_string()));
        }
``` [2](#0-1) 

By contrast, all `ErrorCause` variants — including those that carry internal strings like `ReplicaErrorDNS(String)`, `ReplicaTLSErrorOther(String)`, and `ReplicaErrorOther(String)` — are mapped through `to_client_facing_error()` to a sanitized `ErrorClientFacing::ReplicaError` that returns only a generic message: [3](#0-2) 

The `ErrorClientFacing::Other` variant similarly returns only `"Internal Server Error"` as its detail string: [4](#0-3) 

A secondary instance exists in the public replica HTTP endpoint:

```rust
// rs/http_endpoints/public/src/common.rs, lines 87-92
} else {
    make_plaintext_response(
        StatusCode::INTERNAL_SERVER_ERROR,
        format!("Unexpected error: {err}"),
    )
}
``` [5](#0-4) 

### Impact Explanation
`anyhow::Error` chains can include: internal Rust type names, file paths embedded by macros, chained error messages from lower-level crates (TLS libraries, HTTP parsers, routing internals), and internal component identifiers. An attacker who triggers an unrecognized middleware error receives this full chain verbatim in the HTTP response body, enabling:

- **Internal architecture mapping**: component names, crate names, and error types reveal the internal software stack.
- **Attack surface expansion**: knowing which middleware layers exist and how they fail guides targeted payload crafting against the boundary node.
- **Compound amplification**: combined with other probing techniques, the feedback loop accelerates discovery of exploitable conditions.

### Likelihood Explanation
The boundary node API endpoints (`/api/v2/canister/{id}/query`, `/api/v2/canister/{id}/call`, `/api/v2/canister/{id}/read_state`, etc.) are publicly reachable by any unauthenticated caller. Any request that causes an unrecognized tower middleware error — for example, through malformed headers, unusual content-type combinations, or edge cases in middleware ordering — will trigger the `From<BoxError>` path and produce a verbose response. No authentication or special privilege is required.

### Recommendation
Replace the `ApiError::Unspecified` arm in `IntoResponse` with a generic response, logging the full error server-side only:

```rust
ApiError::Unspecified(err) => {
    // Log internally with a correlation ID
    error!("Internal error: {:?}", err);
    (StatusCode::INTERNAL_SERVER_ERROR, "Internal server error.").into_response()
}
```

Apply the same fix to `map_box_error_to_response` in `rs/http_endpoints/public/src/common.rs`, replacing `format!("Unexpected error: {err}")` with a generic message and server-side logging.

### Proof of Concept
Send any request to a boundary node API endpoint that triggers an unrecognized tower `BoxError` (e.g., a request that exercises a middleware not covered by the `GovernorError` match arm). The HTTP response body will contain the full `anyhow` error chain string from `err.to_string()` at line 216, exposing internal component names and error details directly to the unauthenticated caller. [6](#0-5)

### Citations

**File:** rs/boundary_node/ic_boundary/src/errors.rs (L75-98)
```rust
    pub fn to_client_facing_error(&self) -> ErrorClientFacing {
        match self {
            Self::Other(_) => ErrorClientFacing::Other,
            Self::BodyTimedOut => ErrorClientFacing::BodyTimedOut,
            Self::UnableToReadBody(_) => ErrorClientFacing::Other,
            Self::PayloadTooLarge(x) => ErrorClientFacing::PayloadTooLarge(*x),
            Self::UnableToParseCBOR(x) => ErrorClientFacing::UnableToParseCBOR(x.clone()),
            Self::UnableToParseHTTPArg(x) => ErrorClientFacing::UnableToParseHTTPArg(x.clone()),
            Self::LoadShed => ErrorClientFacing::LoadShed,
            Self::MalformedRequest(x) => ErrorClientFacing::MalformedRequest(x.clone()),
            Self::NoRoutingTable => ErrorClientFacing::ServiceUnavailable,
            Self::SubnetNotFound => ErrorClientFacing::SubnetNotFound,
            Self::CanisterNotFound => ErrorClientFacing::CanisterNotFound,
            Self::NoHealthyNodes => ErrorClientFacing::NoHealthyNodes,
            Self::ReplicaErrorDNS(_) => ErrorClientFacing::ReplicaError,
            Self::ReplicaErrorConnect => ErrorClientFacing::ReplicaError,
            Self::ReplicaTimeout => ErrorClientFacing::ReplicaError,
            Self::ReplicaTLSErrorOther(_) => ErrorClientFacing::ReplicaError,
            Self::ReplicaTLSErrorCert(_) => ErrorClientFacing::ReplicaError,
            Self::ReplicaErrorOther(_) => ErrorClientFacing::ReplicaError,
            Self::Forbidden => ErrorClientFacing::Forbidden,
            Self::RateLimited(_) => ErrorClientFacing::RateLimited,
        }
    }
```

**File:** rs/boundary_node/ic_boundary/src/errors.rs (L166-183)
```rust
    pub fn details(&self) -> String {
        match self {
            Self::BodyTimedOut => "Reading the request body timed out due to data arriving too slowly.".to_string(),
            Self::CanisterNotFound => "The specified canister does not exist.".to_string(),
            Self::LoadShed => "Temporarily unable to handle the request due to high load. Please try again later.".to_string(),
            Self::MalformedRequest(x) => x.clone(),
            Self::NoHealthyNodes => "There are currently no healthy replica nodes available to handle the request. This may be due to an ongoing upgrade of the replica software in the subnet. Please try again later.".to_string(),
            Self::Other => "Internal Server Error".to_string(),
            Self::PayloadTooLarge(x) => format!("Payload is too large: maximum body size is {x} bytes."),
            Self::Forbidden => "Request is forbidden according to currently active policy, it might work later.".to_string(),
            Self::RateLimited => "Rate limit exceeded. Please slow down requests and try again later.".to_string(),
            Self::ReplicaError => "An unexpected error occurred while communicating with the upstream replica node. Please try again later.".to_string(),
            Self::ServiceUnavailable => "The API boundary node is temporarily unable to process the request. Please try again later.".to_string(),
            Self::SubnetNotFound => "The specified subnet cannot be found.".to_string(),
            Self::UnableToParseCBOR(x) => format!("Failed to parse the CBOR request body: {x}"),
            Self::UnableToParseHTTPArg(x) => format!("Unable to decode the arguments of the request to the http_request method: {x}"),
        }
    }
```

**File:** rs/boundary_node/ic_boundary/src/errors.rs (L210-219)
```rust
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        match self {
            ApiError::_Custom(c, b) => (c, b).into_response(),
            ApiError::ProxyError(c) => c.into_response(),
            ApiError::Unspecified(err) => {
                (StatusCode::INTERNAL_SERVER_ERROR, err.to_string()).into_response()
            }
        }
    }
```

**File:** rs/boundary_node/ic_boundary/src/errors.rs (L228-232)
```rust
impl From<BoxError> for ApiError {
    fn from(item: BoxError) -> Self {
        if !item.is::<GovernorError>() {
            return ApiError::Unspecified(anyhow!(item.to_string()));
        }
```

**File:** rs/http_endpoints/public/src/common.rs (L87-92)
```rust
    } else {
        make_plaintext_response(
            StatusCode::INTERNAL_SERVER_ERROR,
            format!("Unexpected error: {err}"),
        )
    }
```
