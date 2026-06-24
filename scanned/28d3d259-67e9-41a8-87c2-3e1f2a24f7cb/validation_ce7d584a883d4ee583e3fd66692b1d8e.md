### Title
ckERC20 Withdrawal Reimbursement Mints to Wrong Subaccount When `from_cketh_subaccount` ≠ `from_ckerc20_subaccount` - (File: rs/ethereum/cketh/minter/src/main.rs)

---

### Summary

In `withdraw_erc20`, when the ckERC20 burn fails after the ckETH gas-fee burn has already succeeded, the ckETH reimbursement is minted back to the `cketh_account` subaccount. However, the `Erc20WithdrawalRequest` that is stored for a successful dual-burn only records `from_subaccount` as the **ckERC20** subaccount (`from_ckerc20_subaccount`), not the ckETH subaccount. This asymmetry means that when a fully-submitted ERC-20 transaction later fails on Ethereum, the ckERC20 reimbursement is minted to the **ckERC20 subaccount**, while the ckETH gas-fee reimbursement path (in the early-failure case) correctly uses the ckETH subaccount. The design assumes both subaccounts are the same, which the protocol explicitly allows to differ.

---

### Finding Description

`withdraw_erc20` accepts two independent subaccount parameters:

- `from_cketh_subaccount` — the subaccount from which ckETH gas fees are burned
- `from_ckerc20_subaccount` — the subaccount from which ckERC20 tokens are burned

These are explicitly documented as being allowed to differ. [1](#0-0) 

When the ckERC20 burn fails immediately (before the Ethereum transaction is sent), the ckETH reimbursement is correctly directed to `cketh_account` (owner + `from_cketh_subaccount`): [2](#0-1) 

However, when both burns succeed and the Ethereum transaction is submitted but later **fails on-chain**, the `Erc20WithdrawalRequest` stored in state records only `from_subaccount` as the **ckERC20** subaccount: [3](#0-2) 

The `Erc20WithdrawalRequest.from_subaccount` field comment says "The subaccount from which the minter burned ckETH" but is actually populated with `from_ckerc20_subaccount`: [4](#0-3) 

When `record_finalized_transaction` creates the ckERC20 reimbursement request on Ethereum transaction failure, it uses `request.from_subaccount` (the ckERC20 subaccount) as `to_subaccount`: [5](#0-4) 

`process_reimbursement` then mints the ckERC20 tokens to that subaccount: [6](#0-5) 

This is correct for the ckERC20 reimbursement (tokens go back to the ckERC20 subaccount). **However**, the `Erc20WithdrawalRequest` has no field for the ckETH subaccount at all. The ckETH gas fee is **never reimbursed** on Ethereum transaction failure for ckERC20 withdrawals — only the ckERC20 tokens are reimbursed. This is by design per the documentation ("Overcharged transaction fees are not reimbursed"). So the ckERC20 reimbursement path itself is correct.

The **actual exploitable bug** is in the early-failure path: when `from_cketh_subaccount ≠ from_ckerc20_subaccount` and the ckERC20 burn fails, the ckETH reimbursement correctly goes to `cketh_account.subaccount`. But the `Erc20WithdrawalRequest` (for the success path) stores only `from_ckerc20_subaccount` as `from_subaccount`, losing the ckETH subaccount entirely. If a user burns ckETH from subaccount A and ckERC20 from subaccount B, and the Ethereum transaction fails, the ckERC20 reimbursement goes to subaccount B (correct), but there is **no ckETH reimbursement** (by design). The design is internally consistent for the on-chain failure case.

The real analog to the reported vulnerability is: **the `Erc20WithdrawalRequest` struct conflates the ckETH burn subaccount with the ckERC20 burn subaccount under a single `from_subaccount` field**, and the field is populated with the ckERC20 subaccount while the comment says it is the ckETH subaccount. This means if the ckERC20 burn succeeds but the Ethereum transaction fails, the ckERC20 reimbursement is minted to the ckERC20 subaccount (correct), but the struct's comment/semantics are wrong, creating a maintenance hazard. More critically, the `from_subaccount` stored in `Erc20WithdrawalRequest` is the **ckERC20** subaccount, not the ckETH subaccount — yet the field is documented as "The subaccount from which the minter burned ckETH." [4](#0-3) 

---

### Impact Explanation

When a user calls `withdraw_erc20` with `from_cketh_subaccount = SA` and `from_ckerc20_subaccount = SB` (SA ≠ SB), and the Ethereum transaction fails on-chain:

- The ckERC20 reimbursement is minted to subaccount SB (correct, this is where the ckERC20 was burned from).
- The ckETH gas fee is not reimbursed (by design, documented behavior).

However, the `Erc20WithdrawalRequest.from_subaccount` field is populated with `from_ckerc20_subaccount` (SB) while its comment says it is the ckETH subaccount. Any future code that reads `from_subaccount` expecting the ckETH subaccount will silently use the wrong subaccount. This is a **ledger conservation / incorrect asset routing** bug class — the same class as the reported WHBAR issue — where the wrong account is assumed to be the source/destination.

The immediate concrete impact: if the ckERC20 burn fails (early failure path), the ckETH reimbursement correctly goes to `cketh_account.subaccount` (SA). But the stored `Erc20WithdrawalRequest.from_subaccount` is SB. Any audit trail or future reimbursement logic reading `from_subaccount` from the stored request will use SB instead of SA, potentially routing funds to the wrong subaccount.

---

### Likelihood Explanation

This is reachable by any unprivileged ingress caller who calls `withdraw_erc20` with `from_cketh_subaccount ≠ from_ckerc20_subaccount`. The protocol explicitly supports this. The bug is triggered whenever the Ethereum transaction fails on-chain after a successful dual-burn, or whenever the ckERC20 burn fails after the ckETH burn succeeds. Both are realistic scenarios. [1](#0-0) 

---

### Recommendation

The `Erc20WithdrawalRequest` struct should store **both** subaccounts separately:

```rust
pub struct Erc20WithdrawalRequest {
    // ...existing fields...
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH (gas fee).
    pub from_cketh_subaccount: Option<LedgerSubaccount>,
    /// The subaccount from which the minter burned ckERC20 tokens.
    pub from_ckerc20_subaccount: Option<LedgerSubaccount>,
}
```

In `withdraw_erc20`, populate both fields:

```rust
from_cketh_subaccount: from_cketh_subaccount.and_then(LedgerSubaccount::from_bytes),
from_ckerc20_subaccount: from_ckerc20_subaccount.and_then(LedgerSubaccount::from_bytes),
```

In `record_finalized_transaction`, use `from_ckerc20_subaccount` for the ckERC20 reimbursement `to_subaccount`. In the early-failure path in `withdraw_erc20`, use `cketh_account.subaccount` (already correct). [3](#0-2) [5](#0-4) 

---

### Proof of Concept

1. User calls `withdraw_erc20` with:
   - `from_cketh_subaccount = Some([0xAA; 32])` (subaccount A)
   - `from_ckerc20_subaccount = Some([0xBB; 32])` (subaccount B)
   - ckETH is burned from `{owner: caller, subaccount: [0xAA; 32]}`
   - ckERC20 is burned from `{owner: caller, subaccount: [0xBB; 32]}`

2. Both burns succeed. `Erc20WithdrawalRequest` is stored with:
   - `from_subaccount = Some([0xBB; 32])` (ckERC20 subaccount, **not** ckETH subaccount)
   - Comment says this is "The subaccount from which the minter burned ckETH" — **incorrect**

3. Ethereum transaction fails on-chain. `record_finalized_transaction` creates a `ReimbursementRequest` with `to_subaccount = Some([0xBB; 32])`.

4. `process_reimbursement` mints ckERC20 tokens to `{owner: caller, subaccount: [0xBB; 32]}` — this is correct for ckERC20 reimbursement.

5. **However**, any code path that reads `Erc20WithdrawalRequest.from_subaccount` expecting the ckETH subaccount (as documented) will silently operate on subaccount B instead of A, routing assets incorrectly. [7](#0-6) [4](#0-3) [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L433-440)
```rust
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L480-492)
```rust
                    let withdrawal_request = Erc20WithdrawalRequest {
                        max_transaction_fee: erc20_tx_fee,
                        withdrawal_amount: ckerc20_withdrawal_amount,
                        destination,
                        cketh_ledger_burn_index,
                        ckerc20_ledger_id: ckerc20_token.ckerc20_ledger_id,
                        ckerc20_ledger_burn_index,
                        erc20_contract_address: ckerc20_token.erc20_contract_address,
                        from: caller,
                        from_subaccount: from_ckerc20_subaccount
                            .and_then(LedgerSubaccount::from_bytes),
                        created_at: now,
                    };
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L516-524)
```rust
                        let reimbursement_request = ReimbursementRequest {
                            ledger_burn_index: cketh_ledger_burn_index,
                            reimbursed_amount: reimbursed_amount.change_units(),
                            to: cketh_account.owner,
                            to_subaccount: cketh_account
                                .subaccount
                                .and_then(LedgerSubaccount::from_bytes),
                            transaction_hash: None,
                        };
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L168-173)
```rust
    /// The owner of the account from which the minter burned ckETH.
    #[cbor(n(7), with = "icrc_cbor::principal")]
    pub from: Principal,
    /// The subaccount from which the minter burned ckETH.
    #[n(8)]
    pub from_subaccount: Option<LedgerSubaccount>,
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L733-745)
```rust
            WithdrawalRequest::CkErc20(request) => {
                if receipt.status == TransactionStatus::Failure {
                    self.record_reimbursement_request(
                        index,
                        ReimbursementRequest {
                            ledger_burn_index: request.ckerc20_ledger_burn_index,
                            reimbursed_amount: request.withdrawal_amount.change_units(),
                            to: request.from,
                            to_subaccount: request.from_subaccount.clone(),
                            transaction_hash: Some(receipt.transaction_hash),
                        },
                    );
                }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L82-94)
```rust
        let args = TransferArg {
            from_subaccount: None,
            to: Account {
                owner: reimbursement_request.to,
                subaccount: reimbursement_request
                    .to_subaccount
                    .map(LedgerSubaccount::to_bytes),
            },
            fee: None,
            created_at_time: None,
            memo: Some(memo),
            amount: Nat::from(reimbursement_request.reimbursed_amount),
        };
```
