### Title
SNS Swap `max_participant_icp_e8s` Per-Principal Cap Bypass via Multiple Principals — (`rs/sns/swap/src/swap.rs`)

### Summary
The SNS Swap canister enforces a per-participant ICP contribution ceiling (`max_participant_icp_e8s`) keyed exclusively on `PrincipalId`. Because any entity can trivially generate or control multiple IC principals, a single real-world actor can participate from each principal up to the cap, accumulating an unbounded total contribution and defeating the decentralization intent of the limit.

### Finding Description
`refresh_buyer_token_e8s` in `rs/sns/swap/src/swap.rs` is the sole enforcement point for the per-buyer ceiling. The function:

1. Reads the ICP balance of the buyer's dedicated subaccount on the ICP ledger. [1](#0-0) 

2. Looks up the buyer's previously recorded participation, keyed by `buyer.to_string()` (the principal string). [2](#0-1) 

3. Caps the new accepted balance at `max_participant_icp_e8s`. [3](#0-2) 

4. Stores the result back into `self.buyers` under the same principal key. [4](#0-3) 

There is no cross-principal aggregation. Each principal is treated as a fully independent participant. The public canister endpoint accepts an arbitrary `buyer` field (defaulting to the caller), so participation on behalf of any principal is permissionless. [5](#0-4) 

**Attack path:**
- Actor controls principal A and principal B (trivially obtained via Internet Identity or any self-authenticating key pair).
- Transfers `max_participant_icp_e8s` ICP to `swap_canister[principal_to_subaccount(A)]`, calls `refresh_buyer_tokens` for A → swap records `max_participant_icp_e8s` for A.
- Transfers `max_participant_icp_e8s` ICP to `swap_canister[principal_to_subaccount(B)]`, calls `refresh_buyer_tokens` for B → swap records `max_participant_icp_e8s` for B.
- Repeat for N principals → total accepted = `N × max_participant_icp_e8s`.

The `max_participant_icp_e8s` parameter is defined in the swap `Params` struct and is set at SNS initialization to enforce fair distribution. [6](#0-5) 

### Impact Explanation
`max_participant_icp_e8s` is the primary mechanism preventing a single entity from dominating an SNS decentralization swap. Bypassing it allows one actor to:
- Acquire an arbitrarily large fraction of the SNS token supply.
- Obtain disproportionate SNS governance voting power, directly undermining the decentralization goal that the swap is designed to achieve.
- Crowd out legitimate small participants if the swap's `max_direct_participation_icp_e8s` ceiling is reached early by a single whale using many principals.

### Likelihood Explanation
Generating additional IC principals requires no special access: any self-authenticating key pair is a valid principal, and Internet Identity supports multiple anchors per user. The attack requires only ICP funds and is executable by any unprivileged ingress sender with no on-chain footprint linking the principals together.

### Recommendation
The root cause is structural: pseudonymous principal-based identity cannot enforce per-person limits without out-of-band identity binding (e.g., KYC). Mitigations to consider:

1. **Acknowledge as a known design limitation** (consistent with the Derby upstream team's response) and document it explicitly in the SNS swap specification so SNS creators set `max_participant_icp_e8s` with this in mind.
2. **Raise `min_participant_icp_e8s`** to increase the cost of Sybil participation (each additional principal requires a fresh ICP transfer above the minimum).
3. **Introduce a neuron-gated participation mode** where participation requires an existing NNS neuron of a minimum age/stake, raising the Sybil cost significantly.

### Proof of Concept
```
// Pseudocode — two principals controlled by the same actor
let max = swap_params.max_participant_icp_e8s;  // e.g. 10_000 ICP

// Round 1: principal A
icp_ledger.transfer({ to: swap_subaccount(principal_A), amount: max });
swap.refresh_buyer_tokens({ buyer: principal_A });
// swap.buyers[A].amount_icp_e8s == max  ✓

// Round 2: principal B
icp_ledger.transfer({ to: swap_subaccount(principal_B), amount: max });
swap.refresh_buyer_tokens({ buyer: principal_B });
// swap.buyers[B].amount_icp_e8s == max  ✓

// Total accepted from one entity: 2 × max, with no error or rejection.
// Repeat for N principals → N × max accepted.
```

The cap enforcement at `rs/sns/swap/src/swap.rs:1237` only compares `new_balance_e8s` against `max_participant_icp_e8s` for the single principal being refreshed; there is no aggregate check across principals. [3](#0-2)

### Citations

**File:** rs/sns/swap/src/swap.rs (L1153-1163)
```rust
        let e8s = {
            let account = Account {
                owner: this_canister.get().0,
                subaccount: Some(principal_to_subaccount(&buyer)),
            };
            icp_ledger
                .account_balance(account)
                .await
                .map_err(|x| x.to_string())?
                .get_e8s()
        };
```

**File:** rs/sns/swap/src/swap.rs (L1210-1213)
```rust
        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());
```

**File:** rs/sns/swap/src/swap.rs (L1236-1237)
```rust
        // Limit the participation based on the maximum per participant.
        let new_balance_e8s = std::cmp::min(new_balance_e8s, max_participant_icp_e8s);
```

**File:** rs/sns/swap/src/swap.rs (L1285-1288)
```rust
        self.buyers
            .entry(buyer.to_string())
            .or_insert_with(|| BuyerState::new(0))
            .set_amount_icp_e8s(new_balance_e8s);
```

**File:** rs/sns/swap/canister/canister.rs (L128-134)
```rust
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    log!(INFO, "refresh_buyer_tokens");
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()
    };
```

**File:** rs/sns/swap/src/gen/ic_sns_swap.pb.v1.rs (L644-649)
```rust
    /// The maximum amount of ICP that each buyer can contribute. Must be
    /// greater than or equal to `min_participant_icp_e8s` and less than
    /// or equal to `max_icp_e8s`. Can effectively be disabled by
    /// setting it to `max_icp_e8s`.
    #[prost(uint64, tag = "5")]
    pub max_participant_icp_e8s: u64,
```
