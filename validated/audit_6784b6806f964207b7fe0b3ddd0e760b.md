### Title
Global Pending-Queue Saturation Griefing Blocks All ckETH and ckERC20 Withdrawals - (File: `rs/ethereum/cketh/minter/src/guard/mod.rs`)

---

### Summary

The ckETH minter enforces a single global `MAX_PENDING = 100` limit on pending withdrawal requests shared across all principals and all token types (ckETH and ckERC20). An unprivileged user can make 100 sequential `withdraw_eth` calls — each burning the minimum withdrawal amount — to saturate `pending_withdrawal_requests`, causing every subsequent `withdraw_eth` and `withdraw_erc20` call from **any** principal to fail with `TooManyPendingRequests` until the minter drains the queue. Because the attacker's funds are eventually returned (minus gas fees), the net cost is only the Ethereum transaction fees, making the attack economically sustainable.

---

### Finding Description

**Root cause — global pending-count gate in `Guard::new`**

`rs/ethereum/cketh/minter/src/guard/mod.rs` defines:

```rust
pub const MAX_PENDING: usize = 100;

fn new(principal: Principal) -> Result<Self, GuardError> {
    mutate_state(|s| {
        if PR::pending_requests_count(s) >= MAX_PENDING {   // ← global gate
            return Err(GuardError::TooManyPendingRequests);
        }
        ...
    })
}
``` [1](#0-0) [2](#0-1) 

`pending_requests_count` for `PendingWithdrawalRequests` is:

```rust
fn pending_requests_count(state: &State) -> usize {
    state.eth_transactions.withdrawal_requests_len()
}
``` [3](#0-2) 

And `withdrawal_requests_len()` returns the raw length of the shared `pending_withdrawal_requests` deque — a **global** count, not per-principal: [4](#0-3) 

**Both `withdraw_eth` and `withdraw_erc20` share the same guard**

Both public update methods call `retrieve_withdraw_guard(caller)` before doing anything else: [5](#0-4) [6](#0-5) 

This means ckETH and ckERC20 withdrawal requests compete for the same 100-slot global queue.

**Guard lifetime vs. queue lifetime mismatch**

The `Guard` is dropped when `withdraw_eth` returns, but the `WithdrawalRequest` remains in `pending_withdrawal_requests` until the minter's timer task processes it. The `AlreadyProcessing` check only prevents *concurrent* calls from the same principal; it does **not** prevent the same principal from making sequential calls that each leave a request in the queue: [7](#0-6) 

**Attack path**

1. Attacker calls `withdraw_eth` 100 times sequentially with the minimum withdrawal amount (`30_000_000_000_000_000` wei = 0.03 ETH each).
2. Each call: guard acquired → ckETH burned → request appended to `pending_withdrawal_requests` → guard dropped.
3. After 100 calls, `pending_withdrawal_requests.len() == 100`.
4. Every subsequent `withdraw_eth` or `withdraw_erc20` call from **any** principal hits the `>= MAX_PENDING` gate and is rejected immediately.
5. The minter processes requests asynchronously in batches; the attacker can re-fill the queue as slots open to sustain the DoS indefinitely.
6. The attacker's 3 ETH is eventually returned (minus actual Ethereum gas fees), so the net cost is only gas — roughly $500–$5,000 at typical gas prices.

---

### Impact Explanation

All ckETH and ckERC20 withdrawals are blocked for every user on the minter until the attacker's 100 queued requests are processed. Because the attacker can continuously re-submit after each batch drains, the disruption can be sustained indefinitely. Users holding ckETH or ckERC20 tokens cannot convert them back to native assets, breaking the chain-fusion bridge's core guarantee of redeemability.

---

### Likelihood Explanation

The attack requires ~3 ETH of capital (recoverable minus fees) and 100 sequential canister calls — both achievable by any motivated adversary. No privileged role, governance majority, or key compromise is needed. The entry path is the public `withdraw_eth` update method, reachable by any non-anonymous ingress sender. The net cost (gas fees only) is low relative to the disruption caused to all minter users.

---

### Recommendation

1. **Add a per-principal pending-request cap** alongside the global cap. Reject a new request if the calling principal already has `N` (e.g., 5) requests in `pending_withdrawal_requests`, preventing a single principal from monopolising the queue.
2. **Separate ckETH and ckERC20 pending queues** so that flooding one token type does not block the other.
3. **Raise `MAX_PENDING`** to a value that reflects realistic throughput, reducing the feasibility of saturation.

---

### Proof of Concept

```
// Attacker script (pseudocode)
for i in 0..100 {
    // Each call burns 0.03 ckETH and enqueues one pending request
    minter.withdraw_eth({
        amount: 30_000_000_000_000_000,  // minimum
        recipient: attacker_eth_address,
    });
}

// Now pending_withdrawal_requests.len() == 100
// Any call from any principal returns TooManyPendingRequests:
victim.withdraw_eth({ amount: 1_000_000_000_000_000_000, recipient: "0x..." });
// → ic_cdk::trap("Failed retrieving guard for principal ...: TooManyPendingRequests")

// Attacker re-fills as the minter drains the queue, sustaining the DoS.
// Net attacker cost: only Ethereum gas fees (~$500–$5,000), not the 3 ETH principal.
``` [8](#0-7) [9](#0-8)

### Citations

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

**File:** rs/ethereum/cketh/minter/src/guard/mod.rs (L46-68)
```rust
impl<PR: RequestsGuardedByPrincipal> Guard<PR> {
    /// Attempts to create a new guard for the current code block. Fails if there is
    /// already a pending request for the specified [principal] or if there
    /// are at least [MAX_CONCURRENT] pending requests.
    fn new(principal: Principal) -> Result<Self, GuardError> {
        mutate_state(|s| {
            if PR::pending_requests_count(s) >= MAX_PENDING {
                return Err(GuardError::TooManyPendingRequests);
            }
            let principals = PR::guarded_principals(s);
            if principals.contains(&principal) {
                return Err(GuardError::AlreadyProcessing);
            }
            if principals.len() >= MAX_CONCURRENT {
                return Err(GuardError::TooManyConcurrentRequests);
            }
            principals.insert(principal);
            Ok(Self {
                principal,
                _marker: PhantomData,
            })
        })
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L453-467)
```rust
    pub fn record_withdrawal_request<R: Into<WithdrawalRequest>>(&mut self, request: R) {
        let request = request.into();
        let burn_index = request.cketh_ledger_burn_index();
        if self
            .pending_withdrawal_requests
            .iter()
            .any(|r| r.cketh_ledger_burn_index() == burn_index)
            || self.created_tx.contains_alt(&burn_index)
            || self.sent_tx.contains_alt(&burn_index)
            || self.finalized_tx.contains_alt(&burn_index)
        {
            panic!("BUG: duplicate ckETH ledger burn index {burn_index}");
        }
        self.pending_withdrawal_requests.push_back(request);
    }
```

**File:** rs/ethereum/cketh/minter/src/state/transactions/mod.rs (L929-931)
```rust
    pub fn withdrawal_requests_len(&self) -> usize {
        self.pending_withdrawal_requests.len()
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L265-340)
```rust
#[update]
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

    let destination = validate_address_as_destination(&recipient).map_err(|e| match e {
        AddressValidationError::Invalid { .. } | AddressValidationError::NotSupported(_) => {
            ic_cdk::trap(e.to_string())
        }
        AddressValidationError::Blocked(address) => WithdrawalError::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;

    let amount = Wei::try_from(amount).expect("failed to convert Nat to u256");

    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }

    let client = read_state(LedgerClient::cketh_ledger_from_state);
    let now = ic_cdk::api::time();
    log!(INFO, "[withdraw]: burning {:?}", amount);
    match client
        .burn_from(
            Account {
                owner: caller,
                subaccount: from_subaccount,
            },
            amount,
            BurnMemo::Convert {
                to_address: destination,
            },
        )
        .await
    {
        Ok(ledger_burn_index) => {
            let withdrawal_request = EthWithdrawalRequest {
                withdrawal_amount: amount,
                destination,
                ledger_burn_index,
                from: caller,
                from_subaccount: from_subaccount.and_then(LedgerSubaccount::from_bytes),
                created_at: Some(now),
            };

            log!(
                INFO,
                "[withdraw]: queuing withdrawal request {:?}",
                withdrawal_request,
            );

            mutate_state(|s| {
                process_event(
                    s,
                    EventType::AcceptedEthWithdrawalRequest(withdrawal_request.clone()),
                );
            });
            Ok(RetrieveEthRequest::from(withdrawal_request))
        }
        Err(e) => Err(WithdrawalError::from(e)),
    }
}
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L400-405)
```rust
    let caller = validate_caller_not_anonymous();
    let _guard = retrieve_withdraw_guard(caller).unwrap_or_else(|e| {
        ic_cdk::trap(format!(
            "Failed retrieving guard for principal {caller}: {e:?}"
        ))
    });
```
