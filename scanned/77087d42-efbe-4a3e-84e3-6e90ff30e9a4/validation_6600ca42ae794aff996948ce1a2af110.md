### Title
SNS Treasury Manager `deposit`/`withdraw` API Missing Slippage Control — (`rs/sns/treasury_manager/treasury_manager.did`, `rs/sns/treasury_manager/src/lib.rs`)

---

### Summary

The SNS Treasury Manager API defines `deposit` and `withdraw` operations that interact with external DEX/liquidity-pool canisters. Neither `DepositRequest` nor `WithdrawRequest` includes a minimum-output (slippage tolerance) parameter. The DID file itself explicitly flags this as a "Known Security Risk." Any conforming implementation is structurally prevented from enforcing caller-specified slippage, leaving SNS DAOs open to sandwich attacks when depositing into or withdrawing from on-chain liquidity pools.

---

### Finding Description

The `DepositRequest` type carries only `allowances` (the amount to deposit) and `WithdrawRequest` carries only `withdraw_accounts` (where to send proceeds). Neither type has a `min_shares_out`, `min_tokens_out`, or equivalent field.

```
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
``` [1](#0-0) 

The Candid service exposes `deposit : (DepositRequest) -> (Result)` and `withdraw : (WithdrawRequest) -> (Result)` with no slippage field in either argument type. [2](#0-1) 

The Rust structs mirror this exactly — `DepositRequest` holds only `allowances` and `WithdrawRequest` holds only `withdraw_accounts`: [3](#0-2) 

The `TreasuryManager` trait's `deposit` and `withdraw` signatures accept only these stripped-down request types, so no conforming implementation can surface a slippage bound to the SNS governance caller: [4](#0-3) 

---

### Impact Explanation

An SNS DAO approves a governance proposal to deposit treasury tokens into a DEX liquidity pool via a Treasury Manager. Because the `DepositRequest` carries no `min_lp_shares_out`, the Treasury Manager cannot reject the DEX call even if the pool ratio has moved adversely. The DAO receives fewer LP shares than expected (deposit) or fewer tokens than expected (withdrawal), with no on-chain recourse. In the worst case a sandwich attacker captures the difference as profit at the DAO's expense — a direct theft of unclaimed yield / loss of treasury value.

---

### Likelihood Explanation

SNS DAOs are the intended users of this API; governance proposals to deposit/withdraw treasury assets are publicly visible on-chain before execution. Any canister or user watching the IC mempool/governance can observe the pending call and front-run the DEX interaction. No privileged access is required — the attacker only needs to interact with the same DEX canister before the Treasury Manager's inter-canister call lands.

---

### Recommendation

1. Add a `min_amount_out : opt nat` (or per-asset equivalent) field to both `DepositRequest` and `WithdrawRequest` in `treasury_manager.did` and the corresponding Rust structs in `src/lib.rs`.
2. Require conforming implementations to pass this bound to the underlying DEX call and return an error (e.g., `ErrorKind::Postcondition`) if the received amount falls below the minimum.
3. Remove or update the "Known Security Risks" comment once the fix is in place.

---

### Proof of Concept

1. SNS governance submits a proposal to call `deposit({ allowances: [{ asset: Token{...}, amount_decimals: 1_000_000, owner_account: ... }] })` on a Treasury Manager backed by an AMM DEX canister.
2. The proposal is publicly visible. An attacker observes it and, before the Treasury Manager's inter-canister `deposit` call to the DEX executes, swaps a large amount into the pool, moving the price.
3. The Treasury Manager's `deposit` call executes at the manipulated price; the DAO receives significantly fewer LP shares than the pre-proposal price implied.
4. The attacker immediately swaps back, profiting from the spread. The DAO has no recourse because no minimum-output check exists in the API or any conforming implementation.

### Citations

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-93)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};

type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```

**File:** rs/sns/treasury_manager/src/lib.rs (L250-262)
```rust
pub trait TreasuryManager {
    /// Implements the `deposit` API function.
    fn deposit(
        &mut self,
        request: DepositRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

    /// Implements the `withdraw` API function.
    fn withdraw(
        &mut self,
        request: WithdrawRequest,
    ) -> impl std::future::Future<Output = TreasuryManagerResult> + Send;

```

**File:** rs/sns/treasury_manager/src/lib.rs (L284-306)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}

#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct BalancesRequest {}

pub type Subaccount = [u8; 32];

#[derive(CandidType, Clone, Copy, Derivative, Deserialize, Eq, Hash, PartialEq, Serialize)]
#[derivative(Debug)]
pub struct Account {
    #[derivative(Debug(format_with = "fmt_principal_as_string"))]
    pub owner: Principal,
    pub subaccount: Option<Subaccount>,
}

#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct WithdrawRequest {
    /// If not set, accounts specified at the time of deposit will be used for the withdrawal.
    pub withdraw_accounts: Option<BTreeMap<Principal, Account>>,
}
```
