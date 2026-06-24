### Title
Unauthenticated `/logs` Endpoint Exposes Internal P0/P1 Error Logs to Any Caller — (`rs/boundary_node/salt_sharing/canister/canister.rs`, `logs.rs`)

### Summary
The `http_request` query handler in the salt-sharing canister routes `/logs` to `export_logs_as_http_response` with no caller authentication. Any anonymous principal can issue a query call to retrieve all buffered P0 and P1 log entries, including internal error messages from registry polling failures and salt regeneration failures.

### Finding Description
`http_request` is declared as a `#[query]` method: [1](#0-0) 

The `inspect_message` hook that guards `get_salt` applies **only to update (ingress) calls**, not query calls. It is therefore entirely irrelevant to `http_request`: [2](#0-1) 

`export_logs_as_http_response` performs no caller check and unconditionally returns HTTP 200 with all P0 and P1 entries serialized as JSON: [3](#0-2) 

The P0 buffer is written to on every registry polling failure and salt regeneration failure, with full `{err:?}` debug formatting of inter-canister call errors: [4](#0-3) [5](#0-4) [6](#0-5) 

### Impact Explanation
Any unauthenticated caller learns:
- Whether registry polling is failing, with full debug-formatted error strings (rejection codes, canister error messages)
- Whether salt regeneration is failing and at what timestamps
- Precise nanosecond timestamps of every logged event, enabling health fingerprinting of the canister over time

Each `LogEntry` includes `timestamp`, `file`, `line`, `message`, and `counter` fields, all returned verbatim. [7](#0-6) 

### Likelihood Explanation
The exploit requires only a standard IC query call to a public canister endpoint — no special role, key, or network position needed. It is trivially reproducible with `dfx canister call --query <canister> http_request '(record { url="/logs"; ... })'`.

### Recommendation
Add a caller check at the top of the `/logs` branch (and `/metrics` branch) in `http_request`, restricting access to a configured controller or operator principal, mirroring the pattern already used in `get_salt`:

```rust
"/logs" => {
    if !is_api_boundary_node_principal(&caller()) {
        return HttpResponseBuilder::forbidden().build();
    }
    export_logs_as_http_response(request)
}
```

Alternatively, expose logs only via a separate `#[update]` method subject to `inspect_message` authorization.

### Proof of Concept
```rust
// In a pocket-ic or replica integration test:
let response = canister
    .query_call(
        Principal::anonymous(),
        "http_request",
        encode_one(HttpRequest {
            method: "GET".into(),
            url: "/logs".into(),
            headers: vec![],
            body: vec![],
        }).unwrap(),
    )
    .unwrap();

let http_resp: HttpResponse = decode_one(&response).unwrap();
assert_eq!(http_resp.status_code, 200);
let log: Log = serde_json::from_slice(&http_resp.body).unwrap();
// After any registry poll failure, entries will be non-empty with P0 messages
assert!(!log.entries.is_empty());
assert!(log.entries[0].message.contains("poll_api_boundary_nodes"));
```

### Citations

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L20-30)
```rust
#[inspect_message]
fn inspect_message() {
    let caller_id = caller();
    let called_method = method_name();

    if called_method == REPLICATED_QUERY_METHOD && is_api_boundary_node_principal(&caller_id) {
        accept_message();
    } else {
        trap("message_inspection_failed: method call is prohibited in the current context");
    }
}
```

**File:** rs/boundary_node/salt_sharing/canister/canister.rs (L68-78)
```rust
#[query(
    hidden = true,
    decode_with = "candid::decode_one_with_decoding_quota::<100000,_>"
)]
fn http_request(request: HttpRequest) -> HttpResponse {
    match request.path() {
        "/metrics" => export_metrics_as_http_response(),
        "/logs" => export_logs_as_http_response(request),
        _ => HttpResponseBuilder::not_found().build(),
    }
}
```

**File:** rs/boundary_node/salt_sharing/canister/logs.rs (L17-25)
```rust
#[derive(Clone, Debug, Deserialize, serde::Serialize)]
pub struct LogEntry {
    pub timestamp: u64,
    pub priority: Priority,
    pub file: String,
    pub line: u32,
    pub message: String,
    pub counter: u64,
}
```

**File:** rs/boundary_node/salt_sharing/canister/logs.rs (L33-77)
```rust
pub fn export_logs_as_http_response(request: HttpRequest) -> HttpResponse {
    let max_skip_timestamp = match request.raw_query_param("time") {
        Some(arg) => match u64::from_str(arg) {
            Ok(value) => value,
            Err(_) => {
                return HttpResponseBuilder::bad_request()
                    .with_body_and_content_length("failed to parse the 'time' parameter")
                    .build();
            }
        },
        None => 0,
    };

    let mut entries: Log = Default::default();

    for entry in export_logs(&P0) {
        entries.entries.push(LogEntry {
            timestamp: entry.timestamp,
            counter: entry.counter,
            priority: Priority::P0,
            file: entry.file.to_string(),
            line: entry.line,
            message: entry.message,
        });
    }

    for entry in export_logs(&P1) {
        entries.entries.push(LogEntry {
            timestamp: entry.timestamp,
            counter: entry.counter,
            priority: Priority::P1,
            file: entry.file.to_string(),
            line: entry.line,
            message: entry.message,
        });
    }

    entries
        .entries
        .retain(|entry| entry.timestamp >= max_skip_timestamp);

    HttpResponseBuilder::ok()
        .header("Content-Type", "application/json; charset=utf-8")
        .with_body_and_content_length(serde_json::to_string(&entries).unwrap_or_default())
        .build()
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L23-27)
```rust
    if (!is_salt_init() || init_arg.regenerate_now)
        && let Err(err) = try_regenerate_salt().await
    {
        log!(P0, "[init_regenerate_salt_failed]: {err}");
    }
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L40-43)
```rust
    set_timer(delay, async {
        if let Err(err) = try_regenerate_salt().await {
            log!(P0, "[scheduled_regenerate_salt_failed]: {err}");
        }
```

**File:** rs/boundary_node/salt_sharing/canister/helpers.rs (L101-114)
```rust
        Ok((Err(err),)) => {
            log!(
                P0,
                "[poll_api_boundary_nodes]: failed to fetch nodes from registry {err:?}",
            );
            ("failure", "calling_canister_method_failed")
        }
        Err(err) => {
            log!(
                P0,
                "[poll_api_boundary_nodes]: failed to fetch nodes from registry {err:?}",
            );
            ("failure", "canister_call_rejected")
        }
```
