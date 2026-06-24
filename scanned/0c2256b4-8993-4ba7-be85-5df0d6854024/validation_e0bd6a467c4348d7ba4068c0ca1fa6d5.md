### Title
Unrestricted Canister-Based Buyer Registration Allows `min_participants` Inflation in SNS Swap — (File: `rs/sns/swap/src/swap.rs`)

---

### Summary

The SNS Swap canister's `refresh_buyer_token_e8s` function accepts an arbitrary `buyer` principal supplied by the caller and does not verify that the caller matches the `buyer`. Combined with the `min_participation_reached()` check that counts `self.buyers.len()` without distinguishing genuine users from canister-controlled fake participants, an attacker controlling a single canister can artificially inflate the direct-participant count to meet `min_participants`, forcing a swap to commit under false pretenses and potentially triggering Neurons' Fund participation.

---

### Finding Description

The SNS Swap canister exposes `refresh_buyer_tokens` as an `#[update]` endpoint. The canister-level handler extracts the buyer principal from the request argument rather than enforcing `caller == buyer`:

```rust
// rs/sns/swap/canister/canister.rs
#[update]
async fn refresh_buyer_tokens(arg: RefreshBuyerTokensRequest) -> RefreshBuyerTokensResponse {
    let p: PrincipalId = if arg.buyer.is_empty() {
        caller_principal_id()
    } else {
        PrincipalId::from_str(&arg.buyer).unwrap()  // ← any principal accepted
    };
    ...
    swap_mut().refresh_buyer_token_e8s(p, ...).await
}
``` [1](#0-0) 

The core function then checks the ICP ledger balance of the subaccount derived from `buyer` (not from the caller) and credits that amount to `buyer` in `self.buyers`:

```rust
// rs/sns/swap/src/swap.rs
let account = Account {
    owner: this_canister.get().0,
    subaccount: Some(principal_to_subaccount(&buyer)),  // ← derived from buyer, not caller
};
let e8s = icp_ledger.account_balance(account).await...;
``` [2](#0-1) 

The swap's commit condition relies on `min_participation_reached()`, which counts `self.buyers.len()` — the number of unique principals in the buyers map — with no restriction on whether those principals are genuine users or canister-controlled identities:

```rust
// rs/sns/swap/src/swap.rs
pub fn min_participation_reached(&self) -> bool {
    if let (Some(params), Some(init)) = (&self.params, &self.init) {
        if init.neurons_fund_participation.is_some() {
            (self.buyers.len() as u32) >= params.min_participants  // ← counts all principals
        } else {
            (self.cf_participants.len().saturating_add(self.buyers.len()) as u32)
                >= params.min_participants
        }
    } else { false }
}
``` [3](#0-2) 

The `new_sale_ticket` function only rejects the anonymous principal — canister principals are fully accepted:

```rust
// rs/sns/swap/src/swap.rs
if caller.is_anonymous() {
    return NewSaleTicketResponse::err_invalid_principal();
}
``` [4](#0-3) 

**Attack path**: A single attacker-controlled canister can:
1. Transfer `min_participant_icp_e8s` ICP to `Account { owner: swap_canister, subaccount: principal_to_subaccount(fake_principal_i) }` for N distinct fake principals (e.g., derived from a counter).
2. Call `refresh_buyer_tokens` with `buyer = fake_principal_i` for each, causing each to be inserted into `self.buyers`.
3. `self.buyers.len()` reaches `min_participants`, `sufficient_participation()` returns `true`, and the swap commits.

The minimum per-participant cost is bounded by `min_participant_icp_e8s`, which has a lower bound of `1_000_000` e8s (0.01 ICP) enforced at SNS initialization: [5](#0-4) 

At 0.01 ICP per fake participant, inflating a `min_participants = 100` swap costs only 1 ICP in ICP locked — and the attacker receives SNS tokens in return, further reducing net cost.

---

### Impact Explanation

A malicious SNS project (or an attacker colluding with one) can force a swap to commit with fake participants, achieving two concrete harms:

1. **False decentralization**: The swap appears to have met its `min_participants` threshold, but all or most participants are canister-controlled. The SNS governance token is not genuinely distributed.

2. **Neurons' Fund drain**: When `neurons_fund_participation` is set, the Neurons' Fund contributes ICP proportional to direct participation. A committed swap with fake participants triggers `settle_neurons_fund_participation`, causing the NNS to mint and transfer real ICP to the SNS treasury under the false premise of sufficient decentralization. This is a direct ledger conservation impact — ICP is minted and transferred to a project that did not achieve genuine participation. [6](#0-5) 

---

### Likelihood Explanation

- The entry path is fully reachable by any unprivileged ingress sender or canister caller — no privileged role is required.
- The economic cost is low: `min_participants * min_participant_icp_e8s` ICP, partially recovered as SNS tokens.
- The attack is automatable from a single canister using a loop over derived subaccounts.
- No existing check in `refresh_buyer_token_e8s` or `new_sale_ticket` prevents canister-based multi-account participation.
- The only existing guard (`is_anonymous()`) is trivially bypassed by any non-anonymous canister principal.

---

### Recommendation

1. **Enforce `caller == buyer`** in `refresh_buyer_token_e8s`: reject calls where `arg.buyer` is non-empty and differs from `caller_principal_id()`. This prevents a single canister from registering arbitrary principals as buyers.

2. **Restrict canister-based participation**: Add a check in `new_sale_ticket` and `refresh_buyer_token_e8s` that rejects callers whose principal is a canister ID (i.e., not self-authenticating), analogous to how the NNS governance canister previously restricted neuron controllers.

3. **Cap the number of participants per controller**: Track the originating caller separately from the credited `buyer`, and limit how many distinct `buyer` entries a single caller can create.

---

### Proof of Concept

```rust
// Attacker canister (pseudo-code)
fn inflate_participants(swap: CanisterId, icp_ledger: CanisterId, n: u64) {
    for i in 0..n {
        // Derive a unique fake principal from counter i
        let fake_principal = derive_principal(i);
        let subaccount = principal_to_subaccount(fake_principal);

        // Transfer min_participant_icp_e8s ICP to the swap's subaccount for fake_principal
        icp_ledger.transfer(Account {
            owner: swap,
            subaccount: Some(subaccount),
        }, min_participant_icp_e8s);

        // Register fake_principal as a buyer — caller != buyer, no check enforced
        swap.refresh_buyer_tokens(RefreshBuyerTokensRequest {
            buyer: fake_principal.to_string(),
            confirmation_text: None,
        });
    }
    // self.buyers.len() == n >= min_participants
    // sufficient_participation() == true
    // swap commits, Neurons' Fund ICP is minted and transferred
}
```

The `refresh_buyer_tokens` canister endpoint accepts `arg.buyer` as any string principal with no caller-equality check, making this attack directly executable from a single canister ingress call sequence. [7](#0-6) [8](#0-7)

### Citations

**File:** rs/sns/swap/canister/canister.rs (L127-143)
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
}
```

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

**File:** rs/sns/swap/src/swap.rs (L2537-2539)
```rust
        if caller.is_anonymous() {
            return NewSaleTicketResponse::err_invalid_principal();
        }
```

**File:** rs/sns/swap/src/swap.rs (L2796-2798)
```rust
    pub fn sufficient_participation(&self) -> bool {
        self.min_participation_reached() && self.min_direct_participation_icp_e8s_reached()
    }
```

**File:** rs/sns/swap/src/swap.rs (L2801-2825)
```rust
    pub fn min_participation_reached(&self) -> bool {
        if let (Some(params), Some(init)) = (&self.params, &self.init) {
            if init.neurons_fund_participation.is_some() {
                // Only count direct participants for determining swap's success.
                // Note that a valid Swap Init should either have `neurons_fund_participation` or
                // `cf_participants`, but not both at the same time; here, we defensively perform
                // the check again anyway.
                if !self.cf_participants.is_empty() {
                    log!(
                        ERROR,
                        "Inconsistent Swap Init: cf_participants has {} elements (starting with \
                        {:?}) while neurons_fund_participation is set.",
                        self.cf_participants.len(),
                        self.cf_participants[0],
                    );
                }
                (self.buyers.len() as u32) >= params.min_participants
            } else {
                (self.cf_participants.len().saturating_add(self.buyers.len()) as u32)
                    >= params.min_participants
            }
        } else {
            false
        }
    }
```

**File:** rs/sns/init/src/lib.rs (L1515-1518)
```rust
    /// (9) min_participant_icp_e8s is at least as big as `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S`.
    ///     This ensures, that users upon calling `swap.refresh_buyer_token()` must participate
    ///     at least `MIN_PARTICIPANT_ICP_LOWER_BOUND_E8S` Hence, no malicious user can overflow
    ///     node's memory by participating with very low amounts.\
```
