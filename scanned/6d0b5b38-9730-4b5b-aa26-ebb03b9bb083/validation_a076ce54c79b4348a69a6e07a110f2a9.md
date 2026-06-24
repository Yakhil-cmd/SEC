Now I have enough information to make a determination. Let me check the key facts:

### Title
Unquota'd `candid::Decode!` in `manage_neuron` Closure Enables Rosetta Process Crash via Crafted Candid Arg — (`rs/rosetta-api/icp/src/request.rs`)

---

### Summary

An unauthenticated attacker can POST a crafted `ConstructionSubmitRequest` to the ICP Rosetta server's `/construction/submit` endpoint. By embedding a Candid-encoded arg whose type table encodes deeply nested or recursive structures, the attacker triggers a stack overflow inside the bare `candid::Decode!` call in the `manage_neuron` closure, crashing the Rosetta process.

---

### Finding Description

The `manage_neuron` closure in `TryFrom<&models::Request> for Request` calls:

```rust
Decode!(
    &payload.update_content().arg.0,
    ic_nns_governance_api::ManageNeuronRequest
)
``` [1](#0-0) 

This is a bare `Decode!` invocation with **no `DecoderConfig`**, meaning no `skipping_quota` and no `decoding_quota` are set. The Candid library's typed decoder must skip wire-format fields that are absent from the Rust target type. When the Candid type table encodes deeply nested or recursive types, this skipping recurses proportionally, exhausting the thread stack.

The DFINITY codebase itself demonstrates awareness of this risk: the `candid_type_decoder` fuzz target wraps every `Decode!` call with `config.set_skipping_quota(10_000)` precisely to prevent stack overflow during fuzzing. [2](#0-1) 

The actix-web server applies a 4 MB JSON body limit: [3](#0-2) 

However, this limit does **not** prevent the attack. Candid type tables can encode thousands of levels of nesting in a few hundred bytes using index references. The actual Candid arg bytes (nested inside JSON → hex → CBOR → `EnvelopePair.update.arg`) can be tiny while still encoding a type table that causes unbounded recursion during field skipping.

The call chain is fully reachable without any authentication or signature verification:

```
POST /construction/submit
  → construction_submit (rosetta_server.rs:142)
  → RosettaRequestHandler::construction_submit (construction_submit.rs:15)
  → SignedTransaction::from_str (parses CBOR, no sig check)
  → ledger.submit (ledger_client.rs:292)
  → Request::try_from (request.rs:235)
  → manage_neuron() closure (request.rs:250)
  → Decode!(...) ← stack overflow here
``` [4](#0-3) [5](#0-4) 

No signature verification occurs before `Request::try_from` is called; the `SignedTransaction::from_str` only parses CBOR structure.

---

### Impact Explanation

A stack overflow in Rust causes a process abort (SIGABRT/SIGSEGV), not a recoverable panic. A single crafted HTTP request crashes the Rosetta server process. The IC replica and ledger are unaffected, but the Rosetta API becomes unavailable until the process is restarted. This is a non-volumetric DoS: one request suffices.

---

### Likelihood Explanation

The endpoint is publicly accessible with no authentication. The attacker only needs to construct a valid CBOR-encoded `SignedTransaction` with a neuron-management `RequestType` (e.g., `SetDissolveTimestamp`) and a Candid arg whose type table encodes deep nesting. No valid signature, no valid neuron ID, and no valid principal are required — the crash occurs before any of those are checked. The construction is straightforward for anyone familiar with the Candid wire format.

---

### Recommendation

Replace the bare `Decode!` call with a quota-limited variant, consistent with the pattern already used in the codebase:

```rust
let manage_neuron = || {
    let mut config = candid::DecoderConfig::new();
    config.set_skipping_quota(10_000);
    config.set_decoding_quota(10_000);
    Decode!([config]; &payload.update_content().arg.0, ic_nns_governance_api::ManageNeuronRequest)
        .map_err(|e| ApiError::invalid_request(format!("Could not parse manage_neuron: {e}")))
        .map(|m| m.command)
};
```

Apply the same fix to every other bare `Decode!` call in the Rosetta ICP codebase (e.g., `convert::from_arg` at line 267 should be audited similarly).

---

### Proof of Concept

1. Construct a Candid type table encoding a record with 50,000 levels of nesting (fits in ~1 KB using index references).
2. Wrap it in a minimal Candid value blob (empty record value).
3. Encode as the `arg` field of an `EnvelopePair.update` with `RequestType::SetDissolveTimestamp`.
4. CBOR-encode the `SignedTransaction`, hex-encode it, embed in a `ConstructionSubmitRequest` JSON body.
5. POST to `http://<rosetta-host>/construction/submit`.
6. The Rosetta process aborts with a stack overflow inside `Decode!` at `request.rs:252`.

### Citations

**File:** rs/rosetta-api/icp/src/request.rs (L250-261)
```rust
        let manage_neuron = || {
            {
                Decode!(
                    &payload.update_content().arg.0,
                    ic_nns_governance_api::ManageNeuronRequest
                )
                .map_err(|e| {
                    ApiError::invalid_request(format!("Could not parse manage_neuron: {e}"))
                })
            }
            .map(|m| m.command)
        };
```

**File:** rs/fuzzers/candid/fuzz_targets/candid_type_decoder.rs (L41-47)
```rust
    let mut config = DecoderConfig::new();
    config.set_skipping_quota(10_000);

    let _decoded = match Decode!([config]; payload.as_slice(), HttpResponse) {
        Ok(_v) => _v,
        Err(_e) => return,
    };
```

**File:** rs/rosetta-api/icp/src/rosetta_server.rs (L297-299)
```rust
                    web::JsonConfig::default()
                        .limit(4 * 1024 * 1024)
                        .error_handler(move |e, _| {
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_submit.rs (L15-33)
```rust
    pub async fn construction_submit(
        &self,
        msg: ConstructionSubmitRequest,
    ) -> Result<ConstructionSubmitResponse, ApiError> {
        verify_network_id(self.ledger.ledger_canister_id(), &msg.network_identifier)?;
        let envelopes = SignedTransaction::from_str(&msg.signed_transaction).map_err(|e| {
            ApiError::invalid_transaction(format!("Failed to parse signed transaction: {e}"))
        })?;
        let results = self.ledger.submit(envelopes).await?;
        let transaction_identifier = transaction_identifier(&results);
        let metadata = TransactionOperationResults::from_transaction_results(
            results,
            self.ledger.token_symbol(),
        )?;
        Ok(ConstructionSubmitResponse {
            transaction_identifier: transaction_identifier.into(),
            metadata: Some((&metadata).into()),
        })
    }
```

**File:** rs/rosetta-api/icp/src/ledger_client.rs (L302-316)
```rust
        let mut results: TransactionResults = signed_transaction
            .requests
            .iter()
            .map(|e| {
                Request::try_from(e).map(|_type| RequestResult {
                    _type,
                    block_index: None,
                    neuron_id: None,
                    transaction_identifier: None,
                    status: crate::request_types::Status::NotAttempted,
                    response: None,
                })
            })
            .collect::<Result<Vec<_>, _>>()?
            .into();
```
