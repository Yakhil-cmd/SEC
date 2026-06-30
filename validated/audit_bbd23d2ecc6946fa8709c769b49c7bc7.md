### Title
ETH Permanently Frozen in `exit_to_near::ADDRESS` When Refund Recipient Cannot Receive ETH - (`engine/src/engine.rs`, `engine/src/contract_methods/connector.rs`)

---

### Summary

When the `error_refund` feature is enabled, the `ExitToNear` precompile records a user-supplied `refund_address` at call time. If the downstream `ft_transfer` NEAR promise fails and the `refund_address` is a contract without a `receive`/`fallback` function, the ETH refund EVM call in `refund_on_error` fails, `ERR_REFUND_FAILURE` is returned, and the ETH is permanently frozen inside `exit_to_near::ADDRESS` with no recovery path.

---

### Finding Description

The `ExitToNear` precompile (flag `0x00`, ETH base-token path) deducts ETH from the caller and credits it to the precompile address `exit_to_near::ADDRESS` during EVM execution. It then schedules a NEAR `ft_transfer` promise with a callback to `exit_to_near_precompile_callback`.

When `error_refund` is compiled in, bytes `[1..21]` of the input are parsed as the `refund_address`: [1](#0-0) 

This address is stored verbatim in `RefundCallArgs::recipient_address`: [2](#0-1) 

If the `ft_transfer` promise fails, `exit_to_near_precompile_callback` calls `refund_on_error`: [3](#0-2) 

For the ETH path, `refund_on_error` issues a plain ETH EVM call (empty calldata) from `exit_to_near::ADDRESS` to the fixed `refund_address`: [4](#0-3) 

If `refund_address` is a contract without a `receive` or `fallback` function, this EVM call reverts. `refund_result.status.is_ok()` is `false`, `ERR_REFUND_FAILURE` is returned, and the NEAR callback panics. The ETH remains in `exit_to_near::ADDRESS` permanently — there is no secondary recovery function, no admin escape hatch, and no private key for the precompile address.

The `error_refund` feature is a separately declared, opt-in feature: [5](#0-4) 

The test suite explicitly acknowledges that without `error_refund` there is also no refund at all (a separate permanent-loss path), and that with it the refund goes to the fixed address: [6](#0-5) 

---

### Impact Explanation

ETH sent through the `ExitToNear` precompile (base-token path) is irrecoverably frozen inside `exit_to_near::ADDRESS` whenever:
1. The NEAR `ft_transfer` fails (e.g., unregistered recipient, insufficient connector balance), **and**
2. The `refund_address` encoded in the call is a contract that cannot receive a plain ETH transfer.

There is no admin function, no secondary withdrawal, and no private key that can move ETH out of the precompile address. This constitutes **permanent freezing of user funds**.

---

### Likelihood Explanation

Any EVM smart contract that:
- calls the `ExitToNear` precompile directly (or via `withdrawEthToNear`) with ETH value,
- encodes its own address (or another non-payable contract) as `refund_address`, and
- targets a NEAR account that is not registered with the eth-connector

will trigger this freeze. Contracts without `receive`/`fallback` are common (e.g., multisigs, DAOs, proxy contracts). The NEAR `ft_transfer` can fail for ordinary reasons (unregistered account, paused connector). The combination is realistic and requires no privileged access.

---

### Recommendation

Mirror the BullvBear fix: allow the `refund_address` to be redirected to an alternative address at refund time, or — more robustly — store the frozen ETH amount keyed by the original caller so the caller can later claim it to any address they control. At minimum, validate at precompile call time that `refund_address` is either an EOA or a contract that can accept ETH, and revert the exit call if it cannot.

---

### Proof of Concept

1. Deploy a contract `Vault` on Aurora **without** a `receive` or `fallback` function.
2. From `Vault`, call the `ExitToNear` precompile (flag `0x00`) with 1 ETH attached, encoding `Vault`'s own address as `refund_address` (bytes 1–20) and `"unregistered.near"` as the NEAR recipient.
3. The EVM deducts 1 ETH from `Vault` and credits `exit_to_near::ADDRESS`.
4. The NEAR `ft_transfer` to `"unregistered.near"` fails (account not registered).
5. `exit_to_near_precompile_callback` fires; `refund_on_error` attempts `engine.call(exit_address → Vault, 1 ETH, [])`.
6. The EVM call reverts because `Vault` has no `receive`/`fallback`.
7. `ERR_REFUND_FAILURE` is returned; the NEAR callback panics.
8. `exit_to_near::ADDRESS` retains 1 ETH permanently. `Vault`'s balance is 0. No recovery is possible. [4](#0-3) [3](#0-2)

### Citations

**File:** engine-precompiles/src/native.rs (L778-785)
```rust
#[cfg(feature = "error_refund")]
fn parse_input(input: &[u8]) -> Result<(Address, &[u8]), ExitError> {
    validate_input_size(input, MIN_INPUT_SIZE, MAX_INPUT_SIZE)?;
    let mut buffer = [0; 20];
    buffer.copy_from_slice(&input[1..21]);
    let refund_address = Address::from_array(buffer);
    Ok((refund_address, &input[21..]))
}
```

**File:** engine-types/src/parameters/connector.rs (L114-120)
```rust
/// withdraw NEAR eth-connector call args
#[derive(Debug, Clone, BorshSerialize, BorshDeserialize, PartialEq, Eq)]
pub struct RefundCallArgs {
    pub recipient_address: Address,
    pub erc20_address: Option<Address>,
    pub amount: RawU256,
}
```

**File:** engine/src/contract_methods/connector.rs (L231-237)
```rust
        } else if let Some(args) = args.refund {
            // Exit call failed; need to refund tokens
            let refund_result = engine::refund_on_error(io, env, state, &args, handler)?;

            if !refund_result.status.is_ok() {
                return Err(errors::ERR_REFUND_FAILURE.into());
            }
```

**File:** engine/src/engine.rs (L1204-1224)
```rust
    } else {
        // ETH exit; transfer ETH back from precompile address
        let exit_address = exit_to_near::ADDRESS;
        let mut engine: Engine<_, _> =
            Engine::new_with_state(state, exit_address, current_account_id, io, env);
        let refund_address = args.recipient_address;
        let amount = Wei::new(U256::from_big_endian(&args.amount));
        engine.call(
            &exit_address,
            &refund_address,
            amount,
            Vec::new(),
            u64::MAX,
            vec![
                (exit_address.raw(), Vec::new()),
                (refund_address.raw(), Vec::new()),
            ],
            Vec::new(),
            handler,
        )
    }
```

**File:** engine/Cargo.toml (L48-48)
```text
error_refund = ["aurora-engine-precompiles/error_refund"]
```

**File:** engine-tests/src/tests/erc20_connector.rs (L771-775)
```rust
        #[cfg(feature = "error_refund")]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE);
        // If the refund feature is not enabled, then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let expected_balance = Wei::new_u64(INITIAL_ETH_BALANCE - ETH_EXIT_AMOUNT);
```
