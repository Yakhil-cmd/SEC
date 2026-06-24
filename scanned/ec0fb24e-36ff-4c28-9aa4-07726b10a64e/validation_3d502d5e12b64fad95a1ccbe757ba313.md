### Title
Wrong Event Type Emitted for ckERC20 Withdrawal Failure Reimbursement in ckETH Minter - (File: rs/ethereum/cketh/minter/src/state/audit.rs)

---

### Summary

When a ckERC20 withdrawal fails at the ckERC20 ledger burn stage (before any Ethereum transaction is created), the ckETH minter records the pending reimbursement under `ReimbursementIndex::CkEth` instead of `ReimbursementIndex::CkErc20`. This causes the subsequent reimbursement processing to emit `EventType::ReimbursedEthWithdrawal` — the event type for a failed ckETH withdrawal — instead of `EventType::ReimbursedErc20Withdrawal`. The wrong event is persisted in the stable event log and exposed via the public `get_events` query endpoint, making it impossible for off-chain services to distinguish ckERC20 withdrawal failure reimbursements from genuine ckETH withdrawal reimbursements.

---

### Finding Description

**Root cause — `apply_state_transition` in `audit.rs`:**

When `EventType::FailedErc20WithdrawalRequest` is processed, the reimbursement request is stored under `ReimbursementIndex::CkEth`:

```rust
// rs/ethereum/cketh/minter/src/state/audit.rs  lines 146-153
EventType::FailedErc20WithdrawalRequest(cketh_reimbursement_request) => {
    state.eth_transactions.record_reimbursement_request(
        ReimbursementIndex::CkEth {                          // ← wrong variant
            ledger_burn_index: cketh_reimbursement_request.ledger_burn_index,
        },
        cketh_reimbursement_request.clone(),
    )
}
``` [1](#0-0) 

`ReimbursementIndex::CkErc20` carries three fields (`cketh_ledger_burn_index`, `ledger_id`, `ckerc20_ledger_burn_index`), while `ReimbursementIndex::CkEth` carries only one (`ledger_burn_index`). By using the `CkEth` variant, the ckERC20 ledger canister ID and the ckERC20 burn index are discarded. [2](#0-1) 

**Downstream consequence — `process_reimbursement` in `withdraw.rs`:**

When the reimbursement is later processed, the event to emit is chosen by matching on the `ReimbursementIndex`:

```rust
// rs/ethereum/cketh/minter/src/withdraw.rs  lines 124-137
let event = match index {
    ReimbursementIndex::CkEth { .. } =>
        EventType::ReimbursedEthWithdrawal(reimbursed),   // ← emitted for ckERC20 failure
    ReimbursementIndex::CkErc20 { cketh_ledger_burn_index, ledger_id, .. } =>
        EventType::ReimbursedErc20Withdrawal { ... },
};
``` [3](#0-2) 

Because the index was stored as `CkEth`, `ReimbursedEthWithdrawal` is emitted. The `ReimbursedEthWithdrawal` event payload lacks the `ledger_id` (ckERC20 ledger canister ID) field that `ReimbursedErc20Withdrawal` carries: [4](#0-3) 

**Trigger path — `withdraw_erc20` in `main.rs`:**

The `FailedErc20WithdrawalRequest` event is emitted when the ckERC20 ledger burn fails after the ckETH gas-fee burn has already succeeded: [5](#0-4) 

**Test confirmation:**

The integration test in `ckerc20.rs` explicitly asserts that `ReimbursedEthWithdrawal` is emitted for a ckERC20 withdrawal failure, confirming this is the live behavior: [6](#0-5) 

---

### Impact Explanation

1. **Event log ambiguity:** The public `get_events` query endpoint exposes `ReimbursedEthWithdrawal` for what is actually a ckERC20 withdrawal failure reimbursement. Off-chain services (dashboards, wallets, indexers) cannot distinguish between the two cases by event type alone.
2. **Missing ckERC20 ledger identity:** `ReimbursedEthWithdrawal` does not carry `ledger_id`. Off-chain consumers therefore cannot determine which ckERC20 token was involved in the failed withdrawal, breaking per-token accounting and filtering.
3. **Incorrect event counts:** Any service that counts `ReimbursedEthWithdrawal` events to track ckETH reimbursements will over-count, and any service that counts `ReimbursedErc20Withdrawal` events to track ckERC20 reimbursements will under-count.

---

### Likelihood Explanation

The trigger is any `withdraw_erc20` call where the ckERC20 ledger burn fails — a realistic scenario reachable by any unprivileged user (e.g., insufficient ckERC20 balance, insufficient allowance, or a temporarily unavailable ckERC20 ledger). No privileged access is required. The minter is live on mainnet and processes real user withdrawals.

---

### Recommendation

In `apply_state_transition`, handle `FailedErc20WithdrawalRequest` with a dedicated `ReimbursementIndex` variant (or reuse `CkErc20` with the available ckETH burn index and ledger ID), so that `process_reimbursement` emits `ReimbursedErc20Withdrawal` (or a new `ReimbursedFailedErc20Withdrawal` event) instead of `ReimbursedEthWithdrawal`. The `FailedErc20WithdrawalRequest` event already carries the ckETH burn index; the ckERC20 ledger ID and ckERC20 burn index should also be stored so the correct event can be emitted downstream.

---

### Proof of Concept

1. User calls `withdraw_erc20` with a valid ckETH allowance but an insufficient ckERC20 allowance.
2. The minter burns the ckETH gas fee successfully (`cketh_ledger_burn_index = N`).
3. The ckERC20 burn fails; the minter emits `FailedErc20WithdrawalRequest { withdrawal_id: N, ... }`.
4. `apply_state_transition` stores the reimbursement under `ReimbursementIndex::CkEth { ledger_burn_index: N }`.
5. `process_reimbursement` matches `CkEth`, mints ckETH back to the user on the ckETH ledger (correct), and emits `ReimbursedEthWithdrawal { withdrawal_id: N, ... }` (wrong event type — no `ledger_id` field).
6. Any caller of `get_events` sees `ReimbursedEthWithdrawal` for a ckERC20 withdrawal failure, indistinguishable from a genuine ckETH withdrawal reimbursement.

### Citations

**File:** rs/ethereum/cketh/minter/src/state/audit.rs (L146-153)
```rust
        EventType::FailedErc20WithdrawalRequest(cketh_reimbursement_request) => {
            state.eth_transactions.record_reimbursement_request(
                ReimbursementIndex::CkEth {
                    ledger_burn_index: cketh_reimbursement_request.ledger_burn_index,
                },
                cketh_reimbursement_request.clone(),
            )
        }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L179-198)
```rust
#[derive(Clone, Eq, PartialEq, Ord, PartialOrd, Debug, Decode, Encode)]
pub enum ReimbursementIndex {
    #[n(0)]
    CkEth {
        /// Burn index on the ckETH ledger
        #[cbor(n(0), with = "crate::cbor::id")]
        ledger_burn_index: LedgerBurnIndex,
    },
    #[n(1)]
    CkErc20 {
        #[cbor(n(0), with = "crate::cbor::id")]
        cketh_ledger_burn_index: LedgerBurnIndex,
        /// The ckERC20 ledger canister ID identifying the ledger on which the burn to be reimbursed was made.
        #[cbor(n(1), with = "icrc_cbor::principal")]
        ledger_id: Principal,
        /// Burn index on the ckERC20 ledger
        #[cbor(n(2), with = "crate::cbor::id")]
        ckerc20_ledger_burn_index: LedgerBurnIndex,
    },
}
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L124-137)
```rust
        let event = match index {
            ReimbursementIndex::CkEth {
                ledger_burn_index: _,
            } => EventType::ReimbursedEthWithdrawal(reimbursed),
            ReimbursementIndex::CkErc20 {
                cketh_ledger_burn_index,
                ledger_id,
                ckerc20_ledger_burn_index: _,
            } => EventType::ReimbursedErc20Withdrawal {
                cketh_ledger_burn_index,
                ckerc20_ledger_id: ledger_id,
                reimbursed,
            },
        };
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L440-453)
```rust
        ReimbursedEthWithdrawal {
            reimbursed_in_block: Nat,
            withdrawal_id: Nat,
            reimbursed_amount: Nat,
            transaction_hash: Option<String>,
        },
        ReimbursedErc20Withdrawal {
            withdrawal_id: Nat,
            burn_in_block: Nat,
            reimbursed_in_block: Nat,
            ledger_id: Principal,
            reimbursed_amount: Nat,
            transaction_hash: Option<String>,
        },
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L506-531)
```rust
                Err(ckerc20_burn_error) => {
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
                    if reimbursed_amount > Wei::ZERO {
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
                        mutate_state(|s| {
                            process_event(
                                s,
                                EventType::FailedErc20WithdrawalRequest(reimbursement_request),
                            );
                        });
                    }
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L468-476)
```rust
            ckerc20
                .check_events()
                .assert_has_unique_events_in_order(&[EventPayload::ReimbursedEthWithdrawal {
                    withdrawal_id: cketh_burn_index.into(),
                    reimbursed_in_block: Nat::from(cketh_burn_index) + 1_u8,
                    reimbursed_amount: reimbursed_amount.clone(),
                    transaction_hash: None,
                }])
                .call_cketh_ledger_get_transaction(3_u8)
```
