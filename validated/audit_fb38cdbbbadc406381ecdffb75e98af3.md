Audit Report

## Title
SNS Swap Canister Has No SNS Token Sweep-Back on Aborted Swap — (File: rs/sns/swap/src/swap.rs)

## Summary
When an SNS decentralization swap is aborted, `finalize_inner` unconditionally returns early after restoring dapp controllers, leaving the full `swap_distribution_sns_e8s` SNS token allocation stranded in the swap canister's ledger account. No automatic sweep-back to the SNS governance treasury is performed. The integration test suite explicitly asserts this stranded state as the expected post-abort outcome, confirming the behavior is real and currently unmitigated at the code level.

## Finding Description
`should_restore_dapp_control` returns `true` if and only if `self.lifecycle() == Lifecycle::Aborted`:

```rust
// rs/sns/swap/src/swap.rs L1348-1350
pub fn should_restore_dapp_control(&self) -> bool {
    self.lifecycle() == Lifecycle::Aborted
}
```

Inside `finalize_inner`, this guard triggers an unconditional early return:

```rust
// rs/sns/swap/src/swap.rs L1572-1584
if self.should_restore_dapp_control() {
    finalize_swap_response.set_set_dapp_controllers_result(
        self.restore_dapp_controllers_for_finalize(environment.sns_root_mut()).await,
    );
    // "finalize() need not do any more work, so always return"
    return finalize_swap_response;
}
```

The `sweep_sns` call at L1593-1598 is only reachable when `should_restore_dapp_control()` is false — i.e., only for `Lifecycle::Committed`. Additionally, `sweep_sns` itself hard-rejects any non-Committed lifecycle at L2170-2178, providing a second layer of confirmation that no SNS token transfer occurs on abort.

The integration test at `rs/nervous_system/integration_tests/tests/sns_lifecycle.rs` L1336-1342 explicitly asserts:
```rust
if swap_finalization_status == SwapFinalizationStatus::Aborted {
    assert_eq!(swap_canister_balance_sns_e8s, swap_distribution_sns_e8s);
}
```
This confirms the full allocation remains in the swap canister after finalization of an aborted swap.

The lifecycle diagram in `rs/sns/swap/proto/ic_sns_swap/pb/v1/swap.proto` L65 shows `ABORTED → <DELETED>` as the terminal path. The proto documentation at L149-150 states "The `swap` canister can be deleted when all tokens registered with the `swap` canister have been disbursed to their rightful owners" — a condition that is never satisfied for SNS tokens on the abort path, yet deletion can still be triggered via governance.

## Impact Explanation
The entire SNS token swap allocation — potentially millions of governance tokens — is stranded in the swap canister's ledger account with no automatic recovery mechanism. The swap canister is the sole authorized signer for that account. If the swap canister is subsequently deleted (the documented terminal state for ABORTED), the tokens become permanently inaccessible on the SNS ledger. This constitutes a significant SNS ledger conservation failure with concrete protocol harm: tokens minted and transferred for a specific purpose are neither distributed nor returned when the distribution event fails. This matches the allowed impact class: "Significant SNS or infrastructure security impact with concrete user or protocol harm" — High severity.

Recovery is theoretically possible via an NNS upgrade proposal to the swap canister before deletion, but this requires NNS governance majority action and is not automatic. No code-level recovery path exists in the current implementation.

## Likelihood Explanation
Any SNS swap that fails to reach `min_participants` or `min_icp_e8s` before the deadline transitions automatically to `Aborted`. No attacker action is required; normal under-participation triggers the stranding. This is a documented, realistic lifecycle path. The subsequent deletion step requires a governance action, but the stranding itself is fully automatic and unprivileged.

## Recommendation
In `finalize_inner`, within the `should_restore_dapp_control()` branch (i.e., the Aborted path), add a step to transfer the remaining SNS token balance from the swap canister's account back to the SNS governance treasury account before returning. This mirrors the existing `sweep_icp` pattern used to refund ICP to buyers. The transfer should be idempotent (guarded by a flag analogous to the existing transfer timestamp guards) so that retried `finalize` calls do not double-transfer.

## Proof of Concept
1. Deploy an SNS; its swap canister is loaded with `N` SNS tokens.
2. Open the swap; allow it to expire without reaching `min_participants`.
3. `try_abort` transitions the swap to `Lifecycle::Aborted`.
4. Call `finalize`. `finalize_inner` executes: `sweep_icp` (ICP refunded), `settle_neurons_fund_participation` (NF maturity refunded), `restore_dapp_controllers_for_finalize` (dapp control restored), then returns early at L1583.
5. Query the SNS ledger: `icrc1_balance_of(swap_canister_account)` returns `N` (unchanged), confirmed by the integration test assertion at `sns_lifecycle.rs` L1338.
6. No callable method on the swap canister in its current code will authorize a transfer of those SNS tokens.
7. Submit a governance proposal to delete the swap canister; upon deletion, the `N` SNS tokens are permanently inaccessible.