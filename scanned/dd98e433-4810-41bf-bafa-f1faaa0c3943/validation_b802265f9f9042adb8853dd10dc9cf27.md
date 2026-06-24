### Title
Unauthenticated Caller Can Read Any Participant's Financial State in SNS Swap - (`File: rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister's `get_buyer_state` query method accepts an arbitrary `principal_id` in its request and returns the corresponding buyer's financial participation data without verifying that the caller is the owner of that principal. Any unprivileged caller who knows (or guesses) another participant's principal ID can retrieve their committed ICP amounts.

### Finding Description

The `get_buyer_state` function in `rs/sns/swap/src/swap.rs` performs a direct lookup into the `buyers` map using the `principal_id` supplied in the request, with no check that the caller matches the queried principal: [1](#0-0) 

The canister endpoint in `rs/sns/swap/canister/canister.rs` exposes this as a public `#[query]` method and passes the request straight through without any caller validation: [2](#0-1) 

The helper `caller_principal_id()` is defined in the same file and used by other methods (e.g., `refresh_buyer_tokens`), but is never consulted in `get_buyer_state`: [3](#0-2) 

The `GetBuyerStateRequest` proto message explicitly carries the victim's `principal_id` as the lookup key: [4](#0-3) 

The returned `GetBuyerStateResponse` contains a `BuyerState` with `TransferableAmount` fields including `amount_e8s`, `amount_transferred_e8s`, `transfer_fee_paid_e8s`, and transfer timestamps — concrete financial data belonging to the queried participant: [5](#0-4) 

The Candid interface confirms `get_buyer_state` is a public `query` endpoint callable by anyone: [6](#0-5) 

### Impact Explanation

An unprivileged caller — including the anonymous principal — can query the exact ICP amount committed by any other participant in an SNS token swap by supplying that participant's principal ID. Principal IDs on the IC are public identifiers (derivable from public keys, observable on-chain, or enumerable from other public swap data such as `list_direct_participants`). This constitutes unauthorized access to another user's private financial data: how much ICP they have committed, whether their transfer succeeded, and the precise timestamps of their financial activity.

The `BuyerState` data is analogous to the KYC/financial identity data exposed in the report — it is per-user, sensitive, and should only be readable by the owner or authorized parties.

### Likelihood Explanation

Exploitation requires no special privileges, no chained vulnerability, and no secret. The attacker only needs:
1. A valid principal ID of a swap participant (obtainable from `list_direct_participants`, on-chain ledger history, or NNS dapp activity).
2. The ability to send a signed query call to the swap canister — trivially done with any IC agent or the `dfx` CLI.

The endpoint is a `query` call, meaning it is cheap, fast, and does not require cycles. There is no rate limiting at the canister level.

### Recommendation

Add a caller authorization check inside `get_buyer_state` (or at the canister entry point) that enforces the caller's principal matches the `principal_id` in the request. Privileged callers (e.g., the NNS governance canister, the swap canister itself during finalization) should be explicitly allowlisted if cross-principal reads are required for operational purposes.

```rust
#[query]
fn get_buyer_state(request: GetBuyerStateRequest) -> GetBuyerStateResponse {
    let caller = caller_principal_id();
    // Only allow the buyer themselves (or an authorized system canister) to read their state.
    if let Some(requested_principal) = request.principal_id {
        if requested_principal != caller {
            panic!("Caller is not authorized to read buyer state for another principal.");
        }
    }
    swap().get_buyer_state(&request)
}
```

### Proof of Concept

1. Alice participates in an SNS swap, committing ICP. Her principal ID (`alice_principal`) is observable from `list_direct_participants` or ledger history.
2. Attacker Bob (any principal, including anonymous) constructs a `GetBuyerStateRequest { principal_id: Some(alice_principal) }`.
3. Bob sends a query call to the swap canister's `get_buyer_state` endpoint.
4. The canister executes `self.buyers.get(&alice_principal.to_string())` with no caller check and returns Alice's `BuyerState` including her committed ICP amount and transfer timestamps.
5. Bob now knows Alice's exact financial participation in the swap without her consent.

The test `test_get_buyer_state` in `rs/sns/swap/tests/swap.rs` demonstrates that `get_buyer_state` is called with an arbitrary `principal_id` and returns data — but never tests that a *different* caller is rejected: [7](#0-6)

### Citations

**File:** rs/sns/swap/src/swap.rs (L3010-3016)
```rust
    pub fn get_buyer_state(&self, request: &GetBuyerStateRequest) -> GetBuyerStateResponse {
        let buyer_state = match request.principal_id {
            Some(buyer_principal_id) => self.buyers.get(&buyer_principal_id.to_string()).cloned(),
            None => panic!("GetBuyerStateRequest must provide principal_id"),
        };
        GetBuyerStateResponse { buyer_state }
    }
```

**File:** rs/sns/swap/canister/canister.rs (L81-84)
```rust
/// Returns caller as PrincipalId
fn caller_principal_id() -> PrincipalId {
    PrincipalId::from(caller())
}
```

**File:** rs/sns/swap/canister/canister.rs (L102-109)
```rust
/// Get the state of a buyer. This will return a `GetBuyerStateResponse`
/// with an optional `BuyerState` struct if the Swap Canister has
/// been successfully notified of a buyer's ICP transfer.
#[query]
fn get_buyer_state(request: GetBuyerStateRequest) -> GetBuyerStateResponse {
    log!(INFO, "get_buyer_state");
    swap().get_buyer_state(&request)
}
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L790-797)
```text
message GetBuyerStateRequest {
  // The principal_id of the user who's buyer state is being queried for.
  ic_base_types.pb.v1.PrincipalId principal_id = 1;
}

message GetBuyerStateResponse {
  BuyerState buyer_state = 1;
}
```

**File:** rs/sns/swap/canister/swap.did (L143-153)
```text
type GetBuyerStateRequest = record {
  principal_id : opt principal;
};

type GetBuyerStateResponse = record {
  buyer_state : opt BuyerState;
};

type GetBuyersTotalResponse = record {
  buyers_total : nat64;
};
```

**File:** rs/sns/swap/canister/swap.did (L478-478)
```text
  get_buyer_state : (GetBuyerStateRequest) -> (GetBuyerStateResponse) query;
```

**File:** rs/sns/swap/tests/swap.rs (L2178-2236)
```rust
    // Assert the same balance using `get_buyer_state`
    assert_eq!(
        swap.get_buyer_state(&GetBuyerStateRequest {
            principal_id: Some(*TEST_USER1_PRINCIPAL)
        })
        .buyer_state
        .unwrap()
        .amount_icp_e8s(),
        6 * E8
    );

    // Deposit 6 ICP from another buyer.
    assert!(
        swap.refresh_buyer_token_e8s(
            *TEST_USER2_PRINCIPAL,
            None,
            SWAP_CANISTER_ID,
            &mock_stub(vec![LedgerExpect::AccountBalance(
                Account {
                    owner: SWAP_CANISTER_ID.get().into(),
                    subaccount: Some(principal_to_subaccount(&TEST_USER2_PRINCIPAL.clone()))
                },
                Ok(Tokens::from_e8s(6 * E8))
            )])
        )
        .now_or_never()
        .unwrap()
        .is_ok()
    );
    // But only 4 ICP is "accepted" as the swap's init.max_direct_participation_icp_e8s is 10 Tokens and has
    // been reached by this point.
    assert_eq!(
        swap.buyers
            .get(&TEST_USER2_PRINCIPAL.to_string())
            .unwrap()
            .amount_icp_e8s(),
        4 * E8
    );

    // Assert the same balance using `get_buyer_state`
    assert_eq!(
        swap.get_buyer_state(&GetBuyerStateRequest {
            principal_id: Some(*TEST_USER2_PRINCIPAL)
        })
        .buyer_state
        .unwrap()
        .amount_icp_e8s(),
        4 * E8
    );

    // Using `get_buyer_state` without a known principal returns None
    assert!(
        swap.get_buyer_state(&GetBuyerStateRequest {
            principal_id: Some(*TEST_USER3_PRINCIPAL)
        })
        .buyer_state
        .is_none()
    );
}
```
