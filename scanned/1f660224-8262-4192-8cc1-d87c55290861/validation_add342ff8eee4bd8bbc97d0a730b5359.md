### Title
Attacker Can Fill 1000 Pending Nonce Slots to DoS Withdrawal Pipeline - (`rs/ethereum/cketh/minter/src/state/transactions/mod.rs`)

---

### Summary

The `withdrawal_requests_batch` function enforces a hard cap of `MAX_NUM_PENDING_TRANSACTION_NONCES = 1000` on in-flight Ethereum nonces. An attacker with sufficient ckETH can cycle through the `MAX_PENDING = 100` guard in repeated rounds to accumulate 1000 entries across `created_tx` and `sent_tx`, causing `withdrawal_requests_batch` to return an empty slice and halting all new withdrawal processing for all users until Ethereum finalizes the attacker's transactions.

---

### Finding Description

`withdrawal_requests_batch` computes the allowed batch size as:

```
actual_batch_size = min(1000 - |created_tx ∪ sent_tx|, requested_batch_size)
``` [1](#0-0) 

When `|created_tx| + |sent_tx| >= 1000`, `actual_batch_size` saturates to 0 and the function returns `[]`, so `create_transactions_batch` in `withdraw.rs` processes nothing. [2](#0-1) 

The guard that protects `withdraw_eth` checks `withdrawal_requests_len()`, which counts only `pending_withdrawal_requests` — the pre-nonce queue — not the already-promoted `created_tx`/`sent_tx` entries: [3](#0-2) [4](#0-3) 

`MAX_PENDING = 100` caps the pre-nonce queue, but once the minter's timer loop promotes those 100 requests into `created_tx`, the guard resets and the attacker can submit 100 more. Repeating this 10 times fills all 1000 nonce slots. [5](#0-4) 

The existing test `should_limit_batch_size_when_too_many_pending_transactions` explicitly confirms that at 1000 pending nonces, `withdrawal_requests_batch(3)` returns `[]`: [6](#0-5) 

---

### Impact Explanation

Once 1000 nonce slots are occupied, `withdrawal_requests_batch` returns `[]` on every timer tick. No new withdrawal request from any user — including legitimate ones — can be promoted to a transaction. The pipeline is frozen until Ethereum finalizes the attacker's 1000 transactions, which can take minutes to hours depending on gas prices and network congestion. This is a complete, cross-user DoS of the ckETH/ckERC-20 withdrawal pipeline.

---

### Likelihood Explanation

**Attack cost:** The minimum withdrawal amount is `30_000_000_000_000_000 wei` (0.03 ETH). Filling 1000 slots requires ~30 ETH of ckETH capital. The attacker recovers the principal on Ethereum (minus gas fees for 1000 transactions). The net cost is only gas fees (~1000 × 21,000 gas), which at moderate gas prices is a few hundred USD — a low cost relative to the disruption caused.

**Execution:** The attacker submits 100 requests, waits for the minter's timer to promote them to `created_tx` (roughly one 6-minute cycle), then repeats 10 times. No privileged access is required; `withdraw_eth` is a public update call. [7](#0-6) 

---

### Recommendation

1. **Count total in-flight nonces in the guard:** Change `pending_requests_count` to include `created_tx.len() + sent_tx.len()` in addition to `pending_withdrawal_requests.len()`, so the guard reflects the true pipeline depth.
2. **Per-principal nonce cap:** Limit the number of in-flight nonces attributable to a single principal, preventing one actor from monopolizing the 1000-slot budget.
3. **Reduce `MAX_NUM_PENDING_TRANSACTION_NONCES`** to a value that is harder to fill with realistic capital, or make it configurable by governance.

---

### Proof of Concept

State-machine level (mirrors the existing test pattern):

```
1. Attacker holds 1000 × min_withdrawal_amount ckETH.
2. Round 1: submit 100 withdraw_eth calls → pending_withdrawal_requests fills to 100 (guard blocks 101st).
3. Wait one minter timer cycle → minter calls create_transactions_batch, promoting all 100 to created_tx.
4. Guard resets (pending_withdrawal_requests.len() == 0).
5. Repeat rounds 2–10 → created_tx + sent_tx accumulates 1000 entries.
6. Assert: withdrawal_requests_batch(5) == [] for any subsequent legitimate user request.
7. Measure: no new withdrawal is processed until Ethereum finalizes attacker's transactions.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L906-923)
```rust
    pub fn withdrawal_requests_batch(&self, requested_batch_size: usize) -> Vec<WithdrawalRequest> {
        // The number of pending transaction nonces is counted and not the number of pending transactions
        // because a nonce may be associated with several distinct transactions (due to re-submission and dynamic fees).
        // However, once a nonce is chosen for a withdrawal request, it's in our interest that the corresponding transaction be finalized asap.
        // Limiting the number of transactions would be counter-productive.
        const MAX_NUM_PENDING_TRANSACTION_NONCES: usize = 1000;
        let unique_pending_transaction_nonces: BTreeSet<_> =
            self.created_tx.keys().chain(self.sent_tx.keys()).collect();
        let actual_batch_size = min(
            MAX_NUM_PENDING_TRANSACTION_NONCES
                .saturating_sub(unique_pending_transaction_nonces.len()),
            requested_batch_size,
        );
        self.withdrawal_requests_iter()
            .take(actual_batch_size)
            .cloned()
            .collect()
    }
```

**File:** rs/ethereum/cketh/minter/src/withdraw.rs (L249-253)
```rust
fn create_transactions_batch(gas_fee_estimate: GasFeeEstimate) {
    for request in read_state(|s| {
        s.eth_transactions
            .withdrawal_requests_batch(WITHDRAWAL_REQUESTS_BATCH_SIZE)
    }) {
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L9-10)
```rust
pub const MAX_CONCURRENT: usize = 100;
pub const MAX_PENDING: usize = 100;
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L27-35)
```rust
impl RequestsGuardedByPrincipal for PendingWithdrawalRequests {
    fn guarded_principals(state: &mut State) -> &mut BTreeSet<Principal> {
        &mut state.pending_withdrawal_principals
    }

    fn pending_requests_count(state: &State) -> usize {
        state.eth_transactions.withdrawal_requests_len()
    }
}
```

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L50-53)
```rust
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/tests.rs (L223-229)
```rust
            create_and_record_pending_transaction(
                &mut transactions,
                withdrawal_requests[999].clone(),
                rng.r#gen(),
            );
            assert_eq!(transactions.withdrawal_requests_batch(3), vec![]);
        }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L266-278)
```rust
async fn withdraw_eth(
    WithdrawalArg {
        amount,
        recipient,
        from_subaccount,
    }: WithdrawalArg,
) -> Result<RetrieveEthRequest, WithdrawalError> {
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```
