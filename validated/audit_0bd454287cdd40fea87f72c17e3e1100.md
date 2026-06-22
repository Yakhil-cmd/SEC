### Title
Missing Minimum Amount Check on ckERC20 Withdrawal Amount Before ckETH Gas Fee Burn in `withdraw_erc20` - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The `withdraw_eth` function enforces an explicit minimum withdrawal amount check (`cketh_minimum_withdrawal_amount`) before burning any ckETH. The `withdraw_erc20` function performs no equivalent minimum check on the `ckerc20_withdrawal_amount` before burning ckETH for gas fees. Any non-anonymous caller can submit a ckERC20 withdrawal with `amount = 0` (or any amount below the ckERC20 ledger's transfer fee), causing the ckETH gas fee to be irreversibly burned first, the ckERC20 burn to then fail with `AmountTooLow`, and the caller to lose `CKETH_LEDGER_TRANSACTION_FEE` worth of ckETH as a penalty.

---

### Finding Description

**Primary path — `withdraw_eth` (protected):**

`withdraw_eth` reads `cketh_minimum_withdrawal_amount` from state and rejects the call before any ledger interaction if the amount is too small: [1](#0-0) 

The `From<LedgerBurnError> for WithdrawalError` implementation even contains an explicit `panic!` asserting that `AmountTooLow` from the ckETH ledger must never occur, because the upfront check is supposed to prevent it: [2](#0-1) 

**Secondary path — `withdraw_erc20` (unprotected):**

`withdraw_erc20` converts the user-supplied `amount` to `ckerc20_withdrawal_amount` with no minimum validation: [3](#0-2) 

It then immediately proceeds to burn `erc20_tx_fee` of ckETH from the caller's account: [4](#0-3) 

Only after the ckETH burn succeeds does the minter attempt to burn `ckerc20_withdrawal_amount` from the ckERC20 ledger: [5](#0-4) 

If `ckerc20_withdrawal_amount` is zero or below the ckERC20 ledger's transfer fee, the ckERC20 ledger returns `BadBurn` (`AmountTooLow`). The error handler then reimburses the caller `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`, permanently deducting `CKETH_LEDGER_TRANSACTION_FEE` from the caller: [6](#0-5) 

The `WithdrawErc20Error` type exposes `AmountTooLow` as a first-class error variant, confirming this path is reachable and not a panic: [7](#0-6) 

The existing test `should_error_when_ckerc20_withdrawal_amount_too_small` confirms the path is reachable by any caller: [8](#0-7) 

The `cketh_minimum_withdrawal_amount` is validated at config time to be at least the ckETH ledger transfer fee, but no analogous validation exists for the ckERC20 withdrawal amount: [9](#0-8) 

---

### Impact Explanation

Any non-anonymous principal can call `withdraw_erc20` with `amount = 0` (or any value below the ckERC20 ledger's transfer fee, e.g., `CKERC20_TRANSFER_FEE - 1`). The sequence is:

1. ckETH gas fee (`erc20_tx_fee`, dynamically estimated, typically millions of wei) is burned from the caller's ckETH account — **irreversible**.
2. ckERC20 burn fails with `AmountTooLow`.
3. Caller is reimbursed `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`.
4. Caller permanently loses `CKETH_LEDGER_TRANSACTION_FEE` (2,000,000,000,000 wei = 0.000002 ETH on mainnet).

The minter state accumulates a `FailedErc20WithdrawalRequest` event per such call. The protocol does not lose funds, but callers suffer a real ckETH loss for each invalid ckERC20 withdrawal submitted. The asymmetry with `withdraw_eth` (which rejects before any burn) is the structural root cause.

---

### Likelihood Explanation

The entry point is the public `withdraw_erc20` update method, callable by any non-anonymous ingress sender or canister. No privileged role is required. The caller only needs to have approved the minter for ckETH (for the gas fee burn). Submitting `amount = 0` is trivially possible. The per-call guard (`retrieve_withdraw_guard`) prevents concurrent calls from the same principal but does not prevent sequential calls.

---

### Recommendation

Add an upfront check in `withdraw_erc20` that validates `ckerc20_withdrawal_amount` is above the ckERC20 ledger's transfer fee before burning any ckETH. The ckERC20 ledger's transfer fee can be read from state (analogous to how `cketh_minimum_withdrawal_amount` is read in `withdraw_eth`). Return a `WithdrawErc20Error` variant (e.g., a new `CkErc20AmountTooLow`) before the `estimate_erc20_transaction_fee` call so no ckETH is ever burned for an invalid ckERC20 amount.

---

### Proof of Concept

1. Caller approves the ckETH minter for a sufficient ckETH allowance (e.g., `DEFAULT_CKERC20_WITHDRAWAL_TRANSACTION_FEE`).
2. Caller calls `withdraw_erc20` with `amount = CKERC20_TRANSFER_FEE - 1` (below the ckERC20 ledger's minimum burn amount).
3. Minter estimates gas fee, burns `erc20_tx_fee` of ckETH from caller — **succeeds**.
4. Minter attempts to burn `CKERC20_TRANSFER_FEE - 1` of ckERC20 — **fails** with `AmountTooLow`.
5. Minter queues `FailedErc20WithdrawalRequest` reimbursing `erc20_tx_fee - CKETH_LEDGER_TRANSACTION_FEE`.
6. Caller's net ckETH balance decreases by `CKETH_LEDGER_TRANSACTION_FEE` (2,000,000,000,000 wei).

This is confirmed by the existing integration test at: [8](#0-7)

### Citations

**File:** rs/ethereum/cketh/minter/src/main.rs (L291-296)
```rust
    let minimum_withdrawal_amount = read_state(|s| s.cketh_minimum_withdrawal_amount);
    if amount < minimum_withdrawal_amount {
        return Err(WithdrawalError::AmountTooLow {
            min_withdrawal_amount: minimum_withdrawal_amount.into(),
        });
    }
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L415-416)
```rust
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L448-458)
```rust
    match cketh_ledger
        .burn_from(
            cketh_account,
            erc20_tx_fee,
            BurnMemo::Erc20GasFee {
                ckerc20_token_symbol: ckerc20_token.ckerc20_token_symbol.clone(),
                ckerc20_withdrawal_amount,
                to_address: destination,
            },
        )
        .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L468-477)
```rust
            match LedgerClient::ckerc20_ledger(&ckerc20_token)
                .burn_from(
                    ckerc20_account,
                    ckerc20_withdrawal_amount,
                    BurnMemo::Erc20Convert {
                        ckerc20_withdrawal_id: cketh_ledger_burn_index.get(),
                        to_address: destination,
                    },
                )
                .await
```

**File:** rs/ethereum/cketh/minter/src/main.rs (L507-514)
```rust
                    let reimbursed_amount = match &ckerc20_burn_error {
                        LedgerBurnError::TemporarilyUnavailable { .. } => erc20_tx_fee, //don't penalize user in case of an error outside of their control
                        LedgerBurnError::InsufficientFunds { .. }
                        | LedgerBurnError::AmountTooLow { .. }
                        | LedgerBurnError::InsufficientAllowance { .. } => erc20_tx_fee
                            .checked_sub(CKETH_LEDGER_TRANSACTION_FEE)
                            .unwrap_or(Wei::ZERO),
                    };
```

**File:** rs/ethereum/cketh/minter/src/endpoints.rs (L236-244)
```rust
            LedgerBurnError::AmountTooLow {
                minimum_burn_amount,
                failed_burn_amount,
                ledger,
            } => {
                panic!(
                    "BUG: withdrawal amount {failed_burn_amount} on the ckETH ledger {ledger:?} should always be higher than the ledger transaction fee {minimum_burn_amount}"
                )
            }
```

**File:** rs/ethereum/cketh/minter/src/endpoints/ckerc20.rs (L56-61)
```rust
    AmountTooLow {
        minimum_burn_amount: Nat,
        failed_burn_amount: Nat,
        token_symbol: String,
        ledger_id: Principal,
    },
```

**File:** rs/ethereum/cketh/minter/tests/ckerc20.rs (L497-530)
```rust
    #[test]
    fn should_error_when_ckerc20_withdrawal_amount_too_small() {
        let ckerc20 = CkErc20Setup::default().add_supported_erc20_tokens();
        let ckusdc = ckerc20.find_ckerc20_token("ckUSDC");
        let caller = ckerc20.caller();
        let ckerc20_tx_fee = CKETH_MINIMUM_WITHDRAWAL_AMOUNT;

        ckerc20
            .deposit_cketh_and_ckerc20(
                EXPECTED_BALANCE,
                TWO_USDC + CKERC20_TRANSFER_FEE,
                ckusdc.clone(),
                caller,
            )
            .expect_mint()
            .call_cketh_ledger_approve_minter(caller, ckerc20_tx_fee, None)
            .call_ckerc20_ledger_approve_minter(ckusdc.ledger_canister_id, caller, TWO_USDC, None)
            .call_minter_withdraw_erc20(
                caller,
                CKERC20_TRANSFER_FEE - 1,
                ckusdc.ledger_canister_id,
                DEFAULT_ERC20_WITHDRAWAL_DESTINATION_ADDRESS,
            )
            .expect_refresh_gas_fee_estimate(identity)
            .expect_error(WithdrawErc20Error::CkErc20LedgerError {
                cketh_block_index: 2_u8.into(),
                error: LedgerError::AmountTooLow {
                    minimum_burn_amount: CKERC20_TRANSFER_FEE.into(),
                    failed_burn_amount: Nat::from(CKERC20_TRANSFER_FEE - 1),
                    token_symbol: "ckUSDC".to_string(),
                    ledger_id: ckusdc.ledger_canister_id,
                },
            });
    }
```

**File:** rs/ethereum/cketh/minter/src/state.rs (L156-171)
```rust
        if self.cketh_minimum_withdrawal_amount == Wei::ZERO {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must be positive".to_string(),
            ));
        }
        let cketh_ledger_transfer_fee = match self.ethereum_network {
            EthereumNetwork::Mainnet => Wei::new(2_000_000_000_000),
            EthereumNetwork::Sepolia => Wei::new(10_000_000_000),
        };
        if self.cketh_minimum_withdrawal_amount < cketh_ledger_transfer_fee {
            return Err(InvalidStateError::InvalidMinimumWithdrawalAmount(
                "minimum_withdrawal_amount must cover ledger transaction fee, \
                otherwise ledger can return a BadBurn error that should be returned to the user"
                    .to_string(),
            ));
        }
```
