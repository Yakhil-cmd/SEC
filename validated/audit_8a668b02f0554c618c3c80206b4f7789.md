### Title
Rebasing NEP-141 Token Bridge Accounting Mismatch Causes Permanent Fund Lock or Insolvency - (`engine/src/engine.rs`, `etc/eth-contracts/contracts/EvmErc20.sol`)

---

### Summary

Aurora Engine's ERC-20 bridge connector mints and burns ERC-20 tokens in exact 1:1 correspondence with explicit NEP-141 transfer amounts. It has no mechanism to account for automatic, out-of-band balance changes in rebasing NEP-141 tokens. When a rebasing NEP-141 is bridged, the ERC-20 supply on Aurora permanently diverges from the actual NEP-141 balance held by the Aurora contract, causing either permanently locked funds (positive rebase) or bridge insolvency (negative rebase).

---

### Finding Description

**Vulnerability class:** Connector/bridge accounting bug — ERC-20 mirror accounting mismatch.

The bridge flow for NEP-141 → ERC-20 is:

1. User calls `ft_transfer_call` on the NEP-141 contract, sending tokens to Aurora.
2. Aurora's `ft_on_transfer` is invoked; it calls `receive_erc20_tokens`.
3. `receive_erc20_tokens` mints exactly `args.amount` ERC-20 tokens to the recipient. [1](#0-0) 

The reverse flow (ERC-20 → NEP-141) is:

1. User calls `withdrawToNear(recipient, amount)` on `EvmErc20`.
2. The contract burns exactly `amount` ERC-20 tokens.
3. It calls the `ExitToNear` precompile with that same `amount`.
4. The precompile schedules an `ft_transfer` of `amount` NEP-141 tokens to the recipient. [2](#0-1) [3](#0-2) 

**The missing invariant:** The system assumes the NEP-141 balance held by Aurora equals the total ERC-20 supply at all times. This holds for standard tokens, but **not for rebasing tokens** whose balances change automatically without any explicit transfer. There is no `sync()` equivalent — no function that can reconcile the ERC-20 supply with the actual NEP-141 balance held by Aurora.

Any NEP-141 token can be deployed as an ERC-20 on Aurora via `deploy_erc20_token` without any check for rebasing behavior: [4](#0-3) 

---

### Impact Explanation

**Positive rebase (yield-bearing tokens, e.g., liquid staking NEP-141):**

- Alice bridges 100 rebasing-NEP-141 → 100 ERC-20 minted; Aurora holds 100 NEP-141.
- Rebase occurs: Aurora now holds 120 NEP-141, but only 100 ERC-20 exist.
- Alice exits with 100 ERC-20 → burns 100 ERC-20, receives 100 NEP-141.
- The extra 20 NEP-141 are **permanently locked** in the Aurora contract. No ERC-20 exist to claim them; no admin recovery path exists.
- **Impact: Critical — Permanent freezing of funds / High — Theft of unclaimed yield.**

**Negative rebase (slashing/deflation):**

- Alice bridges 100 rebasing-NEP-141 → 100 ERC-20 minted; Aurora holds 100 NEP-141.
- Negative rebase: Aurora now holds 80 NEP-141, but 100 ERC-20 still exist.
- First user exits 80 ERC-20 → succeeds, gets 80 NEP-141.
- Second user tries to exit 20 ERC-20 → `ft_transfer` fails (Aurora has 0 NEP-141). Without the `error_refund` feature, the ERC-20 are burned and the user receives nothing.
- **Impact: Critical — Insolvency / Permanent freezing of funds.** [5](#0-4) 

---

### Likelihood Explanation

- Rebasing and yield-bearing NEP-141 tokens exist and will continue to be deployed on NEAR (e.g., liquid staking derivatives).
- Any unprivileged user can deploy an ERC-20 mirror for any NEP-141 token via `deploy_erc20_token` and bridge tokens via `ft_transfer_call`.
- The discrepancy grows automatically over time with no user action required after the initial bridge.
- There is no on-chain guard preventing rebasing NEP-141 tokens from being bridged.

---

### Recommendation

1. **Introduce a balance-sync mechanism:** Add a function analogous to Uniswap V2's `sync()` that reads the actual NEP-141 balance held by Aurora (via a cross-contract call) and mints or burns ERC-20 tokens to match. This requires a NEAR cross-contract call to `ft_balance_of` on the NEP-141 contract.
2. **Rate multiplier approach:** For known rebasing tokens, store a `rate_multiplier` (similar to Curve MetaPools) and apply it when computing mint/burn amounts, so the ERC-20 supply tracks the underlying share rather than the rebased balance.
3. **Token allowlist:** Restrict `deploy_erc20_token` to non-rebasing tokens, or require explicit acknowledgment that rebasing tokens are not supported.

---

### Proof of Concept

**Setup:** A rebasing NEP-141 token `rebase.near` is deployed on NEAR. Its balance automatically increases by 20% per period.

1. Alice calls `deploy_erc20_token` on Aurora for `rebase.near` → ERC-20 deployed at address `0xABC`.
2. Alice calls `ft_transfer_call` on `rebase.near`, sending 100 tokens to Aurora with `msg = alice_evm_address`.
3. Aurora's `ft_on_transfer` fires; `receive_erc20_tokens` mints 100 ERC-20 to Alice's EVM address. [6](#0-5) 

4. One period passes. `rebase.near` automatically increases Aurora's balance to 120. No transaction occurs on Aurora.
5. Alice calls `withdrawToNear("alice.near", 100)` on the ERC-20 contract.
6. `EvmErc20.withdrawToNear` burns 100 ERC-20 and calls the `ExitToNear` precompile with `amount = 100`. [2](#0-1) 

7. The precompile schedules `ft_transfer(receiver_id: "alice.near", amount: 100)` on `rebase.near`.
8. Alice receives 100 NEP-141. The remaining 20 NEP-141 are permanently locked in Aurora with no ERC-20 to claim them and no recovery mechanism. [3](#0-2)

### Citations

**File:** engine/src/engine.rs (L796-837)
```rust
    pub fn receive_erc20_tokens<P: PromiseHandler>(
        &mut self,
        token: &AccountId,
        args: &FtOnTransferArgs,
        current_account_id: &AccountId,
        handler: &mut P,
    ) -> Result<Option<SubmitResult>, ContractError> {
        let amount = args.amount.as_u128();
        // Parse message to determine recipient
        let mut recipient = {
            // The message should contain the recipient EOA address.
            let message = args.msg.strip_prefix("0x").unwrap_or(&args.msg);
            // Recipient - 40 characters (Address in hex without '0x' prefix)
            if message.len() < 40 {
                return Err(ParseOnTransferMessageError::WrongMessageFormat.into());
            }
            let mut address_bytes = [0; 20];
            hex::decode_to_slice(&message[..40], &mut address_bytes)
                .map_err(|_| ParseOnTransferMessageError::WrongMessageFormat)?;
            Address::from_array(address_bytes)
        };

        if let Some(fallback_address) = silo::get_erc20_fallback_address(&self.io)
            && !silo::is_allow_receive_erc20_tokens(&self.io, &recipient)
        {
            recipient = fallback_address;
        }

        let erc20_token = get_erc20_from_nep141(&self.io, token)?;
        let erc20_admin_address = current_address(current_account_id);
        let result = self
            .call(
                &erc20_admin_address,
                &erc20_token,
                Wei::zero(),
                setup_receive_erc20_tokens_input(&recipient, amount),
                u64::MAX,
                Vec::new(), // TODO: are there values we should put here?
                Vec::new(),
                handler,
            )
            .and_then(submit_result_or_err)?;
```

**File:** etc/eth-contracts/contracts/EvmErc20.sol (L53-63)
```text
    function withdrawToNear(bytes memory recipient, uint256 amount) external override {
        _burn(_msgSender(), amount);

        bytes32 amount_b = bytes32(amount);
        bytes memory input = abi.encodePacked("\x01", amount_b, recipient);
        uint input_size = 1 + 32 + recipient.length;

        assembly {
            let res := call(gas(), 0xe9217bc70b7ed1f598ddd3199e80b093fa71124f, 0, add(input, 32), input_size, 0, 32)
        }
    }
```

**File:** engine-precompiles/src/native.rs (L627-647)
```rust
        _ => {
            // There is no way to inject json, given the encoding of both arguments
            // as decimal and valid account id respectively.
            (
                nep141_account_id,
                format!(
                    r#"{{"receiver_id":"{}","amount":"{}"}}"#,
                    exit_params.receiver_account_id,
                    exit_params.amount.as_u128()
                ),
                "ft_transfer",
                None,
                events::ExitToNear::Legacy(ExitToNearLegacy {
                    sender: Address::new(erc20_address),
                    erc20_address: Address::new(erc20_address),
                    dest: exit_params.receiver_account_id.to_string(),
                    amount: exit_params.amount,
                }),
            )
        }
    };
```

**File:** engine/src/contract_methods/connector.rs (L111-158)
```rust
#[named]
pub fn deploy_erc20_token<I: IO + Copy, E: Env, H: PromiseHandler>(
    io: I,
    env: &E,
    handler: &mut H,
) -> Result<PromiseOrValue<Address>, ContractError> {
    with_hashchain(io, env, function_name!(), |mut io| {
        require_running(&state::get_state(&io)?)?;
        let bytes = io.read_input().to_vec();
        let args =
            DeployErc20TokenArgs::deserialize(&bytes).map_err(|_| errors::ERR_BORSH_DESERIALIZE)?;

        match args {
            DeployErc20TokenArgs::Legacy(nep141) => {
                let address = engine::deploy_erc20_token(nep141, None, io, env, handler)?;

                io.return_output(
                    &borsh::to_vec(address.as_bytes()).map_err(|_| errors::ERR_SERIALIZE)?,
                );
                Ok(PromiseOrValue::Value(address))
            }
            DeployErc20TokenArgs::WithMetadata(nep141) => {
                let args = borsh::to_vec(&nep141).map_err(|_| errors::ERR_SERIALIZE)?;
                let base = PromiseCreateArgs {
                    target_account_id: nep141,
                    method: "ft_metadata".to_string(),
                    args: vec![],
                    attached_balance: ZERO_YOCTO,
                    attached_gas: READ_PROMISE_ATTACHED_GAS,
                };
                let callback = PromiseCreateArgs {
                    target_account_id: env.current_account_id(),
                    method: "deploy_erc20_token_callback".to_string(),
                    args,
                    attached_balance: ZERO_YOCTO,
                    attached_gas: DEPLOY_ERC20_TOKEN_CALLBACK_ATTACHED_GAS,
                };
                // Safe because these promises are read-only calls to the main engine contract
                // and this transaction could be executed by the owner of the contract only.
                let promise_args = PromiseWithCallbackArgs { base, callback };
                let promise_id = handler.promise_create_with_callback(&promise_args);

                handler.promise_return(promise_id);

                Ok(PromiseOrValue::Promise(promise_args))
            }
        }
    })
```

**File:** engine-tests/src/tests/erc20_connector.rs (L656-665)
```rust
        #[cfg(feature = "error_refund")]
        let balance = FT_TRANSFER_AMOUNT.into();
        // If the refund feature is not enabled then there is no refund in the EVM
        #[cfg(not(feature = "error_refund"))]
        let balance = (FT_TRANSFER_AMOUNT - FT_EXIT_AMOUNT).into();

        assert_eq!(
            erc20_balance(&erc20, ft_owner_address, &aurora).await,
            balance
        );
```
