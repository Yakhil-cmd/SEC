### Title
Missing Incentive to Call `execute_scheduled` Causes Permanent Freezing of User NEAR Funds - (`File: etc/xcc-router/src/lib.rs`)

### Summary

The XCC (Cross-Contract Call) system supports a two-step `Delayed` flow: an EVM user first schedules a promise (burning wNEAR from their EVM balance to fund the router), and then a second NEAR transaction must call `execute_scheduled` on the router to actually dispatch the promise. There is no incentive for any third party to call `execute_scheduled`, and EVM-native users may have no NEAR account to call it themselves. If `execute_scheduled` is never called, the NEAR that was already burned from the user's EVM wNEAR balance is permanently frozen in the router sub-account with no recovery path.

### Finding Description

The `CrossContractCallArgs::Delayed` variant in the XCC precompile implements a two-step flow:

**Step 1 — Schedule (EVM transaction):** The EVM user calls the XCC precompile with `Delayed` args. The precompile burns wNEAR from the user's EVM balance and sends it as NEAR to the router sub-account, then calls `schedule` on the router, which stores the `PromiseArgs` in `scheduled_promises`. [1](#0-0) 

**Step 2 — Execute (separate NEAR transaction):** A separate NEAR transaction must call `execute_scheduled(nonce)` on the router to actually dispatch the stored promise. The function is intentionally open to any caller: [2](#0-1) 

The design comment acknowledges this openness but provides no economic incentive for a third party to pay the NEAR gas cost of calling `execute_scheduled`. The caller pays NEAR gas and receives nothing in return.

The NEAR that was sent to the router (from the user's burned wNEAR) is stored in the router's account balance and is meant to be attached to the actual cross-contract call when `execute_scheduled` fires. If `execute_scheduled` is never called, this NEAR has no recovery path:

- The router's `send_refund` function only returns a fixed `REFUND_AMOUNT` of 2 NEAR (the storage deposit), not the NEAR attached to pending scheduled promises. [3](#0-2) 

- There is no admin function, timeout, or cancellation mechanism to reclaim NEAR locked in `scheduled_promises`.
- The user's wNEAR ERC-20 balance was already burned in Step 1 and cannot be restored. [4](#0-3) 

### Impact Explanation

An EVM user who uses `CrossContractCallArgs::Delayed` has their wNEAR burned from their EVM balance in Step 1. If `execute_scheduled` is never called, the equivalent NEAR is permanently frozen in the router sub-account. There is no on-chain mechanism to recover it. This constitutes **permanent freezing of user funds**.

### Likelihood Explanation

- EVM-native users (Ethereum wallets, MetaMask, etc.) typically do not have NEAR accounts and cannot call `execute_scheduled` themselves.
- Third parties (relayers, keepers) have zero economic incentive to call `execute_scheduled` — they pay NEAR gas and receive nothing.
- The `Delayed` flow is explicitly designed for cases where the EVM transaction runs low on NEAR gas, meaning the user is already in a resource-constrained situation and may not anticipate the need for a follow-up NEAR transaction.
- The test suite itself demonstrates that `execute_scheduled` must be called manually by an external account after scheduling. [5](#0-4) 

### Recommendation

Introduce an economic incentive for calling `execute_scheduled`. Options include:

1. **Tip mechanism:** Allow the EVM user to attach a small NEAR tip (stored alongside the scheduled promise) that is paid out to whoever calls `execute_scheduled`.
2. **Keeper reward from router balance:** Reserve a small portion of the router's NEAR balance as a reward for the caller of `execute_scheduled`.
3. **Self-execution via relayer:** Integrate with Aurora's relayer infrastructure so that scheduled promises are automatically executed by the relayer, which is already compensated via EVM gas fees.

### Proof of Concept

1. Alice (EVM-only user, no NEAR account) calls the XCC precompile with `CrossContractCallArgs::Delayed`, scheduling a promise that requires 10 NEAR attached. Her wNEAR ERC-20 balance is burned for 10 NEAR worth, and the NEAR is sent to her router sub-account. [1](#0-0) 

2. The promise is stored in `scheduled_promises` at nonce 0. [6](#0-5) 

3. No third party calls `execute_scheduled` because doing so costs NEAR gas with no reward. Alice cannot call it herself (no NEAR account).
4. The 10 NEAR sits permanently in the router sub-account. Alice's wNEAR is gone. `send_refund` only returns the 2 NEAR storage deposit, not the 10 NEAR locked in the pending promise. [7](#0-6)

### Citations

**File:** engine-precompiles/src/xcc.rs (L159-172)
```rust
            CrossContractCallArgs::Delayed(call) => {
                let attached_near = call.total_near();
                let promise = PromiseCreateArgs {
                    target_account_id,
                    method: consts::ROUTER_SCHEDULE_NAME.into(),
                    args: borsh::to_vec(&call)
                        .map_err(|_| ExitError::Other(Cow::from(consts::ERR_SERIALIZE)))?,
                    attached_balance: ZERO_YOCTO,
                    // We don't need to add any gas to the amount need for the schedule call
                    // since the promise is not executed right away.
                    attached_gas: costs::ROUTER_SCHEDULE,
                };
                (promise, attached_near)
            }
```

**File:** etc/xcc-router/src/lib.rs (L39-40)
```rust
/// Must match aurora_engine_precompiles::xcc::state::STORAGE_AMOUNT
const REFUND_AMOUNT: NearToken = NearToken::from_near(2);
```

**File:** etc/xcc-router/src/lib.rs (L136-144)
```rust
    pub fn schedule(&mut self, #[serializer(borsh)] promise: PromiseArgs) {
        self.assert_preconditions();

        let nonce = self.nonce.get().unwrap_or_default();
        self.scheduled_promises.insert(nonce, promise);
        self.nonce.set(&(nonce + 1));

        near_sdk::log!("Promise scheduled at nonce {}", nonce);
    }
```

**File:** etc/xcc-router/src/lib.rs (L146-156)
```rust
    /// It is intentional that this function can be called by anyone (not just the parent).
    /// There is no security risk to allowing this function to be open because it can only
    /// act on promises that were created via `schedule`.
    #[payable]
    pub fn execute_scheduled(&mut self, nonce: U64) {
        let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
            env::panic_str("ERR_PROMISE_NOT_FOUND")
        };
        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

**File:** etc/xcc-router/src/lib.rs (L176-184)
```rust
    pub fn send_refund(&self) -> Promise {
        let parent = self.get_parent().unwrap_or_else(env_panic);

        require_caller(&parent)
            .and_then(|_| require_no_failed_promises())
            .unwrap_or_else(env_panic);

        Promise::new(parent).transfer(REFUND_AMOUNT)
    }
```

**File:** engine-types/src/parameters/promise.rs (L279-284)
```rust
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
```

**File:** engine-tests/src/tests/xcc.rs (L903-921)
```rust
        if is_scheduled {
            // The promise was only scheduled, not executed immediately. So the FT balance has not changed yet.
            assert_eq!(
                nep_141_balance_of(&nep_141, &ft_owner.id()).await,
                nep_141_supply - transfer_amount
            );

            // Now we execute the scheduled promise
            let result = aurora
                .root()
                .call(&router_account_id, "execute_scheduled")
                .args_json(json!({
                    "nonce": "0"
                }))
                .max_gas()
                .transact()
                .await
                .unwrap();
            assert!(result.is_success(), "{result:?}");
```
