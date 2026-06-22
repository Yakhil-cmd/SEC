### Title
Unvalidated `buyer` Field in `refresh_buyer_tokens` Causes Canister Trap - (File: `rs/sns/swap/canister/canister.rs`)

### Summary
The SNS Swap canister's `refresh_buyer_tokens` update endpoint calls `.unwrap()` on the result of parsing the attacker-controlled `buyer` string field without any prior validation. An unprivileged user can send a non-empty but syntactically invalid principal string, causing the canister call to panic/trap. This is a direct analog to the reported nil-dereference pattern: a required field is accepted from the wire without validation, and then unconditionally dereferenced/unwrapped, causing a panic.

### Finding Description

In `rs/sns/swap/canister/canister.rs`, the `refresh_buyer_tokens` `#[update]` endpoint deserializes a `RefreshBuyerTokensRequest` via Candid and then branches on the `buyer` string field:

```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()   // ← panics on invalid input
    };
    ...
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),               // ← also panics on any Err
    }
}
``` [1](#0-0) 

The `buyer` field is a plain `String` in the Candid-deserialized request struct. When it is non-empty but not a valid `PrincipalId` encoding, `PrincipalId::from_str(&arg.buyer)` returns `Err(...)`, and the unconditional `.unwrap()` panics. There is no validation gate between deserialization and use.

The parallel to the original report is exact:
- **Original**: `Fee *big.Int` is accepted as `null` from JSON; `gencodec:"required"` does not enforce non-nil; subsequent `Cmp(nil)` panics.
- **IC analog**: `buyer: String` is accepted as any string from Candid; no validation enforces a valid principal encoding; subsequent `PrincipalId::from_str(...).unwrap()` panics.

Additionally, the second panic path `Err(msg) => panic!("{}", msg)` converts all internal errors from `refresh_buyer_token_e8s` into traps rather than returning them as proper Candid errors to the caller. [2](#0-1) 

### Impact Explanation

On the Internet Computer, a panic inside an `#[update]` handler is caught by the IC runtime and converted to a **canister trap**: the call is rejected, the canister's state is rolled back to before the call, and the canister continues processing subsequent messages. This differs from the original report's persistent server crash, but the impact is still meaningful:

1. **Per-call denial of service**: Any unprivileged user can force the `refresh_buyer_tokens` call to trap for any principal string they supply, preventing legitimate participation in the SNS swap for that call.
2. **Incorrect error surface**: Legitimate errors from `refresh_buyer_token_e8s` (e.g., lifecycle not open, confirmation text mismatch) are also converted to traps rather than being returned as structured Candid errors, making the endpoint unreliable for callers.
3. **Panic after `await`**: The `panic!("{}", msg)` branch fires *after* the `await` on the ICP ledger call. While no swap-canister state changes are committed at that point, this pattern is fragile: any future refactor that moves state mutations before the error return would introduce a state-inconsistency window.

### Likelihood Explanation

The `refresh_buyer_tokens` endpoint is a publicly callable `#[update]` method on the SNS Swap canister, reachable by any principal on the Internet Computer without any authentication or role requirement. The `buyer` field is a free-form `String` in the Candid interface. An attacker needs only to send a single ingress message with `buyer` set to any non-empty, non-principal string (e.g., `"not-a-principal"`). No special knowledge, keys, or privileges are required.

### Recommendation

Replace the unconditional `.unwrap()` with explicit error handling that returns a structured error to the caller:

```rust
let p: PrincipalId = if arg.buyer.is_empty() {
    caller_principal_id()
} else {
    match PrincipalId::from_str(&arg.buyer) {
        Ok(p) => p,
        Err(e) => ic_cdk::trap(&format!("Invalid buyer principal: {e}")),
        // or better: return a typed error via the Candid response type
    }
};
```

Similarly, replace `Err(msg) => panic!("{}", msg)` with a proper error response. The `refresh_buyer_token_e8s` function already returns `Result<RefreshBuyerTokensResponse, String>`; the canister endpoint should propagate that `Err` as a Candid-level error rather than trapping.

### Proof of Concept

Send the following ingress message to the SNS Swap canister's `refresh_buyer_tokens` endpoint (Candid-encoded):

```
record {
  buyer = "this-is-not-a-valid-principal-id";
  confirmation_text = null;
}
```

`PrincipalId::from_str("this-is-not-a-valid-principal-id")` returns `Err(...)`, and `.unwrap()` panics, causing the update call to trap with a runtime error. Any non-empty string that is not a valid base32-encoded principal will trigger this path. [3](#0-2)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L127-142)
```rust
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
    let icp_ledger = create_real_icp_ledger(swap().init_or_panic().icp_ledger_or_panic());
    match swap_mut()
        .refresh_buyer_token_e8s(p, arg.confirmation_text, this_canister_id(), &icp_ledger)
        .await
    {
        Ok(r) => r,
        Err(msg) => panic!("{}", msg),
    }
```
