### Title
`max_participant_icp_e8s` Per-Principal Cap in SNS Swap Can Be Gamed via Multiple Principals, Giving False Decentralization Security - (File: rs/sns/swap/src/swap.rs)

---

### Summary

The SNS swap canister enforces `max_participant_icp_e8s` strictly per-principal in `refresh_buyer_token_e8s`. Because IC principals are freely creatable and there is no cross-principal identity verification, a single entity can use N controlled principals to acquire up to `N × max_participant_icp_e8s` ICP worth of SNS tokens. This also allows a single entity to artificially satisfy the `min_participants` decentralization requirement, breaking the core invariant the swap is designed to enforce.

---

### Finding Description

The SNS swap canister is explicitly designed to "decentralize an SNS, i.e., to ensure that a sufficient number of governance tokens of the SNS are distributed among different participants." [1](#0-0) 

The `max_participant_icp_e8s` parameter is the on-chain mechanism meant to enforce per-participant concentration limits: [2](#0-1) 

In `refresh_buyer_token_e8s`, the cap is applied by looking up the buyer's existing balance in the `buyers` map keyed by `buyer.to_string()` (the principal string), then clamping the new balance to `max_participant_icp_e8s`: [3](#0-2) 

There is no mechanism to link multiple principals to the same real-world identity. Any IC user can trivially generate an arbitrary number of fresh principals (e.g., via `dfx identity new`), fund each subaccount with `max_participant_icp_e8s` ICP, and call `refresh_buyer_token_e8s` once per principal. Each call succeeds independently because each principal has a zero `old_amount_icp_e8s` in the `buyers` map: [4](#0-3) 

Additionally, the `min_participants` check — which is the swap's primary decentralization gate — counts entries in `self.buyers`, which is also keyed per-principal: [5](#0-4) 

A single attacker controlling `min_participants` principals, each contributing `min_participant_icp_e8s`, can single-handedly trigger the swap into the COMMITTED state, receiving all SNS tokens while appearing to satisfy the decentralization requirement.

---

### Impact Explanation

**Impact: Medium.** A core protocol invariant is broken: the swap is designed to distribute SNS governance tokens broadly, but a single entity can acquire a controlling share. The `max_participant_icp_e8s` cap and `min_participants` requirement both give a false sense of decentralization security. The SNS governance token distribution — which determines voting power over the SNS — can be monopolized by one actor. [6](#0-5) 

---

### Likelihood Explanation

**Likelihood: Medium.** Creating multiple IC principals requires no special privilege — it is a standard operation available to any user. Funding multiple subaccounts and calling `refresh_buyer_token_e8s` in parallel is straightforward to script. This attack pattern (Sybil via multiple wallets) is well-known and has been executed against similar token launch mechanisms on other chains. [7](#0-6) 

---

### Recommendation

Document explicitly that `max_participant_icp_e8s` and `min_participants` do **not** protect against Sybil attacks by a single entity controlling multiple principals. Consider one or more of the following mitigations:

1. **Off-chain allowlist / Merkle-tree pre-registration**: Require participants to register an address off-chain before the swap opens; validate inclusion proof on `refresh_buyer_token_e8s`.
2. **NNS-neuron-gated participation**: Require the calling principal to control an NNS neuron of a minimum age/stake, making Sybil attacks economically costly.
3. **Document the known limitation** in the swap proto and `validate_participation_constraints` so SNS deployers understand the cap is principal-scoped, not identity-scoped. [8](#0-7) 

---

### Proof of Concept

**Setup**: SNS swap with `min_participants = 100`, `max_participant_icp_e8s = 10 ICP`, `max_direct_participation_icp_e8s = 1000 ICP`.

**Attack**:
1. Attacker generates 100 fresh IC principals `P_1 … P_100`.
2. For each `P_i`, attacker transfers 10 ICP to the swap canister subaccount `principal_to_subaccount(P_i)`.
3. For each `P_i`, attacker calls `refresh_buyer_token_e8s(P_i, …)`.
4. Each call independently passes the `max_participant_icp_e8s` check (old balance = 0, new balance = 10 ICP ≤ 10 ICP).
5. After 100 calls, `self.buyers.len() == 100 == min_participants` and `buyers_total == 1000 ICP == max_direct_participation_icp_e8s`.
6. The swap immediately commits. The attacker — a single entity — receives 100% of the SNS tokens distributed in the swap, while the swap canister reports 100 "distinct" participants. [9](#0-8) [10](#0-9)

### Citations

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L46-53)
```text
// The `swap` canister smart contract is used to perform a type of
// single-price auction (SNS/ICP) of one token type SNS for another token
// type ICP (this is typically ICP, but can be treated as a variable) at a
// specific date/time in the future.
//
// Such a single-price auction is typically used to decentralize an SNS,
// i.e., to ensure that a sufficient number of governance tokens of the
// SNS are distributed among different participants.
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L376-380)
```text
  // The maximum amount of ICP that each buyer can contribute. Must be
  // greater than or equal to `min_participant_icp_e8s` and less than
  // or equal to `max_icp_e8s`. Can effectively be disabled by
  // setting it to `max_icp_e8s`.
  optional uint64 max_participant_icp_e8s = 21;
```

**File:** rs/sns/swap/src/swap.rs (L1134-1140)
```rust
    pub async fn refresh_buyer_token_e8s(
        &mut self,
        buyer: PrincipalId,
        confirmation_text: Option<String>,
        this_canister: CanisterId,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> Result<RefreshBuyerTokensResponse, String> {
```

**File:** rs/sns/swap/src/swap.rs (L1180-1198)
```rust
        {
            let num_direct_participants = self.buyers.len() as u64;
            let num_sns_neurons_per_basket = params
                .neuron_basket_construction_parameters
                .as_ref()
                .expect("neuron_basket_construction_parameters must be specified")
                .count;
            if (num_direct_participants + 1) * num_sns_neurons_per_basket
                > MAX_NEURONS_FOR_DIRECT_PARTICIPANTS
            {
                return Err(format!(
                    "The swap has reached the maximum number of direct participants ({num_direct_participants}) and does \
                     not accept new participants; existing participants may still increase their \
                     ICP participation amount. This constraint ensures that SNS neuron baskets can \
                     be created for all existing participants (SNS neuron basket size: {num_sns_neurons_per_basket}, \
                     MAX_NEURONS_FOR_DIRECT_PARTICIPANTS: {MAX_NEURONS_FOR_DIRECT_PARTICIPANTS}).",
                ));
            }
        }
```

**File:** rs/sns/swap/src/swap.rs (L1208-1237)
```rust
        let max_participant_icp_e8s = params.max_participant_icp_e8s;

        let old_amount_icp_e8s = self
            .buyers
            .get(&buyer.to_string())
            .map_or(0, |buyer| buyer.amount_icp_e8s());

        if old_amount_icp_e8s >= e8s {
            // Already up-to-date. Strict inequality can happen if messages are re-ordered.
            return Ok(RefreshBuyerTokensResponse {
                icp_accepted_participation_e8s: old_amount_icp_e8s,
                icp_ledger_account_balance_e8s: e8s,
            });
        }
        // Subtraction safe because of the preceding if-statement.
        let requested_increment_e8s = e8s - old_amount_icp_e8s;
        let actual_increment_e8s = std::cmp::min(max_increment_e8s, requested_increment_e8s);
        let new_balance_e8s = old_amount_icp_e8s.saturating_add(actual_increment_e8s);
        if new_balance_e8s > max_participant_icp_e8s {
            log!(
                INFO,
                "Participant {} contributed {} e8s - the limit per participant is {}",
                buyer,
                new_balance_e8s,
                max_participant_icp_e8s
            );
        }

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

**File:** rs/sns/init/src/lib.rs (L1524-1525)
```rust
    /// - `max_participant_icp_e8s`          - Maximum ICP amount from one participant.
    /// - `min_participants`                 - Required number of *direct* participants for the swap to succeed. This does not restrict the number of *Neurons' Fund* participants.
```

**File:** rs/sns/init/src/lib.rs (L1530-1546)
```rust
    fn validate_participation_constraints(&self) -> Result<(), String> {
        // (1)
        let min_direct_participation_icp_e8s = self
            .min_direct_participation_icp_e8s
            .ok_or("Error: min_direct_participation_icp_e8s must be specified")?;

        let max_direct_participation_icp_e8s = self
            .max_direct_participation_icp_e8s
            .ok_or("Error: max_direct_participation_icp_e8s must be specified")?;

        let min_participant_icp_e8s = self
            .min_participant_icp_e8s
            .ok_or("Error: min_participant_icp_e8s must be specified")?;

        let max_participant_icp_e8s = self
            .max_participant_icp_e8s
            .ok_or("Error: max_participant_icp_e8s must be specified")?;
```
