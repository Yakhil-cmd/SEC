### Title
Unbounded Linear Scan in `withdrawal_status()` Query Over Monotonically Growing `processed_withdrawal_requests` — (File: rs/ethereum/cketh/minter/src/state/transactions/mod.rs)

---

### Summary

The ckETH/ckERC20 minter's public `withdrawal_status()` query function performs an O(n) full linear scan over `processed_withdrawal_requests`, a `BTreeMap` that accumulates every processed withdrawal request and is **never pruned**. As the minter processes withdrawals over its lifetime, this map grows monotonically. Any unprivileged caller can invoke the query, and its instruction cost grows linearly with the total number of ever-processed withdrawals, eventually risking exhaustion of the IC query instruction limit.

---

### Finding Description

`EthTransactions` holds two collections relevant here:

- `pending_withdrawal_requests: VecDeque<WithdrawalRequest>` — requests awaiting transaction creation
- `processed_withdrawal_requests: BTreeMap<LedgerBurnIndex, WithdrawalRequest>` — every request for which a transaction was ever created; entries are **never removed** [1](#0-0) 

The public `withdrawal_status()` method iterates over **all** values in both collections on every call:

```rust
let pending = self.pending_withdrawal_requests.iter().filter_map(|r| {
    r.match_parameter(parameter).then_some(...)
});

let processed = self
    .processed_withdrawal_requests
    .values()          // full O(n) scan — no early exit, no index
    .filter(|r| r.match_parameter(parameter))
    ...;

pending.chain(processed).collect()
``` [2](#0-1) 

This is exposed as a `#[query]` endpoint callable by any principal: [3](#0-2) 

The `ByWithdrawalId` search variant is the most egregious case: `processed_withdrawal_requests` is keyed by `LedgerBurnIndex`, so a direct `.get()` would be O(log n), yet the code iterates all values instead.

`transaction_status()` has the same pattern for `pending_withdrawal_requests`: [4](#0-3) 

There is no cap, cutoff, or pruning on `processed_withdrawal_requests`. Compare with the ckBTC minter, which explicitly caps `finalized_requests` at `MAX_FINALIZED_REQUESTS = 100`: [5](#0-4) [6](#0-5) 

No equivalent guard exists for `processed_withdrawal_requests` in the ckETH minter.

---

### Impact Explanation

The IC enforces a per-query instruction limit. As `processed_withdrawal_requests` grows with every withdrawal the minter ever processes, each call to `withdrawal_status()` consumes more instructions. Once the map is large enough, the query will trap with an instruction-limit error for **all callers**, making withdrawal status lookup permanently unavailable through this endpoint. This degrades the user-facing observability of the minter and breaks integrations that rely on `withdrawal_status`.

---

### Likelihood Explanation

The ckETH minter has been live on mainnet since 2023 and processes withdrawals continuously. `processed_withdrawal_requests` has been growing since genesis with no bound. The degradation is not hypothetical — it is a function of normal minter operation. Any user can accelerate it by submitting withdrawal requests (each requires burning ckETH, so there is a real cost, but the map growth is permanent and cumulative). The `withdrawal_status` DID interface is public: [7](#0-6) 

---

### Recommendation

1. **For `ByWithdrawalId`**: replace the full `.values()` scan with a direct `processed_withdrawal_requests.get(&burn_index)` lookup — O(log n) instead of O(n).
2. **For `ByRecipient` / `BySenderAccount`**: maintain secondary reverse indexes (recipient → burn index, sender → burn index) updated on insertion, analogous to `retrieve_btc_account_to_block_indices` in the ckBTC minter.
3. **Prune `processed_withdrawal_requests`**: after finalization is confirmed and reimbursement is settled, entries can be removed. Status for old requests can be served from a compact finalized-status map (similar to ckBTC's capped `finalized_requests` deque).

---

### Proof of Concept

1. Submit N ckETH withdrawal requests via `withdraw_eth` (each burns ckETH).
2. Allow the minter's timer to process them — each moves from `pending_withdrawal_requests` into `processed_withdrawal_requests`.
3. Call `withdrawal_status(ByWithdrawalId(k))` for any `k`.
4. Observe that instruction consumption grows linearly with N, despite the BTreeMap key being directly available.
5. At sufficiently large N, the query traps with an instruction-limit error for all callers. [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L361-377)
```rust
pub struct EthTransactions {
    pub(in crate::state) pending_withdrawal_requests: VecDeque<WithdrawalRequest>,
    // Processed withdrawal requests (transaction created, sent, or finalized).
    pub(in crate::state) processed_withdrawal_requests:
        BTreeMap<LedgerBurnIndex, WithdrawalRequest>,
    pub(in crate::state) created_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, TransactionRequest>,
    pub(in crate::state) sent_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, Vec<SignedTransactionRequest>>,
    pub(in crate::state) finalized_tx:
        MultiKeyMap<TransactionNonce, LedgerBurnIndex, FinalizedEip1559Transaction>,
    pub(in crate::state) next_nonce: TransactionNonce,

    pub(in crate::state) maybe_reimburse: BTreeSet<LedgerBurnIndex>,
    pub(in crate::state) reimbursement_requests: BTreeMap<ReimbursementIndex, ReimbursementRequest>,
    pub(in crate::state) reimbursed: BTreeMap<ReimbursementIndex, ReimbursedResult>,
}
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L805-841)
```rust
    pub fn withdrawal_status(
        &self,
        parameter: &WithdrawalSearchParameter,
    ) -> Vec<(
        &WithdrawalRequest,
        WithdrawalStatus,
        Option<&Eip1559TransactionRequest>,
    )> {
        // Pending requests matching the given search parameter
        let pending = self.pending_withdrawal_requests.iter().filter_map(|r| {
            r.match_parameter(parameter)
                .then_some((r, WithdrawalStatus::Pending, None))
        });

        // Processed withdrawal requests matching the given search parameter.
        let processed = self
            .processed_withdrawal_requests
            .values()
            .filter(|r| r.match_parameter(parameter))
            .map(|request| {
                match self.processed_transaction_status(&request.cketh_ledger_burn_index()) {
                    (RetrieveEthStatus::TxCreated, Some(tx)) => {
                        (request, WithdrawalStatus::TxCreated, Some(tx))
                    }
                    (RetrieveEthStatus::TxSent(sent), Some(tx)) => {
                        (request, WithdrawalStatus::TxSent(sent), Some(tx))
                    }
                    (RetrieveEthStatus::TxFinalized(status), Some(tx)) => {
                        (request, WithdrawalStatus::TxFinalized(status), Some(tx))
                    }
                    _ => {
                        panic!("Status of processed request is not found {request:?}")
                    }
                }
            });

        pending.chain(processed).collect()
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L844-853)
```rust
    pub fn transaction_status(&self, burn_index: &LedgerBurnIndex) -> RetrieveEthStatus {
        if self
            .pending_withdrawal_requests
            .iter()
            .any(|r| &r.cketh_ledger_burn_index() == burn_index)
        {
            return RetrieveEthStatus::Pending;
        }
        self.processed_transaction_status(burn_index).0
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L348-387)
```rust
#[query]
async fn withdrawal_status(parameter: WithdrawalSearchParameter) -> Vec<WithdrawalDetail> {
    use transactions::WithdrawalRequest::*;
    let parameter = transactions::WithdrawalSearchParameter::try_from(parameter).unwrap();
    read_state(|s| {
        s.eth_transactions
            .withdrawal_status(&parameter)
            .into_iter()
            .map(|(request, status, tx)| WithdrawalDetail {
                withdrawal_id: *request.cketh_ledger_burn_index().as_ref(),
                recipient_address: request.payee().to_string(),
                token_symbol: match request {
                    CkEth(_) => CkTokenSymbol::cketh_symbol_from_state(s).to_string(),
                    CkErc20(r) => s
                        .ckerc20_tokens
                        .get_alt(&r.erc20_contract_address)
                        .unwrap()
                        .to_string(),
                },
                withdrawal_amount: match request {
                    CkEth(r) => r.withdrawal_amount.into(),
                    CkErc20(r) => r.withdrawal_amount.into(),
                },
                max_transaction_fee: match (request, tx) {
                    (CkEth(_), None) => None,
                    (CkEth(r), Some(tx)) => {
                        r.withdrawal_amount.checked_sub(tx.amount).map(|x| x.into())
                    }
                    (CkErc20(r), _) => Some(r.max_transaction_fee.into()),
                },
                from: request.from(),
                from_subaccount: request
                    .from_subaccount()
                    .cloned()
                    .map(LedgerSubaccount::to_bytes),
                status,
            })
            .collect()
    })
}
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L45-47)
```rust
/// The maximum number of finalized BTC retrieval requests that we keep in the
/// history.
const MAX_FINALIZED_REQUESTS: usize = 100;
```

**File:** rs/bitcoin/ckbtc/minter/src/state.rs (L1330-1337)
```rust
    fn push_finalized_request(&mut self, req: FinalizedBtcRequest) {
        assert!(!self.has_pending_retrieve_btc_request(req.request.block_index()));

        if self.finalized_requests.len() >= MAX_FINALIZED_REQUESTS {
            self.finalized_requests.pop_front();
        }
        self.finalized_requests.push_back(req)
    }
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L354-364)
```text
// Search parameter for withdrawals.
type WithdrawalSearchParameter = variant {
    // Search by recipient's ETH address.
    ByRecipient : text;

    // Search by sender's token account.
    BySenderAccount : Account;

    // Search by ckETH burn index (which is also used to index ckERC20 withdrawals).
    ByWithdrawalId : nat64;
};
```
