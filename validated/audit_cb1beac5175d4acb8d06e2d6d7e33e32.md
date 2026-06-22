### Title
Unbounded O(N) Candid Decoding in `/construction/hash` Allows CPU Exhaustion — (`rs/rosetta-api/icrc1/src/construction_api/utils.rs`)

---

### Summary

`handle_construction_hash` iterates every envelope in a caller-supplied `SignedTransaction` and performs full Candid decoding plus hashing for each one, with no limit on envelope count and no HTTP body-size cap on the server. An unauthenticated attacker can submit a single crafted request containing arbitrarily many envelopes and drive the Rosetta process to CPU exhaustion.

---

### Finding Description

**Vulnerable loop — no early exit, no count guard:** [1](#0-0) 

Every iteration calls `build_transaction_hash_from_envelope_content`, which performs:
1. Method-name parsing (`CanisterMethodName::new_from_envelope_content`)
2. Full Candid decoding of the `arg` bytes (`Decode!` macro — `TransferArg`, `ApproveArgs`, or `TransferFromArgs`)
3. ICRC-1 transaction hash computation [2](#0-1) 

The loop does **not** short-circuit when a second distinct hash is detected; it exhausts the entire envelope vector before the `tx_hashes.len() > 1` check. [3](#0-2) 

**No HTTP body-size limit is configured.** The axum router in `main.rs` applies only a metrics layer, a tracing span layer, and a request-ID layer — no `DefaultBodyLimit` middleware: [4](#0-3) 

Without `DefaultBodyLimit`, axum imposes no cap on the incoming body, so the `signed_transaction` hex string (and the CBOR-encoded `Vec<Envelope>` it decodes to) can be arbitrarily large.

**`SignedTransaction` carries an unbounded `Vec<Envelope>`:** [5](#0-4) 

**The endpoint is unauthenticated and publicly reachable:** [6](#0-5) 

---

### Impact Explanation

Each envelope requires a Candid decode of a non-trivial struct. With N = 100,000 envelopes (easily fitting in a multi-MB body), the server performs 100,000 sequential Candid decodes on a single request before returning any response. Concurrent requests multiply the effect. The Rosetta process is single-binary and handles all token operations; sustained CPU saturation prevents legitimate `/construction/submit` calls from being processed, effectively blocking ICRC-1 transaction submission for any exchange or integrator running this Rosetta instance.

---

### Likelihood Explanation

The Rosetta API binds on `0.0.0.0` (default port 8080) and is commonly exposed to internal networks or, in some deployments, the public internet. No authentication, API key, or rate-limiting is present in the codebase. Crafting the payload requires only knowledge of the CBOR/hex encoding of `SignedTransaction`, which is fully documented and trivially reproducible from the public `FromStr`/`Display` implementations.

---

### Recommendation

1. **Add a `DefaultBodyLimit` layer** to the axum router (e.g., 1–2 MB) to cap total request size.
2. **Add an envelope-count guard** at the top of `handle_construction_hash` (e.g., reject if `envelopes.len() > MAX_ENVELOPES`, where `MAX_ENVELOPES` reflects the legitimate 24 h / ingress-interval ceiling, roughly 288).
3. **Exit the loop early** once `tx_hashes.len() > 1` is detected, avoiding processing of remaining envelopes.

---

### Proof of Concept

```rust
// Build N identical-but-valid envelopes (same method, different created_at_time → different hash)
let mut envelopes = vec![];
for i in 0..100_000u64 {
    let arg = Encode!(&TransferArg {
        from_subaccount: None,
        to: Account { owner: Principal::anonymous(), subaccount: None },
        fee: None,
        created_at_time: Some(i),   // distinct → distinct tx hash
        memo: None,
        amount: 1u64.into(),
    }).unwrap();
    envelopes.push(Envelope {
        content: Cow::Owned(EnvelopeContent::Call {
            nonce: None, ingress_expiry: u64::MAX,
            sender: Principal::anonymous(),
            canister_id: Principal::anonymous(),
            method_name: "icrc1_transfer".to_string(),
            arg,
        }),
        sender_pubkey: None, sender_sig: None, sender_delegation: None,
    });
}
let tx = SignedTransaction { envelopes };
let hex = tx.to_string();   // hex-encoded CBOR

// POST to /construction/hash with signed_transaction = hex
// → server performs 100,000 Candid decodes before returning an error
```

The loop processes all N envelopes before the `tx_hashes.len() > 1` check fires, confirming O(N) unbounded work per request.

### Citations

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L194-216)
```rust
pub fn build_transaction_hash_from_envelope_content(
    envelope_content: &EnvelopeContent,
) -> anyhow::Result<String> {
    // First we can derive the canister method args and the caller of the function from the envelope content
    let canister_method_name = CanisterMethodName::new_from_envelope_content(envelope_content)?;

    let candid_encoded_bytes = match envelope_content {
        EnvelopeContent::Call { arg, .. } => arg.clone(),
        _ => bail!(
            "Wrong EnvelopeContent type, expected EnvelopeContent::Call, got {:?}",
            envelope_content
        ),
    };

    // Then we can derive the icrc1 transaction from the canister method args and the caller
    let icrc1_transaction = build_icrc1_transaction_from_canister_method_args(
        &canister_method_name,
        envelope_content.sender(),
        candid_encoded_bytes,
    )?;

    Ok(hex::encode(icrc1_transaction.hash()))
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L331-335)
```rust
    let mut tx_hashes = HashSet::new();
    for envelope in signed_transaction.envelopes {
        let transaction_hash = build_transaction_hash_from_envelope_content(&envelope.content)?;
        tx_hashes.insert(transaction_hash);
    }
```

**File:** rs/rosetta-api/icrc1/src/construction_api/utils.rs (L338-342)
```rust
    if tx_hashes.len() > 1 {
        bail!(
            "Only one icrc1 ledger transaction is supported per signed transaction. Found more than one icrc1 ledger transaction."
        );
    }
```

**File:** rs/rosetta-api/icrc1/src/main.rs (L361-391)
```rust
    let app = Router::new()
        .route("/ready", get(ready))
        .route("/health", get(health))
        .route("/call", post(call))
        .route("/network/list", post(network_list))
        .route("/network/options", post(network_options))
        .route("/network/status", post(network_status))
        .route("/block", post(block))
        .route("/account/balance", post(account_balance))
        .route("/block/transaction", post(block_transaction))
        .route("/search/transactions", post(search_transactions))
        .route("/mempool", post(mempool))
        .route("/mempool/transaction", post(mempool_transaction))
        .route("/construction/derive", post(construction_derive))
        .route("/construction/preprocess", post(construction_preprocess))
        .route("/construction/metadata", post(construction_metadata))
        .route("/construction/combine", post(construction_combine))
        .route("/construction/submit", post(construction_submit))
        .route("/construction/hash", post(construction_hash))
        .route("/construction/payloads", post(construction_payloads))
        .route("/construction/parse", post(construction_parse))
        // Apply the metrics middleware
        .layer(metrics_layer)
        // This layer creates a span for each http request and attaches
        // the request_id, HTTP Method and path to it.
        .layer(add_request_span())
        // This layer creates a new id for each request and puts it into the
        // request extensions. Note that it should be added after the
        // Trace layer.
        .layer(RequestIdLayer)
        .with_state(token_app_states.clone());
```

**File:** rs/rosetta-api/icrc1/src/construction_api/types.rs (L45-61)
```rust
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct SignedTransaction<'a> {
    pub envelopes: Vec<Envelope<'a>>,
}

impl std::fmt::Display for SignedTransaction<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", hex::encode(serde_cbor::ser::to_vec(self).unwrap()))
    }
}

impl FromStr for SignedTransaction<'_> {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        serde_cbor::from_slice(hex::decode(s)?.as_slice()).map_err(|err| anyhow!("{:?}", err))
    }
}
```

**File:** rs/rosetta-api/icrc1/src/construction_api/endpoints.rs (L67-76)
```rust
pub async fn construction_hash(
    State(state): State<Arc<MultiTokenAppState>>,
    Json(request): Json<ConstructionHashRequest>,
) -> Result<Json<ConstructionHashResponse>> {
    get_state_from_network_id(&request.network_identifier, &state)
        .map_err(|err| Error::invalid_network_id(&err))?;
    Ok(Json(services::construction_hash(
        request.signed_transaction,
    )?))
}
```
