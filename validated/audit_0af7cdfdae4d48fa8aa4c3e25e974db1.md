### Title
SNS Tokens Deposited to Swap Canister Remain Locked After Aborted Sale - (File: rs/sns/swap/src/swap.rs)

### Summary
When an SNS decentralization swap is aborted, the SNS tokens pre-loaded into the Swap canister for distribution to buyers remain permanently locked in the Swap canister's account on the SNS ledger. The `finalize_inner` aborted path refunds buyer ICP but returns early without calling `sweep_sns`, and no built-in recovery mechanism exists to return these tokens to the SNS governance treasury.

### Finding Description
The SNS Swap canister implements a token sale where SNS tokens are deposited into the Swap canister's default account on the SNS ledger at initialization (`SwapDistribution.initial_swap_amount_e8s`), held in escrow for distribution to buyers upon a successful swap.

`finalize_inner` in `rs/sns/swap/src/swap.rs` handles two lifecycle paths:

**Committed path** (lines 1556–1623): calls `sweep_icp` (ICP → SNS governance), `settle_neurons_fund_participation`, `create_sns_neuron_recipes`, **`sweep_sns`** (SNS tokens → buyers), `claim_swap_neurons`, `set_sns_governance_to_normal_mode`.

**Aborted path** (lines 1556–1583): calls `sweep_icp` (ICP → buyers), `settle_neurons_fund_participation`, then hits `should_restore_dapp_control()` which returns `true` when `lifecycle() == Lifecycle::Aborted` (line 1348–1350), restores dapp controllers, and **returns early** — `sweep_sns` is never called. [1](#0-0) [2](#0-1) 

The `error_refund_icp` function (lines 1925–2031) only handles ICP refunds from buyer subaccounts, not SNS tokens from the Swap canister's default account. The SNS governance canister's `TransferSnsTreasuryFunds` proposal can only transfer from the governance canister's own treasury accounts, not from the Swap canister's account on the SNS ledger. [3](#0-2) 

This is explicitly confirmed by the integration test in `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs` which asserts that after an aborted swap, `swap_canister_balance_sns_e8s == swap_distribution_sns_e8s` — the full SNS token allocation remains locked in the Swap canister: [4](#0-3) 

The protocol documentation in `swap.proto` confirms the aborted path only addresses ICP: *"On a call to `finalize`, participants get their ICP refunded"* — no mention of SNS token recovery. [5](#0-4) 

The SNS token allocation is set at SNS genesis via `SwapDistribution.initial_swap_amount_e8s` and deposited to the Swap canister's account: [6](#0-5) 

### Impact Explanation
Every SNS decentralization swap that reaches `Lifecycle::Aborted` permanently locks the full `swap_distribution_sns_e8s` allocation of SNS tokens in the Swap canister's account on the SNS ledger. These tokens cannot be recovered via any built-in canister method. The SNS DAO's only recourse is to submit a governance proposal to upgrade the Swap canister with a custom recovery function — a multi-day governance process with no guarantee of passage. For a failed SNS launch, this means the entire token allocation intended for public distribution is stranded, reducing the effective circulating supply and treasury flexibility of the SNS.

### Likelihood Explanation
Any SNS swap that fails to reach `min_participants` or `min_icp_e8s` before `swap_due_timestamp_seconds` transitions to `Lifecycle::Aborted` automatically via the canister heartbeat. This is a normal, expected protocol outcome — not an edge case. Any unprivileged user can trigger this outcome simply by not participating (or participating insufficiently), making the entry path fully reachable without any privileged access.

### Recommendation
Add a `sweep_sns` call in the aborted path of `finalize_inner`, transferring the remaining SNS token balance from the Swap canister's account back to the SNS governance treasury subaccount (analogous to how `sweep_icp` sends ICP to `sns_governance` in the committed path). The destination is already known from `init.sns_governance_canister_id`. Alternatively, expose a dedicated `recover_sns_tokens` instruction callable only by the SNS governance canister, mirroring the fix described in the external report (`withdraw_funds` instruction).

### Proof of Concept
1. An SNS is deployed with `swap_distribution.initial_swap_amount_e8s = 200_000 * E8` SNS tokens deposited to the Swap canister's default account on the SNS ledger.
2. The swap opens (`Lifecycle::Open`). Buyers participate but total ICP falls below `min_icp_e8s` before `swap_due_timestamp_seconds`.
3. The canister heartbeat calls `try_abort`, transitioning to `Lifecycle::Aborted`.
4. `finalize_swap` is called (manually or via `should_auto_finalize`).
5. `finalize_inner` calls `sweep_icp` — ICP is refunded to buyers. ✓
6. `should_restore_dapp_control()` returns `true` (lifecycle == Aborted).
7. `restore_dapp_controllers_for_finalize` is called; function returns early.
8. `sweep_sns` is **never called**.
9. The Swap canister's SNS ledger balance remains at `swap_distribution_sns_e8s`.
10. `error_refund_icp` cannot recover SNS tokens. `TransferSnsTreasuryFunds` cannot reach the Swap canister's account. The tokens are locked with no built-in recovery path.

### Citations

**File:** rs/sns/swap/src/swap.rs (L1348-1350)
```rust
    pub fn should_restore_dapp_control(&self) -> bool {
        self.lifecycle() == Lifecycle::Aborted
    }
```

**File:** rs/sns/swap/src/swap.rs (L1572-1584)
```rust
        if self.should_restore_dapp_control() {
            // Restore controllers of dapp canisters to their original
            // owners (i.e. self.init.fallback_controller_principal_ids).
            finalize_swap_response.set_set_dapp_controllers_result(
                self.restore_dapp_controllers_for_finalize(environment.sns_root_mut())
                    .await,
            );

            // In the case of returning control of the dapp(s) to the fallback
            // controllers, finalize() need not do any more work, so always return
            // and end execution.
            return finalize_swap_response;
        }
```

**File:** rs/sns/swap/src/swap.rs (L1925-1936)
```rust
    pub async fn error_refund_icp(
        &self,
        self_canister_id: CanisterId,
        request: &ErrorRefundIcpRequest,
        icp_ledger: &dyn ICRC1Ledger,
    ) -> ErrorRefundIcpResponse {
        // Fail if the request is premature.
        if !(self.lifecycle() == Lifecycle::Aborted || self.lifecycle() == Lifecycle::Committed) {
            return ErrorRefundIcpResponse::new_precondition_error(
                "Error refunds can only be performed when the swap is ABORTED or COMMITTED",
            );
        }
```

**File:** rs/nervous_system/integration_tests/tests/sns_lifecycle.rs (L1336-1342)
```rust
        if swap_finalization_status == SwapFinalizationStatus::Aborted {
            // If the swap fails, the SNS swap does not distribute any tokens.
            assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
        } else {
            // In a happy scenario, the SNS swap distributes all the tokens.
            assert_eq!(swap_canister_balance_sns_e8s, 0);
        }
```

**File:** rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto (L40-43)
```text
  // In ABORTED state the token swap has been aborted, e.g., because the due
  // date/time occurred before the minimum (reserve) amount of ICP has been
  // retrieved. On a call to `finalize`, participants get their ICP refunded.
  LIFECYCLE_ABORTED = 4;
```

**File:** rs/sns/init/proto/ic_sns_init/pb/v1/sns_init.proto (L287-296)
```text
message SwapDistribution {
  // The total token distribution denominated in e8s (10E-8 of a token) of the
  // swap bucket. All tokens used in initial_swap_amount_e8s will be
  // deducted from total_e8s. The remaining tokens will be distributed to
  // a subaccount of Governance for use in future token swaps.
  uint64 total_e8s = 1;
  // The initial number of tokens denominated in e8s (10E-8 of a token)
  // deposited in the swap canister's account for the initial token swap.
  uint64 initial_swap_amount_e8s = 2;
}
```
