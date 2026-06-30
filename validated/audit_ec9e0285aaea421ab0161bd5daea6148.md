### Title
Scheduled XCC Promises in Router Contract Cannot Be Canceled - (`etc/xcc-router/src/lib.rs`)

### Summary

The XCC router contract stores `Delayed` cross-contract call promises in a `scheduled_promises` map. Only the Aurora Engine parent can write to this map (via `schedule`), but there is no function anywhere — in the router or in the engine — to remove a stored promise before it is executed. `execute_scheduled` is callable by anyone. Once a promise is scheduled, it is irrevocable.

### Finding Description

The XCC router contract (`etc/xcc-router/src/lib.rs`) maintains a `LookupMap<u64, PromiseArgs>` called `scheduled_promises`. [1](#0-0) 

Promises are written into this map only by the parent Aurora Engine account via the `schedule` method. This is triggered when an EVM user submits a transaction that calls the XCC precompile with `CrossContractCallArgs::Delayed(promise_args)`. [2](#0-1) 

The `execute_scheduled` function removes and executes a stored promise. It has **no access control** — any NEAR account can call it: [3](#0-2) 

A grep search across all files in `etc/xcc-router/src/` for `cancel`, `remove_scheduled`, or `abort` returns **zero matches**. There is no function in the router or in the engine that removes a scheduled promise without executing it. The engine's own contract method list (`engine-workspace/src/operation.rs`) also contains no cancel-scheduled operation: [4](#0-3) 

### Impact Explanation

The router account holds real NEAR tokens deposited by users via `fund_xcc_sub_account`. When a `Delayed` promise is scheduled with a non-zero `attached_balance`, those NEAR tokens are earmarked for the outgoing call. If the promise encodes an incorrect target account or incorrect arguments (e.g., due to a bug in the EVM contract that constructed the call), the NEAR will be irrevocably sent to the wrong destination when `execute_scheduled` is called. Because `execute_scheduled` is permissionless, an adversary can race to execute the flawed promise before the user or protocol can react. The user's NEAR is permanently lost with no recovery path.

**Impact class: Critical — permanent loss of user funds held in the XCC router sub-account.**

### Likelihood Explanation

Any EVM contract that uses the `Delayed` XCC path and attaches NEAR to the promise is exposed. The `Delayed` variant exists precisely for high-gas calls where the EVM transaction itself cannot afford to execute the NEAR call inline. This is a documented, intended production use-case. A single programming error in the EVM contract (wrong account ID string, wrong method name, wrong amount) produces an unrecoverable state. The permissionless `execute_scheduled` means there is no window to intervene once the promise is stored.

### Recommendation

Add a `cancel_scheduled` function to the XCC router that:
1. Requires the caller to be the parent Aurora Engine account (same check as `schedule`).
2. Removes the promise from `scheduled_promises` without executing it.
3. Refunds any NEAR that was pre-allocated for the call back to the originating EVM address's router account.

Correspondingly, expose a `cancel_scheduled_xcc` method on the Aurora Engine contract that forwards the cancellation to the correct router sub-account, callable by the EVM address that originally scheduled the promise (verified via the engine's predecessor/signer logic).

### Proof of Concept

1. Alice's EVM contract calls the XCC precompile with:
   ```
   CrossContractCallArgs::Delayed(PromiseArgs::Create(PromiseCreateArgs {
       target_account_id: "wrong-account.near",  // typo
       method: "some_method",
       args: ...,
       attached_balance: Yocto::new(1_000_000_000_000_000_000_000_000), // 1 NEAR
       attached_gas: ...,
   }))
   ```
2. The engine calls `router.schedule(promise)`. The promise is stored at nonce `N` in `scheduled_promises`. [1](#0-0) 
3. Alice notices the typo and wants to cancel. She finds no `cancel_scheduled` method exists anywhere in the router or engine.
4. Bob (or any NEAR account) calls `router.execute_scheduled({"nonce": N})`. [3](#0-2) 
5. The router executes the promise, sending 1 NEAR to `wrong-account.near`. Alice's funds are permanently lost.

### Citations

**File:** etc/xcc-router/src/lib.rs (L55-59)
```rust
    /// A sequential id to keep track of how many scheduled promises this router has executed.
    /// This allows multiple promises to be scheduled before any of them are executed.
    nonce: LazyOption<u64>,
    /// The storage for the scheduled promises.
    scheduled_promises: LookupMap<u64, PromiseArgs>,
```

**File:** etc/xcc-router/src/lib.rs (L149-156)
```rust
    #[payable]
    pub fn execute_scheduled(&mut self, nonce: U64) {
        let Some(promise) = self.scheduled_promises.remove(&nonce.0) else {
            env::panic_str("ERR_PROMISE_NOT_FOUND")
        };
        let promise_id = Self::promise_create(promise);
        env::promise_return(promise_id);
    }
```

**File:** engine-types/src/parameters/promise.rs (L275-285)
```rust
#[derive(Debug, BorshSerialize, BorshDeserialize)]
pub enum CrossContractCallArgs {
    /// The promise is to be executed immediately (as part of the same NEAR transaction as the EVM call).
    Eager(PromiseArgs),
    /// The promise is to be stored in the router contract, and can be executed in a future transaction.
    /// The purpose of this is to expand how much NEAR gas can be made available to a cross contract call.
    /// For example, if an expensive EVM call ends with a NEAR cross contract call, then there may not be
    /// much gas left to perform it. In this case, the promise could be `Delayed` (stored in the router)
    /// and executed in a separate transaction with a fresh 300 Tgas available for it.
    Delayed(PromiseArgs),
}
```

**File:** engine-workspace/src/operation.rs (L97-144)
```rust
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
#[allow(clippy::enum_variant_names)]
pub(crate) enum Call {
    New,
    DeployCode,
    DeployErc20Token,
    DeployErc20TokenLegacy,
    MirrorErc20Token,
    Call,
    Submit,
    SetOwner,
    RegisterRelayer,
    FtOnTransfer,
    Withdraw,
    FtTransfer,
    FtTransferCall,
    StorageDeposit,
    StorageUnregister,
    StorageWithdraw,
    PausePrecompiles,
    Upgrade,
    StageUpgrade,
    DeployUpgrade,
    StateMigration,
    ResumePrecompiles,
    FactoryUpdate,
    FundXccSubAccount,
    FactorySetWNearAddress,
    SetEthConnectorContractAccount,
    FactoryUpdateAddressVersion,
    RefundOnError,
    MintAccount,
    SetPausedFlags,
    SetKeyManager,
    AddRelayerKey,
    RemoveRelayerKey,
    PauseContract,
    ResumeContract,
    SetFixedGas,
    SetErc20FallbackAddress,
    SetSiloParams,
    SetWhitelistStatus,
    AddEntryToWhitelist,
    AddEntryToWhitelistBatch,
    RemoveEntryFromWhitelist,
    SetErc20Metadata,
    AttachFullAccessKey,
}
```
