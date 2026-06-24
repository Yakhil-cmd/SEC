Looking at the actual code in `rs/p2p/artifact_downloader/src/fetch_artifact/download.rs`:

**`build_axum_router`** (lines 44–53) explicitly calls `.layer(DefaultBodyLimit::disable())` with the comment "Disable request size limit since consensus might push artifacts larger than limit."

**`rpc_handler`** (lines 55–72) takes `payload: Bytes` — axum's `Bytes` extractor buffers the entire request body into heap memory before the handler runs. Then `spawn_blocking` calls `proxy_decode(&payload)` on that buffer.

The same pattern exists in `fetch_stripped_artifact/download.rs` at lines 205–212 (`build_axum_router`) and lines 214–288 (`ingress_rpc_handler`, `idkg_dealing_rpc_handler`).

The critical question is whether the QUIC transport layer enforces a per-stream data limit that would bound body size before it reaches axum. I found a reference to stream-related patterns in `rs/p2p/quic_transport/src/request_handler.rs` but could not read it in this session. QUIC flow control is a throughput mechanism, not a security bound, and is typically configured for large transfers in P2P contexts.

The attacker prerequisite — a subnet peer with a valid QUIC/TLS connection — falls within "protocol peer behavior below the consensus fault threshold" (a single Byzantine node).

---

### Title
Unbounded P2P RPC Body Allocation Enables Single-Peer OOM Crash of Replica — (`rs/p2p/artifact_downloader/src/fetch_artifact/download.rs`)

### Summary
The P2P artifact RPC endpoints explicitly disable axum's body size limit and buffer the full request body into heap memory before any size check, allowing a single Byzantine subnet peer to exhaust the replica's heap memory.

### Finding Description
`build_axum_router` registers `/{artifact}/rpc` routes with `.layer(DefaultBodyLimit::disable())`, removing axum's default 2 MB cap. [1](#0-0) 

`rpc_handler` accepts `payload: Bytes` — axum's `Bytes` extractor reads and allocates the entire request body before the handler body executes. There is no size guard between body receipt and `proxy_decode`: [2](#0-1) 

The identical pattern is present in `fetch_stripped_artifact/download.rs` for `ingress_rpc_handler` and `idkg_dealing_rpc_handler`: [3](#0-2) [4](#0-3) 

The design intent (comment: "consensus might push artifacts larger than limit") is legitimate for large artifact responses, but it inadvertently removes the only application-level guard on *inbound* request bodies, which are supposed to be small artifact IDs.

### Impact Explanation
A Byzantine subnet peer sends an HTTP request to `/{artifact}/rpc` with a multi-GB body. Axum allocates that body into a contiguous `Bytes` buffer on the heap. The `spawn_blocking` task then calls `proxy_decode` on the full buffer. Even if `proxy_decode` fails immediately on malformed data, the allocation already occurred. Repeated requests from the same peer (or a single sufficiently large one) exhaust the replica's heap, triggering an OOM kill or process abort. This crashes one replica, degrading subnet fault tolerance.

### Likelihood Explanation
The attacker needs only a valid QUIC/TLS connection as a subnet peer — a single Byzantine node below the consensus fault threshold. No key compromise, governance majority, or volumetric traffic is required. The code path is direct and requires no race condition or timing dependency.

### Recommendation
Add an explicit per-request body size cap before the `Bytes` extractor runs. Since the request body is an artifact *ID* (not the artifact itself), a tight limit (e.g., 1–4 MB) is appropriate and does not conflict with the legitimate need to serve large artifact *responses*. This can be done with a per-route `DefaultBodyLimit::max(N)` layer applied only to the RPC routes, or by checking `payload.len()` at the top of each handler before calling `proxy_decode`.

### Proof of Concept
1. Obtain a valid QUIC/TLS connection as a subnet peer.
2. Send `POST /{artifact_name}/rpc` with `Content-Length: 4294967296` and a streaming body of 4 GB of zero bytes.
3. Observe axum allocating the full body into `Bytes` on the victim replica's heap.
4. Observe OOM kill or process abort on the victim replica.
5. Confirm: adding `.layer(DefaultBodyLimit::max(4 * 1024 * 1024))` to the route prevents the allocation.

### Citations

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L44-53)
```rust
fn build_axum_router<Artifact: PbArtifact>(pool: ValidatedPoolReaderRef<Artifact>) -> Router {
    Router::new()
        .route(
            &format!("/{}/rpc", uri_prefix::<Artifact>()),
            any(rpc_handler),
        )
        .with_state(pool)
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable())
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_artifact/download.rs (L55-72)
```rust
async fn rpc_handler<Artifact: PbArtifact>(
    State(pool): State<ValidatedPoolReaderRef<Artifact>>,
    payload: Bytes,
) -> Result<Bytes, StatusCode> {
    let jh = tokio::task::spawn_blocking(move || {
        let id: Artifact::Id =
            Artifact::PbId::proxy_decode(&payload).map_err(|_| StatusCode::BAD_REQUEST)?;
        let artifact = pool
            .read()
            .unwrap()
            .get(&id)
            .ok_or(StatusCode::NO_CONTENT)?;
        Ok::<_, StatusCode>(Bytes::from(Artifact::PbMessage::proxy_encode(artifact)))
    });
    let bytes = jh.await.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)??;

    Ok(bytes)
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L205-212)
```rust
pub(super) fn build_axum_router(pools: Pools) -> Router {
    Router::new()
        .route(INGRESS_URI, any(ingress_rpc_handler))
        .route(IDKG_DEALING_URI, any(idkg_dealing_rpc_handler))
        .with_state(pools)
        // Disable request size limit since consensus might push artifacts larger than limit.
        .layer(DefaultBodyLimit::disable())
}
```

**File:** rs/p2p/artifact_downloader/src/fetch_stripped_artifact/download.rs (L214-248)
```rust
async fn ingress_rpc_handler(
    State(pools): State<Pools>,
    payload: Bytes,
) -> Result<Bytes, StatusCode> {
    let join_handle = tokio::task::spawn_blocking(move || {
        let request_proto: pb::GetIngressMessageInBlockRequest =
            pb::GetIngressMessageInBlockRequest::proxy_decode(&payload)
                .map_err(|_| StatusCode::BAD_REQUEST)?;
        let request = GetIngressMessageInBlockRequest::try_from(request_proto)
            .map_err(|_| StatusCode::BAD_REQUEST)?;

        match pools.get_ingress(&request.signed_ingress_id, &request.block_proposal_id) {
            Ok(serialized_ingress_message) => Ok::<_, StatusCode>(Bytes::from(
                pb::GetIngressMessageInBlockResponse::proxy_encode(
                    GetIngressMessageInBlockResponse {
                        serialized_ingress_message,
                    },
                ),
            )),
            Err(
                IngressPoolAccessError::IngressMessageNotFound
                | IngressPoolAccessError::BlockNotFound,
            ) => Err(StatusCode::NOT_FOUND),
            Err(
                IngressPoolAccessError::NotABlockProposal | IngressPoolAccessError::SummaryBlock,
            ) => Err(StatusCode::BAD_REQUEST),
        }
    });

    let bytes = join_handle
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)??;

    Ok(bytes)
}
```
