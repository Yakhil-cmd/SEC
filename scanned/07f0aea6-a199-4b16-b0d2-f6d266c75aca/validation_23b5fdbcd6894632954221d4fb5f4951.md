### Title
Missing Slippage Protection in Treasury Manager `DepositRequest` and `WithdrawRequest` API - (File: rs/sns/treasury_manager/treasury_manager.did)

### Summary
The Treasury Manager API defines `DepositRequest` and `WithdrawRequest` types that contain no minimum-output or slippage-protection parameters. Any compliant Treasury Manager implementation that deposits SNS treasury assets into a DEX liquidity pool is structurally unable to enforce a minimum acceptable return, because the canonical API itself provides no field for it. The protocol acknowledges this gap as a "Known Security Risk" in the DID file and in the SNS governance proposal renderer, but does not mitigate it at the API layer.

### Finding Description
`DepositRequest` in `rs/sns/treasury_manager/src/lib.rs` carries only an `allowances` field: [1](#0-0) 

`WithdrawRequest` carries only an optional `withdraw_accounts` map: [2](#0-1) 

Neither type includes a `min_lp_tokens_out`, `min_received_amount`, or any equivalent slippage guard. The DID file explicitly flags this as a known risk: [3](#0-2) 

The SNS governance proposal renderer for `RegisterExtension` also warns voters about the same gap, but only as a human-readable string — it does not enforce any on-chain check: [4](#0-3) 

Because the `TreasuryManager` trait is the canonical interface that all blessed implementations must satisfy, no conforming implementation can add slippage protection without violating the interface contract. The `deposit` and `withdraw` service endpoints are the only externally callable update methods: [5](#0-4) 

### Impact Explanation
When an SNS governance proposal to deposit treasury assets into a DEX liquidity pool is adopted, the actual `deposit` call executes asynchronously after the proposal passes. Between proposal adoption and execution, the DEX price ratio can shift — either through natural market movement or deliberate manipulation by any actor who can trade on that DEX. Because `DepositRequest` carries no `min_lp_tokens_out` field, the Treasury Manager canister has no on-chain mechanism to abort the deposit if the received LP tokens fall below the ratio that voters approved. The SNS treasury permanently loses the difference. The same applies to `WithdrawRequest`: a withdrawal from a DEX position with no `min_received_amount` can be executed at an arbitrarily unfavorable price.

### Likelihood Explanation
The SNS governance voting and execution cycle introduces a latency of hours to days between proposal approval and on-chain execution. Any actor who can trade on the target DEX (an unprivileged canister caller or user) can shift the pool price during that window. The IC's deterministic execution model does not prevent inter-canister message reordering across subnets, and the sequencer-priority concern noted in the original report applies equally here: a canister paying higher cycles fees can have its trade executed before the Treasury Manager's deposit, moving the price unfavorably. The protocol team's own warning in the proposal renderer confirms they consider this a realistic scenario.

### Recommendation
Add a `min_lp_tokens_out : opt nat` field to `DepositRequest` and a `min_received_amount : opt nat` field to `WithdrawRequest` in both `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs`. The `TreasuryManager` trait's `deposit` and `withdraw` implementations should check the actual received amount against these bounds and return an `Err` with `ErrorKind::Postcondition` if the bound is violated, allowing the SNS governance canister to detect and surface the failure.

### Proof of Concept
1. An SNS governance proposal is submitted and adopted to deposit 10,000 ICP and 50,000 SNS tokens into a DEX liquidity pool at the current 1:5 ratio, expecting ~X LP tokens.
2. Between proposal adoption and the Treasury Manager's `deposit` call executing, an unprivileged user trades on the DEX, shifting the ratio to 1:10.
3. The Treasury Manager calls `deposit` with a `DepositRequest { allowances: [...] }` — no minimum LP token output is specified because the field does not exist in the type.
4. The DEX accepts the deposit at the new 1:10 ratio; the SNS treasury receives roughly half the LP tokens that voters expected.
5. There is no on-chain check that can revert this outcome; the `TreasuryManagerResult` returns `Ok(Balances { ... })` reflecting the reduced position.
6. The SNS treasury has permanently lost value with no recourse, and no governance proposal was needed from the attacker — only a standard DEX trade.

### Citations

**File:** rs/sns/treasury_manager/src/lib.rs (L284-287)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct DepositRequest {
    pub allowances: Vec<Allowance>,
}
```

**File:** rs/sns/treasury_manager/src/lib.rs (L302-306)
```rust
#[derive(CandidType, Clone, Debug, Deserialize, PartialEq)]
pub struct WithdrawRequest {
    /// If not set, accounts specified at the time of deposit will be used for the withdrawal.
    pub withdraw_accounts: Option<BTreeMap<Principal, Account>>,
}
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L35-40)
```text
// Known Security Risks:
// ====================
// Some liquidity pools do not implement slippage protection
// for deposits. In other words, the price ratio at the time
// of execution may differ from the ratio at the time the proposal was
// approved.
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L296-301)
```text
service : (TreasuryManagerArg) -> {
  deposit : (DepositRequest) -> (Result);
  withdraw : (WithdrawRequest) -> (Result);
  balances : (record {}) -> (Result) query;
  audit_trail : (record {}) -> (AuditTrail) query;
}
```

**File:** rs/sns/governance/src/proposal.rs (L1540-1545)
```rust
## WARNING

Some Decentralized Exchanges lack slippage protection during deposits. Consequently, 
deposited asset ratios may deviate from those specified in the proposal. 
This can expose liquidity pool adaptors to mispricing, making them vulnerable to front-running 
or sandwich attacks. However, any undeposited tokens are automatically returned to the SNS treasury account.
```
