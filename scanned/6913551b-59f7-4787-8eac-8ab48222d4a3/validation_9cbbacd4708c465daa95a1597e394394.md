### Title
Missing ICRC-21 Consent Message Support in ckETH Minter Allows Opaque Withdrawal Authorization - (File: `rs/ethereum/cketh/minter/src/main.rs`)

---

### Summary

The ckETH minter exposes high-value withdrawal endpoints (`withdraw_eth`, `withdraw_erc20`) that burn user tokens and dispatch Ethereum transactions, but unlike the ckBTC and ckDOGE minters, it does **not** implement the ICRC-21 `icrc21_canister_call_consent_message` endpoint. Wallets that follow the ICRC-21 standard to display human-readable transaction details before user authorization receive no structured consent message from the ckETH minter, leaving users unable to verify the withdrawal amount, destination Ethereum address, or fee before signing.

---

### Finding Description

The ckBTC minter implements `icrc21_canister_call_consent_message` as an `#[update]` endpoint that decodes `RetrieveBtcWithApprovalArgs` and returns a structured `ConsentInfo` showing the amount and Bitcoin destination address to the user before they authorize the burn-and-send operation. [1](#0-0) 

The ckDOGE minter follows the same pattern for `retrieve_doge_with_approval`. [2](#0-1) 

The ckETH minter, however, exposes `withdraw_eth` and `withdraw_erc20` — operations that burn ckETH and/or ckERC20 tokens and send real ETH/ERC-20 to a caller-supplied Ethereum address — with **no** `icrc21_canister_call_consent_message` endpoint. A search for `icrc21` in `rs/ethereum/cketh/minter/src/main.rs` returns zero matches, and the canister's `.did` file lists no such method. [3](#0-2) [4](#0-3) [5](#0-4) 

The `withdraw_erc20` path is especially sensitive: it burns ckETH from the user's account to pay gas fees (at a dynamically estimated amount) **and** burns the specified ckERC20 amount, both irreversibly, before dispatching an Ethereum transaction.

---

### Impact Explanation

A user interacting with the ckETH minter through any ICRC-21-aware wallet (hardware wallet, browser extension, or dApp) will receive an error or a raw Candid blob when the wallet queries for a consent message, because the endpoint does not exist. The wallet either:

1. Falls back to displaying raw binary arguments the user cannot interpret, or
2. Presents a generic "sign this call" prompt with no amount or destination shown.

In either case the user cannot verify:
- The exact ckETH or ckERC20 amount being burned
- The Ethereum destination address
- The dynamically estimated gas fee deducted from their ckETH balance

A malicious dApp can craft a `withdraw_eth` or `withdraw_erc20` call targeting an attacker-controlled Ethereum address. Because no structured consent message is available, the user has no on-wallet confirmation of the destination before authorizing the burn. This directly mirrors the original report's scenario: funds are transferred without the user being shown the transaction parameters.

---

### Likelihood Explanation

The ckETH minter is a live, high-value canister on the IC mainnet. The ICRC-21 standard is already adopted by hardware wallets (e.g., the Ledger ICP app) and browser wallets that integrate with IC canisters. Any user of such a wallet who is directed to call `withdraw_eth` or `withdraw_erc20` by a phishing dApp or malicious canister will encounter the missing consent message. The attack requires only that the victim use an ICRC-21-aware wallet and be socially engineered into initiating a withdrawal — no privileged access, no key compromise, and no consensus manipulation is needed.

---

### Recommendation

Implement `icrc21_canister_call_consent_message` on the ckETH minter following the same pattern as the ckBTC minter:

- For `withdraw_eth`: decode `WithdrawalArg`, display the ckETH amount being burned and the Ethereum recipient address.
- For `withdraw_erc20`: decode `WithdrawErc20Arg`, display the ckERC20 token symbol, amount, the dynamically estimated ckETH gas fee, and the Ethereum recipient address.
- Advertise `ICRC-21` via `icrc10_supported_standards`.

Reference implementation: [6](#0-5) [7](#0-6) 

---

### Proof of Concept

1. Deploy or interact with the live ckETH minter canister.
2. From an ICRC-21-aware wallet, query:
   ```
   icrc21_canister_call_consent_message({
     method = "withdraw_eth";
     arg = <encoded WithdrawalArg>;
     user_preferences = { metadata = { language = "en"; utc_offset_minutes = null }; device_spec = null };
   })
   ```
3. The call will be rejected with a method-not-found error (or the canister will trap), because the endpoint does not exist in `rs/ethereum/cketh/minter/src/main.rs` and is absent from `rs/ethereum/cketh/minter/cketh_minter.did`.
4. The wallet falls back to a raw or generic prompt. A crafted `withdraw_eth` call with `recipient = "0xAttackerAddress"` and a large `amount` will be authorized by the user without them seeing the destination address. [8](#0-7)

### Citations

**File:** rs/bitcoin/ckbtc/minter/src/main.rs (L271-276)
```rust
#[update]
fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    updates::icrc21::icrc21_canister_call_consent_message(consent_msg_request)
}
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs (L65-71)
```rust
pub fn icrc21_canister_call_consent_message(
    consent_msg_request: ConsentMessageRequest,
) -> Result<ConsentInfo, Icrc21Error> {
    let network =
        read_state(|s| Network::try_from(s.btc_network).unwrap_or_else(|err| ic_cdk::trap(err)));
    build_consent_info(consent_msg_request, network)
}
```

**File:** rs/dogecoin/ckdoge/minter/src/updates/icrc21.rs (L132-194)
```rust
fn build_retrieve_doge_with_approval_message(
    args: &RetrieveDogeWithApprovalArgs,
    display_type: &DisplayMessageType,
    symbols: TokenSymbols,
) -> ConsentMessage {
    let TokenSymbols { ckdoge, doge } = symbols;
    let amount = format_amount(args.amount, DECIMALS);
    match display_type {
        DisplayMessageType::GenericDisplay => {
            let mut message = format!(
                "# Convert {ckdoge} to {doge}\n\n\
                 Authorize the {ckdoge} minter to burn {ckdoge} from your account and \
                 send the equivalent amount in {doge} (minus network and minter fees) to \
                 the Dogecoin address below.\n\n\
                 **Amount to convert:** `{amount} {ckdoge}`\n\n\
                 **Dogecoin destination address:**\n`{address}`",
                address = args.address,
            );
            if let Some(subaccount) = args.from_subaccount {
                message.push_str(&format!(
                    "\n\n**{ckdoge} source subaccount:**\n`{}`",
                    hex::encode(subaccount)
                ));
            }
            ConsentMessage::GenericDisplayMessage(message)
        }
        DisplayMessageType::FieldsDisplay => {
            // Long values (Dogecoin addresses, subaccount hex) are sent as a
            // single `Value::Text` per the ICRC-21 spec — wallets are
            // responsible for paginating them across screens. See e.g. the
            // Ledger ICP app, which calls `handle_ui_message` to chunk the
            // value into device-sized pages.
            let mut fields = vec![
                (
                    "Amount".to_string(),
                    Value::TokenAmount {
                        decimals: DECIMALS,
                        amount: args.amount,
                        symbol: ckdoge.to_string(),
                    },
                ),
                (
                    format!("{doge} address"),
                    Value::Text {
                        content: args.address.clone(),
                    },
                ),
            ];
            if let Some(subaccount) = args.from_subaccount {
                fields.push((
                    "From subaccount".to_string(),
                    Value::Text {
                        content: hex::encode(subaccount),
                    },
                ));
            }
            ConsentMessage::FieldsDisplayMessage(FieldsDisplay {
                intent: format!("{ckdoge} to {doge}"),
                fields,
            })
        }
    }
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

**File:** rs/ethereum/cketh/minter/src/main.rs (L389-448)
```rust
#[update]
async fn withdraw_erc20(
    WithdrawErc20Arg {
        amount,
        ckerc20_ledger_id,
        recipient,
        from_cketh_subaccount,
        from_ckerc20_subaccount,
    }: WithdrawErc20Arg,
) -> Result<RetrieveErc20Request, WithdrawErc20Error> {
    validate_ckerc20_active();
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
        AddressValidationError::Blocked(address) => WithdrawErc20Error::RecipientAddressBlocked {
            address: address.to_string(),
        },
    })?;
    let ckerc20_withdrawal_amount =
        Erc20Value::try_from(amount).expect("ERROR: failed to convert Nat to u256");

    let ckerc20_token = read_state(|s| s.find_ck_erc20_token_by_ledger_id(&ckerc20_ledger_id))
        .ok_or_else(|| {
            let supported_ckerc20_tokens: BTreeSet<_> = read_state(|s| {
                s.supported_ck_erc20_tokens()
                    .map(|token| token.into())
                    .collect()
            });
            WithdrawErc20Error::TokenNotSupported {
                supported_tokens: Vec::from_iter(supported_ckerc20_tokens),
            }
        })?;
    let cketh_ledger = read_state(LedgerClient::cketh_ledger_from_state);
    let erc20_tx_fee = estimate_erc20_transaction_fee().await.ok_or_else(|| {
        WithdrawErc20Error::TemporarilyUnavailable("Failed to retrieve current gas fee".to_string())
    })?;
    let cketh_account = Account {
        owner: caller,
        subaccount: from_cketh_subaccount,
    };
    let ckerc20_account = Account {
        owner: caller,
        subaccount: from_ckerc20_subaccount,
    };
    let now = ic_cdk::api::time();
    log!(
        INFO,
        "[withdraw_erc20]: burning {:?} ckETH from account {}",
        erc20_tx_fee,
        cketh_account
    );
    match cketh_ledger
```

**File:** rs/ethereum/cketh/minter/cketh_minter.did (L696-750)
```text
service : (MinterArg) -> {
    // Retrieve the Ethereum address controlled by the minter:
    // * Deposits will be transferred from the helper smart contract to this address
    // * Withdrawals will originate from this address
    // IMPORTANT: Do NOT send ETH to this address directly. Use the helper smart contract instead so that the minter
    // knows to which IC principal the funds should be deposited.
    minter_address : () -> (text);

    // Address of the helper smart contract.
    // Returns "N/A" if the helper smart contract is not set.
    // IMPORTANT:
    // * Use this address to send ETH to the minter to convert it to ckETH.
    // * In case the smart contract needs to be updated the returned address will change!
    //   Always check the address before making a transfer.
    smart_contract_address : () -> (text) query;

    // Estimate the price of a transaction issued by the minter when converting ckETH to ETH.
    eip_1559_transaction_price : (opt Eip1559TransactionPriceArg) -> (Eip1559TransactionPrice) query;

    // Returns internal minter parameters
    get_minter_info : () -> (MinterInfo) query;

    // Withdraw the specified amount in Wei to the given Ethereum address.
    // IMPORTANT: The current gas limit is set to 21,000 for a transaction so withdrawals to smart contract addresses will likely fail.
    withdraw_eth : (WithdrawalArg) -> (variant { Ok : RetrieveEthRequest; Err : WithdrawalError });

    // Withdraw the specified amount of ERC-20 tokens to the given Ethereum address.
    withdraw_erc20 : (WithdrawErc20Arg) -> (variant { Ok : RetrieveErc20Request; Err : WithdrawErc20Error });

    // Retrieve the status of a Eth withdrawal request.
    retrieve_eth_status : (nat64) -> (RetrieveEthStatus);

    // Return details of all withdrawals matching the given search parameter.
    withdrawal_status : (WithdrawalSearchParameter) -> (vec WithdrawalDetail) query;

    // Check if an address is blocked by the minter.
    is_address_blocked : (text) -> (bool) query;

    // Retrieve the status of the minter canister.
    //
    // This is a debug endpoint where backwards-compatibility is not guaranteed.
    get_canister_status : () -> (CanisterStatusResponse);

    // Retrieve events from the minter's audit log.
    // The endpoint can return fewer events than requested to bound the response size.
    // IMPORTANT: this endpoint is meant as a debugging tool and is not guaranteed to be backwards-compatible.
    get_events : (record { start : nat64; length : nat64 }) -> (record { events : vec Event; total_event_count : nat64 }) query;

    // Add a ckERC-20 token to be supported by the minter.
    // This call is restricted to the orchestrator ID.
    add_ckerc20_token : (AddCkErc20Token) -> ();

    // Decode ledger memos produced by the minter when minting (deposits) or burning (withdrawals).
    decode_ledger_memo : (DecodeLedgerMemoArgs) -> (DecodeLedgerMemoResult) query;
}
```

**File:** rs/bitcoin/ckbtc/minter/src/updates/icrc21.rs (L131-193)
```rust
fn build_retrieve_btc_with_approval_message(
    args: &RetrieveBtcWithApprovalArgs,
    display_type: &DisplayMessageType,
    symbols: TokenSymbols,
) -> ConsentMessage {
    let TokenSymbols { ckbtc, btc } = symbols;
    let amount = format_amount(args.amount, DECIMALS);
    match display_type {
        DisplayMessageType::GenericDisplay => {
            let mut message = format!(
                "# Convert {ckbtc} to {btc}\n\n\
                 Authorize the {ckbtc} minter to burn {ckbtc} from your account and \
                 send the equivalent amount in {btc} (minus network and minter fees) to \
                 the Bitcoin address below.\n\n\
                 **Amount to convert:** `{amount} {ckbtc}`\n\n\
                 **Bitcoin destination address:**\n`{address}`",
                address = args.address,
            );
            if let Some(subaccount) = args.from_subaccount {
                message.push_str(&format!(
                    "\n\n**{ckbtc} source subaccount:**\n`{}`",
                    hex::encode(subaccount)
                ));
            }
            ConsentMessage::GenericDisplayMessage(message)
        }
        DisplayMessageType::FieldsDisplay => {
            // Long values (Bitcoin addresses, subaccount hex) are sent as a
            // single `Value::Text` per the ICRC-21 spec — wallets are
            // responsible for paginating them across screens. See e.g. the
            // Ledger ICP app, which calls `handle_ui_message` to chunk the
            // value into device-sized pages.
            let mut fields = vec![
                (
                    "Amount".to_string(),
                    Value::TokenAmount {
                        decimals: DECIMALS,
                        amount: args.amount,
                        symbol: ckbtc.to_string(),
                    },
                ),
                (
                    format!("{btc} address"),
                    Value::Text {
                        content: args.address.clone(),
                    },
                ),
            ];
            if let Some(subaccount) = args.from_subaccount {
                fields.push((
                    "From subaccount".to_string(),
                    Value::Text {
                        content: hex::encode(subaccount),
                    },
                ));
            }
            ConsentMessage::FieldsDisplayMessage(FieldsDisplay {
                intent: format!("{ckbtc} to {btc}"),
                fields,
            })
        }
    }
}
```
