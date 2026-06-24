### Title
Missing Slippage Protection in SNS TreasuryManager Deposit/Withdraw API - (File: rs/sns/treasury_manager/treasury_manager.did)

### Summary

The `TreasuryManager` API, which governs how SNS DAOs deposit and withdraw treasury assets into external DEX liquidity pools (e.g., KongSwap), provides no slippage protection parameters in its `deposit` or `withdraw` operations. The price ratio at execution time can differ arbitrarily from the ratio at the time the governance proposal was approved, resulting in a loss of SNS treasury assets. This risk is explicitly acknowledged in the production interface file but left unmitigated at the API level.

### Finding Description

The `TreasuryManager` interface is defined in `rs/sns/treasury_manager/treasury_manager.did` and its Rust trait in `rs/sns/treasury_manager/src/lib.rs`. It is the standard contract that all NNS-blessed SNS treasury extension canisters (e.g., the KongSwap Adaptor) must implement.

The `DepositRequest` type contains only `allowances` — the amounts to deposit — with no `min_lp_tokens_out`, `min_amount0`, `min_amount1`, or any equivalent slippage bound: [1](#0-0) 

The `WithdrawRequest` type similarly contains only optional destination accounts, with no minimum token amounts to receive back: [2](#0-1) 

The Rust trait mirrors this exactly — `deposit` and `withdraw` accept these parameter-less structs with no slippage fields: [3](#0-2) 

The production `.did` file itself explicitly acknowledges this as a known security risk: [4](#0-3) 

The KongSwap Adaptor (`kongswap-adaptor-canister`) is the concrete NNS-blessed implementation of this interface, used in integration tests that exercise real deposit and withdrawal flows against a KongSwap backend canister: [5](#0-4) 

### Impact Explanation

An SNS DAO submits a governance proposal to deposit treasury assets (e.g., SNS tokens + ICP) into a KongSwap liquidity pool. The proposal is voted on and approved. Between approval and execution — or during the async execution window — a malicious actor manipulates the DEX pool price by front-running. The `deposit` call executes at the manipulated price ratio, causing the SNS treasury to receive significantly fewer LP tokens than expected. The attacker back-runs to extract the value difference. Because `DepositRequest` carries no `min_lp_out` or price-deviation bound, the TreasuryManager canister has no on-chain mechanism to abort the deposit when the price has moved adversarially. The same applies to `withdraw`: the SNS treasury can receive fewer tokens back than the LP position is worth at the time the proposal was approved.

### Likelihood Explanation

The KongSwap Adaptor is already deployed and NNS-blessed. Any SNS DAO that registers it and submits a deposit proposal is exposed. The attack requires only the ability to trade on KongSwap — an unprivileged, permissionless action. The window between proposal adoption and execution is publicly observable on-chain, making front-running straightforward. The integration test at `rs/nervous_system/integration_tests/tests/sns_extension_test.rs` already demonstrates that the deposit ratio can differ from the market ratio (the comment at line 454–456 notes "the excess amount of ICP is returned to the treasury owner"), confirming the price-ratio mismatch is a live, exercised code path. [6](#0-5) 

### Recommendation

1. Add `min_lp_tokens_out` (or equivalent `min_amount0` / `min_amount1`) fields to `DepositRequest` and `WithdrawRequest` in `rs/sns/treasury_manager/treasury_manager.did` and `rs/sns/treasury_manager/src/lib.rs`.
2. Require all NNS-blessed TreasuryManager implementations to enforce these bounds before submitting liquidity calls to the underlying DEX.
3. SNS governance proposals that invoke `deposit` or `withdraw` should include the slippage bounds as part of the proposal payload so token holders can evaluate the acceptable price range at vote time.

### Proof of Concept

1. An SNS DAO with the KongSwap Adaptor registered submits a `deposit` proposal allocating 100 ICP + 200 SNS tokens to the KongSwap ICP/SNS pool.
2. The proposal is adopted. The execution is queued.
3. An attacker observes the pending execution and swaps a large amount of ICP into the pool, moving the SNS/ICP price significantly against the DAO.
4. The KongSwap Adaptor's `deposit` call executes. Because `DepositRequest` carries no slippage bound, the adaptor deposits at the manipulated price, receiving far fewer LP tokens than the fair-market value of the deposited assets.
5. The attacker swaps back, extracting the price impact as profit. The SNS treasury has permanently lost value with no on-chain recourse. [4](#0-3) [7](#0-6)

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

**File:** rs/sns/treasury_manager/treasury_manager.did (L84-86)
```text
type DepositRequest = record {
  allowances : vec Allowance;
};
```

**File:** rs/sns/treasury_manager/treasury_manager.did (L88-93)
```text
type WithdrawRequest = record {
  // Maps Ledger canister IDs of assets to be withdrawn to the respective withdraw accounts.
  //
  // If not set, accounts specified at the time of deposit will be used for the withdrawal.
  withdraw_accounts : opt vec record { principal; Account };
};
```

**File:** rs/sns/treasury_manager/src/lib.rs (L250-261)
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

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L365-397)
```rust
    // Testing the top-up deposit operation.
    {
        let proposal = Proposal {
            title: "Test top-up deposit".to_string(),
            summary: "test".to_string(),
            url: "https://example.com".to_string(),
            action: Some(Action::ExecuteExtensionOperation(
                ExecuteExtensionOperation {
                    extension_canister_id: Some(extension_canister_id),
                    operation_name: Some("deposit".to_string()),
                    operation_arg: Some(ExtensionOperationArg {
                        value: make_deposit_allowances(
                            topup_treasury_allocation_icp_e8s,
                            topup_treasury_allocation_sns_e8s,
                        ),
                    }),
                },
            )),
        };

        let proposal_data = propose_and_wait(
            &pocket_ic,
            sns.governance.canister_id,
            sender,
            neuron_id.clone(),
            proposal.clone(),
        )
        .await
        .unwrap();

        assert_eq!(proposal_data.failure_reason, None);
        assert!(proposal_data.executed_timestamp_seconds > 0);
    }
```

**File:** rs/nervous_system/integration_tests/tests/sns_extension_test.rs (L453-457)
```rust
        let expected_sns_fee_collector = 8 * SNS_FEE;
        // Second deposit takes place with deposit ratio (SNS/ICP)
        // lower than the market ratio (SNS/ICP in the pool). Hence,
        // the excess amount of ICP is returned to the treasury owner.
        let expected_icp_fee_collector = 9 * ICP_FEE;
```
