### Title
Unbounded `read_state` Response Materialization Without Size Cap — (`rs/http_endpoints/public/src/read_state.rs`)

### Summary
The IC replica's `/api/v{2,3}/canister/.../read_state` and `/api/v{2,3}/subnet/.../read_state` HTTP endpoints fully materialize a `MixedHashTree` in memory and then serialize it to CBOR without enforcing any response size limit. An unauthenticated caller can request large, unrestricted subtrees of the state tree (e.g., the entire `subnet` or `api_boundary_nodes` subtree), causing the replica to allocate memory proportional to the returned tree twice — once for the `MixedHashTree` and once for the CBOR-encoded `Certificate`. Under concurrent load this can exhaust the replica process's memory and cause a denial of service.

### Finding Description

**Step 1 — Path count and depth are validated, but response size is not.**

`validate_paths_width_and_depth()` in `rs/validator/src/ingress_validation.rs` enforces:
- At most `MAXIMUM_NUMBER_OF_PATHS = 1,000` paths per request
- At most `MAXIMUM_NUMBER_OF_LABELS_PER_PATH = 127` labels per path [1](#0-0) [2](#0-1) 

No check on the total byte size of the response that those paths would produce is performed anywhere in the validation pipeline.

**Step 2 — Large subtrees are reachable by unauthenticated callers.**

`verify_paths()` in `rs/http_endpoints/public/src/read_state.rs` authorizes individual paths. The paths `[b"subnet"]` (the entire subnet subtree — all subnets, all nodes, all public keys, all canister ranges) and `[b"api_boundary_nodes"]` (all boundary node records) are unconditionally allowed for any caller, including anonymous: [3](#0-2) 

**Step 3 — Full materialization before serialization, no size cap.**

`get_certificate_and_create_response()` in `rs/http_endpoints/public/src/read_state.rs`:
1. Calls `read_certified_state_with_exclusion()`, which materializes the full `MixedHashTree` in memory via `materialize_partial` + `hash_tree.witness::<MixedHashTree>`.
2. Immediately serializes the entire `Certificate` (containing the tree) to CBOR via `into_cbor()` — a second full allocation.
3. Returns the result with **no size check at any point**. [4](#0-3) 

The `CertifiedStateSnapshotImpl::read_certified_state_with_exclusion` implementation confirms the full in-memory materialization: [5](#0-4) 

This is the direct analog of the Monad RPC pattern: the response is fully built in memory before any size gate is applied, and no gate exists.

**Step 4 — Concurrency amplification.**

The `read_state` handler runs inside `tokio::task::spawn_blocking`, meaning each concurrent request occupies a blocking thread and its own heap allocation. The concurrency limiter `max_read_state_concurrent_requests` is the only throttle, but it does not bound per-request memory. [6](#0-5) 

### Impact Explanation

An unauthenticated attacker sends concurrent `read_state` requests with paths such as `["subnet"]` or `["api_boundary_nodes"]`. Each request causes the replica to:
1. Materialize the full `MixedHashTree` for the requested subtree (potentially several MB on a production subnet with dozens of subnets and hundreds of nodes).
2. Serialize it to CBOR — a second allocation of comparable size.

With `N` concurrent requests, the replica allocates `O(N × subtree_size)` bytes. Because there is no response size cap and no per-request memory budget, a sustained flood of such requests can exhaust the replica's heap and trigger OOM termination, denying service to all honest users of that subnet.

### Likelihood Explanation

The `/read_state` endpoint is publicly reachable by any unauthenticated HTTP client. No authentication, cycles payment, or privileged role is required to request `["subnet"]` or `["api_boundary_nodes"]`. The attack requires only standard HTTP tooling and knowledge of the IC API spec. The state tree grows naturally as the subnet adds subnets, nodes, and boundary nodes, increasing the amplification factor over time.

### Recommendation

1. **Enforce a response size cap.** After `read_certified_state_with_exclusion` returns the `MixedHashTree`, measure its serialized size (or an upper-bound estimate) and reject with HTTP 413 if it exceeds a configurable limit (e.g., 2 MB, matching `MAX_CANISTER_HTTP_RESPONSE_BYTES`).
2. **Cap the total label data per request.** In `verify_paths`, accumulate the byte length of all label values across all paths and reject requests whose aggregate label size exceeds a threshold, providing an early pre-materialization gate.
3. **Restrict broad subtree paths.** Consider requiring at least one non-root label for paths like `["subnet"]` and `["api_boundary_nodes"]` so callers cannot request the entire subtree in a single path.

### Proof of Concept

```python
import cbor2, requests, time, threading

# Craft a read_state request for the entire "subnet" subtree (anonymous, no signature required)
envelope = {
    "content": {
        "request_type": "read_state",
        "sender": bytes(29),          # anonymous principal
        "ingress_expiry": int(time.time() * 1e9) + 300_000_000_000,
        "paths": [["subnet"]],        # entire subnet subtree
    }
}
body = cbor2.dumps(envelope)

REPLICA = "https://<boundary-node>/api/v2/canister/<any-canister-id>/read_state"

def flood():
    while True:
        requests.post(REPLICA, data=body,
                      headers={"Content-Type": "application/cbor"})

# Launch concurrent requests to exhaust replica memory
threads = [threading.Thread(target=flood) for _ in range(50)]
for t in threads: t.start()
```

Each request forces the replica to materialize and CBOR-serialize the full subnet subtree (all subnets × all nodes × public keys + canister ranges). With 50 concurrent threads and a multi-MB subtree, the replica's heap grows unboundedly until OOM termination.

### Citations

**File:** rs/validator/src/ingress_validation.rs (L55-61)
```rust
const MAXIMUM_NUMBER_OF_PATHS: usize = 1_000;

/// Maximum number of labels than can be specified in a single path inside a read state request.
/// Requests having a single path with more labels will be declared invalid without any further verification.
/// **Note**: this limit is part of the [IC specification](https://internetcomputer.org/docs/current/references/ic-interface-spec#http-read-state)
/// and so changing this value might be breaking or result in a deviation from the specification.
const MAXIMUM_NUMBER_OF_LABELS_PER_PATH: usize = 127;
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

**File:** rs/http_endpoints/public/src/read_state.rs (L252-305)
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

        let Some(certified_state_reader) = state_reader.get_certified_state_snapshot() else {
            return make_service_unavailable_response();
        };

        // Verify authorization for requested paths.
        if let Err(HttpError { status, message }) = verify_paths(
            &metrics,
            target,
            version,
            certified_state_reader.get_state(),
            &read_state.source,
            &read_state.paths,
            &targets,
            effective_canister_id.into(),
            nns_subnet_id,
        ) {
            return (status, message).into_response();
        }

        let delegation_from_nns = match (version, target) {
            (Version::V2, _) => nns_delegation_reader.get_delegation(CanisterRangesFilter::Flat),
            (Version::V3, Target::Canister) => nns_delegation_reader
                .get_delegation(CanisterRangesFilter::Tree(effective_canister_id)),
            (Version::V3, Target::Subnet) => {
                nns_delegation_reader.get_delegation(CanisterRangesFilter::None)
            }
        };

        let maybe_nns_subnet_filter = match version {
            Version::V2 => DeprecatedCanisterRangesFilter::KeepAll,
            Version::V3 => DeprecatedCanisterRangesFilter::KeepOnlyNNS(nns_subnet_id),
        };

        get_certificate_and_create_response(
            read_state.paths,
            delegation_from_nns,
            certified_state_reader.as_ref(),
            maybe_nns_subnet_filter,
        )
    })
    .await;
```

**File:** rs/http_endpoints/public/src/read_state.rs (L355-404)
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

    let exclusion_rule = match deprecated_canister_ranges_filter {
        DeprecatedCanisterRangesFilter::KeepAll => None,
        DeprecatedCanisterRangesFilter::KeepOnlyNNS(nns_subnet_id) => {
            let deprecated_canister_ranges_except_the_nns_subnet_id_pattern = vec![
                MatchPattern::Inclusive(Label::from("subnet")),
                MatchPattern::Exclusive(Label::from(nns_subnet_id.get_ref())),
                MatchPattern::Inclusive(Label::from("canister_ranges")),
            ];

            Some(deprecated_canister_ranges_except_the_nns_subnet_id_pattern)
        }
    };

    let Some((tree, certification)) = certified_state_reader
        .read_certified_state_with_exclusion(&labeled_tree, exclusion_rule.as_ref())
    else {
        return make_service_unavailable_response();
    };

    let signature = certification.signed.signature.signature.get().0;

    Cbor(HttpReadStateResponse {
        certificate: Blob(into_cbor(&Certificate {
            tree,
            signature: Blob(signature),
            delegation: delegation_from_nns,
        })),
    })
    .into_response()
}
```

**File:** rs/http_endpoints/public/src/read_state.rs (L461-476)
```rust
            [b"api_boundary_nodes"] => {
                metrics.observe_read_state_path(endpoint, "api_boundary_nodes");
            }
            [b"api_boundary_nodes", _node_id]
            | [
                b"api_boundary_nodes",
                _node_id,
                b"domain" | b"ipv4_address" | b"ipv6_address",
            ] => {
                metrics.observe_read_state_path(endpoint, "api_boundary_nodes_info");
            }
            [b"subnet"] => {
                metrics.observe_read_state_path(endpoint, "subnet");
            }
            [b"subnet", _subnet_id] => {
                metrics.observe_read_state_path(endpoint, "subnet_info");
```

**File:** rs/state_manager/src/lib.rs (L3876-3900)
```rust
    fn read_certified_state_with_exclusion(
        &self,
        paths: &LabeledTree<()>,
        exclusion: Option<&MatchPatternPath>,
    ) -> Option<(MixedHashTree, Certification)> {
        let _timer = self.read_certified_state_duration_histogram.start_timer();

        let mixed_hash_tree = {
            let lazy_tree = replicated_state_as_lazy_tree(self.get_state(), self.get_height());
            let partial_tree = materialize_partial(&lazy_tree, paths, exclusion.map(|v| &v[..]));
            self.hash_tree.witness::<MixedHashTree>(&partial_tree)
        }
        .ok()?;

        debug_assert_eq!(
            crypto_hash_of_partial_state(&mixed_hash_tree.digest()),
            self.certification.signed.content.hash,
            "produced invalid hash tree {:?} for paths {:?}, full hash tree: {:?}",
            mixed_hash_tree,
            paths,
            self.hash_tree
        );

        Some((mixed_hash_tree, self.certification.clone()))
    }
```
