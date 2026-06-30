### Title
ERC-20 Burn Committed Before Asynchronous NEP-141 Transfer — Failed NEAR Promise Causes Permanent Token Loss - (File: `engine-precompiles/src/native.rs`)

### Summary

The `ExitToNear` precompile burns ERC-20 tokens atomically within EVM execution, then schedules an asynchronous NEAR `ft_transfer` promise. If that NEAR promise fails, the EVM burn is already committed and irrecoverable — an exact structural analog to the reported `EXECTYPE_TRY` pattern where accounting state is updated before the actual operation and persists on failure.

### Finding Description

In `engine-precompiles/src/native.rs`, the `ExitToNear` precompile handles ERC-20 bridge-out as follows:

1. The ERC-20 contract calls its own `burn()` function, reducing the user's EVM balance. This is an atomic EVM state change committed when the EVM execution succeeds.
2. The exit precompile emits a promise log encoding a `ft_transfer` (or `ft_transfer_call`) call on the corresponding NEP-141 contract.
3. After EVM execution completes, the engine processes the promise log and schedules the NEAR cross-contract call.

The NEAR promise is **asynchronous** — it executes in a subsequent NEAR block. If it fails (e.g., NEP-141 contract paused, recipient account does not exist, out of gas, or any other NEAR-side error), the EVM burn is **not rolled back**. The tokens are permanently destroyed.

The code path that controls whether a refund callback is attached is gated behind the `error_refund` compile-time feature flag:

```rust
let callback_args = ExitToNearPrecompileCallbackArgs {
    #[cfg(feature = "error_refund")]
    refund: refund_call_args(&exit_to_near_params, &exit_event),
    #[cfg(not(feature = "error_refund"))]
    refund: None,          // <-- no refund path compiled in
    transfer_near: transfer_near_args,
};
``` [1](#0-0) 

When `error_refund` is not enabled and `transfer_near` is `None` (the standard ERC-20 exit case), `callback_args` equals the default value, so the promise is created without any callback:

```rust
let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
    PromiseArgs::Create(transfer_promise)   // no callback attached
} else {
    PromiseArgs::Callback(PromiseWithCallbackArgs { ... })
};
``` [2](#0-1) 

The promise log is then emitted and the EVM execution returns success — the burn is final. [3](#0-2) 

The structural parallel to the reported bug:

| Reported Bug | Aurora Analog |
|---|---|
| `spentMap` incremented before transfer | ERC-20 `burn()` committed before NEAR promise |
| `EXECTYPE_TRY` lets execution continue on failure | NEAR async promise failure does not revert EVM state |
| Allowance drained without actual transfer | Tokens burned without actual NEP-141 transfer |

### Impact Explanation

**Critical — Permanent freezing / direct theft of user funds.**

A user who calls the `ExitToNear` bridge path has their ERC-20 tokens burned on the Aurora EVM side. If the corresponding NEP-141 `ft_transfer` promise fails for any reason, those tokens are permanently destroyed: they no longer exist on the EVM side and were never delivered on the NEAR side. The user suffers a total, unrecoverable loss of the bridged amount.

### Likelihood Explanation

**Medium.** The NEAR `ft_transfer` call can fail due to:
- The NEP-141 contract being paused by its owner
- The recipient NEAR account not being registered with the NEP-141 token (storage deposit not paid)
- Insufficient NEAR gas attached to the promise
- Any bug or panic in the NEP-141 contract

The first two conditions are entirely user-reachable and do not require any privileged action. A user who specifies an unregistered recipient account will reliably trigger this path.

### Recommendation

1. **Enable `error_refund` unconditionally** in production builds, or remove the feature flag and always attach the refund callback for ERC-20 exits.
2. Alternatively, validate on-chain (before burning) that the recipient account is registered with the target NEP-141 contract, so the promise cannot fail for that reason.
3. Audit all other precompile promise paths (`ExitToEthereum`, XCC) for the same pattern: any EVM state change that is contingent on a subsequent NEAR promise succeeding must have a rollback/refund callback.

### Proof of Concept

1. Deploy an ERC-20 token on Aurora that is bridged to a NEP-141 contract.
2. Call the ERC-20's `withdraw` / burn function targeting a NEAR account that has **not** registered storage with the NEP-141 contract.
3. The EVM burn succeeds and is committed.
4. The NEAR `ft_transfer` promise fails because the recipient is not registered.
5. No refund callback fires (when `error_refund` is not compiled in).
6. The user's ERC-20 tokens are permanently destroyed with no NEP-141 tokens delivered.

The root cause is in `engine-precompiles/src/native.rs` in the `ExitToNear::run` implementation, specifically the conditional promise construction at lines 470–483, combined with the `#[cfg(not(feature = "error_refund"))]` branch at lines 452–453 that sets `refund: None`. [4](#0-3)

### Citations

**File:** engine-precompiles/src/native.rs (L381-501)
```rust
impl<I: IO> Precompile for ExitToNear<I> {
    fn required_gas(_input: &[u8]) -> Result<EthGas, ExitError> {
        Ok(costs::EXIT_TO_NEAR_GAS)
    }

    #[allow(clippy::too_many_lines)]
    fn run(
        &self,
        input: &[u8],
        target_gas: Option<EthGas>,
        context: &Context,
        is_static: bool,
    ) -> EvmPrecompileResult {
        // ETH (base) transfer input format: (85 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled
        //  - recipient_account_id (max MAX_INPUT_SIZE - 20 - 1 bytes)
        // ERC-20 transfer input format: (124 bytes)
        //  - flag (1 byte)
        //  - refund_address (20 bytes), present if the feature "error_refund" is enabled.
        //  - amount (32 bytes)
        //  - recipient_account_id (max MAX_INPUT_SIZE - 1 - (20) - 32 bytes)
        //  - `:unwrap` suffix in a case of wNEAR (7 bytes)
        let required_gas = Self::required_gas(input)?;

        if let Some(target_gas) = target_gas
            && required_gas > target_gas
        {
            return Err(ExitError::OutOfGas);
        }

        // It's not allowed to call exit precompiles in static mode
        if is_static {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_STATIC")));
        } else if context.address != exit_to_near::ADDRESS.raw() {
            return Err(ExitError::Other(Cow::from("ERR_INVALID_IN_DELEGATE")));
        }

        let exit_to_near_params = ExitToNearParams::try_from(input)?;

        let (nep141_address, args, exit_event, method, transfer_near_args) =
            match exit_to_near_params {
                // ETH(base) token transfer
                //
                // Input slice format:
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 (base) tokens, or also can contain the `:unwrap` suffix in case of
                //  withdrawing wNEAR, or another message of JSON in case of OMNI, or address of
                //  receiver in case of transfer tokens to another engine contract.
                ExitToNearParams::BaseToken(ref exit_params) => {
                    let eth_connector_account_id = self.get_eth_connector_contract_account()?;
                    exit_base_token_to_near(eth_connector_account_id, context, exit_params)?
                }
                // ERC-20 token transfer
                //
                // This precompile branch is expected to be called from the ERC-20 burn function.
                //
                // Input slice format:
                //  amount (U256 big-endian bytes) - the amount that was burned
                //  recipient_account_id (bytes) - the NEAR recipient account which will receive
                //  NEP-141 tokens, or also can contain the `:unwrap` suffix in case of withdrawing
                //  wNEAR, or another message of JSON in case of OMNI, or address of receiver in case
                //  of transfer tokens to another engine contract.
                ExitToNearParams::Erc20TokenParams(ref exit_params) => {
                    exit_erc20_token_to_near(context, exit_params, &self.io)?
                }
            };

        let callback_args = ExitToNearPrecompileCallbackArgs {
            #[cfg(feature = "error_refund")]
            refund: refund_call_args(&exit_to_near_params, &exit_event),
            #[cfg(not(feature = "error_refund"))]
            refund: None,
            transfer_near: transfer_near_args,
        };
        let attached_gas = if method == "ft_transfer_call" {
            costs::FT_TRANSFER_CALL_GAS
        } else {
            costs::FT_TRANSFER_GAS
        };

        let transfer_promise = PromiseCreateArgs {
            target_account_id: nep141_address,
            method,
            args: args.into_bytes(),
            attached_balance: Yocto::new(1),
            attached_gas,
        };

        let promise = if callback_args == ExitToNearPrecompileCallbackArgs::default() {
            PromiseArgs::Create(transfer_promise)
        } else {
            PromiseArgs::Callback(PromiseWithCallbackArgs {
                base: transfer_promise,
                callback: PromiseCreateArgs {
                    target_account_id: self.current_account_id.clone(),
                    method: "exit_to_near_precompile_callback".to_string(),
                    args: borsh::to_vec(&callback_args).unwrap(),
                    attached_balance: Yocto::new(0),
                    attached_gas: costs::EXIT_TO_NEAR_CALLBACK_GAS,
                },
            })
        };
        let promise_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: Vec::new(),
            data: borsh::to_vec(&promise).unwrap(),
        };
        let ethabi::RawLog { topics, data } = exit_event.encode();
        let exit_event_log = Log {
            address: exit_to_near::ADDRESS.raw(),
            topics: topics.into_iter().map(|h| H256::from(h.0)).collect(),
            data,
        };

        Ok(PrecompileOutput {
            logs: vec![promise_log, exit_event_log],
            cost: required_gas,
            output: Vec::new(),
        })
    }
```
